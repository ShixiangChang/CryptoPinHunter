# -*- coding: utf-8 -*-
"""只读：查池子 50 币的最小名义订单（MIN_NOTIONAL）+ 精度，判断给定账户规模能否下单。

用法：python deploy/check_min_notional.py [账户权益USDT]   # 默认 100
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine.strategies import PinStrategy
from engine.executor import BinanceFutures

eq = float(sys.argv[1]) if len(sys.argv) > 1 else 100.0

u = PinStrategy().universe
ex = BinanceFutures("", "")  # 公开接口，不需 key
info = ex._get("/fapi/v1/exchangeInfo")
syms = {s["symbol"]: s for s in info["symbols"]}

rows = []
for s in u:
    if s not in syms:
        continue
    min_notional = None
    step = minq = None
    for f in syms[s]["filters"]:
        if f["filterType"] == "MIN_NOTIONAL":
            min_notional = float(f["notional"])
        if f["filterType"] == "LOT_SIZE":
            step = float(f["stepSize"])
            minq = float(f["minQty"])
    rows.append((s, min_notional, step, minq))

# 按最小名义分组
from collections import Counter
c = Counter(r[1] for r in rows if r[1])
print("=== 池子 %d 币的 MIN_NOTIONAL 分布 ===" % len(rows))
for mn, n in sorted(c.items()):
    print("  最小名义 %s USDT: %d 币" % (mn, n))
print()
print("=== 明细（前 20）===")
for s, mn, step, minq in rows[:20]:
    print("  %-14s min_notional=%s step=%s minQty=%s" % (s, mn, step, minq))
print()
# 账户权益 eq USDT 下的单笔能力
print("=== %.2f USDT 账户，单笔 5%% = %.2f USDT / 封顶15%% = %.2f USDT ===" % (eq, eq*0.05, eq*0.15))
can_5 = sum(1 for r in rows if r[1] and r[1] <= eq*0.05)
can_15 = sum(1 for r in rows if r[1] and r[1] <= eq*0.15)
print("  5%% 仓位(%.2f)能下的币: %d / %d" % (eq*0.05, can_5, len(rows)))
print("  15%% 仓位(%.2f)能下的币: %d / %d" % (eq*0.15, can_15, len(rows)))
