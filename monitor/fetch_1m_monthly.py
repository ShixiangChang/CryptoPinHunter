# -*- coding: utf-8 -*-
"""币安官方月度 1m K 线批量下载（走 CDN，不碰 fapi REST 限流）。

数据源：https://data.binance.vision/data/futures/um/monthly/klines/{SYMBOL}/1m/{SYMBOL}-1m-{YYYY-MM}.zip
每月一个 zip（约 2MB），内含整月 CSV。150 币 × 3 个月 = 450 个下载，几分钟到十几分钟。
落库 klines_1m（PRIMARY KEY symbol+open_time 天然去重）。

用法：
    python monitor/fetch_1m_monthly.py                 # 前150币，近3个月
    python monitor/fetch_1m_monthly.py --top 300 --months 2026-06,2026-07,2026-08
"""
from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
import config

PROXY = {"http": config.PROXY, "https": config.PROXY} if config.PROXY else None
BASE = "https://data.binance.vision/data/futures/um/monthly/klines"
DB = Path(config.DB_PATH)
TICKERS_FILE = Path(config.OUTPUT_DIR) / "all_usdt_tickers.json"
PROGRESS_FILE = Path(config.OUTPUT_DIR) / "fetch_1m_monthly_progress.json"

# CSV 列序：open_time, open, high, low, close, volume, close_time, ...
IDX = {"open_time": 0, "high": 2, "low": 3, "close": 4, "volume": 5}


def init_table():
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS klines_1m (
            symbol TEXT NOT NULL,
            open_time INTEGER NOT NULL,
            high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY (symbol, open_time)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kl1m_sym_ts ON klines_1m(symbol, open_time)")
    conn.commit()
    conn.close()


def load_progress() -> set:
    if PROGRESS_FILE.exists():
        return set(json.loads(PROGRESS_FILE.read_text(encoding="utf-8")))
    return set()


def save_progress(p: set):
    PROGRESS_FILE.write_text(json.dumps(sorted(p)), encoding="utf-8")


def fetch_one(symbol: str, ym: str) -> tuple[str, int]:
    """下载并落库一个币的一个月。返回 (symbol, 落库条数)。"""
    url = f"{BASE}/{quote(symbol)}/1m/{quote(symbol)}-1m-{ym}.zip"
    for attempt in range(3):
        try:
            r = requests.get(url, proxies=PROXY, timeout=60)
        except Exception:
            time.sleep(3)
            continue
        if r.status_code == 404:
            return symbol, 0  # 该币该月无数据（新上线币），不算失败
        if r.status_code != 200:
            time.sleep(3)
            continue
        try:
            z = zipfile.ZipFile(io.BytesIO(r.content))
            name = z.namelist()[0]
            text = z.read(name).decode("utf-8")
        except Exception:
            time.sleep(2)
            continue
        break
    else:
        return symbol, -1  # 3 次都失败

    rows = []
    for line in text.strip().split("\n")[1:]:  # 跳过表头
        parts = line.split(",")
        if len(parts) < 6:
            continue
        try:
            ot = int(parts[IDX["open_time"]]) // 1000
            high = float(parts[IDX["high"]])
            low = float(parts[IDX["low"]])
            close = float(parts[IDX["close"]])
            vol = float(parts[IDX["volume"]])
        except (ValueError, IndexError):
            continue
        rows.append((symbol, ot, high, low, close, vol))

    if rows:
        conn = sqlite3.connect(DB, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executemany(
            "INSERT OR REPLACE INTO klines_1m (symbol, open_time, high, low, close, volume) "
            "VALUES (?,?,?,?,?,?)", rows)
        conn.commit()
        conn.close()
    return symbol, len(rows)


def fetch_tickers() -> list[tuple[str, float]]:
    """拉全量 USDT 永续 24h 成交额，落盘并返回 [(symbol, quoteVolume), ...]。"""
    url = f"{config.BASE_URL}/fapi/v1/ticker/24hr"
    proxies = {"http": config.PROXY, "https": config.PROXY} if config.PROXY else None
    r = requests.get(url, proxies=proxies, timeout=30)
    r.raise_for_status()
    rows = [(t["symbol"], float(t["quoteVolume"]))
            for t in r.json() if t["symbol"].endswith("USDT")]
    rows.sort(key=lambda x: -x[1])
    TICKERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TICKERS_FILE.write_text(json.dumps(rows), encoding="utf-8")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=150, help="成交额前 N 币")
    ap.add_argument("--months", default="2026-06,2026-07,2026-08", help="逗号分隔的月份")
    a = ap.parse_args()

    init_table()
    if not TICKERS_FILE.exists():
        print("未找到 ticker 文件，先拉全量 24h 成交额...", flush=True)
        tickers = fetch_tickers()
        print(f"全量 {len(tickers)} 币，已存 {TICKERS_FILE.name}", flush=True)
    else:
        tickers = json.loads(TICKERS_FILE.read_text(encoding="utf-8"))
    symbols = [s for s, _ in tickers[:a.top]]
    months = [m.strip() for m in a.months.split(",")]
    jobs = [(s, m) for s in symbols for m in months]

    done = load_progress()
    todo = [(s, m) for s, m in jobs if f"{s}|{m}" not in done]
    print(f"池子 {len(symbols)} 币 × {len(months)} 月 = {len(jobs)} 个文件，"
          f"已完成 {len(done)}，待下载 {len(todo)}", flush=True)
    if not todo:
        print("全部完成。")
        return

    from concurrent.futures import ThreadPoolExecutor, as_completed

    t0 = time.time()
    finished = 0
    total_rows = 0
    failed = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch_one, s, m): (s, m) for s, m in todo}
        for fut in as_completed(futs):
            s, m = futs[fut]
            try:
                sym, n = fut.result()
                if n >= 0:
                    done.add(f"{s}|{m}")
                    save_progress(done)
                    finished += 1
                    total_rows += n
                    print(f"[{finished}/{len(todo)}] {s} {m}: +{n} 条 ({time.time()-t0:.0f}s)", flush=True)
                else:
                    failed.append(f"{s}|{m}")
            except Exception as e:
                failed.append(f"{s}|{m}: {e}")

    conn = sqlite3.connect(DB)
    total = conn.execute("SELECT COUNT(*) FROM klines_1m").fetchone()[0]
    n_sym = conn.execute("SELECT COUNT(DISTINCT symbol) FROM klines_1m").fetchone()[0]
    conn.close()
    print(f"\n完成。本次 +{total_rows:,} 条；klines_1m 共 {total:,} 条，{n_sym} 币，"
          f"用时 {time.time()-t0:.0f}s")
    if failed:
        print(f"失败 {len(failed)} 个: {failed[:20]}")


if __name__ == "__main__":
    main()
