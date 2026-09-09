# -*- coding: utf-8 -*-
"""Momentum 策略：横截面动量（市场中性多空）。

纯规则：每币算「30 天前 → 24h 前」的对数收益（跳过最近 24h 短期噪声），
排序后 top_n 做多、bottom_n 做空，对冲掉市场 beta，赚纯截面价差。
空头腿权重打折（做空成本高 + 逼空风险），净敞口偏多。
"""
from __future__ import annotations

import math

import pandas as pd

from ..strategy import Strategy
from .. import data


class MomentumStrategy(Strategy):
    name = "momentum"
    interval = "1h"
    hold_hours = 96
    decision_interval_h = 96   # 每 96h 重选一次（与持有期一致，避免每日换手）
    stop_atr_mult = 3.0        # 多头止损 ATR 倍数
    stop_atr_mult_short = 2.0  # 空头止损 ATR 倍数（收紧）
    atr_hours = 24
    lookback_h = 720   # 30 天
    skip_h = 24        # 跳过最近 24h（压短期反转噪声）
    top_n = 20
    bottom_n = 20
    short_scale = 0.5  # 空头权重 ×0.5

    @classmethod
    def default_universe(cls) -> list[str]:
        # 1h K 线有完整 2 年数据的币
        return data.available_symbols("1h", min_rows=17500)

    def generate_signals(self, data: dict[str, pd.DataFrame], now: int) -> list[dict]:
        scored: list[tuple[float, str, float]] = []  # (动量, sym, entry)
        for sym in self.universe:
            df = data.get(sym)
            if df is None:
                continue
            s = df[df["open_time"] <= now]
            need = self.lookback_h + self.skip_h + 1
            if len(s) < need:
                continue
            close = s["close"].to_numpy(dtype=float)
            p_far = close[-need]              # 30 天前（再往前挪 skip 根）
            p_near = close[-self.skip_h - 1]  # 24h 前
            if p_far <= 0 or p_near <= 0:
                continue
            mom = math.log(p_near / p_far)
            scored.append((mom, sym, float(close[-1])))

        scored.sort(key=lambda x: x[0], reverse=True)
        longs = scored[: self.top_n]
        long_syms = {x[1] for x in longs}
        shorts = [x for x in scored[-self.bottom_n:] if x[1] not in long_syms]

        signals = []
        for _mom, sym, entry in longs:
            s = data[sym][data[sym]["open_time"] <= now]
            atr = self.atr(s, self.atr_hours)
            stop = entry - self.stop_atr_mult * atr if atr else None
            signals.append({"symbol": sym, "side": 1, "weight": 1.0 / self.top_n,
                            "entry": entry, "stop": stop})
        for _mom, sym, entry in shorts:
            s = data[sym][data[sym]["open_time"] <= now]
            atr = self.atr(s, self.atr_hours)
            stop = entry + self.stop_atr_mult_short * atr if atr else None
            signals.append({"symbol": sym, "side": -1,
                            "weight": self.short_scale / max(len(shorts), 1),
                            "entry": entry, "stop": stop})
        return signals
