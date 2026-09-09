# -*- coding: utf-8 -*-
"""Pin 策略：插针抄底（liquidation-wick mean reversion）。

纯规则，两个条件同时满足才算「插针」：
1. 针尖深：low 相对 15 分钟前 close 跌超 threshold（用 low 捕捉针尖，close 会低估深度）。
2. 针被接住：close 相对 low 反弹 >= recovery（有承接盘，V 型反转而非下跌中继）。

设计要点（各条件均经无前视样本外验证）：
- 币池：流动性前 N + 有现货（回归锚 = 现货-合约套利 / 做市商再平衡）。
- funding 过滤：只在 funding 为正（杠杆多头拥挤）时抄底——多头拥挤下的插针由连环
  强平造成，属「错杀」；中性/负 funding 的插针多为信息驱动，弱回归。
- 趋势过滤：币价距 90 天高点跌幅超过阈值则跳过——深度下跌趋势中的插针是公允值
  下降（真相），回归无锚。
- 深度平方加权（跌越深下注越重，但单币权重封顶），防「跌越深 = 真死」的尾部风险。
- 持有 12h 到期平仓，无价格止损（见 `stop_buffer=0`）。tail risk 用「事前不碰」
  管理（币池 + 趋势过滤），而非止损——归零/插针瞬间止损单无法成交。
"""
from __future__ import annotations

import os

import pandas as pd

from ..strategy import Strategy
from .. import data
from ..data import load_funding, prev_funding, load_trend_highs, trend_dd_at


