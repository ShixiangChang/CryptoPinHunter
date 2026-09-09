# -*- coding: utf-8 -*-
"""Beta 猎犬：时序动量（TSMOM，双向趋势跟随）。

纯规则：每币独立看「价格 vs 720h(30 天)均线」——在均线上方做多、下方做空，
涨跌都吃。空头腿权重打折（做空成本高）。净敞口随市场方向自动偏斜：
全市场在均线上方 → 净多 100%；全在下方 → 净空 100%。
"""
from __future__ import annotations

import pandas as pd

from ..strategy import Strategy
from .. import data


class BetaStrategy(Strategy):
    name = "beta"
    interval = "1h"
    hold_hours = 96
    decision_interval_h = 96   # 每 96h 重评一次趋势（与持有期一致）
    stop_atr_mult = 3.0        # 多头止损 ATR 倍数
    stop_atr_mult_short = 2.0  # 空头止损 ATR 倍数（收紧）
    atr_hours = 24
    trend_ma_hours = 720       # 30 天均线
    short_scale = 0.5          # 空头权重 ×0.5

    @classmethod
    def default_universe(cls) -> list[str]:
        # 头部大币：趋势跟随的 beta 是做多大盘，不是做多山寨。只取流动性好的主流币。
        large = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
                 "ADAUSDT", "DOGEUSDT", "LINKUSDT", "LTCUSDT", "TRXUSDT",
                 "AVAXUSDT", "DOTUSDT", "UNIUSDT", "NEARUSDT", "ATOMUSDT",
                 "ETCUSDT", "FILUSDT", "ARBUSDT"]
        avail = set(data.available_symbols("1h", min_rows=17500))
        return [s for s in large if s in avail]

    def generate_signals(self, data: dict[str, pd.DataFrame], now: int) -> list[dict]:
        signals = []
        for sym in self.universe:
            df = data.get(sym)
            if df is None:
                continue
            s = df[df["open_time"] <= now]
            if len(s) < self.trend_ma_hours + 1:
                continue
            close = s["close"]
            ma = float(close.rolling(self.trend_ma_hours).mean().iloc[-1])
            if pd.isna(ma) or ma <= 0:
                continue
            entry = float(close.iloc[-1])
            atr = self.atr(s, self.atr_hours)
            if entry > ma:
                side = 1
                stop = entry - self.stop_atr_mult * atr if atr else None
                w = 1.0
            else:
                side = -1
                stop = entry + self.stop_atr_mult_short * atr if atr else None
                w = self.short_scale
            signals.append({"symbol": sym, "side": side, "weight": w,
                            "entry": entry, "stop": stop})
        return signals
