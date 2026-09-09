# -*- coding: utf-8 -*-
"""Strategy 基类：一只策略。

核心契约只有一个方法 `generate_signals(data, now)`：
- 回测引擎喂「截至历史决策点 t 的数据」，now = t；
- 纸面调度器喂「截至当前的数据」，now = 当前时刻。
两者调的是同一个方法、同一套信号逻辑 —— 回测和实盘是同一只狗。

信号结构（list[dict]，每行一笔目标持仓）：
    {"symbol": str, "side": +1 多 / -1 空, "weight": 占总资金比例(0~1),
     "entry": 入场参考价(可 None=市价), "stop": 止损价(可 None=不设)}
"""
from __future__ import annotations

import pandas as pd


class Strategy:
    name = ""                 # 策略名（落库/看板用）
    interval = "1h"           # 数据粒度："1h" / "1m"
    hold_hours = 96           # 固定持有期（小时），到期重算
    decision_interval_h = 1   # 决策步进（小时）：回测/纸面每隔多久重算一次信号
    stop_atr_mult = 3.0       # 止损 = 入场 ± ATR 倍数（0 = 不设止损）
    atr_hours = 24            # ATR 计算窗口（小时）

    def __init__(self, universe: list[str] | None = None):
        self.universe: list[str] = universe if universe is not None else self.default_universe()

    @classmethod
    def default_universe(cls) -> list[str]:
        """默认吃哪些币。子类覆盖（可依赖 data.available_symbols 动态确定）。"""
        return []

    def generate_signals(self, data: dict[str, pd.DataFrame], now: int) -> list[dict]:
        """给定截至 now 的 K 线数据，返回目标持仓信号列表。

        data: {symbol: DataFrame(open_time, open, high, low, close, volume)}
        now: 决策时刻 unix 秒。
        """
        raise NotImplementedError

    # ---- 公共工具（子类复用） ----
    @staticmethod
    def slice_upto(data: dict[str, pd.DataFrame], now: int) -> dict[str, pd.DataFrame]:
        """把每个币的数据切片到 <= now（策略内部按「当时点」计算，避免未来信息）。"""
        out: dict[str, pd.DataFrame] = {}
        for sym, df in data.items():
            s = df[df["open_time"] <= now]
            if not s.empty:
                out[sym] = s
        return out

    @staticmethod
    def atr(df: pd.DataFrame, hours: int) -> float | None:
        """1h K 线上的 ATR（真波幅均值）。df 需按 open_time 升序。"""
        if len(df) < hours + 1:
            return None
        c = df["close"].to_numpy(dtype=float)
        h = df["high"].to_numpy(dtype=float)
        l = df["low"].to_numpy(dtype=float)
        pc = c[:-1]
        tr = pd.concat([
            pd.Series(h[1:] - l[1:]),
            pd.Series((h[1:] - pc).__abs__()),
            pd.Series((l[1:] - pc).__abs__()),
        ], axis=1).max(axis=1)
        v = tr.rolling(hours).mean().iloc[-1]
        return float(v) if pd.notna(v) and v > 0 else None
