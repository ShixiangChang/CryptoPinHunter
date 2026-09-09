# -*- coding: utf-8 -*-
"""fetch_1h_funding.py —— 预采前 N 币的 1h 90 天 + funding 90 天历史（趋势过滤 + funding 过滤预热）。

pin 策略两条过滤依赖的历史上下文，部署时一次性预采，之后 live_feed.py 负责增量：
  klines(1h)   趋势过滤：过去 90 天最高 high（REST 分页，每币约 3 请求）
  funding_hist funding 过滤：过去 90 天已结算 funding（REST 一次 limit=1000 拉完）

池子口径与 fetch_1m_monthly 一致：读 all_usdt_tickers.json（ticker/24hr 成交额排序）取前 N，
采的池子比定池 top_n 大（留余量，因 liquid_symbols 会再剔除 TradFi + 无现货）。

用法：
    python monitor/fetch_1h_funding.py --top 150 --days 90
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
import config

PROXY = {"http": config.PROXY, "https": config.PROXY} if config.PROXY else None
DB = Path(config.DB_PATH)
TICKERS_FILE = Path(config.OUTPUT_DIR) / "all_usdt_tickers.json"


def init_tables(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("""CREATE TABLE IF NOT EXISTS klines (
        symbol TEXT NOT NULL, open_time INTEGER NOT NULL,
        open REAL, high REAL, low REAL, close REAL, volume REAL,
        quote_volume REAL, trades INTEGER, taker_buy_base REAL, taker_buy_quote REAL,
        PRIMARY KEY (symbol, open_time))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kl_sym_ts ON klines(symbol, open_time)")
    conn.execute("""CREATE TABLE IF NOT EXISTS funding_hist (
        symbol TEXT NOT NULL, funding_time INTEGER NOT NULL, funding REAL,
        PRIMARY KEY (symbol, funding_time))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fh_sym_ts ON funding_hist(symbol, funding_time)")
    conn.commit()


def _get(url: str, params: dict):
    r = requests.get(url, params=params, proxies=PROXY, timeout=30)
    if r.status_code != 200:
        return []
    return r.json()


def fetch_klines_1h(symbol: str, days: int) -> list[tuple]:
    """分页拉 1h K 线（days 天），返回 11 列完整 rows。"""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    out: list[tuple] = []
    cursor = start_ms
    while cursor < end_ms:
        rows = _get(f"{config.BASE_URL}/fapi/v1/klines",
                    {"symbol": symbol, "interval": "1h", "startTime": cursor,
                     "endTime": end_ms, "limit": 1000})
        if not rows:
            break
        for k in rows:
            try:
                out.append((symbol, int(k[0]) // 1000, float(k[1]), float(k[2]),
                            float(k[3]), float(k[4]), float(k[5]), float(k[7]),
                            int(k[8]), float(k[9]), float(k[10])))
            except (ValueError, IndexError):
                continue
        if len(rows) < 1000:
            break
        cursor = rows[-1][0] + 1  # 下一根
        time.sleep(0.1)
    return out


def fetch_funding(symbol: str, days: int) -> list[tuple]:
    """拉已结算 funding rate（days 天），返回 (symbol, funding_time秒, funding)。"""
    start_ms = int(time.time() * 1000) - days * 86400 * 1000
    rows = _get(f"{config.BASE_URL}/fapi/v1/fundingRate",
                {"symbol": symbol, "startTime": start_ms, "limit": 1000})
    out = []
    for d in rows:
        try:
            out.append((symbol, int(d["fundingTime"]) // 1000, float(d["fundingRate"])))
        except (ValueError, KeyError):
            continue
    return out


def load_tickers(top: int) -> list[str]:
    if TICKERS_FILE.exists():
        rows = json.loads(TICKERS_FILE.read_text(encoding="utf-8"))
    else:
        data = _get(f"{config.BASE_URL}/fapi/v1/ticker/24hr", {})
        rows = [(t["symbol"], float(t["quoteVolume"]))
                for t in data if t["symbol"].endswith("USDT")]
        rows.sort(key=lambda x: -x[1])
        TICKERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        TICKERS_FILE.write_text(json.dumps(rows), encoding="utf-8")
    return [s for s, _ in rows[:top]]


def work(symbol: str, days: int) -> tuple[str, int, int]:
    k = fetch_klines_1h(symbol, days)
    f = fetch_funding(symbol, days)
    if k or f:
        conn = sqlite3.connect(DB, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        if k:
            conn.executemany(
                "INSERT OR REPLACE INTO klines "
                "(symbol, open_time, open, high, low, close, volume, "
                "quote_volume, trades, taker_buy_base, taker_buy_quote) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)", k)
        if f:
            conn.executemany(
                "INSERT OR REPLACE INTO funding_hist (symbol, funding_time, funding) "
                "VALUES (?,?,?)", f)
        conn.commit()
        conn.close()
    return symbol, len(k), len(f)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=150, help="成交额前 N 币")
    ap.add_argument("--days", type=int, default=90, help="预采天数（趋势/funding 预热窗口）")
    a = ap.parse_args()

    init_tables(sqlite3.connect(DB))
    symbols = load_tickers(a.top)
    print(f"[fetch_1h_funding] 池子 {len(symbols)} 币 × {a.days} 天，开始预采", flush=True)

    t0 = time.time()
    done = 0
    total_k = total_f = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(work, s, a.days): s for s in symbols}
        for fut in as_completed(futs):
            s = futs[fut]
            try:
                sym, nk, nf = fut.result()
                done += 1
                total_k += nk
                total_f += nf
                if done % 20 == 0:
                    print(f"[{done}/{len(symbols)}] 累计 1h:{total_k:,} funding:{total_f:,} "
                          f"({time.time()-t0:.0f}s)", flush=True)
            except Exception as e:
                print(f"[fetch_1h_funding] {s} 失败: {e}", flush=True)

    conn = sqlite3.connect(DB)
    n_kl = conn.execute("SELECT COUNT(*) FROM klines").fetchone()[0]
    n_fh = conn.execute("SELECT COUNT(*) FROM funding_hist").fetchone()[0]
    conn.close()
    print(f"\n完成。1h 累计 {total_k:,} 根（klines 共 {n_kl:,}）；funding {total_f:,} 条（共 {n_fh:,}）；"
          f"用时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
