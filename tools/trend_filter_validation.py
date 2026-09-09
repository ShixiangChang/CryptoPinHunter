# -*- coding: utf-8 -*-
"""验证「趋势过滤」是否是遗漏的那条规则。

假设：pin 的 edge 是「错杀回归」，但「跌>90%」的币是「公允值下降」（真相），不是错杀。
所以遗漏的规则 = 事前判断「币的公允值方向（趋势）」，避开「处于深度下跌趋势」的币。

信号（事前、无前视）：触发点 t 时，币价相对「过去 90 天最高价」的跌幅。
  - 跌幅浅（趋势健康/横盘）→ 插针是错杀 → 会回归 → pin 该抄
  - 跌幅深（深度下跌趋势）→ 插针是真相 → 不会回归 → pin 该避开

用 1h 数据算 90 天滚动高点（2160 根），映射到 1m 触发点，避免事后视角（不用未来最高点）。
"""
from __future__ import annotations

import datetime
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
import config
from engine import data
from engine.strategies.pin import PinStrategy

START = int(datetime.datetime(2024, 9, 1, tzinfo=datetime.timezone.utc).timestamp())
END = int(datetime.datetime(2026, 9, 7, tzinfo=datetime.timezone.utc).timestamp())
TREND_LOOKBACK_H = 90 * 24  # 90 天（1h 根数）


def _all_symbols() -> list[str]:
    con = sqlite3.connect(config.DB_PATH)
    try:
        rows = con.execute("SELECT DISTINCT symbol FROM klines_1m").fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def _trend_signal(sym: str) -> tuple[np.ndarray, np.ndarray]:
    """1h 数据算「过去 90 天最高价」时间序列，返回 (ot, high90)。"""
    df = data.load_klines([sym], interval="1h", start=START - 100 * 24 * 3600, end=END)
    if not df or sym not in df:
        return np.array([], dtype=np.int64), np.array([])
    d = df[sym]
    ot = d["open_time"].to_numpy(dtype=np.int64)
    high = d["high"].to_numpy(dtype=float)
    roll = pd.Series(high).rolling(TREND_LOOKBACK_H, min_periods=TREND_LOOKBACK_H).max().to_numpy()
    return ot, roll


def _find_events(df: pd.DataFrame, s: PinStrategy) -> list[tuple[int, float]]:
    close = df["close"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    ot = df["open_time"].to_numpy(dtype=np.int64)
    lb = s.lookback
    if len(close) <= lb:
        return []
    base = close[:-lb]
    price_series = low[lb:] if s.use_low else close[lb:]
    low_ret = price_series / np.where(base > 0, base, np.nan) - 1.0
    rec = close[lb:] / np.where(low[lb:] > 0, low[lb:], np.nan) - 1.0
    mask = (low_ret <= s.threshold) & (rec >= s.recovery)
    return [(int(ot[j + lb]), float(close[j + lb])) for j in np.nonzero(mask)[0]]


def main() -> None:
    s = PinStrategy()
    syms = [x for x in _all_symbols() if x not in data._load_tradfi_blacklist()]
    funding_map = data.load_funding(syms)
    data_map = data.load_klines(syms, interval="1m", start=START - 7200, end=END,
                                columns=["open_time", "low", "close"])

    # 趋势分档：触发时距 90 天高点的跌幅
    bins = [("深跌(<-70%)", -0.70), ("跌(-70~-30%)", -0.30), ("浅跌/横盘(>=-30%)", 1.0)]
    agg = {k: {"pos": [], "n_pos": 0, "n_trig": 0} for k, _ in bins}

    for sym in syms:
        df = data_map.get(sym)
        if df is None or df.empty:
            continue
        ot_h, high90 = _trend_signal(sym)
        if ot_h.size == 0:
            continue
        events = _find_events(df, s)
        ot = df["open_time"].to_numpy(dtype=np.int64)
        close = df["close"].to_numpy(dtype=float)
        for t, entry in events:
            fv = data.prev_funding(funding_map, sym, t)
            if fv is None or fv < s.funding_min:
                continue  # 只看 funding 正（会开仓）的针
            # 触发时距 90 天高点跌幅（事前：只取 t 之前的高点）
            hp = int(np.searchsorted(ot_h, t, side="right")) - 1
            if hp < 0:
                continue
            h90 = high90[hp]
            if not np.isfinite(h90) or h90 <= 0:
                continue
            dd = entry / h90 - 1.0
            for name, th in bins:
                if dd <= th:
                    agg[name]["n_trig"] += 1
                    agg[name]["n_pos"] += 1
                    exit_pos = int(np.searchsorted(ot, t + s.hold_hours * 3600, side="right")) - 1
                    exit_pos = min(exit_pos, len(close) - 1)
                    if exit_pos >= 0:
                        agg[name]["pos"].append(close[exit_pos] / entry - 1.0)
                    break

    print(f"{'触发时趋势':<16} {'正针数':>6} {'12h中位':>9} {'胜率':>7} {'12h均值':>9}")
    print("-" * 50)
    for name, _th in bins:
        a = agg[name]
        arr = np.array(a["pos"]) if a["pos"] else np.array([])
        med = float(np.median(arr)) if arr.size else 0.0
        win = float((arr > 0).mean()) if arr.size else 0.0
        mean = float(arr.mean()) if arr.size else 0.0
        print(f"{name:<16} {a['n_pos']:>6} {med:>+8.2%} {win:>7.1%} {mean:>+8.2%}")


if __name__ == "__main__":
    main()
