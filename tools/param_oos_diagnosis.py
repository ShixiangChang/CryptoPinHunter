# -*- coding: utf-8 -*-
"""参数样本外诊断：当前参数在 train / test 两段 walk-forward 上表现是否一致。

train = 2024-12-01 ~ 2025-09-01，test = 2025-09-01 ~ 2026-09-07，同一组当前参数。
若 train 正、test 崩 = 参数过拟合强信号。
"""
from __future__ import annotations

import datetime
import sys
import time
from pathlib import Path as _Path

_root = _Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from engine.strategies.pin import PinStrategy
from engine.backtest import run_backtest_walkforward

TRAIN_START = int(datetime.datetime(2024, 12, 1, tzinfo=datetime.timezone.utc).timestamp())
SPLIT = int(datetime.datetime(2025, 9, 1, tzinfo=datetime.timezone.utc).timestamp())
TEST_END = int(datetime.datetime(2026, 9, 7, tzinfo=datetime.timezone.utc).timestamp())


def _fmt(tag: str, r) -> str:
    return (f"[{tag}] 收益 {r.total_return:+.2%} | 交易 {r.n_trades} | "
            f"胜率 {r.win_rate:.1%} | 回撤 {r.max_dd:.2%} | Sharpe {r.sharpe:.2f}")


def main() -> None:
    t0 = time.time()

    r_train = run_backtest_walkforward(PinStrategy(), TRAIN_START, SPLIT,
                                       pool_window_days=90, step_days=30)
    print(_fmt("train 2024-12~2025-09", r_train))

    r_test = run_backtest_walkforward(PinStrategy(), SPLIT, TEST_END,
                                      pool_window_days=90, step_days=30)
    print(_fmt("test 2025-09~2026-09", r_test))

    print(f"\n耗时 {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
