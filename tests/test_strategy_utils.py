"""Strategy 基类公共工具函数单元测试（ATR / 时间切片）。"""
from __future__ import annotations

import pandas as pd
import pytest

from engine.strategy import Strategy


def _df(rows: list[tuple[int, float]]) -> pd.DataFrame:
    ts = [r[0] for r in rows]
    px = [r[1] for r in rows]
    return pd.DataFrame({
        "open_time": ts,
        "open": px, "high": px, "low": px, "close": px,
        "volume": [1.0] * len(px),
    })


class TestAtr:
    def test_atr_of_steady_drift(self):
        """价格每根 +1 单调上涨：TR = 1/根，atr(hours) = 1.0。"""
        rows = [(1_700_000_000 + i * 3600, 100.0 + i) for i in range(30)]
        v = Strategy.atr(_df(rows), hours=24)
        assert v == pytest.approx(1.0)

    def test_atr_none_when_not_enough_data(self):
        """不足 hours+1 根 → None。"""
        rows = [(1_700_000_000 + i * 3600, 100.0) for i in range(10)]
        assert Strategy.atr(_df(rows), hours=24) is None


class TestSliceUpto:
    def test_slices_future_bars(self):
        """返回的切片不含 now 之后的行（无前视）。"""
        now = 1_700_000_000
        rows = [(now - 3 * 3600, 100.0), (now - 2 * 3600, 101.0),
                (now - 1 * 3600, 102.0), (now + 1 * 3600, 103.0),
                (now + 2 * 3600, 104.0)]
        out = Strategy.slice_upto({"A": _df(rows)}, now)
        assert len(out["A"]) == 3
        assert out["A"]["open_time"].max() <= now

    def test_empty_when_all_future(self):
        """全部行都在 now 之后 → 该币被剔除。"""
        now = 1_700_000_000
        rows = [(now + 3600, 100.0), (now + 7200, 101.0)]
        out = Strategy.slice_upto({"A": _df(rows)}, now)
        assert "A" not in out
