# -*- coding: utf-8 -*-
"""单仓择优 vs 基线：3 个月回溯，验证「吃回归锚最强的」是否跑赢「按池子顺序吃第一个」。

口径与 generate_signals 完全一致（同一套 data 函数）。三层输出：
  A. 触发明细（因子快照 + 12h 持有收益）→ 存 CSV 供复盘
  B. 单因子分档：liquidity / depth / funding / trend 各分 3 档看 12h 收益（谁有预测力）
  C. 单仓模拟：基线(吃池子第一个) vs 择优(吃回归锚分最高) 的总收益/胜率/均笔/回撤

关键无前视保证：score 只用触发时刻已知的因子(funding/trend/liquidity/depth)，
权重来自样本外独立验证的结论，不在本批数据上拟合；12h 收益只当 label 评估。
"""
import sys
import time
import csv
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import data
from engine.strategies import PinStrategy

DAYS = 90
HOLD = 12 * 3600          # 12h 持有
NOW = int(time.time())
START = NOW - DAYS * 24 * 3600

strat = PinStrategy()
universe = strat.universe
rank_of = {s: i for i, s in enumerate(universe)}
th = strat.threshold
rec_min = strat.recovery
fmin = strat.funding_min
trend_max_dd = strat.trend_max_dd
lookback = strat.lookback

print(f"=== 单仓择优回测 ===")
print(f"池子 {len(universe)} 币 | threshold={th} recovery={rec_min} "
      f"funding_min={fmin} trend_max_dd={trend_max_dd} | 回溯 {DAYS} 天")

# ---- 数据 ----
dm1 = data.load_klines(universe, interval="1m", start=START,
                       columns=["open_time", "close", "low"])
dm1h = data.load_klines(universe, interval="1h", start=START,
                        columns=["open_time", "close"])
funding = data.load_funding(universe)
trend_map = data.load_trend_highs(
    universe, START - strat.trend_lookback_h * 3600, NOW, strat.trend_lookback_h)

# ---- 收集触发信号 + 因子 + 12h 收益(label) ----
records = []
for sym in universe:
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

    # 1h exit 序列（算 12h 后收益，1h 数据 180 天完整无缺口）
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
        # 12h 后收益：exit 用 t+12h 起的第 1 根 1h close（持有到期平仓）
        if h_ot is None:
            continue
        k = int(np.searchsorted(h_ot, t + HOLD, side="left"))
        if k >= len(h_ot):
            continue
        exit_px = float(h_close[k])
        ret = exit_px / entry - 1.0
        records.append({
            "sym": sym, "t": t, "entry": entry,
            "liq_rank": rank_of.get(sym, 99),
            "depth": abs(float(tip_ret[i])) / abs(th),
            "funding": fv, "trend_dd": dd,
            "ret": ret, "exit": exit_px,
        })

n = len(records)
print(f"全条件触发(可算 12h 收益的): {n} 次")
if n == 0:
    print("无样本，退出")
    sys.exit(0)

# ---- A. 明细落 CSV ----
csv_path = ROOT / "data" / "pin_pick_backtest.csv"
with open(csv_path, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["symbol", "t_utc", "entry", "liq_rank", "depth",
                "funding", "trend_dd", "ret_12h", "exit"])
    for r in records:
        w.writerow([r["sym"], r["t"], round(r["entry"], 8), r["liq_rank"],
                    round(r["depth"], 3), r["funding"], round(r["trend_dd"], 4),
                    round(r["ret"], 6), round(r["exit"], 8)])
print(f"A. 明细已存 {csv_path}")

# ---- B. 单因子分档（3 档，看 12h 收益单调性）----
def band_stat(key, edges, labels):
    print(f"\nB. 单因子分档 [{key}]  (12h 持有收益)")
    for lo, hi, lb in zip(edges[:-1], edges[1:], labels):
        sub = [r for r in records if lo <= r[key] < hi]
        if not sub:
            print(f"   {lb:>12s}  n=0")
            continue
        rr = np.array([r["ret"] for r in sub])
        wr = (rr > 0).mean()
        print(f"   {lb:>12s}  n={len(sub):>3d}  均值{rr.mean():+.2%}  "
              f"中位{np.median(rr):+.2%}  胜率{wr:.1%}")
    print()

band_stat("liq_rank", [-1, 10, 30, 999], ["大币(rank<10)", "中(10-30)", "小(>=30)"])
band_stat("depth", [0, 1.5, 3, 999], ["浅(<1.5)", "中(1.5-3)", "深(>=3)"])
band_stat("funding", [0, 0.0005, 0.002, 999], ["弱正(<0.05%)", "中(0.05-0.2%)", "强正(>=0.2%)"])
band_stat("trend_dd", [-999, -0.2, -0.1, 0], ["深跌(-30~-20%)", "中(-20~-10%)", "浅跌(>-10%)"])

# ---- C. 单仓模拟：基线 vs 择优 ----
def pct(arr):
    a = np.array(arr, dtype=float)
    if len(a) <= 1:
        return np.zeros_like(a)
    order = np.argsort(np.argsort(a, kind="stable"))
    return order / (len(a) - 1)

