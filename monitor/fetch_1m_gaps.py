# -*- coding: utf-8 -*-
"""补 klines_1m 的内部缺口（gap > 5min 的孔洞）。

为什么：
- 并发拉取（fetch_1m_extra + 主循环 + 纸面引擎）会偶发 429 限流，给 1m 表留下
  18~143 分钟的孔洞。20/28 币各 1 个孔洞、全在今天傍晚，合计 1722 分钟。
- 这些孔洞会：①漏掉孔洞内的插针（低估事件）②跨孔洞算 15min 收益时失真。

设计：扫描所有币相邻 open_time 差 > 5min 的孔洞，逐个按缺口区间拉回补上。
  幂等（INSERT OR REPLACE）、限流退避、单币失败不炸全局。

用法：
    python monitor/fetch_1m_gaps.py            # 扫全表所有孔洞
    python monitor/fetch_1m_gaps.py --hours 24 # 只扫最近 24h 的孔洞
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

import requests

# 代理从环境变量读（直连留空），不硬编码本地代理
_proxy = __import__("os").environ.get("BINANCE_PROXY", "").strip()
PROXY = {"http": _proxy, "https": _proxy} if _proxy else None
BASE = "https://fapi.binance.com/fapi/v1/klines"
DB = Path("data/monitor.db")
GAP_MIN = 5            # 相邻差 > 5min 视为孔洞
SLEEP = 0.15


def _scan_gaps(conn, min_ts: int) -> list[tuple[str, int, int]]:
    """返回 [(symbol, gap_start_s, gap_end_s)]，gap 区间为开区间（两边已有数据）。

    用 SQL 窗口函数 LAG 找相邻 open_time 差 > 5min 的孔洞，避免把全表拉进 Python。
    """
    sql = """
        SELECT symbol, prev_ts, open_time FROM (
            SELECT symbol, open_time,
                   LAG(open_time) OVER (PARTITION BY symbol ORDER BY open_time) AS prev_ts
            FROM klines_1m
            WHERE open_time >= ?
        ) WHERE prev_ts IS NOT NULL AND open_time - prev_ts > ?
    """
    rows = conn.execute(sql, (min_ts, GAP_MIN * 60)).fetchall()
    return [(s, prev + 60, ts - 60) for s, prev, ts in rows]


def _fill_gap(symbol: str, start_s: int, end_s: int) -> int:
    """拉 [start_s, end_s] 秒区间（含端点）的 1m K 线并 upsert，返回条数。"""
    session = requests.Session()
    start_ms = start_s * 1000
    end_ms = end_s * 1000
    total = 0
    cur = start_ms
    fails = 0
    MAX_FAILS = 12          # 连续失败上限，防网络/代理长期故障时无限重试
    while cur <= end_ms:
        try:
            r = session.get(BASE, params={
                "symbol": symbol, "interval": "1m",
                "startTime": cur, "endTime": end_ms, "limit": 1000,
            }, proxies=PROXY, timeout=20)
        except Exception as e:
            fails += 1
            print(f"  [{symbol}] 请求异常 {e}，退避 5s（{fails}/{MAX_FAILS}）")
            if fails >= MAX_FAILS:
                break
            time.sleep(5)
            continue
        if r.status_code in (429, 418):
            fails += 1
            print(f"  [{symbol}] {r.status_code} 限流，退避 30s（{fails}/{MAX_FAILS}）")
            if fails >= MAX_FAILS:
                break
            time.sleep(30)
            continue
        if r.status_code != 200:
            fails += 1
            print(f"  [{symbol}] HTTP {r.status_code}，退避 3s（{fails}/{MAX_FAILS}）")
            if fails >= MAX_FAILS:
                break
            time.sleep(3)
            continue
        fails = 0
        data = r.json()
        if not data or not isinstance(data, list):
            break
        rows = [(symbol, int(d[0]) // 1000, float(d[2]), float(d[3]),
                 float(d[4]), float(d[5])) for d in data]
        conn = sqlite3.connect(DB)
        conn.executemany(
            "INSERT OR REPLACE INTO klines_1m (symbol, open_time, high, low, close, volume) "
            "VALUES (?,?,?,?,?,?)", rows)
        conn.commit()
        conn.close()
        total += len(rows)
        cur = int(data[-1][0]) + 60_000
        if len(data) < 1000:
            break
        time.sleep(SLEEP)
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24,
                    help="只扫最近 N 小时的孔洞（默认 24h，限流洞都在近期；0=全表）")
    a = ap.parse_args()

    min_ts = int(time.time()) - a.hours * 3600 if a.hours else 0
    conn = sqlite3.connect(DB)
    gaps = _scan_gaps(conn, min_ts)
    conn.close()

    if not gaps:
        print("无孔洞，数据连续。")
        return

    total_min = sum((e - s) // 60 for _, s, e in gaps)
    print(f"发现 {len(gaps)} 个孔洞，合计 {total_min} min，开始补拉…", flush=True)

    filled = 0
    for s, gs, ge in gaps:
        n = _fill_gap(s, gs, ge)
        filled += n
        print(f"  {s:14s} 补 {n:>4} 根（缺 {(ge-gs)//60:>3}min）", flush=True)
        time.sleep(SLEEP)

    print(f"完成：补 {filled:,} 根，覆盖 {total_min} min", flush=True)


if __name__ == "__main__":
    main()
