# -*- coding: utf-8 -*-
"""分布诊断：回答「2024/2025/2026 三个 regime 的插针特征分布是否均匀」。

用途：决定验证能否用「时间切分（早期推演 / 后期验证）」，还是必须按 regime 分层或
用 block bootstrap。时间切分隐含假设「各时段同质」，本脚本用真实数据检验这个假设。

诊断三个量（按月聚合，再按季度对比）：
1. 波动率：1m log return 的 std（年化 %）。
2. 针事件频率：每币每月的插针次数。
   - 固定阈值版（现行 pin：15 分钟 low 跌超 -5% 且 close 反弹 >= +1%）。
   - z-score 版（波动率自适应：z = tip_ret / (σ_240m * sqrt(15))，z <= -3）。
3. 针后回归率：针后 12 小时内 high 回到「针前水平」的比例（pin 的 edge 核心）。

只用 33 个有完整两年 1m 数据的币（其余 120 币只有近 90 天，无法跨期诊断）。
局限：这 33 币是幸存者（活过两年），结论需注明。
"""
from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
import pandas as pd

DB = "data/monitor.db"
TWO_YEAR_START = 1725148800  # 2024-09-01 00:00 UTC

LOOKBACK = 15          # 15 分钟窗口
THRESHOLD = -0.05      # 固定阈值：针尖跌超 -5%
RECOVERY = 0.01        # 收盘相对针尖反弹 >= +1%
VOL_WIN = 240          # z-score 滚动波动率窗口（4 小时）
Z_THRESH = -3.0        # z-score 触发阈值
REGRESS_MIN = 12 * 60  # 回归窗口（对应 pin hold_hours）
COOLDOWN = LOOKBACK    # 事件冷却期（分钟），同一次插针不重复计


def dedup(idx: np.ndarray, cooldown: int) -> np.ndarray:
    """按冷却期去重：保留相邻间隔 >= cooldown 的触发。"""
    if len(idx) == 0:
        return idx
    keep = [int(idx[0])]
    last = int(idx[0])
    for i in idx[1:]:
        if int(i) - last >= cooldown:
            keep.append(int(i))
            last = int(i)
    return np.array(keep, dtype=np.int64)


