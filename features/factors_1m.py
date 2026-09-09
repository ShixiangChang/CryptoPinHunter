# -*- coding: utf-8 -*-
"""1m 精细因子：从 klines_1m 构造 1h 造不出的因子，对齐到 1h 整点截面，喂 LightGBM。

背景（用 1260 万分钟截面实测）：
- 1h 数据只有 24 根 K 线估「日波动」，1m 数据用 1440 个点算精确 realized vol，才挖出
  rv_24h（1m 已实现波动）——逐截面 rank IC ≈ -0.09（t≈-38），是 mom_720 的 8 倍。
- 但辛普森悖论：平静日低波动异象（负 IC）、暴涨日彩票动量（正 IC），简单多空被彩票效应吃光。
  正解 = 把 rv_24h 家族喂给 LightGBM 学「什么市场状态下高波动→跌还是涨」的条件关系。

无前视纪律：
- 1h bar 的 open_time = 整点 T，其 close 是 T 小时最后一分钟（HH:59）的收盘。
- 本模块对 open_time=T，用「到 HH:59 为止」的 1m 数据构造因子——信息截止与 1h 因子
  的 close 严格对齐，不引入任何额外前视。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

import sys
from pathlib import Path as _Path
_root = _Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
import config

# monitor.db 的 klines_1m 表：open_time 为 unix 秒（分钟整点），列含 close/high/low/volume
_DB_PATH = Path(config.CACHE_DIR).parent / "monitor.db"

# 1m 完整 2 年的币（约 105.6 万分钟 / 币，2024-08-29 ~ 2026-09-01）
FULL_1M_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "LINKUSDT", "UNIUSDT", "NEARUSDT",
    "ONGUSDT", "TAOUSDT", "WLDUSDT", "MOVRUSDT", "1000PEPEUSDT",
    "SUIUSDT", "ENAUSDT", "ZECUSDT",
]

# 1m 因子列名（都以 _1m 结尾，feature_cols 自动识别为特征，不冲突）
FACTOR_COLS_1M = (
    "rv_24h_1m",       # 过去 24h（1440 分钟）1m 对数收益 std × sqrt(1440)：精确已实现波动
    "rv_7d_1m",        # 过去 7d（10080 分钟）已实现波动：长期波动基准
    "rv_ratio_1m",     # rv_24h / rv_7d：短期波动相对长期（波动加速/减速）
    "hl_range_24h_1m", # 过去 24h 最高-最低 / 收盘：日内振幅（用 1440 个点，非 24 根 K 线）
    "mom_1h_1m",       # 过去 60 分钟收益：日内动量
    "rev_15m_1m",      # 过去 15 分钟收益：日内短反转
    "dn_vol_24h_1m",   # 过去 24h 负收益半方差的开方：下行风险
    "up_vol_24h_1m",   # 过去 24h 正收益半方差的开方：上行动能
    "dn_up_ratio_1m",  # 下行波动 / 上行波动：崩盘不对称
)

_DAY_MIN = 24 * 60
_MIN = 60
_HOUR_OFFSET = 59 * 60   # 每小时最后一分钟 HH:59 的秒偏移 = 3540


def _load_1m(symbol: str) -> pd.DataFrame:
    conn = sqlite3.connect(str(_DB_PATH))
    try:
        df = pd.read_sql_query(
            "SELECT open_time, close, high, low FROM klines_1m "
            "WHERE symbol=? ORDER BY open_time",
            conn, params=(symbol,),
        )
    finally:
        conn.close()
    return df


def _symbol_factors(symbol: str) -> pd.DataFrame:
    df = _load_1m(symbol)
    if df.empty:
        return pd.DataFrame()
    ts = df["open_time"].to_numpy(dtype=np.int64)
    C = df["close"].to_numpy(dtype=float)
    H = df["high"].to_numpy(dtype=float)
    L = df["low"].to_numpy(dtype=float)

    # 每小时最后一分钟（HH:59）的索引 —— 这些是「决策时刻」，因子信息截止到该分钟
    idx = np.where((ts % 3600) == _HOUR_OFFSET)[0]
    if idx.size < 300:
        return pd.DataFrame()

    close_s = pd.Series(C)
    logc = np.log(close_s.replace(0, np.nan))
    lr = logc.diff()   # lr[j] = log(C[j]/C[j-1])，对齐 C 索引 j

    # 已实现波动（population std，论文口径）
    rv24 = lr.rolling(_DAY_MIN, min_periods=_DAY_MIN).std(ddof=0) * np.sqrt(1440)
    rv7 = lr.rolling(7 * _DAY_MIN, min_periods=7 * _DAY_MIN).std(ddof=0) * np.sqrt(1440)

    # 日内振幅：过去 24h 最高-最低 / 收盘
    rng = (pd.Series(H).rolling(_DAY_MIN).max() - pd.Series(L).rolling(_DAY_MIN).min()) / close_s

    # 日内动量 / 短反转
    mom_1h = close_s / close_s.shift(_MIN) - 1.0
    rev_15m = close_s / close_s.shift(15) - 1.0

    # 半方差（上行/下行不对称）
    up = lr.clip(lower=0.0)
    dn = lr.clip(upper=0.0)
    up_vol = up.rolling(_DAY_MIN, min_periods=_DAY_MIN).var(ddof=0).pow(0.5) * np.sqrt(1440)
    dn_vol = dn.rolling(_DAY_MIN, min_periods=_DAY_MIN).var(ddof=0).pow(0.5) * np.sqrt(1440)
    dn_up = dn_vol / up_vol.replace(0.0, np.nan)

    out = pd.DataFrame({
        "open_time": ts[idx] - _HOUR_OFFSET,      # 整点秒
        "rv_24h_1m": rv24.to_numpy()[idx],
        "rv_7d_1m": rv7.to_numpy()[idx],
        "rv_ratio_1m": (rv24 / rv7.replace(0.0, np.nan)).to_numpy()[idx],
        "hl_range_24h_1m": rng.to_numpy()[idx],
        "mom_1h_1m": mom_1h.to_numpy()[idx],
        "rev_15m_1m": rev_15m.to_numpy()[idx],
        "dn_vol_24h_1m": dn_vol.to_numpy()[idx],
        "up_vol_24h_1m": up_vol.to_numpy()[idx],
        "dn_up_ratio_1m": dn_up.to_numpy()[idx],
    })
    out["symbol"] = symbol
    out["open_time"] = pd.to_datetime(out["open_time"], unit="s", utc=True)
    return out.replace([np.inf, -np.inf], np.nan)


def build_1m_factors(symbols: list[str] | None = None, progress: bool = True) -> pd.DataFrame:
    """构造全部币的 1m 因子面板（对齐 1h 整点）。"""
    symbols = symbols if symbols is not None else FULL_1M_SYMBOLS
    frames = []
    for i, sym in enumerate(symbols, 1):
        if progress:
            print(f"[features_1m] {i}/{len(symbols)}: {sym}")
        try:
            f = _symbol_factors(sym)
        except Exception as exc:
            print(f"[features_1m] {sym} 跳过: {exc}")
            continue
        if not f.empty:
            frames.append(f)
    if not frames:
        raise RuntimeError("没有可用的 1m 因子数据")
    return pd.concat(frames, ignore_index=True)


def merge_1m(panel: pd.DataFrame, f1m: pd.DataFrame) -> pd.DataFrame:
    """把 1m 因子按 (symbol, open_time) 合并进 1h 面板（left join，缺失留 NaN，LightGBM 原生处理）。"""
    return panel.merge(f1m, on=["symbol", "open_time"], how="left")
