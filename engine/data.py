# -*- coding: utf-8 -*-
"""数据加载层：从 monitor.db 读 K 线，统一入口（唯一数据源）。

原则：全系统只认 data/monitor.db 这一份数据。回测喂历史切片，纸面喂最新数据，
读的是同一张表、同一个 schema —— 数据源头天然同源，不依赖任何 CSV 缓存。
"""
from __future__ import annotations

import json
import sqlite3

import numpy as np
import pandas as pd

import sys
from pathlib import Path as _Path

_root = _Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
import config

_TABLES = {"1h": "klines", "1m": "klines_1m"}


def _conn(table: str):
    """连接 db；表不存在时返回 None（干净环境首次运行优雅降级，不崩溃）。"""
    conn = sqlite3.connect(config.DB_PATH)
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        conn.close()
        return None
    return conn


def load_klines(symbols: list[str], interval: str = "1h",
                start: int | None = None, end: int | None = None,
                columns: list[str] | None = None) -> dict[str, pd.DataFrame]:
    """从 monitor.db 读 K 线，返回 {symbol: DataFrame}。

    df 列：open_time(unix 秒), open, high, low, close, volume，按 open_time 升序。
    start/end 为 unix 秒（含边界），None 表示不限制。
    columns 指定只读这些列（省内存，1m 回测只读 open_time/close）。
    """
    table = _TABLES[interval]
    # klines_1m 表无 open 列（只有 open_time/high/low/close/volume），用 close 兜底 open
    if columns is not None:
        cols = ", ".join(columns)
    elif interval == "1m":
        cols = "open_time, close AS open, high, low, close, volume"
    else:
        cols = "open_time, open, high, low, close, volume"
    conn = _conn(table)
    if conn is None:
        return {}
    out: dict[str, pd.DataFrame] = {}
    try:
        for sym in symbols:
            q = (f"SELECT {cols} "
                 f"FROM {table} WHERE symbol=?")
            params: list = [sym]
            if start is not None:
                q += " AND open_time>=?"
                params.append(start)
            if end is not None:
                q += " AND open_time<=?"
                params.append(end)
            q += " ORDER BY open_time"
            df = pd.read_sql_query(q, conn, params=params)
            if not df.empty:
                out[sym] = df.reset_index(drop=True)
    finally:
        conn.close()
    return out


def available_symbols(interval: str = "1h", min_rows: int = 0,
                      start: int | None = None, end: int | None = None) -> list[str]:
    """该粒度下，K 线行数 >= min_rows 的币（按行数降序）。用于动态确定策略 universe。"""
    table = _TABLES[interval]
    conn = _conn(table)
    if conn is None:
        return []
    try:
        q = f"SELECT symbol, COUNT(*) AS n FROM {table}"
        where: list[str] = []
        if start is not None:
            where.append(f"open_time>={start}")
        if end is not None:
            where.append(f"open_time<={end}")
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " GROUP BY symbol ORDER BY n DESC"
        rows = conn.execute(q).fetchall()
    finally:
        conn.close()
    return [s for s, n in rows if n >= min_rows]


