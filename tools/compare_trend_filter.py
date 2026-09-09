# -*- coding: utf-8 -*-
"""对比 pin 回测：趋势过滤 开 vs 关（只变这一个变量，验证趋势过滤砍「接飞刀」的效果）。

口径 = walk-forward 无前视定池（funding 过滤 + point-in-time 现货已内置在 PinStrategy 默认值）。
两边只有 require_trend_filter 一个变量不同，其余完全一致。
"""
from __future__ import annotations

import datetime
import sys
import time
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from engine.strategies.pin import PinStrategy
from engine.backtest import run_backtest_walkforward

# 起点设在 2024-12-01：1h 数据从 2024-08-29 起，90 天滚动高点要满 2160 根才有值
# （约 2024-11-27 后），从 12-01 起两边趋势信号都就绪，纯隔离「趋势过滤」这一个变量。
START = int(datetime.datetime(2024, 12, 1, tzinfo=datetime.timezone.utc).timestamp())
END = int(datetime.datetime(2026, 9, 7, tzinfo=datetime.timezone.utc).timestamp())


def _fmt(tag: str, r) -> str:
    return (f"[{tag}] 收益 {r.total_return:+.2%} | 交易 {r.n_trades} | "
            f"胜率 {r.win_rate:.1%} | 均笔 {r.avg_pnl:+.2%} | 回撤 {r.max_dd:.2%} | Sharpe {r.sharpe:.2f}")


def main() -> None:
    t0 = time.time()

    # 基线：趋势过滤关
    s_off = PinStrategy()
    s_off.require_trend_filter = False
    r_off = run_backtest_walkforward(s_off, START, END, pool_window_days=90, step_days=30)
    print(_fmt("趋势过滤关", r_off))

    # 实验：趋势过滤开
    s_on = PinStrategy()
    r_on = run_backtest_walkforward(s_on, START, END, pool_window_days=90, step_days=30)
    print(_fmt("趋势过滤开", r_on))

    print(f"\n耗时 {time.time() - t0:.1f}s")
    print(f"\n过滤效果：交易 {r_off.n_trades} -> {r_on.n_trades}，"
          f"胜率 {r_off.win_rate:.1%} -> {r_on.win_rate:.1%}，"
          f"均笔 {r_off.avg_pnl:+.2%} -> {r_on.avg_pnl:+.2%}，"
          f"回撤 {r_off.max_dd:.2%} -> {r_on.max_dd:.2%}")


if __name__ == "__main__":
    main()
