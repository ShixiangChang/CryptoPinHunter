# -*- coding: utf-8 -*-
"""统一回测引擎：喂历史数据跑 Strategy，产出净值 / Sharpe / 回撤 / 交易流水。

与纸面共用同一套 Strategy.generate_signals —— 回测与纸面信号同源。
两条推进路径，由 strategy.interval 决定：
- "1h"（momentum / beta）：逐 1h bar 推进，止损/到期逐 bar 检查，每 decision_interval_h 重算信号。
- "1m"（pin）：事件驱动，向量化找触发点，逐个模拟「触发→开仓→持有→到期平仓」。

前视安全：数据一次性加载完整历史，但策略内部只取 `open_time <= now` 的切片计算信号。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict

import numpy as np
import pandas as pd

import sys
from pathlib import Path as _Path

_root = _Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
import config
from . import data
from .strategy import Strategy

COST_SIDE = config.COST_SIDE
_WARMUP_SEC = 31 * 24 * 3600   # 预热窗口：给 720h 动量/均线留足历史


@dataclass
class BacktestResult:
    name: str = ""
    start: int = 0
    end: int = 0
    total_return: float = 0.0
    sharpe: float = 0.0
    max_dd: float = 0.0
    n_trades: int = 0
    win_rate: float = 0.0
    avg_pnl: float = 0.0
    lookahead: str = "前视"          # 前视档位声明（"前视"/"无前视"），写进结果对象删不掉
    nav_series: list = field(default_factory=list)   # [[ts, nav], ...]
    trades: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False), encoding="utf-8")


def run_backtest(strategy: Strategy, start: int, end: int | None = None,
                 lookahead: str = "前视") -> BacktestResult:
    """跑一次回测。start/end 为 unix 秒；end=None 时用该粒度数据的最新时间。

    lookahead：前视档位声明，只能是「前视」或「无前视」。默认「前视」（最保守口径）。
    档位被写进结果对象（删不掉），任何打印/落库都必须带出 —— 事前强制声明，杜绝
    「先跑出数、事后补注脚」。要声称「无前视」必须显式传参，且自己保证定池无前视。
    """
    if lookahead not in ("前视", "无前视"):
        raise ValueError("lookahead 必须是 '前视' 或 '无前视'，拒绝运行")
    if end is None:
        end = data.latest_open_time(strategy.interval) or (start + 365 * 24 * 3600)
    symbols = strategy.universe
    if strategy.interval == "1m":
        # 插针三条件用 close/low/volume；预热 vol_win + lookback 分钟（2h 保险）
        data_map = data.load_klines(symbols, interval="1m", start=start - 7200, end=end,
                                    columns=["open_time", "low", "close", "volume"])
    else:
        data_map = data.load_klines(symbols, interval=strategy.interval,
                                    start=start - _WARMUP_SEC, end=end)
    if not data_map:
        return BacktestResult(name=strategy.name, start=start, end=end, lookahead=lookahead)
    if strategy.interval == "1m":
        r = _backtest_pin(strategy, data_map, start, end)
    else:
        r = _backtest_hourly(strategy, data_map, start, end)
    r.lookahead = lookahead
    return r


def run_backtest_walkforward(strategy: Strategy, start: int, end: int | None = None,
                             pool_window_days: int = 90, step_days: int = 30,
                             top_n: int | None = None) -> BacktestResult:
    """walk-forward 无前视回测。

    把 [start, end] 切成 step_days 步长的窗口，每个窗口起点 t 只用「<= t 的数据」
    滚动定池（消除「用未来名单回测过去」的定池前视），逐窗拼接净值。

    - 定池：liquid_symbols(end_ts=t) 只扫 (t-window, t] 的成交额，t 之后的币不进场。
    - 拼接：每个窗口净值从 1.0 起，乘以上一窗累计净值接续，复利无缝。
    - 边界代价：窗口末强制平仓 + 跨窗事件被截断，pin 持有 12h、窗 30 天，受影响交易 <3%。
    """
    if end is None:
        end = data.latest_open_time(strategy.interval) or (start + 365 * 24 * 3600)
    step_sec = step_days * 24 * 3600
    top_n = top_n or getattr(strategy, "top_n", 50)

    window_results: list[BacktestResult] = []
    t = start
    while t < end:
        w_end = min(t + step_sec, end)
        symbols = data.liquid_symbols(
            strategy.interval, top_n=top_n, window_days=pool_window_days,
            min_adv=getattr(strategy, "min_adv", None),
            end_ts=t, require_spot=getattr(strategy, "require_spot", True),
        )
        if not symbols:
            t = w_end
            continue
        strategy.universe = symbols
        r = run_backtest(strategy, t, w_end, lookahead="无前视")
        if r.nav_series:
            window_results.append(r)
        t = w_end

    if not window_results:
        return BacktestResult(name=strategy.name, start=start, end=end, lookahead="无前视")

    nav_series: list = []
    trades: list = []
    cum_nav = 1.0
    for r in window_results:
        for ts, nav in r.nav_series:
            nav_series.append([ts, nav * cum_nav])
        cum_nav *= (r.total_return + 1.0)
        trades.extend(r.trades)

    result = BacktestResult(name=strategy.name, start=start, end=end,
                            nav_series=nav_series, trades=trades, lookahead="无前视")
    return _compute(result, bars_per_year=365 * 24 * 60)


# ---------------------------------------------------------------- 工具
def _build_index(data_map: dict[str, pd.DataFrame]) -> dict:
    idx = {}
    for sym, df in data_map.items():
        idx[sym] = {
            "ot": df["open_time"].to_numpy(dtype=np.int64),
            "high": df["high"].to_numpy(dtype=float),
            "low": df["low"].to_numpy(dtype=float),
            "close": df["close"].to_numpy(dtype=float),
        }
    return idx


def _bar(idx: dict, sym: str, t: int):
    """sym 在 t 时刻的 (high, low, close)；该时刻无 bar 则取最后一个 <= t 的 bar。"""
    a = idx.get(sym)
    if a is None:
        return None
    pos = int(np.searchsorted(a["ot"], t, side="right")) - 1
    if pos < 0:
        return None
    return float(a["high"][pos]), float(a["low"][pos]), float(a["close"][pos])


def _normalize(signals: list[dict]) -> list[dict]:
    """总仓位 > 1 时等比缩放，硬保证不超满仓。"""
    total = sum(s["weight"] for s in signals)
    if total > 1.0:
        k = 1.0 / total
        for s in signals:
            s["weight"] *= k
    return signals


def _compute(r: BacktestResult, bars_per_year: int) -> BacktestResult:
    if not r.nav_series:
        return r
    navs = np.array([n for _, n in r.nav_series], dtype=float)
    r.total_return = float(navs[-1] - 1.0)
    peak = np.maximum.accumulate(navs)
    dd = navs / peak - 1.0
    r.max_dd = float(dd.min()) if dd.size else 0.0
    if bars_per_year >= 365 * 24 * 60:
        # 事件驱动（1m）：净值序列稀疏，Sharpe 改用每笔交易 pnl 分布
        pnls = np.array([t["pnl"] for t in r.trades], dtype=float)
        if pnls.size > 1 and float(pnls.std()) > 0:
            years = max((r.end - r.start) / (365 * 24 * 3600), 0.5)
            r.sharpe = float(pnls.mean() / pnls.std() * math.sqrt(pnls.size / years))
    else:
        rets = np.diff(navs) / navs[:-1]
        if rets.size and float(rets.std()) > 0:
            r.sharpe = float(rets.mean() / rets.std() * math.sqrt(bars_per_year))
    pnls = [t["pnl"] for t in r.trades]
    r.n_trades = len(pnls)
    if pnls:
        r.win_rate = float(sum(1 for p in pnls if p > 0) / len(pnls))
        r.avg_pnl = float(np.mean(pnls))
    return r


# ---------------------------------------------------------------- 1h：逐 bar 推进
def _backtest_hourly(strategy: Strategy, data_map: dict[str, pd.DataFrame],
                     start: int, end: int) -> BacktestResult:
    idx = _build_index(data_map)
    all_ts = np.concatenate([a["ot"] for a in idx.values()])
    bars = np.unique(all_ts)
    bars = bars[(bars >= start) & (bars <= end)]
    decision_step = max(1, round(strategy.decision_interval_h))
    hold_sec = strategy.hold_hours * 3600

    cash_box = [1.0]                    # 现金（列表包装，便于循环内修改）
    positions: dict[str, dict] = {}     # sym -> {side, weight, entry, stop, open_t}
    nav_series: list = []
    trades: list = []

    for t in bars:
        t = int(t)
        # 1) 止损（只做多：low 触及 stop 平在 stop）
        for sym in list(positions.keys()):
            p = positions[sym]
            if p["stop"] is None:
                continue
            b = _bar(idx, sym, t)
            if b is None:
                continue
            if p["side"] == 1 and b[1] <= p["stop"]:
                exit_px = p["stop"]
                pnl = exit_px / p["entry"] - 1.0
                cash_box[0] += p["weight"] * (1.0 + pnl) * (1.0 - COST_SIDE)
                trades.append({"symbol": sym, "side": p["side"], "entry_time": p["open_t"],
                               "entry": p["entry"], "exit_time": t, "exit": exit_px,
                               "pnl": pnl - 2 * COST_SIDE, "weight": p["weight"], "reason": "stop"})
                del positions[sym]

        # 2) 到期
        for sym in list(positions.keys()):
            p = positions[sym]
            if t - p["open_t"] >= hold_sec:
                b = _bar(idx, sym, t)
                if b is None:
                    continue
                exit_px = b[2]
                pnl = exit_px / p["entry"] - 1.0
                cash_box[0] += p["weight"] * (1.0 + pnl) * (1.0 - COST_SIDE)
                trades.append({"symbol": sym, "side": p["side"], "entry_time": p["open_t"],
                               "entry": p["entry"], "exit_time": t, "exit": exit_px,
                               "pnl": pnl - 2 * COST_SIDE, "weight": p["weight"], "reason": "expire"})
                del positions[sym]

        # 3) 决策点：重算目标持仓
        if (int(t) - start) % (decision_step * 3600) == 0:
            signals = _normalize(strategy.generate_signals(data_map, t))
            target = {s["symbol"]: s for s in signals}
            # 平掉不在目标的仓
            for sym in list(positions.keys()):
                if sym not in target:
                    b = _bar(idx, sym, t)
                    exit_px = b[2] if b else positions[sym]["entry"]
                    pnl = positions[sym]["side"] * (exit_px / positions[sym]["entry"] - 1.0)
                    cash_box[0] += positions[sym]["weight"] * (1.0 + pnl) * (1.0 - COST_SIDE)
                    trades.append({"symbol": sym, "side": positions[sym]["side"],
                                   "entry_time": positions[sym]["open_t"],
                                   "entry": positions[sym]["entry"], "exit_time": t,
                                   "exit": exit_px, "pnl": pnl - 2 * COST_SIDE,
                                   "weight": positions[sym]["weight"], "reason": "rotate"})
                    del positions[sym]
            # 开新仓（已在持仓的续期，不重复开）
            for sym, s in target.items():
                if sym in positions:
                    continue
                b = _bar(idx, sym, t)
                if b is None:
                    continue
                entry = s["entry"] if s["entry"] else b[2]
                if entry <= 0:
                    continue
                w = s["weight"]
                cash_box[0] -= w * (1.0 + COST_SIDE)
                positions[sym] = {"side": s["side"], "weight": w, "entry": entry,
                                  "stop": s["stop"], "open_t": t}

        # 4) 净值
        nav = cash_box[0]
        for sym, p in positions.items():
            b = _bar(idx, sym, t)
            if b:
                nav += p["weight"] * (1.0 + p["side"] * (b[2] / p["entry"] - 1.0))
        nav_series.append([int(t), nav])

    r = BacktestResult(name=strategy.name, start=start, end=end,
                       nav_series=nav_series, trades=trades)
    return _compute(r, bars_per_year=365 * 24)


# ---------------------------------------------------------------- 1m：事件驱动（pin）
def _backtest_pin(strategy: Strategy, data_map: dict[str, pd.DataFrame],
                  start: int, end: int) -> BacktestResult:
    hold_sec = strategy.hold_hours * 3600
    lb = strategy.lookback
    vol_win = getattr(strategy, "vol_win", 60)
    vol_mult = getattr(strategy, "vol_mult", 2.0)
    recovery = getattr(strategy, "recovery", 0.01)
    stop_buffer = getattr(strategy, "stop_buffer", 0.02)

    # 向量化找触发点：三条件（针尖深 + 针被接住 + 量能放大），与纸面 generate_signals 严格同源
    events: list[tuple[int, str, float, float, float]] = []  # (t, sym, entry, weight, stop)
    close_lookup: dict[str, tuple[np.ndarray, np.ndarray]] = {}  # sym -> (ot, close)
    low_lookup: dict[str, tuple[np.ndarray, np.ndarray]] = {}    # sym -> (ot, low)
    for sym, df in data_map.items():
        close = df["close"].to_numpy(dtype=float)
        low = df["low"].to_numpy(dtype=float)
        volume = df["volume"].to_numpy(dtype=float)
        ot = df["open_time"].to_numpy(dtype=np.int64)
        close_lookup[sym] = (ot, close)
        low_lookup[sym] = (ot, low)
        if len(close) <= lb:
            continue
        base = close[:-lb]
        # 条件1：针尖深（use_low=True 用 low 捕捉针尖 / False 用 close 跌幅，等价旧版）
        price_series = low[lb:] if getattr(strategy, "use_low", True) else close[lb:]
        low_ret = price_series / np.where(base > 0, base, np.nan) - 1.0
        # 条件2：针被接住（close 相对 low 反弹）
        low_now = low[lb:]
        rec = close[lb:] / np.where(low_now > 0, low_now, np.nan) - 1.0
        # 条件3：量能（相对过去 vol_win 分钟均量）
        vol_mean = pd.Series(volume).rolling(vol_win, min_periods=1).mean().to_numpy()
        vol_ratio = volume[lb:] / np.where(vol_mean[lb:] > 0, vol_mean[lb:], np.nan)

        mask = (low_ret <= strategy.threshold) & (rec >= recovery) & (vol_ratio >= vol_mult)
        for j in np.nonzero(mask)[0]:
            i = j + lb
            t = int(ot[i])
            if t < start or t > end:
                continue
            depth = abs(float(low_ret[j])) / abs(strategy.threshold)
            w = min(depth ** strategy.weight_exp, strategy.weight_cap) * strategy.pos
            stop = None if stop_buffer <= 0 else float(low[i] * (1.0 - stop_buffer))
            events.append((t, sym, float(close[i]), w, stop))
    events.sort(key=lambda x: x[0])

    # funding 过滤：只在多头拥挤（funding 正）时抄底。加载一次，事件循环里查「针前最近 funding」。
    funding_map = data.load_funding(list(data_map.keys())) if getattr(strategy, "require_funding_positive", False) else {}
    funding_min = getattr(strategy, "funding_min", 0.0001)

    # 趋势过滤（公允值方向）：90 天滚动高点（1h），判断触发时币价距高点跌幅。
    trend_map = {}
    if getattr(strategy, "require_trend_filter", False):
        trend_lb = getattr(strategy, "trend_lookback_h", 90 * 24)
        trend_map = data.load_trend_highs(
            list(data_map.keys()), start - (trend_lb + 24) * 3600, end, trend_lb,
        )
    trend_max_dd = getattr(strategy, "trend_max_dd", -0.30)

    def _close_at(sym: str, t: int) -> float | None:
        ot, close = close_lookup[sym]
        pos = int(np.searchsorted(ot, t, side="right")) - 1
        return float(close[pos]) if pos >= 0 else None

    def _stop_hit(sym: str, open_t: int, exit_t: int, stop: float) -> int | None:
        """(open_t, exit_t] 区间内第一个 low <= stop 的时间点；无则 None。"""
        ot, low = low_lookup[sym]
        lo = int(np.searchsorted(ot, open_t, side="right"))
        hi = int(np.searchsorted(ot, exit_t, side="right"))
        if lo >= len(low) or lo >= hi:
            return None
        hit = np.nonzero(low[lo:hi] <= stop)[0]
        return int(ot[lo + hit[0]]) if hit.size else None

    positions: dict[str, dict] = {}
    nav = 1.0
    available = 1.0
    nav_series: list = [[start, 1.0]]
    trades: list = []

    for t, sym, entry, w, stop in events:
        # 1) 平掉已到期/已止损的仓
        for psym in list(positions.keys()):
            p = positions[psym]
            if p["exit_t"] <= t:
                if p["reason"] == "stop":
                    exit_px = p["exit_px"]                      # 止损：平在 stop 价
                else:
                    exit_px = _close_at(psym, p["exit_t"]) or p["entry"]  # 到期：平在 close
                pnl = exit_px / p["entry"] - 1.0
                nav *= (1.0 + p["weight"] * (pnl - 2 * COST_SIDE))
                available += p["weight"]
                trades.append({"symbol": psym, "side": 1, "entry_time": p["open_t"],
                               "entry": p["entry"], "exit_time": p["exit_t"],
                               "exit": exit_px, "pnl": pnl - 2 * COST_SIDE,
                               "weight": p["weight"], "reason": p["reason"]})
                nav_series.append([p["exit_t"], nav])
                del positions[psym]
        # 2) 同币插针期间不重复开
        if sym in positions:
            continue
        # 3) funding 过滤：负/中性 = 信息驱动真相，弱回归，跳过
        if funding_map:
            fv = data.prev_funding(funding_map, sym, t)
            if fv is None or fv < funding_min:
                continue
        # 3.5) 趋势过滤：距 90 天高点跌幅 >30% = 公允值下降 = 真相，跳过
        if trend_map:
            dd = data.trend_dd_at(trend_map, sym, t, entry)
            if dd is None or dd < trend_max_dd:
                continue
        # 4) 开仓：开仓即确定退出方式（止损 or 到期）
        actual_w = min(w, available)
        if actual_w <= 1e-6:
            continue
        expire_t = t + hold_sec
        stop_t = _stop_hit(sym, t, expire_t, stop) if stop is not None else None
        if stop_t is not None:
            positions[sym] = {"entry": entry, "weight": actual_w, "open_t": t,
                              "exit_t": stop_t, "exit_px": stop, "reason": "stop"}
        else:
            positions[sym] = {"entry": entry, "weight": actual_w, "open_t": t,
                              "exit_t": expire_t, "exit_px": None, "reason": "expire"}
        available -= actual_w

    # 收盘：平掉剩余仓
    for sym, p in positions.items():
        if p["reason"] == "stop":
            exit_px = p["exit_px"]
        else:
            exit_px = _close_at(sym, end) or p["entry"]
        pnl = exit_px / p["entry"] - 1.0
        nav *= (1.0 + p["weight"] * (pnl - 2 * COST_SIDE))
        trades.append({"symbol": sym, "side": 1, "entry_time": p["open_t"],
                       "entry": p["entry"], "exit_time": end, "exit": exit_px,
                       "pnl": pnl - 2 * COST_SIDE, "weight": p["weight"], "reason": p["reason"]})
    nav_series.append([end, nav])

    r = BacktestResult(name=strategy.name, start=start, end=end,
                       nav_series=nav_series, trades=trades)
    return _compute(r, bars_per_year=365 * 24 * 60)
