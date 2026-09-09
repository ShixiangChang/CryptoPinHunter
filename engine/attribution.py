# -*- coding: utf-8 -*-
"""归因对账：已结算交易 → 命中率 / 期望值 / 淘汰判定。

替代旧 predictions 表的重复插入 + 手动结算。交易在平仓时就算好 pnl（回测引擎与
纸面调度器共用同一套结算），这里只做汇总与「要不要停」的判定。
"""
from __future__ import annotations


def summarize(trades: list[dict]) -> dict:
    """从已结算交易汇总。trades 每项含 pnl。"""
    n = len(trades)
    if n == 0:
        return {"n": 0, "win_rate": 0.0, "avg_pnl": 0.0, "total_pnl": 0.0}
    pnls = [t["pnl"] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    return {
        "n": n,
        "win_rate": wins / n,
        "avg_pnl": sum(pnls) / n,
        "total_pnl": sum(pnls),
    }


def should_stop(trades: list[dict], min_n: int = 20, max_dd: float = -0.15) -> bool:
    """判定某猎犬是否该停：样本够了、期望为负、且累计回撤超阈值 → 停。

    - 样本 < min_n：数据不足，先跑着，不下结论。
    - 期望 >= 0：继续。
    - 期望 < 0 且累计复利净值相对峰值回撤 > max_dd：停。
    """
    if len(trades) < min_n:
        return False
    eq = 1.0
    peak = 1.0
    dd = 0.0
    for t in trades:
        eq *= (1.0 + t["pnl"])
        peak = max(peak, eq)
        dd = min(dd, eq / peak - 1.0)
    avg = sum(t["pnl"] for t in trades) / len(trades)
    return avg < 0 and dd <= max_dd