class PinStrategy(Strategy):
    name = "pin"
    interval = "1m"
    hold_hours = 12          # 720 分钟
    decision_interval_h = 1.0 / 60.0  # 每分钟决策一次
    lookback = 15            # 15 分钟窗口
    # 触发阈值支持环境变量覆盖（PIN_TH_OVERRIDE / PIN_RECOVERY_OVERRIDE）：
    # 默认 -0.05/+0.01 = 严格回测参数；实盘跑通测试时可临时放宽（如 -0.03/+0.005），
    # 让信号更快触发、验证「下单→成交→持仓→平仓」全链路。改环境变量无需改代码重打包。
    threshold = float(os.environ.get("PIN_TH_OVERRIDE", "-0.05"))
    use_low = True           # True=用 low 捕捉针尖 / False=用 close 跌幅（旧版）
    recovery = float(os.environ.get("PIN_RECOVERY_OVERRIDE", "0.01"))
    vol_win = 60             # 量能均线窗口（分钟）
    vol_mult = 0.0           # 放量倍数；0 = 关闭量能条件（实测有害）
    weight_exp = 2           # 深度平方加权
    weight_cap = 3.0         # 单币权重上限（× pos）；从 6 降到 3，深度封顶
    pos = 0.05               # 基础仓位
    stop_buffer = 0.0        # 止损宽度；0=禁用（止损实测有害，默认关）
    top_n = 50               # 币池：流动性前 50（待剔除 TradFi + 剔除无现货后重新校准 Calmar 拐点）
    window_days = 90         # 定池滚动窗口：近 90 天成交额（消除全周期前视 + 会换血）
    min_adv = None           # 流动性下限（USD/分钟均额）；None=不启用，靠 top_n 控制
    require_spot = True      # 剔除「仅合约无现货」（无现货盘=套利锚缺失，插针是真相归零不是错杀）
    require_funding_positive = True  # 只在 funding 正（多头拥挤）时抄底（样本外验证）
    funding_min = float(os.environ.get("PIN_FUNDING_MIN_OVERRIDE", "0.0001"))
    # ↑ funding 下限：> +0.01%（超币安基准）= 多头拥挤 = 可抄的错杀。
    # 环境变量 PIN_FUNDING_MIN_OVERRIDE 可放宽（如 0.0=只要 funding 非负就抄），
    # 用于中性市场下跑通链路测试；默认 0.01% 在正常市场几乎不会触发（该参数按需覆盖）。
    require_trend_filter = os.environ.get("PIN_REQUIRE_TREND_FILTER", "1") == "1"
    # ↑ 趋势过滤（公允值方向）：跌超阈值 = 趋势性下跌（真相）而非错杀。
    # 环境变量 PIN_REQUIRE_TREND_FILTER=0 可关闭（跑通链路测试用，正式实盘保留）。
    trend_lookback_h = 90 * 24    # 90 天（1h 根数）
    trend_max_dd = float(os.environ.get("PIN_TREND_MAX_DD", "-0.30"))

    @classmethod
    def default_universe(cls) -> list[str]:
        # 定池判据 = edge 本质（插针→回归，回归锚有多强）：滚动窗口成交额排序 + 剔除 TradFi
        #（underlyingType != COIN）+ 剔除无现货（套利锚缺失）。三处（回测/纸面/实盘）同源。
        return data.liquid_symbols(
            "1m", top_n=cls.top_n,
            window_days=cls.window_days,
            min_adv=cls.min_adv,
            require_spot=cls.require_spot,
        )

    def generate_signals(self, data: dict[str, pd.DataFrame], now: int) -> list[dict]:
        signals = []
        # funding 过滤：只在多头拥挤（funding 正）时抄底。加载一次，逐币查「针前最近 funding」。
        funding = load_funding(self.universe) if self.require_funding_positive else {}
        # 趋势过滤：90 天滚动高点（1h），判断触发时币价距高点跌幅（公允值方向）。
        trend_map = {}
        if self.require_trend_filter:
            trend_map = load_trend_highs(
                self.universe, now - (self.trend_lookback_h + 24) * 3600, now,
                self.trend_lookback_h,
            )
        for sym in self.universe:
            df = data.get(sym)
            if df is None:
                continue
            s = df[df["open_time"] <= now]
            need = max(self.lookback, self.vol_win) + 1
            if len(s) < need:
                continue
            close = s["close"].to_numpy(dtype=float)
            low = s["low"].to_numpy(dtype=float)
            base = close[-self.lookback - 1]
            if base <= 0 or low[-1] <= 0:
                continue
            # 条件1：针尖深（use_low=用 low 捕捉针尖）
            tip = low[-1] if self.use_low else close[-1]
            tip_ret = tip / base - 1.0
            # 条件2：针被接住（close 相对 low 反弹）
            rec = close[-1] / low[-1] - 1.0
            if not (tip_ret <= self.threshold and rec >= self.recovery):
                continue
            # 条件3：量能（默认关闭 vol_mult=0）
            if self.vol_mult > 0:
                volume = s["volume"].to_numpy(dtype=float)
                vol_mean = volume[-self.vol_win:].mean()
                if vol_mean <= 0 or volume[-1] / vol_mean < self.vol_mult:
                    continue
            # 条件4：funding 正（多头拥挤=错杀可抄；负/中性=信息驱动真相，弱回归，跳过）
            if self.require_funding_positive:
                fv = prev_funding(funding, sym, now)
                if fv is None or fv < self.funding_min:
                    continue
            # 条件5：趋势过滤（公允值方向）：距 90 天高点跌幅 >30% = 公允值下降 = 真相，跳过
            if self.require_trend_filter:
                dd = trend_dd_at(trend_map, sym, now, float(close[-1]))
                if dd is None or dd < self.trend_max_dd:
                    continue
            depth = abs(tip_ret) / abs(self.threshold)
            w = min(depth ** self.weight_exp, self.weight_cap) * self.pos
            # 无价格止损（止损实测有害）；tail risk 靠币池前 50 事前规避
            stop = None if self.stop_buffer <= 0 else float(low[-1] * (1.0 - self.stop_buffer))
            signals.append({"symbol": sym, "side": 1, "weight": w, "depth": depth,
                            "entry": float(close[-1]), "stop": stop})
        return signals
