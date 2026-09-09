# -*- coding: utf-8 -*-
"""研究假设：funding 能否事前区分「可抄的针」与「不可抄的针」。

设计要点：
- 度量用**持有收益**：针后 12h 收益 = close[i+720]/close[i] - 1（entry=针后收盘，
  exit=12h 后收盘），即 pin 策略每笔的真实盈亏；不用「是否回到针前水平」这类二元量
  （其对照组天然偏向「触及当前价」，会虚高回归率）。
- 分组：funding 正 / 负 / 中性三组对比。机制假设 =「杠杆极端（多/空拥挤）→ 错杀 →
  回归；中性 → 信息驱动 → 不回归」。

预先注册（分析前写死）：
- 度量：针后 12h 持有收益（几何）。
- H1：funding 中性组的针后收益 < 极端组（正或负），即「中性针」是坏针。
- H2（对照）：针后 12h 收益 > 同币同时段随机时点（针前 24h）的 12h 收益，即针携带超额信息。
- 检验：Mann-Whitney U（收益偏态，非参数更稳），p<0.05 且效应量方向正确才认。
"""
from __future__ import annotations

import math
import sqlite3

import numpy as np
import pandas as pd
from scipy.stats import mannwhitneyu

DB = "data/monitor.db"
TWO_YEAR_START = 1725148800

LOOKBACK = 15
THRESHOLD = -0.05
RECOVERY = 0.01
VOL_WIN = 240
Z_THRESH = -3.0
HOLD = 12 * 60
COOLDOWN = LOOKBACK

FUND_POS = 0.0001
FUND_NEG = -0.0001


def dedup(idx: np.ndarray, cooldown: int) -> np.ndarray:
    if len(idx) == 0:
        return idx
    keep = [int(idx[0])]
    last = int(idx[0])
    for i in idx[1:]:
        if int(i) - last >= cooldown:
            keep.append(int(i))
            last = int(i)
    return np.array(keep, dtype=np.int64)


def load_funding(con):
    cur = con.cursor()
    cur.execute("SELECT symbol, funding_time, funding FROM funding_hist ORDER BY symbol, funding_time")
    out = {}
    for sym, ft, fv in cur.fetchall():
        out.setdefault(sym, ([], []))
        out[sym][0].append(ft)
        out[sym][1].append(fv)
    return {s: (np.array(t, dtype=np.int64), np.array(f, dtype=float)) for s, (t, f) in out.items()}


def prev_funding(fund, ts):
    t, f = fund
    idx = np.searchsorted(t, ts, side="right") - 1
    if idx < 0:
        return None
    return float(f[idx])


def scan_pins(df, zscore):
    ot = df["open_time"].to_numpy(dtype=np.int64)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    n = len(close)
    base = np.full(n, np.nan)
    base[LOOKBACK:] = close[:-LOOKBACK]
    tip_ret = low / base - 1.0
    rec = close / low - 1.0
    if zscore:
        log_ret = np.log(close[1:] / close[:-1])
        vol = np.full(n, np.nan)
        vol[1:] = pd.Series(log_ret).rolling(VOL_WIN).std().to_numpy(dtype=float)
        z = tip_ret / (vol * math.sqrt(LOOKBACK))
        mask = (z <= Z_THRESH) & (rec >= RECOVERY)
    else:
        mask = (tip_ret <= THRESHOLD) & (rec >= RECOVERY)
    idx = dedup(np.where(mask)[0], COOLDOWN)
    pins = []
    for i in idx:
        if i + HOLD >= n:
            continue
        r = close[i + HOLD] / close[i] - 1.0
        pins.append({"i": int(i), "ts": int(ot[i]), "ret": float(r)})
    return pins


def summarize(name, rets):
    if not rets:
        return f"{name}: n=0"
    a = np.array(rets)
    return (f"{name}: n={len(a)}  均值={a.mean()*100:+.2f}%  "
            f"中位={np.median(a)*100:+.2f}%  胜率={(a>0).mean()*100:.1f}%")


def run(zscore, label):
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("SELECT symbol, MIN(open_time) FROM klines_1m GROUP BY symbol HAVING MIN(open_time) <= ?",
                (TWO_YEAR_START,))
    symbols = sorted(r[0] for r in cur.fetchall())
    fund = load_funding(con)

    g = {"pos": [], "neu": [], "neg": []}
    ctl = []  # 对照组：同币同时段随机时点 12h 收益
    missing = 0

    for sym in symbols:
        df = pd.read_sql_query(
            "SELECT open_time, high, low, close FROM klines_1m WHERE symbol=? ORDER BY open_time",
            con, params=(sym,),
        )
        if len(df) < VOL_WIN + LOOKBACK + 1:
            continue
        close = df["close"].to_numpy(dtype=float)
        n = len(close)
        pins = scan_pins(df, zscore)
        f = fund.get(sym)
        if f is None:
            continue
        for p in pins:
            i, ts, r = p["i"], p["ts"], p["ret"]
            fv = prev_funding(f, ts)
            if fv is None:
                missing += 1
                continue
            key = "pos" if fv > FUND_POS else ("neg" if fv < FUND_NEG else "neu")
            g[key].append(r)
            # 对照组：针前 24h 同一 bar 位置的 12h 收益
            j = i - 1440
            if j > 0 and j + HOLD < n:
                ctl.append(close[j + HOLD] / close[j] - 1.0)

    con.close()

    print(f"\n===== {label} =====")
    print(summarize("针后12h 正组(funding>0.01%)", g["pos"]))
    print(summarize("针后12h 中性组", g["neu"]))
    print(summarize("针后12h 负组(funding<-0.01%)", g["neg"]))
    print(summarize("对照组(随机时点12h)", ctl))
    print(f"缺 funding 针数：{missing}")

    # 成本敏感性：正组扣单笔总成本（taker 0.045%*2≈0.09% + 滑点）后还剩多少
    print("--- 成本敏感性（正组扣单笔总成本后）---")
    for cost in [0.0005, 0.0010, 0.0015]:
        a = np.array(g["pos"]) - cost
        print(f"  扣 {cost*100:.2f}%: 中位={np.median(a)*100:+.2f}% 均值={a.mean()*100:+.2f}% "
              f"胜率={(a>0).mean()*100:.1f}%")

    # 检验：正组 vs 中性组；负组 vs 中性组；针(全体) vs 对照
    def mw(a, b):
        if len(a) < 5 or len(b) < 5:
            return None
        u, p = mannwhitneyu(a, b, alternative="two-sided")
        return p

    allpin = g["pos"] + g["neu"] + g["neg"]
    p_pos_neu = mw(g["pos"], g["neu"])
    p_neg_neu = mw(g["neg"], g["neu"])
    p_pin_ctl = mw(allpin, ctl)
    print(f"正组 vs 中性 Mann-Whitney p = {p_pos_neu:.4f}" if p_pos_neu else "正组 vs 中性 n<5")
    print(f"负组 vs 中性 Mann-Whitney p = {p_neg_neu:.4f}" if p_neg_neu else "负组 vs 中性 n<5")
    print(f"针(全体) vs 对照 Mann-Whitney p = {p_pin_ctl:.4f}" if p_pin_ctl else "针 vs 对照 n<5")


if __name__ == "__main__":
    run(False, "固定阈值版（15min 跌超 -5%）")
    run(True, "z-score 版（z <= -3 波动率自适应）")
