# -*- coding: utf-8 -*-
"""数据层：拉取历史 K 线 + 资金费率，本地缓存 CSV（避免重复请求）。

缓存策略：首次全量拉取；之后每次调用做「增量补拉」——只请求缓存最后时间到现在的缺口，
够了就直接返回。这样日常跑只发十几二十个请求，不会触发 Binance 限频（429）。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests

import sys
from pathlib import Path as _Path
_root = _Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
import config

# 全局节流：418 是 IP 信誉封禁（不是权重），突发连发几百请求会触发。压到每请求 ≥ 0.3s，
# 2 年回填（每币 ~15 请求 × 50 币）也不会连发触发。
_throttle_s = 0.3
_last_req = 0.0


def _throttle() -> None:
    global _last_req
    wait = _throttle_s - (time.time() - _last_req)
    if wait > 0:
        time.sleep(wait)
    _last_req = time.time()


def _get_json(path: str, params: dict[str, Any] | None = None, retries: int = 4) -> Any:
    params = params or {}
    proxies = {"http": config.PROXY, "https": config.PROXY} if config.PROXY else None
    url = f"{config.BASE_URL}{path}"
    backoff = 2.0
    for attempt in range(retries + 1):
        _throttle()
        try:
            resp = requests.get(url, params=params, timeout=30, proxies=proxies)
        except requests.RequestException:
            if attempt == retries:
                raise
            time.sleep(backoff * (attempt + 1))  # 网络闪断（国内直连/代理不稳）退避重试
            continue
        if resp.status_code in (429, 418):
            # 429 = 权重限频（每分钟重置）；418 = IP 被反爬封（降温以分钟计，不是秒）。
            # 都退避重试，别静默丢币 → 得到被污染的偏子集。
            if attempt < retries:
                if resp.status_code == 418:
                    # IP 信誉封禁：睡 3 分钟等解封再重试（短时封禁通常几分钟内解除）
                    time.sleep(180.0)
                else:
                    time.sleep(backoff * (attempt + 1) * 3)
                continue
        if resp.status_code >= 500 and attempt < retries:
            time.sleep(backoff * (attempt + 1))
            continue
        break
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("code", 0) < 0:
        raise RuntimeError(str(data))
    return data


def _cache_path(kind: str, symbol: str) -> Path:
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # 缓存名带上天数，改 DAYS 会自动重新下载，而不是复用旧的短历史
    return config.CACHE_DIR / f"{kind}_{config.DAYS}d_{symbol}.csv"


def _interval_ms() -> int:
    return int(pd.Timedelta(config.INTERVAL).total_seconds() * 1000)


def _download_klines(symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """从 Binance 分页拉 [start_ms, end_ms) 的 1h K 线，返回含 datetime 列的 DataFrame。"""
    interval_ms = _interval_ms()
    rows: list[dict] = []
    while start_ms < end_ms:
        data = _get_json("/fapi/v1/klines", {
            "symbol": symbol, "interval": config.INTERVAL,
            "startTime": start_ms, "limit": config.KLINE_LIMIT,
        })
        if not data:
            break
        for k in data:
            rows.append({
                "open_time": int(k[0]),
                "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]),
                "volume": float(k[5]),          # 基础币成交量
                "quote_volume": float(k[7]),    # 计价币成交额
                "trades": float(k[8]),          # 成交笔数
                "taker_buy_base": float(k[9]),  # 主动买入基础量
                "taker_buy_quote": float(k[10]),  # 主动买入计价额
            })
        last_ts = int(data[-1][0])
        if last_ts + interval_ms <= start_ms:  # 无进展，防死循环
            break
        start_ms = last_ts + interval_ms
        time.sleep(0.15)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).drop_duplicates("open_time").sort_values("open_time")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df


def _save_klines(df, path) -> None:
    out = df.copy()
    out["open_time"] = out["open_time"].astype("int64") // 10**9  # 存 unix 秒，避免读回字符串解析
    out.to_csv(path, index=False)


def fetch_klines(symbol: str, force: bool = False) -> pd.DataFrame:
    """1h K 线。有缓存→增量补拉缺口；force=True 或没缓存→全量重拉。"""
    path = _cache_path("klines", symbol)
    if path.exists() and not force:
        df = pd.read_csv(path)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="s", utc=True)
        now = pd.Timestamp.utcnow()
        if now - df["open_time"].max() <= pd.Timedelta(hours=1):
            return df  # 缓存足够新（最新 bar 已收盘或即将），直接返回
        start_ms = int((df["open_time"].max() + pd.Timedelta(hours=1)).timestamp() * 1000)
        end_ms = int(now.timestamp() * 1000)
        new = _download_klines(symbol, start_ms, end_ms)
        if not new.empty:
            df = pd.concat([df, new]).drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)
            _save_klines(df, path)
        return df

    start_ms = int((pd.Timestamp.utcnow() - pd.Timedelta(days=config.DAYS)).timestamp() * 1000)
    end_ms = int(pd.Timestamp.utcnow().timestamp() * 1000)
    df = _download_klines(symbol, start_ms, end_ms)
    if not df.empty:
        _save_klines(df, path)
    return df


def _download_funding(symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    rows: list[dict] = []
    cur = start_ms
    while cur < end_ms:
        data = _get_json("/fapi/v1/fundingRate", {
            "symbol": symbol, "startTime": cur, "limit": 1000,
        })
        if not data:
            break
        rows += [{"funding_time": int(r["fundingTime"]), "funding": float(r["fundingRate"])} for r in data]
        last = int(data[-1]["fundingTime"])
        if last <= cur:
            break
        cur = last + 1
        time.sleep(0.15)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).drop_duplicates("funding_time").sort_values("funding_time")
    df["funding_time"] = pd.to_datetime(df["funding_time"], unit="ms", utc=True)
    return df


def _save_funding(df, path) -> None:
    out = df.copy()
    out["funding_time"] = out["funding_time"].astype("int64") // 10**9
    out.to_csv(path, index=False)


def fetch_funding(symbol: str, force: bool = False) -> pd.DataFrame:
    """历史资金费率。有缓存→增量补拉；force=True 或没缓存→全量重拉。"""
    path = _cache_path("funding", symbol)
    if path.exists() and not force:
        df = pd.read_csv(path)
        df["funding_time"] = pd.to_datetime(df["funding_time"], unit="s", utc=True)
        now = pd.Timestamp.utcnow()
        if now - df["funding_time"].max() <= pd.Timedelta(hours=8):  # 8h 一结算
            return df
        start_ms = int((df["funding_time"].max() + pd.Timedelta(hours=8)).timestamp() * 1000)
        end_ms = int(now.timestamp() * 1000)
        new = _download_funding(symbol, start_ms, end_ms)
        if not new.empty:
            df = pd.concat([df, new]).drop_duplicates("funding_time").sort_values("funding_time").reset_index(drop=True)
            _save_funding(df, path)
        return df

    start_ms = int((pd.Timestamp.utcnow() - pd.Timedelta(days=config.DAYS)).timestamp() * 1000)
    end_ms = int(pd.Timestamp.utcnow().timestamp() * 1000)
    df = _download_funding(symbol, start_ms, end_ms)
    if not df.empty:
        _save_funding(df, path)
    return df


# ---------------------------------------------------------------- 微观结构（OI / 多空比 / 主动买卖）
# monitor 实时验证过有信号的三条线，历史由 fapi 数据端点提供、可回测。
_MICRO_SPEC = {
    "oi":       ("/futures/data/openInterestHist",            "sumOpenInterest"),
    "lsr":      ("/futures/data/globalLongShortAccountRatio", "longShortRatio"),
    "top_lsr":  ("/futures/data/topLongShortPositionRatio",   "longShortRatio"),
    "taker_bs": ("/futures/data/takerlongshortRatio",         "buySellRatio"),
}


def _download_micro(symbol: str, path: str, field: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    # 这些数据端点只保留最近 ~30 天（startTime 超过 30 天返回 400），窗口钳到最近 30 天。
    start_ms = max(start_ms, end_ms - 30 * 86400_000)
    rows: list[dict] = []
    cur_end = end_ms
    while cur_end > start_ms:
        data = _get_json(path, {"symbol": symbol, "period": "1h", "endTime": cur_end, "limit": 500})
        if not data:
            break
        for r in data:
            v = r.get(field)
            if v is not None:
                rows.append({"ts": int(r["timestamp"]), "v": float(v)})
        oldest = min(int(r["timestamp"]) for r in data)
        if oldest <= start_ms or oldest >= cur_end:
            break
        cur_end = oldest - 1
        time.sleep(0.15)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).drop_duplicates("ts").sort_values("ts")
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def _save_micro(df, path) -> None:
    out = df.copy()
    out["ts"] = out["ts"].astype("int64") // 10**9
    out.to_csv(path, index=False)


def fetch_micro(symbol: str, kind: str, force: bool = False) -> pd.DataFrame:
    """历史 OI / 多空比 / 主动买卖（1h）。有缓存→增量补拉；无缓存→全量。

    冷门币可能无多空比数据（Binance 只对头部币提供）→ 返回空 DataFrame，由特征层补 NaN。
    """
    path_api, field = _MICRO_SPEC[kind]
    path = _cache_path(f"micro_{kind}", symbol)
    if path.exists() and not force:
        df = pd.read_csv(path)
        df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True)
        now = pd.Timestamp.utcnow()
        if now - df["ts"].max() <= pd.Timedelta(hours=1):
            return df
        start_ms = int((df["ts"].max() + pd.Timedelta(hours=1)).timestamp() * 1000)
        end_ms = int(now.timestamp() * 1000)
        new = _download_micro(symbol, path_api, field, start_ms, end_ms)
        if not new.empty:
            df = pd.concat([df, new]).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
            _save_micro(df, path)
        return df

    start_ms = int((pd.Timestamp.utcnow() - pd.Timedelta(days=config.DAYS)).timestamp() * 1000)
    end_ms = int(pd.Timestamp.utcnow().timestamp() * 1000)
    df = _download_micro(symbol, path_api, field, start_ms, end_ms)
    if not df.empty:
        _save_micro(df, path)
    return df


def fetch_micro_db(symbol: str, kind: str) -> pd.DataFrame:
    """从 monitor.db 读微观结构历史（合并 micro_1h 静态 30 天 + monitor 实时 oi/ratios）。

    这是「接通断裂」后的统一入口：monitor 实时积累的 oi/ratios 会随时间超过 Binance 的
    30 天上限，特征历史自动变长。返回 (ts, v) DataFrame，与 fetch_micro 同构，供
    _merge_micro 直接 merge_asof。无数据返回空 DataFrame，由调用方 fallback。

    kind: oi / lsr / top_lsr / taker_bs
    """
    import sqlite3
    from monitor import config as mcfg

    rt_map = {
        "oi": ("oi", "oi_base"),
        "lsr": ("ratios", "global_ls"),
        "top_lsr": ("ratios", "top_pos_ls"),
        "taker_bs": ("ratios", "taker_ls"),
    }
    table, col = rt_map[kind]
    frames: list[pd.DataFrame] = []
    try:
        conn = sqlite3.connect(mcfg.DB_PATH)
        # 1) micro_1h：归档的 1h 静态历史（model 自拉，30 天）
        rows = conn.execute(
            "SELECT ts, value FROM micro_1h WHERE symbol=? AND kind=? ORDER BY ts",
            (symbol, kind),
        ).fetchall()
        if rows:
            frames.append(pd.DataFrame(rows, columns=["ts", "v"]))
        # 2) 实时表：oi（30s）/ ratios（60s），monitor 常驻持续积累
        rows = conn.execute(
            f"SELECT ts, {col} FROM {table} WHERE symbol=? ORDER BY ts",
            (symbol,),
        ).fetchall()
        if rows:
            frames.append(pd.DataFrame(rows, columns=["ts", "v"]))
        conn.close()
    except Exception as exc:
        print(f"[data] fetch_micro_db {symbol}/{kind} 读取失败: {exc}")
        return pd.DataFrame()

    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df = df.dropna(subset=["v"]).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    return df