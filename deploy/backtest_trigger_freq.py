# -*- coding: utf-8 -*-
"""回溯统计：当前 50 币池，过去 N 天，各层过滤的触发次数。

口径与 engine/strategies/pin.py 的 generate_signals 完全一致（同一套 data 函数），
只把「逐分钟实盘判断」改成「向量化扫历史 + 逐针查 funding/趋势」。

输出分层计数，回答一个问题：正式参数下，这 50 个币过去 90 天到底会触发几次？
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
from engine import data
from engine.strategies import PinStrategy

DAYS = 90
NOW = int(time.time())
START = NOW - DAYS * 24 * 3600

strat = PinStrategy()
universe = strat.universe
th = strat.threshold          # 针尖阈值（-0.05）
rec_min = strat.recovery      # 反弹（+0.01）
fmin = strat.funding_min      # funding 下限（0.0001）
trend_max_dd = strat.trend_max_dd  # 趋势（-0.30）
lookback = strat.lookback     # 15

print(f"=== pin 回溯触发统计 ===")
print(f"池子: {len(universe)} 币")
print(f"参数: threshold={th}  recovery={rec_min}  funding_min={fmin}  trend_max_dd={trend_max_dd}")
print(f"回溯窗口: {DAYS} 天")

# 数据时间范围（1m）
dm1 = data.load_klines(universe, interval="1m", start=START,
                       columns=["open_time", "close", "low"])
have = [s for s, df in dm1.items() if len(df) > lookback]
mn = min(int(df["open_time"].iloc[0]) for df in dm1.values())
mx = max(int(df["open_time"].iloc[-1]) for df in dm1.values())
print(f"1m 数据: {len(have)}/{len(universe)} 币有数据, "
      f"{time.strftime('%Y-%m-%d', time.gmtime(mn))} ~ {time.strftime('%Y-%m-%d', time.gmtime(mx))} "
      f"(约 {(mx-mn)//86400} 天)")

# funding / 趋势（复用实盘同一套函数）
funding = data.load_funding(universe) if strat.require_funding_positive else {}
trend_map = {}
if strat.require_trend_filter:
    # 趋势过滤需「触发点往前 90 天 1h 高点」，这里多往前取 90 天
    trend_map = data.load_trend_highs(
        universe, START - strat.trend_lookback_h * 3600, NOW, strat.trend_lookback_h)

tot = {"pin": 0, "funding_ok": 0, "trend_ok": 0, "trend_none": 0}
rows = []   # 最终通过全条件的明细

for sym in universe:
    df = dm1.get(sym)
    if df is None or len(df) <= lookback:
        continue
    ot = df["open_time"].to_numpy(dtype=np.int64)
    close = df["close"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)

    # 向量化插针：base[i] = close[i-15], tip=low[i], rec=close[i]/low[i]-1
    base = np.full_like(close, np.nan)
    base[lookback:] = close[:-lookback]
    tip_ret = low / base - 1.0
    rec = close / low - 1.0
    pin_mask = (tip_ret <= th) & (rec >= rec_min) & np.isfinite(tip_ret) & np.isfinite(rec)
    pin_idx = np.where(pin_mask)[0]

    n_pin = int(len(pin_idx))
    n_fund = 0
    n_trend = 0
    n_trend_none = 0

    for i in pin_idx:
        t = int(ot[i])
        # 条件4：funding 正（针前最近已结算 funding）
        if strat.require_funding_positive:
            fv = data.prev_funding(funding, sym, t)
            if fv is None or fv < fmin:
                continue
        n_fund += 1
        # 条件5：趋势过滤（距 90 天高点跌幅 ≤30%）
        if strat.require_trend_filter:
            dd = data.trend_dd_at(trend_map, sym, t, float(close[i]))
            if dd is None:
                n_trend_none += 1
                continue
            if dd < trend_max_dd:
                continue
        n_trend += 1
        rows.append((sym, t, float(close[i])))

    tot["pin"] += n_pin
    tot["funding_ok"] += n_fund
    tot["trend_ok"] += n_trend
    tot["trend_none"] += n_trend_none
    if n_pin > 0:
        print(f"  {sym:>14s} 插针{n_pin:>3d}  funding后{n_fund:>3d}  趋势后{n_trend:>3d}")

print()
print("=== 分层汇总 ===")
print(f"层1 纯插针(条件1+2)      : {tot['pin']} 次")
print(f"层2 +funding(条件4)      : {tot['funding_ok']} 次")
print(f"层3 +趋势(条件5)         : {tot['trend_ok']} 次  (数据不足跳过 {tot['trend_none']} 次)")
print(f"最终全条件触发            : {tot['trend_ok']} 次")
print()
print("=== 最终触发明细（全条件通过）===")
for sym, t, px in rows:
    print(f"  {sym} {time.strftime('%Y-%m-%d %H:%M', time.gmtime(t))} @ {px}")
if not rows:
    print("  （无）")
