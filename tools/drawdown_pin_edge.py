# -*- coding: utf-8 -*-
"""研究：pin 的 funding 正组 edge 是否随「币的跌幅」单调衰减。

僵尸币测试发现：跌 >70% 的币上，funding 正针中位收益仅 +0.99%（远低于正常币）。
本脚本按「历史最高 → 最新价」跌幅分四档，逐档跑 pin 事件统计，验证 edge 是否随跌幅
单调衰减。若单调，则 pin 应加「趋势过滤」：避开长期下跌趋势的币，而非仅靠 funding。

度量纪律：持有收益（12h 后 close / 开仓 close），未扣成本。
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

BINS = [("跌>90%", -0.90), ("跌70~90%", -0.70), ("跌50~70%", -0.50), ("跌<50%", 1.0)]


def _peak_and_latest() -> dict[str, tuple[float, float]]:
    con = sqlite3.connect(config.DB_PATH)
    try:
        df_peak = pd.read_sql_query(
            "SELECT symbol, MAX(high) AS peak FROM klines_1m "
            "WHERE open_time>=? AND open_time<=? GROUP BY symbol",
            con, params=(START, END))
        df_last = pd.read_sql_query(
            "SELECT k1.symbol, k1.close FROM klines_1m k1 "
            "INNER JOIN (SELECT symbol, MAX(open_time) mx FROM klines_1m GROUP BY symbol) k2 "
            "ON k1.symbol=k2.symbol AND k1.open_time=k2.mx", con)
    finally:
        con.close()
    pk = df_peak.set_index("symbol")["peak"].to_dict()
    lc = df_last.set_index("symbol")["close"].to_dict()
    return {s: (float(pk.get(s, 0) or 0), float(lc.get(s, 0) or 0)) for s in pk}


def _find_events(df: pd.DataFrame, s: PinStrategy) -> tuple[list, np.ndarray, np.ndarray]:
    close = df["close"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    ot = df["open_time"].to_numpy(dtype=np.int64)
    lb = s.lookback
    if len(close) <= lb:
        return [], ot, close
    base = close[:-lb]
    price_series = low[lb:] if s.use_low else close[lb:]
    low_ret = price_series / np.where(base > 0, base, np.nan) - 1.0
    rec = close[lb:] / np.where(low[lb:] > 0, low[lb:], np.nan) - 1.0
    mask = (low_ret <= s.threshold) & (rec >= s.recovery)
    ev = [(int(ot[j + lb]), float(close[j + lb])) for j in np.nonzero(mask)[0]]
    return ev, ot, close


def main() -> None:
    s = PinStrategy()
    info = _peak_and_latest()
    black = data._load_tradfi_blacklist()

    # 分档
    groups: dict[str, list[str]] = {k: [] for k, _ in BINS}
    for sym, (peak, last) in sorted(info.items()):
        if sym in black or peak <= 0 or last <= 0:
            continue
        dd = last / peak - 1.0
        for name, th in BINS:
            if dd <= th:
                groups[name].append(sym)
                break

    print(f"{'跌幅档':<10} {'币数':>4} {'针数':>6} {'正组占比':>8} {'正组中位':>9} {'正组胜率':>8} {'正组均值':>9}")
    print("-" * 62)

    all_syms = [x for _, v in groups.items() for x in v]
    funding_map = data.load_funding(all_syms)
    data_map = data.load_klines(all_syms, interval="1m", start=START - 7200, end=END,
                                columns=["open_time", "low", "close"])

    for name, _th in BINS:
        syms = groups[name]
        if not syms:
            print(f"{name:<10} {0:>4}")
            continue
        pos_rets: list[float] = []
        n_trigger = n_pos = 0
        for sym in syms:
            df = data_map.get(sym)
            if df is None or df.empty:
                continue
            ev, ot, close = _find_events(df, s)
            for t, entry in ev:
                n_trigger += 1
                fv = data.prev_funding(funding_map, sym, t)
                if fv is None or fv < s.funding_min:
                    continue
                n_pos += 1
                exit_pos = int(np.searchsorted(ot, t + s.hold_hours * 3600, side="right")) - 1
                exit_pos = min(exit_pos, len(close) - 1)
                if exit_pos >= 0:
                    pos_rets.append(close[exit_pos] / entry - 1.0)
        a = np.array(pos_rets) if pos_rets else np.array([])
        med = float(np.median(a)) if a.size else 0.0
        win = float((a > 0).mean()) if a.size else 0.0
        mean = float(a.mean()) if a.size else 0.0
        ratio = n_pos / max(n_trigger, 1)
        print(f"{name:<10} {len(syms):>4} {n_trigger:>6} {ratio:>7.1%} {med:>+8.2%} {win:>7.1%} {mean:>+8.2%}")


if __name__ == "__main__":
    main()
