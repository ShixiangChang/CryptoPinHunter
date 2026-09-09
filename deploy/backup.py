# -*- coding: utf-8 -*-
"""每日备份：只导出「不可再生」的核心资产（决策/交易/权益曲线），行情表不备份（币安可重拉）。

- live_pin_decisions -> backups/decisions_YYYY-MM-DD.csv
- live_pin_trades     -> backups/trades_YYYY-MM-DD.csv
- 权益曲线 history     -> backups/equity_YYYY-MM-DD.csv

行情数据（klines_1m/klines/funding）量级大且可随时从币安免费重拉，不做每日全量备份。
"""
import csv
import datetime
import json
import sqlite3
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

DB = _root / "data" / "monitor.db"
STATE = _root / "data" / "live_state.json"
BAK = _root / "backups"
BAK.mkdir(parents=True, exist_ok=True)

today = datetime.date.today().isoformat()

conn = sqlite3.connect(DB)
for table, name in [("live_pin_decisions", "decisions"), ("live_pin_trades", "trades")]:
    cols = [d[0] for d in conn.execute(f"PRAGMA table_info({table})")]
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    out = BAK / f"{name}_{today}.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    print(f"[backup] {name}: {len(rows)} 行 -> {out.name}")

if STATE.exists():
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
        hist = st.get("history", [])
        out = BAK / f"equity_{today}.csv"
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["ts", "equity"])
            w.writerows(hist)
        print(f"[backup] equity: {len(hist)} 点 -> {out.name}")
    except Exception as e:
        print(f"[backup] equity 读取失败: {e}")

conn.close()
print("[backup] 完成")