def _load_tradfi_blacklist() -> set[str]:
    """读 TradFi 黑名单（股票/贵金属/ETF/大宗，underlyingType != COIN）。

    权威来源：币安 exchangeInfo 的 underlyingType 字段，由 tools/fetch_coin_universe.py
    一次性抓取后固化到 data/tradfi_symbols.json。文件缺失/损坏时返回空集（宁可不过滤，
    也不凭名字误杀中文 meme 这类真实 COIN）。
    """
    try:
        with open(config.TRADFI_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
        syms = payload.get("tradfi_symbols", [])
        if isinstance(syms, list):
            return set(syms)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return set()


def _load_no_spot() -> set[str]:
    """读「仅合约无现货」名单（无现货盘 = 现货-合约套利锚缺失）。

    权威来源：币安 fapi exchangeInfo（COIN 永续）与 spot exchangeInfo（现货交易对）
    求 base asset 差集，由 tools/fetch_coin_universe.py 固化到 data/no_spot_symbols.json。
    文件缺失/损坏时返回空集（宁可不过滤，也不凭猜测误杀）。
    """
    try:
        with open(config.NO_SPOT_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
        syms = payload.get("no_spot_symbols", [])
        if isinstance(syms, list):
            return set(syms)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return set()


def _load_spot_onboard() -> dict[str, int]:
    """读现货上线时间映射 {base_asset: 上线毫秒}，供 point-in-time「有现货」判断。

    来源：tools/fetch_spot_onboard.py 抓的 data/spot_onboard_dates.json（现货 exchangeInfo
    无 onboardDate 字段，故用每个 USDT 现货的「最早日线 openTime」兜底）。
    文件缺失/损坏返回空 dict（安全降级：不加载则不触发 point-in-time 过滤）。
    """
    try:
        with open(config.SPOT_ONBOARD_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
        d = payload.get("spot_onboard_dates", {})
        if isinstance(d, dict):
            return {str(k): int(v) for k, v in d.items()}
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError, TypeError):
        pass
    return {}


def _base_of(symbol: str) -> str:
    """从永续 symbol 提取 base（去掉 USDT/USDC/USD 报价后缀）。"""
    for q in ("USDT", "USDC", "USD"):
        if symbol.endswith(q):
            return symbol[:-len(q)]
    return symbol


def _normalize_base(base: str, spot_bases: set[str]) -> str:
    """归一化 1000/1000000 倍合约前缀（1000PEPE→PEPE），对齐现货 base。

    只在「去前缀后确实存在于现货 base 集合」时才归一化，避免误伤名字本身带 1000 的代币
    （如 1000CAT 现货就叫 1000CAT，去前缀变成 CAT 不在集合，保持原样）。
    """
    if base.startswith("1000000") and base[7:] in spot_bases:
        return base[7:]
    if base.startswith("1000") and base[4:] in spot_bases:
        return base[4:]
    return base


def has_spot_at(symbol: str, t: int, spot_od: dict[str, int] | None = None) -> bool:
    """point-in-time：symbol 在 t 时刻（unix 秒）是否已有现货盘。

    True = t 时刻已上线现货（现货-合约套利锚存在，插针是错杀、会回归）；
    False = 无现货（含「从未有现货」或「现货已下架」两类，都保守判为无现货=套利锚缺失）。
    消除「用 2026 快照判断 2024 是否有现货」的时点前视。
    """
    if spot_od is None:
        spot_od = _load_spot_onboard()
    if not spot_od:
        return True  # 数据缺失：不过滤（宁可保留，不凭猜测误杀）
    base = _normalize_base(_base_of(symbol), set(spot_od.keys()))
    od = spot_od.get(base)
    if od is None:
        return False  # 当前在线现货里没有此 base → 无现货（或已下架）
    return t * 1000 >= od  # t 秒换算毫秒后与上线毫秒比较


def liquid_symbols(interval: str = "1m", top_n: int = 50,
                   window_days: int | None = None,
                   min_adv: float | None = None,
                   end_ts: int | None = None,
                   require_spot: bool = True) -> list[str]:
    """滚动窗口流动性定池：剔除 TradFi + 剔除「仅合约无现货」后，按近 window_days 天成交额取 top_n。

    判据 = edge 本质（插针→回归，回归的锚有多强），回归锚三层：现货-合约套利(最强) >
    做市商再平衡 > 投机抄底。pin 的 edge 成立 = 至少有现货套利锚。四条一起：
    1. 滚动窗口：只用 (end_ts - window_days, end_ts] 的成交额定池，消除「全周期成交额」的前视；
       window_days=None 表示不限窗口（等价旧版全周期）。
    2. 剔除 TradFi：股票/贵金属/ETF/大宗（underlyingType != COIN）本质不同，直接排除。
    3. 剔除「t 时刻无现货」：point-in-time 判断（has_spot_at），无现货盘 = 套利锚缺失，
       插针是「真相归零」不是「错杀」，排除。（替代旧静态 no_spot 名单，消除时点前视）
    4. 会换血：窗口随 end_ts 滚动，新活跃币进、老枯竭币出，不锁死。

    end_ts：定池截止时间（unix 秒）。None = 用该表最新时间（实盘/纸面当前时刻，天然无前视）；
    walk-forward 回测传入每个时点 t，只用 t 之前的数据定池，彻底消除前视。
    min_adv：流动性下限（USD/分钟平均成交额）；已按成交额降序，第一个低于下限即 break。
    require_spot：True=剔除「t 时刻无现货」（point-in-time，默认）；False=关闭（诊断用）。
    """
    table = _TABLES[interval]
    conn = _conn(table)
    if conn is None:
        return []
    try:
        if end_ts is None:
            latest = conn.execute(f"SELECT MAX(open_time) FROM {table}").fetchone()[0]
            end_ts = latest or 0
        where = ["volume>0"]
        params: list = []
        if window_days is not None and window_days > 0:
            where.append("open_time>?")
            params.append(end_ts - window_days * 24 * 3600)
        where.append("open_time<=?")
        params.append(end_ts)
        q = (f"SELECT symbol, AVG(close*volume) AS adv FROM {table} "
             f"WHERE {' AND '.join(where)} GROUP BY symbol ORDER BY adv DESC")
        rows = conn.execute(q, params).fetchall()
    finally:
        conn.close()

    black = _load_tradfi_blacklist()
    spot_od = _load_spot_onboard() if require_spot else None
    out: list[str] = []
    for s, adv in rows:
        if s in black:
            continue
        # point-in-time 现货判断（替代旧静态 no_spot 名单）：t 时刻无现货=套利锚缺失，剔除
        if require_spot and spot_od and not has_spot_at(s, end_ts, spot_od):
            continue
        if min_adv is not None and (adv or 0) < min_adv:
            break  # 已按成交额降序，后续只会更小
        out.append(s)
        if len(out) >= top_n:
            break
    return out


def latest_open_time(interval: str = "1h") -> int:
    """该粒度下全表最新的 open_time（unix 秒），用于数据新鲜度检查。0 = 无数据。"""
    table = _TABLES[interval]
    conn = _conn(table)
    if conn is None:
        return 0
    try:
        row = conn.execute(f"SELECT MAX(open_time) FROM {table}").fetchone()
    finally:
        conn.close()
    return int(row[0]) if row and row[0] is not None else 0


def load_funding(symbols: list[str]) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """读 funding_hist，返回 {symbol: (funding_time数组, funding数组)}，均按 funding_time 升序。

    funding_time 为 unix 秒（该结算周期的结束时刻），funding 为该周期应付的费率。
    供 pin 等策略判断「针前最近一次已结算 funding」。
    """
    table = "funding_hist"
    conn = _conn(table)
    if conn is None:
        return {}
    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    try:
        for sym in symbols:
            df = pd.read_sql_query(
                "SELECT funding_time, funding FROM funding_hist WHERE symbol=? ORDER BY funding_time",
                conn, params=(sym,),
            )
            if not df.empty:
                out[sym] = (
                    df["funding_time"].to_numpy(dtype=np.int64),
                    df["funding"].to_numpy(dtype=float),
                )
    finally:
        conn.close()
    return out


def prev_funding(funding_map: dict, symbol: str, ts: int) -> float | None:
    """symbol 在 ts 时刻之前最近一次已结算的 funding rate；无数据则 None。

    无前视：只取 funding_time <= ts 的最近一条（已结算、当时可知）。
    """
    arr = funding_map.get(symbol)
    if arr is None:
        return None
    t, f = arr
    idx = int(np.searchsorted(t, ts, side="right")) - 1
    if idx < 0:
        return None
    return float(f[idx])


def load_trend_highs(symbols: list[str], start: int, end: int,
                     lookback_h: int = 90 * 24) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """算「过去 lookback_h 根 1h 的最高 high」滚动序列，用于趋势过滤（公允值方向）。

    返回 {symbol: (open_time(1h, unix秒), high_roll)}，high_roll[i] = 截至第 i 根为止、
    过去 lookback_h 根 1h high 的最大值（含当前根，事前：绝不包含未来）。不足 lookback_h
    根时为 NaN（趋势信号未就绪，调用方跳过）。

    依据（样本外验证）：距 90 天高点跌幅 ≤30% 的针，12h 中位收益显著为正、胜率过半；
    跌幅更深则回归效应消失甚至转负（公允值下降 = 趋势性下跌，非错杀）。
    """
    table = _TABLES["1h"]
    conn = _conn(table)
    if conn is None:
        return {}
    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    try:
        for sym in symbols:
            df = pd.read_sql_query(
                "SELECT open_time, high FROM klines WHERE symbol=? AND open_time>=? AND open_time<=? ORDER BY open_time",
                conn, params=(sym, start, end),
            )
            if df.empty:
                continue
            ot = df["open_time"].to_numpy(dtype=np.int64)
            high = df["high"].to_numpy(dtype=float)
            roll = pd.Series(high).rolling(lookback_h, min_periods=lookback_h).max().to_numpy()
            out[sym] = (ot, roll)
    finally:
        conn.close()
    return out


def trend_dd_at(trend_map: dict, sym: str, t: int, entry: float) -> float | None:
    """触发点 t 时，币价 entry 相对「过去 lookback_h 根 1h 最高 high」的跌幅（负值）。

    无前视：只取 open_time <= t 的滚动高点。无数据/未就绪返回 None（调用方跳过）。
    """
    a = trend_map.get(sym)
    if a is None:
        return None
    ot, roll = a
    hp = int(np.searchsorted(ot, t, side="right")) - 1
    if hp < 0:
        return None
    h = roll[hp]
    if not np.isfinite(h) or h <= 0 or entry <= 0:
        return None
    return entry / h - 1.0
