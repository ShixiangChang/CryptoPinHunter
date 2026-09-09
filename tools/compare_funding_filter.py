# -*- coding: utf-8 -*-
"""对比 pin 回测：funding 过滤开 vs 关（只变这一个变量，验证过滤的实际效果）。"""
from __future__ import annotations

import datetime
import sys
import time
from pathlib import Path as _Path

_root = _Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from engine.strategies.pin import PinStrategy
from engine.backtest import run_backtest


def main() -> None:
    start = int(datetime.datetime(2024, 9, 1, tzinfo=datetime.timezone.utc).timestamp())
    end = int(datetime.datetime(2026, 9, 7, tzinfo=datetime.timezone.utc).timestamp())

    # 无过滤基线（已跑出，硬编码对比）：收益 +204.32% | 交易 1055 | 胜率 55.0% | 均笔 +1.64% | 回撤 -33.78%
    off = dict(ret=2.0432, n=1055, win=0.550, avg=0.0164, dd=-0.3378)

    t0 = time.time()
    s_on = PinStrategy()
    r_on = run_backtest(s_on, start, end)
    print(f"[有 funding 过滤] 收益 {r_on.total_return:+.2%} | 交易 {r_on.n_trades} | "
          f"胜率 {r_on.win_rate:.1%} | 均笔 {r_on.avg_pnl:+.2%} | 回撤 {r_on.max_dd:.2%}")

    print(f"耗时 {time.time() - t0:.1f}s")
    print(f"\n过滤效果：交易数 {off['n']} -> {r_on.n_trades}，"
          f"胜率 {off['win']:.1%} -> {r_on.win_rate:.1%}，"
          f"均笔 {off['avg']:+.2%} -> {r_on.avg_pnl:+.2%}，"
          f"回撤 {off['dd']:.2%} -> {r_on.max_dd:.2%}")


if __name__ == "__main__":
    main()