def month_of(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")


def process_symbol(df: pd.DataFrame) -> dict:
    """对一个币算插针事件 + 回归，返回 month -> 聚合字典。"""
    ot = df["open_time"].to_numpy(dtype=np.int64)
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    n = len(close)

    # 针尖收益 + 反弹
    base = np.full(n, np.nan)
    base[LOOKBACK:] = close[:-LOOKBACK]
    tip_ret = low / base - 1.0
    rec = close / low - 1.0

    # 波动率（1m log return 滚动 std，VOL_WIN 窗口），对齐：vol[t] 用 r[t-VOL_WIN+1..t]
    log_ret = np.log(close[1:] / close[:-1])
    vol = np.full(n, np.nan)
    vol[1:] = pd.Series(log_ret).rolling(VOL_WIN).std().to_numpy(dtype=float)

    # 未来 REGRESS_MIN 分钟内（含当前）的 high 最大值
    fut_max = pd.Series(high).iloc[::-1].rolling(REGRESS_MIN, min_periods=1).max().iloc[::-1].to_numpy(dtype=float)

    # 触发掩码
    mask_fixed = (tip_ret <= THRESHOLD) & (rec >= RECOVERY)
    idx_fixed = dedup(np.where(mask_fixed)[0], COOLDOWN)

    z = tip_ret / (vol * math.sqrt(LOOKBACK))
    mask_z = (z <= Z_THRESH) & (rec >= RECOVERY)
    idx_z = dedup(np.where(mask_z)[0], COOLDOWN)

    # 波动率按月聚合（向量化：整数年月编码，避免逐行 strftime）
    dt = pd.to_datetime(ot, unit="s", utc=True)
    ym = (dt.year * 100 + dt.month).to_numpy()
    valid = ~np.isnan(vol) & (vol > 0)
    vol_clean = np.where(valid, vol, 0.0)
    vsum = pd.Series(vol_clean).groupby(ym).sum()
    vn = pd.Series(valid.astype(int)).groupby(ym).sum()

    def ym_str(k: int) -> str:
        return f"{k // 100:04d}-{k % 100:02d}"

    agg = defaultdict(lambda: {
        "vol_sum": 0.0, "vol_n": 0, "pins_fixed": 0, "pins_z": 0,
        "regress_ok": 0, "regress_n": 0,
    })
    for k in vsum.index:
        m = ym_str(int(k))
        agg[m]["vol_sum"] = float(vsum[k])
        agg[m]["vol_n"] = int(vn[k])

    # 固定阈值版针事件 + 回归（idx 数量少，循环可忽略）
    for i in idx_fixed:
        a = agg[month_of(int(ot[i]))]
        a["pins_fixed"] += 1
        a["regress_n"] += 1
        if fut_max[i] >= base[i]:
            a["regress_ok"] += 1

    # z-score 版针事件
    for i in idx_z:
        agg[month_of(int(ot[i]))]["pins_z"] += 1

    return agg


def main() -> None:
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute(
        "SELECT symbol, MIN(open_time) FROM klines_1m GROUP BY symbol "
        "HAVING MIN(open_time) <= ?",
        (TWO_YEAR_START,),
    )
    two_yr_symbols = sorted(r[0] for r in cur.fetchall())
    print(f"两年完整 1m 数据的币：{len(two_yr_symbols)} 个")

    agg = defaultdict(lambda: {
        "vol_sum": 0.0, "vol_n": 0, "pins_fixed": 0, "pins_z": 0,
        "regress_ok": 0, "regress_n": 0,
    })
    for sym in two_yr_symbols:
        df = pd.read_sql_query(
            "SELECT open_time, high, low, close FROM klines_1m "
            "WHERE symbol=? ORDER BY open_time",
            con, params=(sym,),
        )
        if len(df) < VOL_WIN + LOOKBACK + 1:
            continue
        sa = process_symbol(df)
        for m, v in sa.items():
            a = agg[m]
            for k in v:
                a[k] += v[k]
        print(f"  {sym}: {sum(v['pins_fixed'] for v in sa.values())} 固定针 / "
              f"{sum(v['pins_z'] for v in sa.values())} z针", flush=True)

    # 输出按月序列
    rows = []
    for month in sorted(agg):
        a = agg[month]
        vol_ann = (a["vol_sum"] / a["vol_n"]) * math.sqrt(525600) * 100 if a["vol_n"] else np.nan
        rr = a["regress_ok"] / a["regress_n"] if a["regress_n"] else np.nan
        rows.append({
            "month": month,
            "vol_ann_pct": round(vol_ann, 2),
            "pins_fixed": a["pins_fixed"],
            "pins_z": a["pins_z"],
            "regress_n": a["regress_n"],
            "regress_ok": a["regress_ok"],
            "regress_rate": round(rr, 4) if not math.isnan(rr) else None,
        })
    out = pd.DataFrame(rows)
    out.to_csv("data/dist_diag_monthly.csv", index=False)
    print("\n=== 按月序列（已存 data/dist_diag_monthly.csv）===")
    print(out.to_string(index=False))

    # 按季度聚合
    print("\n=== 按季度聚合 ===")
    qagg = defaultdict(lambda: {
        "vol": [], "pins_fixed": 0, "pins_z": 0, "regress_ok": 0, "regress_n": 0,
    })
    for r in rows:
        y, m = r["month"].split("-")
        q = f"{y}-Q{(int(m) - 1) // 3 + 1}"
        qagg[q]["vol"].append(r["vol_ann_pct"])
        qagg[q]["pins_fixed"] += r["pins_fixed"]
        qagg[q]["pins_z"] += r["pins_z"]
        qagg[q]["regress_ok"] += r["regress_ok"]
        qagg[q]["regress_n"] += r["regress_n"]
    qrows = []
    for q in sorted(qagg):
        a = qagg[q]
        vol = float(np.nanmean(a["vol"]))
        rr = a["regress_ok"] / a["regress_n"] if a["regress_n"] else np.nan
        qrows.append({
            "quarter": q,
            "vol_ann_mean_pct": round(vol, 1),
            "pins_fixed": a["pins_fixed"],
            "pins_z": a["pins_z"],
            "regress_rate": round(rr, 4) if not math.isnan(rr) else None,
            "regress_n": a["regress_n"],
        })
    qdf = pd.DataFrame(qrows)
    print(qdf.to_string(index=False))
    qdf.to_csv("data/dist_diag_quarterly.csv", index=False)

    con.close()
    print("\n完成。")


if __name__ == "__main__":
    main()
