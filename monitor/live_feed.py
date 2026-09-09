# -*- coding: utf-8 -*-
"""live_feed.py —— 实盘数据源：1m + 1h K 线 + funding 三路常驻采集。

pin 策略实时决策依赖三张表，本进程负责全部写入：
  klines_1m    1m K 线（插针检测触发信号；每轮拉最新 500 根≈8h）
  klines       1h K 线（趋势过滤：过去 90 天最高 high；每轮拉最新 500 根≈20 天增量）
  funding_hist 资金费率（funding 过滤：针前最近一次已结算 funding；每轮拉最新 100 条≈33 天增量）

预热说明：1h 的 90 天、funding 的 90 天、1m 的 3 个月（定池成交额），首次部署由
deploy/fetch_history.sh 预采；本进程只负责持续增量（INSERT OR REPLACE 幂等），
崩溃重启自动补齐（1m 停机<8h、1h 停机<20天、funding 停机<33天无需维护断点）。

香港服务器直连币安（PROXY 留空）。50 币 × 3 请求/轮 ≈ 150 weight/分钟，远低于限流。
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import requests

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
import config
from engine.strategies import PinStrategy

FETCH_LIMIT_1M = 500          # 1m 每币拉最新 500 根（约 8.3 小时）
FETCH_LIMIT_1H = 500          # 1h 每币拉最新 500 根（约 20.8 天）
FETCH_LIMIT_FUND = 100        # funding 每币拉最新 100 条（约 33 天）
SLEEP_PER_SYMBOL = 0.2        # 每币间隔，避免瞬时打爆
CYCLE_SEC = 60                # 每轮间隔


def _proxies():
    return {"http": config.PROXY, "https": config.PROXY} if config.PROXY else None


def _get(url: str, params: dict):
    r = requests.get(url, params=params, proxies=_proxies(), timeout=config.TIMEOUT)
    if r.status_code != 200:
        return []
    return r.json()


def fetch_klines(symbol: str, interval: str, limit: int) -> list[tuple]:
    """拉 K 线。1m 返回 6 列，1h 返回 11 列（完整 schema）。"""
    rows = _get(f"{config.BASE_URL}/fapi/v1/klines",
                {"symbol": symbol, "interval": interval, "limit": limit})
    out = []
    for k in rows:
        try:
            if interval == "1m":
                out.append((symbol, int(k[0]) // 1000, float(k[2]), float(k[3]),
                            float(k[4]), float(k[5])))
            else:  # 1h 完整 11 列
                out.append((symbol, int(k[0]) // 1000, float(k[1]), float(k[2]),
                            float(k[3]), float(k[4]), float(k[5]), float(k[7]),
                            int(k[8]), float(k[9]), float(k[10])))
        except (ValueError, IndexError):
            continue
    return out


def fetch_funding(symbol: str) -> list[tuple]:
    """拉已结算 funding rate（fundingTime 为该周期结算时刻，已可知、无前视）。"""
    rows = _get(f"{config.BASE_URL}/fapi/v1/fundingRate",
                {"symbol": symbol, "limit": FETCH_LIMIT_FUND})
    out = []
    for d in rows:
        try:
            out.append((symbol, int(d["fundingTime"]) // 1000, float(d["fundingRate"])))
        except (ValueError, KeyError):
            continue
    return out


def init_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS klines_1m (
        symbol TEXT NOT NULL, open_time INTEGER NOT NULL,
        high REAL, low REAL, close REAL, volume REAL,
        PRIMARY KEY (symbol, open_time))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kl1m_sym_ts ON klines_1m(symbol, open_time)")
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


def main() -> None:
    strat = PinStrategy()
    universe = strat.universe
    print(f"[live_feed] 池子 {len(universe)} 币，每 {CYCLE_SEC}s 一轮（1m + 1h + funding）")
    conn = sqlite3.connect(config.DB_PATH)
    init_tables(conn)
    try:
        while True:
            ok1m = ok1h = okfund = 0
            for sym in universe:
                try:
                    rows = fetch_klines(sym, "1m", FETCH_LIMIT_1M)
                    if rows:
                        conn.executemany(
                            "INSERT OR REPLACE INTO klines_1m "
                            "(symbol, open_time, high, low, close, volume) VALUES (?,?,?,?,?,?)",
                            rows)
                        ok1m += 1
                except Exception as e:
                    print(f"[live_feed] 1m {sym} 失败: {e}")
                try:
                    rows = fetch_klines(sym, "1h", FETCH_LIMIT_1H)
                    if rows:
                        conn.executemany(
                            "INSERT OR REPLACE INTO klines "
                            "(symbol, open_time, open, high, low, close, volume, "
                            "quote_volume, trades, taker_buy_base, taker_buy_quote) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
                        ok1h += 1
                except Exception as e:
                    print(f"[live_feed] 1h {sym} 失败: {e}")
                try:
                    rows = fetch_funding(sym)
                    if rows:
                        conn.executemany(
                            "INSERT OR REPLACE INTO funding_hist "
                            "(symbol, funding_time, funding) VALUES (?,?,?)", rows)
                        okfund += 1
                except Exception as e:
                    print(f"[live_feed] funding {sym} 失败: {e}")
                time.sleep(SLEEP_PER_SYMBOL)
            conn.commit()
            import datetime
            latest = conn.execute("SELECT MAX(open_time) FROM klines_1m").fetchone()[0]
            t = datetime.datetime.fromtimestamp(latest, tz=datetime.timezone.utc).strftime("%m-%d %H:%M") if latest else "无"
            print(f"[live_feed] 一轮完成 1m:{ok1m}/{len(universe)} 1h:{ok1h}/{len(universe)} "
                  f"funding:{okfund}/{len(universe)}，最新 1m {t}")
            time.sleep(CYCLE_SEC)
    except KeyboardInterrupt:
        print("[live_feed] 退出")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
