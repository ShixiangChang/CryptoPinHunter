# -*- coding: utf-8 -*-
"""扫描实盘状态：账户权益、持仓浮盈、决策表、state、数据新鲜度。只读，不下单。"""
import sqlite3
import sys
import datetime
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from engine.executor import BinanceFutures
from monitor import credentials as c

print("=" * 50)
print("账户 + 持仓")
print("=" * 50)
ex = BinanceFutures(c.BINANCE_FUTURES_API_KEY, c.BINANCE_FUTURES_API_SECRET,
                    base_url=config.TRADE_BASE_URL)
a = ex.account()
print("权益 totalMarginBalance:", a.get("totalMarginBalance"))
print("未实现盈亏:", a.get("totalUnrealizedProfit"))
print("可用余额 availableBalance:", a.get("availableBalance"))
pos = [p for p in ex.positions() if float(p.get("positionAmt", 0)) != 0]
if pos:
    for p in pos:
        print(f"  持仓 {p.get('symbol')}: qty={p.get('positionAmt')} "
              f"entry={p.get('entryPrice')} 浮盈={p.get('unRealizedProfit')}")
else:
    print("  无持仓")

print()
print("=" * 50)
print("决策表（最近 15 条）")
print("=" * 50)
conn = sqlite3.connect(config.DB_PATH)
rows = conn.execute(
    "SELECT id, ts, symbol, action, weight, notional_usdt, detail "
    "FROM live_pin_decisions ORDER BY id DESC LIMIT 15").fetchall()
print(f"决策总数: {conn.execute('SELECT COUNT(*) FROM live_pin_decisions').fetchone()[0]}")
for r in rows:
    t = datetime.datetime.utcfromtimestamp(r[1]).strftime("%m-%d %H:%M:%S")
    print(f"  [{t}] {r[0]} {r[2]} {r[3]} w={r[4]} notional={r[5]} {r[6]}")
trades = conn.execute("SELECT COUNT(*) FROM live_pin_trades").fetchone()[0]
print(f"已平仓交易流水: {trades} 条")
conn.close()

print()
print("=" * 50)
print("state 文件")
print("=" * 50)
st = json.loads(Path(config.ROOT / "data" / "live_state.json").read_text(encoding="utf-8"))
print("initial_usdt:", st.get("initial_usdt"))
print("halted:", st.get("halted"))
print("env_fingerprint:", st.get("env_fingerprint"))
print("持仓:", st.get("positions"))
print("history 点数:", len(st.get("history", [])))
if st.get("history"):
    print("  最早:", st["history"][0], " 最新:", st["history"][-1])

print()
print("=" * 50)
print("数据新鲜度")
print("=" * 50)
conn = sqlite3.connect(config.DB_PATH)
for tbl, col in [("klines_1m", "open_time"), ("klines", "open_time"),
                 ("funding_hist", "funding_time")]:
    n, mx = conn.execute(f"SELECT COUNT(*), MAX({col}) FROM {tbl}").fetchone()
    t = datetime.datetime.utcfromtimestamp(mx).strftime("%m-%d %H:%M:%S")
    print(f"  {tbl}: {n:,} 条, 最新 {t} UTC")
conn.close()
