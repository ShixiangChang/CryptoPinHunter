"""Pin 策略触发逻辑单元测试。

不依赖币安 API 与真实数据库：
- K 线经 generate_signals 的 data 参数传入（策略内部 `data.get(sym)` 即此 dict）；
- 数据库读取层（load_funding / load_trend_highs）mock；
- prev_funding / trend_dd_at 两个无前视纯函数走真实实现。

测的是「插针判定 + funding/趋势过滤 + 权重」这一核心决策逻辑。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from engine.strategies.pin import PinStrategy

SYM = "TESTUSDT"
BASE = 100.0          # 插针前基准价
NOW = 1_700_000_000   # 任意决策时刻（unix 秒）


def _kline(lows: list[float], closes: list[float]) -> pd.DataFrame:
    """按给出的每根 low/close 构造 1m K 线。末根 open_time = NOW。"""
    n = len(lows)
    assert len(closes) == n
    ts = [NOW - (n - i) * 60 for i in range(n)]
    return pd.DataFrame({
        "open_time": ts,
        "open": closes,
        "high": [max(o, c, low_v) for o, c, low_v in zip(closes, closes, lows)],
        "low": lows,
        "close": closes,
        "volume": [1000.0] * n,
    })


def _make_strategy(monkeypatch, funding_rate: float, trend_high: float,
                   **overrides) -> PinStrategy:
    """组装 PinStrategy：mock 数据库 funding/趋势读取层，纯函数走真实现。

    overrides 里是 PinStrategy 类属性（如 require_funding_positive=False），
    构造后覆盖（该类参数是类级常量，非 __init__ 参数）。
    """
    from engine.strategies import pin as pin_mod

    strat = PinStrategy(universe=[SYM])
    for k, v in overrides.items():
        assert hasattr(strat, k), f"PinStrategy 无属性 {k}"
        setattr(strat, k, v)

    # load_funding -> 每 8h 一条结算记录，覆盖 NOW 之前
    ft = np.array([NOW - 8 * 3600 * (i + 1) for i in reversed(range(5))], dtype=np.int64)
    fr = np.array([funding_rate] * 5, dtype=float)

    def fake_load_funding(symbols):
        return {s: (ft, fr) for s in symbols}

    monkeypatch.setattr(pin_mod, "load_funding", fake_load_funding)

    # load_trend_highs -> 90 天 1h 序列，滚动高点恒为 trend_high
    ot = np.arange(NOW - 90 * 24 * 3600, NOW + 3600, 3600, dtype=np.int64)
    roll = np.full(ot.shape, float(trend_high))

    def fake_load_trend_highs(symbols, start, end, lookback_h):
        return {s: (ot, roll) for s in symbols}

    monkeypatch.setattr(pin_mod, "load_trend_highs", fake_load_trend_highs)
    return strat


# 通用触发场景：前 61 根平稳 100，末根深针 low=94(-6%)、close=97(反弹 3.2%)
def _wick_df() -> pd.DataFrame:
    return _kline([BASE] * 61 + [94.0], [BASE] * 61 + [97.0])


class TestPinTrigger:
    def test_fires_on_valid_wick(self, monkeypatch):
        """深针 + 承接 + funding 正 + 趋势健康 → 触发，权重 = depth^2 * pos。"""
        strat = _make_strategy(monkeypatch, funding_rate=0.0005, trend_high=100.0)
        sigs = strat.generate_signals({SYM: _wick_df()}, NOW)
        assert len(sigs) == 1
        s = sigs[0]
        assert s["symbol"] == SYM and s["side"] == 1
        # depth = 0.06/0.05 = 1.2 → w = 1.2^2 * 0.05 = 0.072
        assert s["weight"] == pytest.approx(strat.pos * 1.2 ** 2)
        assert s["entry"] == pytest.approx(97.0)

    def test_no_trigger_on_flat_market(self, monkeypatch):
        """无插针（平稳价格）→ 不触发。"""
        strat = _make_strategy(monkeypatch, funding_rate=0.0005, trend_high=100.0)
        df = _kline([BASE] * 62, [BASE] * 62)
        assert strat.generate_signals({SYM: df}, NOW) == []

    def test_no_trigger_when_not_recovered(self, monkeypatch):
        """针深但未被接住（反弹 0.55% < 1%）→ 下跌中继，不触发。"""
        strat = _make_strategy(monkeypatch, funding_rate=0.0005, trend_high=100.0)
        df = _kline([BASE] * 61 + [90.0], [BASE] * 61 + [90.5])
        assert strat.generate_signals({SYM: df}, NOW) == []

    def test_no_trigger_on_negative_funding(self, monkeypatch):
        """funding 为负（信息驱动而非错杀）→ 不触发。"""
        strat = _make_strategy(monkeypatch, funding_rate=-0.0005, trend_high=100.0)
        assert strat.generate_signals({SYM: _wick_df()}, NOW) == []

    def test_no_trigger_on_deep_downtrend(self, monkeypatch):
        """距 90 天高点跌幅超阈值（公允值下降）→ 不触发。"""
        strat = _make_strategy(monkeypatch, funding_rate=0.0005, trend_high=150.0)
        # dd = 97/150-1 = -35.3% < -30%
        assert strat.generate_signals({SYM: _wick_df()}, NOW) == []

    def test_weight_capped_by_depth(self, monkeypatch):
        """极端深针触发且深度权重封顶（weight_cap * pos）。"""
        strat = _make_strategy(monkeypatch, funding_rate=0.0005, trend_high=100.0)
        df = _kline([BASE] * 61 + [70.0], [BASE] * 61 + [72.0])  # -30% 针
        sigs = strat.generate_signals({SYM: df}, NOW)
        assert len(sigs) == 1
        assert sigs[0]["weight"] == pytest.approx(3.0 * strat.pos)

    def test_funding_below_min_not_trigger(self, monkeypatch):
        """funding 低于下限（5e-05 < 1e-04）→ 不触发。"""
        strat = _make_strategy(monkeypatch, funding_rate=0.00005, trend_high=100.0)
        assert strat.generate_signals({SYM: _wick_df()}, NOW) == []

    def test_funding_filter_can_be_disabled(self, monkeypatch):
        """require_funding_positive=False 时，funding 负也触发（链路测试用开关）。"""
        strat = _make_strategy(monkeypatch, funding_rate=-0.0005, trend_high=100.0,
                               require_funding_positive=False)
        assert len(strat.generate_signals({SYM: _wick_df()}, NOW)) == 1

    def test_trend_filter_can_be_disabled(self, monkeypatch):
        """require_trend_filter=False 时，深跌趋势也触发（链路测试用开关）。"""
        strat = _make_strategy(monkeypatch, funding_rate=0.0005, trend_high=150.0,
                               require_trend_filter=False)
        assert len(strat.generate_signals({SYM: _wick_df()}, NOW)) == 1