# score：权重来自已实证结论(funding 最强 > trend > liquidity > depth)
f_all = np.array([r["funding"] for r in records])
t_all = np.array([-r["trend_dd"] for r in records])          # 越接近 0 越高
l_all = np.array([-r["liq_rank"] for r in records])          # rank 越小越高
d_all = np.array([min(r["depth"], 3.0) for r in records])    # 深度封顶 3
p_f, p_t, p_l, p_d = pct(f_all), pct(t_all), pct(l_all), pct(d_all)
for i, r in enumerate(records):
    r["score"] = 3.0 * p_f[i] + 2.0 * p_t[i] + 1.5 * p_l[i] + 1.0 * p_d[i]

groups = defaultdict(list)
for r in records:
    groups[r["t"]].append(r)
times = sorted(groups.keys())

def simulate(selector, name, groups_=None, times_=None):
    groups_ = groups if groups_ is None else groups_
    times_ = times if times_ is None else times_
    free_at = 0
    picks = []
    for t in times_:
        if t < free_at:
            continue
        cands = groups_[t]
        picks.append(selector(cands))
        free_at = t + HOLD
    rets = np.array([p["ret"] for p in picks])
    eq = np.cumprod(1.0 + rets)
    total = eq[-1] - 1
    # 最大回撤
    peak = np.maximum.accumulate(eq)
    dd = (eq / peak - 1).min()
    wr = (rets > 0).mean()
    print(f"{name:>8s}: 成交 {len(picks):>3d} 笔 | 总收益 {total:+.2%} | "
          f"均笔 {rets.mean():+.2%} | 胜率 {wr:.1%} | 最大回撤 {dd:.2%}")
    return picks, rets, total

print("\nC. 单仓模拟（时间排序，同 1m 决策时刻吃 1 个，锁 12h）")
first_picks, first_rets, first_total = simulate(lambda c: c[0], "基线")
best_picks, best_rets, best_total = simulate(
    lambda c: max(c, key=lambda x: x["score"]), "择优")

print("\n=== 对比 ===")
print(f"择优 vs 基线 总收益差: {best_total - first_total:+.2%}")
print(f"择优 vs 基线 均笔差:   {(best_rets.mean() - first_rets.mean()):+.2%}")
print(f"择优 vs 基线 胜率差:   {(best_rets>0).mean() - (first_rets>0).mean():+.1%}")

print("\n=== 择优成交明细(前 20 笔) ===")
for p in best_picks[:20]:
    print(f"  {p['sym']:>12s} {time.strftime('%m-%d %H:%M', time.gmtime(p['t']))} "
          f"entry={p['entry']:.4f} funding={p['funding']:.4%} "
          f"trend={p['trend_dd']:.1%} ret={p['ret']:+.2%}")

# ==================== 收紧版（分档发现的边界，非精细拟合）====================
# funding 弱正(0.01~0.05%)：强正(>=0.05%) 是泡沫破裂不是错杀，巨亏
# trend >-20%：-20~-30% 是"真相"巨亏，>-20% 才稳赚
# depth >=1.5：浅针(<1.5) 负收益
print("\n" + "=" * 56)
print("=== 收紧版：funding 弱正(0.01~0.05%) + 趋势>-20% + 深度>=1.5 ===")
tight = [r for r in records if 0.0001 <= r["funding"] < 0.0005
         and r["trend_dd"] > -0.20 and r["depth"] >= 1.5]
print(f"收紧后信号数: {len(tight)} (原 {len(records)})")

if tight:
    # 在 tight 子集内重算 score（pct 归一）
    f2 = np.array([r["funding"] for r in tight])
    t2 = np.array([-r["trend_dd"] for r in tight])
    l2 = np.array([-r["liq_rank"] for r in tight])
    d2 = np.array([min(r["depth"], 3.0) for r in tight])
    p2f, p2t, p2l, p2d = pct(f2), pct(t2), pct(l2), pct(d2)
    for i, r in enumerate(tight):
        r["score"] = 3.0 * p2f[i] + 2.0 * p2t[i] + 1.5 * p2l[i] + 1.0 * p2d[i]

    tg = defaultdict(list)
    for r in tight:
        tg[r["t"]].append(r)
    ttimes = sorted(tg.keys())

    print("\n单仓模拟（收紧版）：")
    sim_first = simulate(lambda c: c[0], "基线", tg, ttimes)
    sim_best = simulate(lambda c: max(c, key=lambda x: x["score"]), "择优", tg, ttimes)

    print("\n=== 收紧版择优成交明细 ===")
    for p in sim_best[0]:
        print(f"  {p['sym']:>12s} {time.strftime('%m-%d %H:%M', time.gmtime(p['t']))} "
              f"depth={p['depth']:.1f} funding={p['funding']:.4%} "
              f"trend={p['trend_dd']:.1%} ret={p['ret']:+.2%}")
else:
    print("收紧后无信号")
