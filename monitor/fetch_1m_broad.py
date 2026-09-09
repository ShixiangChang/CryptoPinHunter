# -*- coding: utf-8 -*-
"""大范围币池 + 近 N 天 1m 数据采集（试试水）。

池子 = 币安 24h 成交额前 N（默认 150，≈$10M 以上），不是锁死的 35 老币。
时间 = 近 90 天（默认）。
落库到 monitor.db 的 klines_1m 表（PRIMARY KEY symbol+open_time 天然去重）。

用法：
    python monitor/fetch_1m_broad.py                 # 前150币 近90天
    python monitor/fetch_1m_broad.py --top 300 --days 30
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# 代理从环境变量读（直连留空），不硬编码本地代理
_proxy = __import__("os").environ.get("BINANCE_PROXY", "").strip()
PROXY = {"http": _proxy, "https": _proxy} if _proxy else None
BASE = "https://fapi.binance.com/fapi/v1/klines"
DB = Path("data/monitor.db")
TICKERS_FILE = Path("data/model_out/all_usdt_tickers.json")
INTERVAL = "1m"
LIMIT = 1000
SLEEP = 0.12


def init_table():
    conn = sqlite3.connect(DB)
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


def load_pool(top_n: int) -> list[str]:
    rows = json.loads(TICKERS_FILE.read_text(encoding="utf-8"))
    return [s for s, _ in rows[:top_n]]


def fetch_symbol(symbol: str, start_ms: int, now_ms: int) -> int:
    conn = sqlite3.connect(DB)
    cur = start_ms
    total = 0
    session = requests.Session()
    while cur < now_ms:
        try:
            r = session.get(BASE, params={
                "symbol": symbol, "interval": INTERVAL,
                "startTime": cur, "limit": LIMIT,
            }, proxies=PROXY, timeout=20)
        except Exception:
            time.sleep(5)
            continue
        if r.status_code in (429, 418):
            time.sleep(30)
            continue
        if r.status_code != 200:
            time.sleep(3)
            continue
        data = r.json()
        if not isinstance(data, list) or not data:
            break
        rows = [(symbol, int(d[0]) // 1000, float(d[2]), float(d[3]), float(d[4]), float(d[5]))
                for d in data]
        conn.executemany(
            "INSERT OR REPLACE INTO klines_1m (symbol, open_time, high, low, close, volume) "
            "VALUES (?,?,?,?,?,?)", rows)
        conn.commit()
        total += len(rows)
        cur = int(data[-1][0]) + 60_000
        if len(data) < LIMIT:
            break
        time.sleep(SLEEP)
    conn.close()
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=150, help="成交额前 N 币")
    ap.add_argument("--days", type=int, default=90, help="近 N 天")
    a = ap.parse_args()

    init_table()
    symbols = load_pool(a.top)
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - a.days * 86400_000
    print(f"池子 {len(symbols)} 币（成交额前 {a.top}），近 {a.days} 天，"
          f"起点 {datetime.fromtimestamp(start_ms/1000, tz=timezone.utc)}", flush=True)

    from concurrent.futures import ThreadPoolExecutor, as_completed
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fetch_symbol, s, start_ms, now_ms): s for s in symbols}
        for fut in as_completed(futs):
            sym = futs[fut]
            try:
                n = fut.result()
                done += 1
                print(f"[{done}/{len(symbols)}] {sym}: +{n} 条 ({time.time()-t0:.0f}s)", flush=True)
            except Exception as e:
                print(f"[{sym}] 失败: {e}", flush=True)

    conn = sqlite3.connect(DB)
    total = conn.execute("SELECT COUNT(*) FROM klines_1m").fetchone()[0]
    n_sym = conn.execute("SELECT COUNT(DISTINCT symbol) FROM klines_1m").fetchone()[0]
    conn.close()
    print(f"\n完成。klines_1m 共 {total:,} 条，{n_sym} 币，用时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
