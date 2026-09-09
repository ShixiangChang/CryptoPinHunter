# -*- coding: utf-8 -*-
"""walk-forward 无前视验证：pin + funding 过滤的真实 edge（消除定池前视）。

对比三组（同一时间区间，只变「定池是否前视」这一个维度）：
1. 【前视-静态池】：default_universe 用当前名单回测历史 = 前视，绝对值不可作收益结论，仅作对比。
2. 【无前视-walkforward】：滚动定池，t 只用 <=t 数据选池，逐窗拼接。这才是可信口径。

START 取 2024-12-01：给 90 天定池窗口留足数据（klines_1m 最早 2024-08-29）。
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
from engine.backtest import run_backtest, run_backtest_walkforward

START = int(datetime.datetime(2024, 12, 1, tzinfo=datetime.timezone.utc).timestamp())
END = int(datetime.datetime(2026, 9, 7, tzinfo=datetime.timezone.utc).timestamp())


def _fmt(tag: str, r) -> str:
    return (f"{tag} | 收益 {r.total_return:+.2%} | 交易 {r.n_trades} | "
            f"胜率 {r.win_rate:.1%} | 均笔 {r.avg_pnl:+.2%} | 回撤 {r.max_dd:.2%} | "
            f"Sharpe {r.sharpe:.2f}")


def main() -> None:
    t0 = time.time()

    # 1) 【前视】静态池（当前名单回测过去）
    s_static = PinStrategy()
    r_static = run_backtest(s_static, START, END)
    print(_fmt("[前视-静态池]", r_static))

    # 2) 【无前视】walk-forward 滚动定池
    s_wf = PinStrategy()
    r_wf = run_backtest_walkforward(s_wf, START, END, pool_window_days=90, step_days=30)
    print(_fmt("[无前视-walkforward]", r_wf))

    print(f"\n耗时 {time.time() - t0:.1f}s")
    print(f"\n定池前视的代价：静态池收益 {r_static.total_return:+.2%} → walk-forward {r_wf.total_return:+.2%}")


if __name__ == "__main__":
    main()
