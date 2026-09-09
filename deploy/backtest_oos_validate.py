# -*- coding: utf-8 -*-
"""样本外验证：分档规律能否在验证期复现。

两段独立窗口（3/12-6/10 与 6/10-9/8），各自 point-in-time 定池在窗口起点，
口径完全一致，只比「因子 -> 12h 收益」的横截面方向是否复现（无前视）。
"""
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import data
from engine.strategies import PinStrategy

strat = PinStrategy()
th = strat.threshold
rec_min = strat.recovery
fmin = strat.funding_min
trend_max_dd = strat.trend_max_dd
lookback = strat.lookback
HOLD = 12 * 3600
DAYS = 90

NOW = int(time.time())
IN_START = NOW - DAYS * 86400          # 6/10
OOS_END = IN_START                     # 6/10
OOS_START = OOS_END - DAYS * 86400     # 3/12


def analyze(start, end, pool_end):
    """point-in-time 定池在 pool_end，跑 start~end 的触发，返回记录列表。"""
    pool = data.liquid_symbols("1m", top_n=50, window_days=90,
                               end_ts=pool_end, require_spot=True)
    rank_of = {s: i for i, s in enumerate(pool)}
    dm1 = data.load_klines(pool, "1m", start=start, end=end,
                           columns=["open_time", "close", "low"])
    dm1h = data.load_klines(pool, "1h", start=start - HOLD, end=end,
                            columns=["open_time", "close"])
    funding = data.load_funding(pool)
    trend_map = data.load_trend_highs(
        pool, start - strat.trend_lookback_h * 3600, end, strat.trend_lookback_h)

    recs = []
    for sym in pool:
        df = dm1.get(sym)
        if df is None or len(df) <= lookback:
            continue
        ot = df["open_time"].to_numpy(dtype=np.int64)
        close = df["close"].to_numpy(dtype=float)
        low = df["low"].to_numpy(dtype=float)
        base = np.full_like(close, np.nan)
        base[lookback:] = close[:-lookback]
        tip_ret = low / base - 1.0
        rec = close / low - 1.0
        pin = (tip_ret <= th) & (rec >= rec_min) & np.isfinite(tip_ret) & np.isfinite(rec)

        h1 = dm1h.get(sym)
        h_ot = h1["open_time"].to_numpy(dtype=np.int64) if h1 is not None and len(h1) else None
        h_close = h1["close"].to_numpy(dtype=float) if h1 is not None and len(h1) else None

        for i in np.where(pin)[0]:
            t = int(ot[i])
            fv = data.prev_funding(funding, sym, t)
            if fv is None or fv < fmin:
                continue
            dd = data.trend_dd_at(trend_map, sym, t, float(close[i]))
            if dd is None or dd < trend_max_dd:
                continue
            entry = float(close[i])
            if h_ot is None:
                continue
            k = int(np.searchsorted(h_ot, t + HOLD, side="left"))
            if k >= len(h_ot):
                continue
            ret = float(h_close[k]) / entry - 1.0
            recs.append({"sym": sym, "t": t, "liq_rank": rank_of.get(sym, 99),
                         "depth": abs(float(tip_ret[i])) / abs(th),
                         "funding": fv, "trend_dd": dd, "ret": ret})
    return recs


def report(recs, label):
    print(f"\n{'=' * 56}")
    print(f"{label}：触发 {len(recs)} 次")
    if not recs:
        return
    for key, edges, labs, name in [
        ("liq_rank", [-1, 10, 30, 999], ["大币<10", "中10-30", "小>=30"], "流动性"),
        ("depth", [0, 1.5, 3, 999], ["浅<1.5", "中1.5-3", "深>=3"], "深度"),
        ("funding", [0, 0.0005, 0.002, 999], ["弱正<0.05%", "中0.05-0.2%", "强正>=0.2%"], "funding"),
        ("trend_dd", [-999, -0.2, -0.1, 0], ["深跌-30~-20%", "中-20~-10%", "浅>-10%"], "趋势"),
    ]:
        print(f"  [{name}]")
        for lo, hi, lb in zip(edges[:-1], edges[1:], labs):
            sub = [r for r in recs if lo <= r[key] < hi]
            if not sub:
                print(f"    {lb:>16s}  n=0")
                continue
            rr = np.array([r["ret"] for r in sub])
            print(f"    {lb:>16s}  n={len(sub):>3d}  均值{rr.mean():+.2%}  "
                  f"中位{np.median(rr):+.2%}  胜率{(rr > 0).mean():.0%}")


report(analyze(IN_START, NOW, IN_START), "样本内 6/10-9/8（定池在 6/10）")
report(analyze(OOS_START, OOS_END, OOS_START), "样本外 3/12-6/10（定池在 3/12）")
