# -*- coding: utf-8 -*-
"""对比：静态 no_spot 名单 vs point-in-time 现货判断（walk-forward 收益差异）。

静态版 = 旧逻辑（base 在当前现货集合里就判「有现货」，忽略回测时点）→ 前视；
point-in-time 版 = 新逻辑（has_spot_at 按 t 时刻判断现货是否已上线）。

用 monkey-patch 临时替换 data.has_spot_at，跑两次 walk-forward，量化前视修正
对收益的影响方向与量级。
"""
from __future__ import annotations

import datetime
import sys
import time
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import engine.data as data
from engine.strategies.pin import PinStrategy
from engine.backtest import run_backtest_walkforward

START = int(datetime.datetime(2024, 9, 1, tzinfo=datetime.timezone.utc).timestamp())
END = int(datetime.datetime(2026, 9, 7, tzinfo=datetime.timezone.utc).timestamp())


def _static_has_spot(symbol: str, t: int, spot_od: dict | None = None) -> bool:
    """旧静态逻辑：忽略 t，base 在当前在线现货集合里就算「有现货」（前视）。"""
    if spot_od is None:
        spot_od = data._load_spot_onboard()
    if not spot_od:
        return True
    base = data._normalize_base(data._base_of(symbol), set(spot_od.keys()))
    return base in spot_od


def _fmt(tag: str, r) -> str:
    return (f"[{tag}] 收益 {r.total_return:+.2%} | 交易 {r.n_trades} | "
            f"胜率 {r.win_rate:.1%} | 均笔 {r.avg_pnl:+.2%} | 回撤 {r.max_dd:.2%} | Sharpe {r.sharpe:.2f}")


def main() -> None:
    t0 = time.time()

    # 1. 静态版（前视）
    _orig = data.has_spot_at
    data.has_spot_at = _static_has_spot
    r_static = run_backtest_walkforward(PinStrategy(), START, END,
                                        pool_window_days=90, step_days=30)
    data.has_spot_at = _orig
    print(_fmt("静态 no_spot(前视)", r_static))

    # 2. point-in-time 版（无前视）
    r_pit = run_backtest_walkforward(PinStrategy(), START, END,
                                     pool_window_days=90, step_days=30)
    print(_fmt("point-in-time(无前视)", r_pit))

    print(f"\n耗时 {time.time() - t0:.1f}s")
    print(f"\n修正影响：收益 {r_static.total_return:+.2%} -> {r_pit.total_return:+.2%} "
          f"（差 {r_pit.total_return - r_static.total_return:+.2%}），"
          f"交易 {r_static.n_trades} -> {r_pit.n_trades}")


if __name__ == "__main__":
    main()
