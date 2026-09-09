# -*- coding: utf-8 -*-
"""僵尸币压力测试 —— 量化幸存者偏差的下界。

幸存者偏差的伤害路径：pin 买了某币 → 该币归零/下架 → 亏损。数据里只有「活口」，
归零/下架币没数据，回测天然高估。本测试用「仍在线的归零币近亲」（僵尸币 = 从历史
最高点跌 >70% 的币）做代理，回答一个关键问题：

    pin 的 funding 过滤，到底挡没挡住「归零路径」？

判据：
- 若僵尸币上「funding 正的针」占比极低（崩盘时 funding 已转负，被过滤掉），且剩余
  的正针买了也亏（是真相不是错杀）→ pin 天然避开归零币，幸存者偏差有界且小；
- 若僵尸币上「funding 正的针」又多、买了又亏 → funding 过滤挡不住归零路径，
  下架币只会更惨，幸存者偏差是真实风险，需加硬过滤或买 Kaiko 数据。

度量纪律：用「持有收益」（12h 后 close 相对开仓 close），不用「是否回到某水平」
（后者对伪针虚高，是之前踩过的坑）。
"""
from __future__ import annotations

import datetime
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
import config
from engine import data
from engine.strategies.pin import PinStrategy

START = int(datetime.datetime(2024, 9, 1, tzinfo=datetime.timezone.utc).timestamp())
END = int(datetime.datetime(2026, 9, 7, tzinfo=datetime.timezone.utc).timestamp())


def _peak_and_latest() -> dict[str, tuple[float, float, int]]:
    """每个 symbol 的 (peak_high, latest_close, 1m 数据起点时间)，仅回测窗内 [START, END]。"""
    con = sqlite3.connect(config.DB_PATH)
    try:
        df_peak = pd.read_sql_query(
            "SELECT symbol, MAX(high) AS peak, MIN(open_time) AS first_ot "
            "FROM klines_1m WHERE open_time>=? AND open_time<=? GROUP BY symbol",
            con, params=(START, END),
        )
        df_last = pd.read_sql_query(
            "SELECT k1.symbol, k1.close FROM klines_1m k1 "
            "INNER JOIN (SELECT symbol, MAX(open_time) mx FROM klines_1m GROUP BY symbol) k2 "
            "ON k1.symbol=k2.symbol AND k1.open_time=k2.mx", con,
        )
    finally:
        con.close()
    pk = df_peak.set_index("symbol")["peak"].to_dict()
    fo = df_peak.set_index("symbol")["first_ot"].to_dict()
    lc = df_last.set_index("symbol")["close"].to_dict()
    return {s: (float(pk.get(s, 0) or 0), float(lc.get(s, 0) or 0), int(fo.get(s, 0) or 0))
            for s in pk}


