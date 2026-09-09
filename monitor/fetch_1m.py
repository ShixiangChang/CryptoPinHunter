# -*- coding: utf-8 -*-
"""拉币安 U 本位合约 1m K线，存 monitor.db 的 klines_1m 表。

为什么：
- 现有 1h K线 = 137 万条（最粗颗粒度）。1m K线 = 1.75 亿条（128 倍），
  币安免费 API 就能拉。这是把「量」从百万级提到亿级的第一步。
- 1m 精度解锁：分钟级入场/止损（1h 太粗会被小时级插针打飞）、日内微观结构因子。

设计：
- 断点续传：progress 记录每币拉到哪，中断后继续，不重复拉。
- 限流退避：每请求 sleep 0.12s（约 500 次/分，安全），遇 429/418 指数退避。
- 只存精简字段 symbol/open_time/high/low/close/volume，批量插入（executemany）。

用法：
    python monitor/fetch_1m.py            # 拉池子全部 50 币的 1m 数据（后台跑 1-3 小时）
    python monitor/fetch_1m.py --symbol BTCUSDT   # 只拉单币（测试）
    python monitor/fetch_1m.py --days 30  # 只拉最近 30 天（快速）
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
POOL_FILE = Path("data/model_out/probe_pool.txt")
PROGRESS_FILE = Path("data/model_out/fetch_1m_progress.json")
INTERVAL = "1m"
LIMIT = 1000
SLEEP = 0.12          # 每请求间隔（约 500 次/分，远低于限流）
START_TS = int(datetime(2024, 8, 29, tzinfo=timezone.utc).timestamp() * 1000)  # 和 1h 对齐


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


def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    return {}


def save_progress(p: dict):
    PROGRESS_FILE.write_text(json.dumps(p), encoding="utf-8")


def fetch_symbol(symbol: str, start_ms: int, now_ms: int) -> int:
    """翻页拉一个币的 1m K线，返回落库条数。"""
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
        except Exception as e:
            print(f"  [{symbol}] 请求异常 {e}，退避 5s")
            time.sleep(5)
            continue

        if r.status_code == 429 or r.status_code == 418:
            print(f"  [{symbol}] {r.status_code} 限流，退避 30s")
            time.sleep(30)
            continue
        if r.status_code != 200:
            print(f"  [{symbol}] HTTP {r.status_code}，退避 3s")
            time.sleep(3)
            continue

        data = r.json()
        if not data or not isinstance(data, list):
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
    ap.add_argument("--symbol", help="只拉单币（测试）")
    ap.add_argument("--days", type=int, default=0, help="只拉最近 N 天（默认拉全部历史）")
    a = ap.parse_args()

    init_table()
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - a.days * 86400_000 if a.days else START_TS

    if a.symbol:
        symbols = [a.symbol]
    else:
        symbols = POOL_FILE.read_text(encoding="utf-8").split()

    progress = load_progress()
    print(f"拉 {len(symbols)} 币 1m K线，起点 {datetime.fromtimestamp(start_ms/1000, tz=timezone.utc)}")
    t0 = time.time()

    # 多线程并行拉（限流内：6 线程 × 每请求 ~1.2s ≈ 5 次/秒 < 8 次/秒上限）
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {}
        for sym in symbols:
            done_until = progress.get(sym)
            sym_start = max(start_ms, done_until) if done_until else start_ms
            futs[ex.submit(fetch_symbol, sym, sym_start, now_ms)] = sym
        for fut in as_completed(futs):
            sym = futs[fut]
            try:
                n = fut.result()
                results[sym] = n
                progress[sym] = now_ms
                save_progress(progress)
                dt = time.time() - t0
                print(f"[{len(results)}/{len(symbols)}] {sym}: +{n} 条（累计 {dt:.0f}s）", flush=True)
            except Exception as e:
                print(f"[{sym}] 失败: {e}", flush=True)

    conn = sqlite3.connect(DB)
    total = conn.execute("SELECT COUNT(*) FROM klines_1m").fetchone()[0]
    n_sym = conn.execute("SELECT COUNT(DISTINCT symbol) FROM klines_1m").fetchone()[0]
    conn.close()
    print(f"\n完成。klines_1m 共 {total:,} 条，{n_sym} 个币，耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