def _find_pin_events(df: pd.DataFrame, s: PinStrategy) -> list[tuple[int, float]]:
    """向量化找 pin 触发点，返回 [(t, entry_close), ...]，与 _backtest_pin 严格同源。"""
    close = df["close"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    ot = df["open_time"].to_numpy(dtype=np.int64)
    lb = s.lookback
    if len(close) <= lb:
        return []
    base = close[:-lb]
    price_series = low[lb:] if s.use_low else close[lb:]
    low_ret = price_series / np.where(base > 0, base, np.nan) - 1.0
    rec = close[lb:] / np.where(low[lb:] > 0, low[lb:], np.nan) - 1.0
    mask = (low_ret <= s.threshold) & (rec >= s.recovery)
    out = []
    for j in np.nonzero(mask)[0]:
        i = j + lb
        out.append((int(ot[i]), float(close[i])))
    return out


def _summary(tag: str, rets: list[float]) -> str:
    if not rets:
        return f"{tag}: 无样本"
    a = np.array(rets, dtype=float)
    win = float((a > 0).mean())
    return f"{tag}: n={len(a)} 均值{a.mean():+.2%} 中位{np.median(a):+.2%} 胜率{win:.1%}"


def main() -> None:
    s = PinStrategy()
    info = _peak_and_latest()
    black = data._load_tradfi_blacklist()

    # 1. 分档（按「历史最高 → 最新价」跌幅，剔除 TradFi）
    bins: dict[str, list[str]] = {"跌>90%": [], "跌70~90%": [], "跌50~70%": [], "跌<50%": []}
    for sym, (peak, last, _fo) in sorted(info.items()):
        if sym in black or peak <= 0 or last <= 0:
            continue
        dd = last / peak - 1.0
        if dd <= -0.90:
            bins["跌>90%"].append(sym)
        elif dd <= -0.70:
            bins["跌70~90%"].append(sym)
        elif dd <= -0.50:
            bins["跌50~70%"].append(sym)
        else:
            bins["跌<50%"].append(sym)

    zombies = bins["跌>90%"] + bins["跌70~90%"]
    print("=== 僵尸币识别（从历史最高点跌幅，剔除 TradFi）===")
    for k in ["跌>90%", "跌70~90%", "跌50~70%", "跌<50%"]:
        print(f"  {k}: {len(bins[k])} 个")
    print(f"\n僵尸币（跌>70%）共 {len(zombies)} 个: {zombies}")

    if not zombies:
        print("无僵尸币，幸存者偏差下界 = 0（数据里没有接近归零的币）")
        return

    # 2. 僵尸币上跑 pin 事件 + funding 分组
    funding_map = data.load_funding(zombies)
    data_map = data.load_klines(zombies, interval="1m", start=START - 7200, end=END,
                                columns=["open_time", "low", "close"])
    pos_rets: list[float] = []   # funding 正（会开仓）的 12h 持有收益
    neu_rets: list[float] = []   # funding 中性
    neg_rets: list[float] = []   # funding 负
    n_trigger = n_pos = n_neu = n_neg = 0

    for sym in zombies:
        df = data_map.get(sym)
        if df is None or df.empty:
            continue
        events = _find_pin_events(df, s)
        ot = df["open_time"].to_numpy(dtype=np.int64)
        close = df["close"].to_numpy(dtype=float)
        for t, entry in events:
            n_trigger += 1
            fv = data.prev_funding(funding_map, sym, t)
            # 12h 持有收益
            exit_pos = int(np.searchsorted(ot, t + s.hold_hours * 3600, side="right")) - 1
            exit_pos = min(exit_pos, len(close) - 1)
            if exit_pos < 0:
                continue
            ret = close[exit_pos] / entry - 1.0
            if fv is None:
                continue  # 无 funding 数据，跳过分组
            if fv >= s.funding_min:
                n_pos += 1
                pos_rets.append(ret)
            elif fv <= -s.funding_min:
                n_neg += 1
                neg_rets.append(ret)
            else:
                n_neu += 1
                neu_rets.append(ret)

    print(f"\n=== 僵尸币 pin 事件 + funding 分组 ===")
    print(f"  针触发总数: {n_trigger}")
    print(f"  funding 正(会开仓): {n_pos} ({n_pos / max(n_trigger, 1):.1%})")
    print(f"  funding 中性(被挡): {n_neu} ({n_neu / max(n_trigger, 1):.1%})")
    print(f"  funding 负(被挡): {n_neg} ({n_neg / max(n_trigger, 1):.1%})")
    print()
    print("  12h 持有收益（未扣成本）:")
    print("   " + _summary("funding 正组", pos_rets))
    print("   " + _summary("funding 中性组", neu_rets))
    print("   " + _summary("funding 负组", neg_rets))

    # 3. 结论判据
    print("\n=== 判据 ===")
    pos_ratio = n_pos / max(n_trigger, 1)
    pos_med = float(np.median(pos_rets)) if pos_rets else 0.0
    if pos_ratio < 0.15:
        print(f"funding 正针占比仅 {pos_ratio:.1%}（<15%）：崩盘时 funding 已转负，"
              f"pin 的 funding 过滤天然挡掉归零币 → 幸存者偏差有界且小。")
    elif pos_med < 0:
        print(f"funding 正针占比 {pos_ratio:.1%} 不算低，但正针中位收益 {pos_med:+.2%} 为负："
              f"剩余的正针是「真相」不是「错杀」→ 买了也亏，偏差仍可控。")
    else:
        print(f"⚠️ funding 正针占比 {pos_ratio:.1%} 且中位收益 {pos_med:+.2%} 为正："
              f"pin 在归零币近亲上仍会开仓且看似有 edge，这是幸存者偏差的最坏信号，"
              f"需加硬过滤或买 Kaiko 数据。")


if __name__ == "__main__":
    main()
