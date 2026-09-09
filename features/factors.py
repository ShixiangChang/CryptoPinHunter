# -*- coding: utf-8 -*-
"""特征工程：从 K 线 + 资金费率构造因子，并构造未来收益标签。

所有特征只用「过去 + 当前收盘」的数据，标签只用未来价格，避免前视泄漏。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import sys
from pathlib import Path as _Path
_root = _Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
import config
from . import data


# 微观结构特征列（允许 NaN：冷门币无多空比数据时缺失，LightGBM 原生处理 NaN）
MICRO_FEAT_COLS = (
    "oi_chg_1h", "oi_chg_24h", "oi_z_24h", "oi_price_corr24",
    "lsr_z_72h", "lsr_chg_24h", "top_lsr_z_72h", "top_lsr_chg_24h",
    "taker_bs_z_72h", "taker_bs_chg_24h",
)


def _fetch_micro_safe(symbol: str, kind: str):
    """拉微观结构历史：优先 monitor.db（长历史 + 未来自动变长），fallback Binance 自拉 30 天。
    失败 / 无数据返回 None → 特征 NaN，不整币丢弃。"""
    try:
        d = data.fetch_micro_db(symbol, kind)
        if d is not None and not d.empty:
            return d
        d = data.fetch_micro(symbol, kind)
        return d if not d.empty else None
    except Exception:
        return None


def _merge_micro(df: pd.DataFrame, symbol: str, use_micro: bool | None = None) -> pd.DataFrame:
    """合并 OI / 多空比 / 主动买卖历史，构造无量纲相对特征。

    这是 monitor 实时验证过有信号的三条线（OI 异动、多空比、资金流），历史可拉取、可回测。
    相对特征（pct_change / z-score）跨币可比；原始水平是币种身份代理，剔除。
    回测时可传 use_micro=False 跳过（Binance 只给 30 天历史，回测训练段几乎全 NaN，拉了也白拉）。
    """
    if not (config.USE_MICRO if use_micro is None else use_micro):
        for c in MICRO_FEAT_COLS:
            df[c] = np.nan
        return df
    src = df.sort_values("open_time")

    oi = _fetch_micro_safe(symbol, "oi")
    if oi is not None:
        src = pd.merge_asof(src, oi.rename(columns={"ts": "open_time", "v": "_oi"}),
                            on="open_time", direction="backward")
        src["oi_chg_1h"] = src["_oi"].pct_change(1)
        src["oi_chg_24h"] = src["_oi"].pct_change(24)
        src["oi_z_24h"] = (src["_oi"] - src["_oi"].rolling(24).mean()) / src["_oi"].rolling(24).std()
        src["oi_price_corr24"] = src["_oi"].pct_change(1).rolling(24).corr(src["ret_1h"])
        src = src.drop(columns=["_oi"])

    for kind, prefix in (("lsr", "lsr"), ("top_lsr", "top_lsr"), ("taker_bs", "taker_bs")):
        r = _fetch_micro_safe(symbol, kind)
        if r is not None:
            src = pd.merge_asof(src, r.rename(columns={"ts": "open_time", "v": f"_{prefix}"}),
                                on="open_time", direction="backward")
            src[f"{prefix}_z_72h"] = (
                src[f"_{prefix}"] - src[f"_{prefix}"].rolling(72).mean()
            ) / src[f"_{prefix}"].rolling(72).std()
            src[f"{prefix}_chg_24h"] = src[f"_{prefix}"].pct_change(24)
            src = src.drop(columns=[f"_{prefix}"])

    for c in MICRO_FEAT_COLS:
        if c not in src.columns:
            src[c] = np.nan
    return src


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def _macd_hist(close: pd.Series) -> pd.Series:
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    return macd - signal


def symbol_features(symbol: str, use_micro: bool | None = None) -> pd.DataFrame:
    """单个币种的因子 + 标签。返回列：open_time, symbol, 各因子, fwd_ret。"""
    k = data.fetch_klines(symbol)
    f = data.fetch_funding(symbol) if config.USE_FUNDING else pd.DataFrame()
    if len(k) < config.MIN_HISTORY_BARS:
        return pd.DataFrame()

    df = k.sort_values("open_time").reset_index(drop=True)
    close: pd.Series = df["close"]
    logc = np.log(close)

    # 动量（多周期对数收益）
    for n in (1, 4, 12, 24, 48):
        df[f"ret_{n}h"] = logc - logc.shift(n)

    # 波动率
    ret1 = df["ret_1h"]
    df["vol_24h"] = ret1.rolling(24).std()
    df["vol_168h"] = ret1.rolling(168).std()

    # ATR（归一化）
    prev_close = close.shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    df["atr14_norm"] = tr.rolling(14).mean() / close

    # 超买超卖 / 趋势
    df["rsi14"] = _rsi(close, 14)
    df["macd_hist_norm"] = _macd_hist(close) / close

    # 均线偏离
    df["close_sma20"] = np.log(close / close.rolling(20).mean())
    df["close_sma50"] = np.log(close / close.rolling(50).mean())
    df["close_sma200"] = np.log(close / close.rolling(200).mean())

    # 市场状态：趋势效率（|净位移|/路径总长，1=光滑趋势，0=来回震荡）
    df["trend_eff_48"] = df["ret_48h"].abs() / df["ret_1h"].abs().rolling(48).sum().replace(0, np.nan)
    # 趋势斜率：均线偏离的 20h 变化（方向 + 速度）
    df["sma_slope_20"] = df["close_sma20"] - df["close_sma20"].shift(20)

    # 布林带位置 / 带宽
    ma20 = close.rolling(20).mean()
    sd20 = close.rolling(20).std()
    df["bb_pctb"] = (close - (ma20 - 2 * sd20)) / (4 * sd20)
    df["bb_width"] = 4 * sd20 / ma20

    # 距滚动高低点
    df["dist_high48"] = np.log(close / df["high"].rolling(48).max())
    df["dist_low48"] = np.log(close / df["low"].rolling(48).min())

    # 成交量
    df["vol_z24"] = df["volume"] / df["volume"].rolling(24).mean() - 1

    # 主动买卖失衡（CVD）与主动买占比
    signed_q = 2 * df["taker_buy_quote"] - df["quote_volume"]
    df["cvd_24"] = signed_q.rolling(24).sum() / df["quote_volume"].rolling(24).sum()
    df["taker_ratio_24"] = df["taker_buy_quote"].rolling(24).sum() / df["quote_volume"].rolling(24).sum()

    # 周期特征（时间点）
    hour = df["open_time"].dt.hour
    dow = df["open_time"].dt.dayofweek
    df["hour_sin"] = np.sin(2 * np.pi * hour / 24)
    df["hour_cos"] = np.cos(2 * np.pi * hour / 24)
    df["dow_sin"] = np.sin(2 * np.pi * dow / 7)
    df["dow_cos"] = np.cos(2 * np.pi * dow / 7)

    # ---- qlib Alpha158 核心因子（无量纲、只用过去+当前，无前视） ----
    o, h, l = df["open"], df["high"], df["low"]
    c = df["close"]
    # (a) 单根 K 线形态（qlib KMID/KLEN/KUP/KLOW/KSFT 族）
    df["kchg"] = (c - o) / o                                    # 实体方向幅度
    df["klen"] = (h - l) / o                                    # 振幅
    df["kup"] = (h - pd.concat([o, c], axis=1).max(axis=1)) / o   # 上影
    df["klow"] = (pd.concat([o, c], axis=1).min(axis=1) - l) / o  # 下影
    df["ksft"] = (2 * c - h - l) / (h - l).replace(0, np.nan)     # 收盘在 bar 内位置（-1..1）
    # (b) 量价相关（qlib CORR 族）
    logvol = np.log(df["volume"] + 1.0)
    for w in (24, 120):
        df[f"corr_ret_{w}"] = df["ret_1h"].rolling(w).corr(logvol)
        df[f"corr_close_{w}"] = c.rolling(w).corr(logvol)
    # (c) 距新高/新低相对时间（qlib IMAX/IMIN 族；0=刚创新高/新低，1=很久前）
    for w in (48, 120):
        df[f"imax_{w}"] = (w - 1 - h.rolling(w).apply(np.argmax, raw=True)) / w
        df[f"imin_{w}"] = (w - 1 - l.rolling(w).apply(np.argmin, raw=True)) / w

    # 标签：未来多个周期（HORIZONS）的对数收益。horizon 由 sweep 统一决定。
    for h in config.HORIZONS:
        df[f"fwd_ret_{h}h"] = logc.shift(-h) - logc
    df["fwd_ret"] = df[f"fwd_ret_{config.HORIZON}h"]  # 兼容旧代码（此时 == fwd_ret_4h）

    # 资金费率（as-of 合并到每根 K 线）
    if config.USE_FUNDING and not f.empty:
        f = f.sort_values("funding_time").rename(columns={"funding_time": "open_time"})
        df = pd.merge_asof(df, f, on="open_time", direction="backward")
        df["funding_mean_24"] = df["funding"].rolling(3).mean()  # 3 个 8h 结算 ≈ 1 天
        df["funding_mean_72"] = df["funding"].rolling(9).mean()
        df["funding_change_24"] = df["funding"] - df["funding"].shift(3)   # 1 天费率变化
        df["funding_z_72"] = (df["funding"] - df["funding"].rolling(9).mean()) / df["funding"].rolling(9).std()  # 极端=拥挤
        df["funding_sign_3"] = np.sign(df["funding"]).rolling(3).sum()   # 连续同号天数

    # 微观结构（OI/多空比/主动买卖）：monitor 实时验证过的信号，历史可回测
    df = _merge_micro(df, symbol, use_micro)

    df["symbol"] = symbol
    # 核心价格/量特征必须非 NaN；微观结构列允许 NaN（冷门币无多空比数据）——
    # LightGBM 原生处理缺失，当「该币无此数据」的分支，而不是整币丢弃。
    label_cols = [f"fwd_ret_{h}h" for h in config.HORIZONS] + ["fwd_ret"]
    core_cols = [c for c in df.columns if c not in label_cols and c not in MICRO_FEAT_COLS]
    return df.replace([np.inf, -np.inf], np.nan).dropna(subset=core_cols).reset_index(drop=True)


def build_panel(progress: bool = True, symbols: list[str] | None = None,
                use_micro: bool | None = None, use_1m: bool = False) -> pd.DataFrame:
    """拼成面板，并加截面排名因子（同时间点内跨币种排名，无前视）。

    symbols 参数允许自定义池（如扩池探针用 probe_pool），默认用 config.UNIVERSE。
    use_micro=False 时跳过微观结构特征（回测闸门用：Binance 只给 30 天 OI/多空比历史，训练段几乎全 NaN）。
    use_1m=True 时合并 1m 精细因子（rv_24h/rv_7d 等，已验证 rv_7d_1m 是比 vol_168h 更强的波动率因子）；
        无 1m 数据的币 merge 后该列 NaN，LightGBM 原生处理。
    截面 z 化标签按「每个时间截面实际存在的币」做，新币上币前自然不参与。
    """
    universe = symbols if symbols is not None else config.UNIVERSE
    frames = []
    for i, sym in enumerate(universe, 1):
        if progress:
            print(f"[features] 处理 {i}/{len(universe)}: {sym}")
        try:
            df = symbol_features(sym, use_micro)
        except Exception as exc:
            print(f"[features] {sym} 跳过: {exc}")
            continue
        if not df.empty:
            frames.append(df)

    if not frames:
        raise RuntimeError("没有任何可用的币种数据，检查网络或 UNIVERSE。")

    panel = pd.concat(frames, ignore_index=True)
    for col in ("ret_24h", "ret_1h", "vol_168h"):
        panel[f"cs_rank_{col}"] = panel.groupby("open_time")[col].rank(pct=True)

    # 截面 z-score 化标签（qlib CSZScoreNorm 口径）：训练目标是「截面相对收益」，不是原始收益。
    # 不截面化，树模型会被暴涨暴跌样本/高波动日期主导（48h 线毛+1.01→净+0.16 的根因之一）。
    for h in config.HORIZONS:
        c = f"fwd_ret_{h}h"
        panel[f"{c}_cs"] = panel.groupby("open_time")[c].transform(
            lambda x: (x - x.mean()) / (x.std() + 1e-12))

    # 波动率调整标签（动作 B，GRJMOM N=1）：超额收益 ÷ 币自身波动率。
    # 消除「波动率偏差」：cs 的分母是全场共享的截面标准差，压不住单币自身波动；
    # 改成币自身波动率后，高波动币不再因波动大而标签天然极端——必须「跑赢幅度相对自身波动」足够大才入选。
    # va = (fwd_ret − 截面均值) / vol_24h，即「市场中性 + 风险调整」，与 cs 同宗不同尺。
    for h in config.HORIZONS:
        c = f"fwd_ret_{h}h"
        excess = panel[c] - panel.groupby("open_time")[c].transform("mean")
        panel[f"{c}_va"] = excess / panel["vol_24h"].clip(lower=1e-6)

    # 市场整体状态（截面，无前视）：全市场过去 24h 平均收益（beta）+ 分散度（分化/拥挤）
    mkt = panel.groupby("open_time")["ret_24h"].agg(["mean", "std"])
    mkt.columns = ["mkt_ret_24h", "mkt_disp_24h"]
    panel = panel.merge(mkt, on="open_time", how="left")

    # 1m 精细因子（可选）：从 klines_1m 构造、对齐 1h 整点、无前视。
    # 已验证 rv_7d_1m 是比 1h 版 vol_168h 更强的波动率因子（第一特征 + IC 翻倍）。
    if use_1m:
        from . import features_1m
        f1m = features_1m.build_1m_factors(universe, progress=progress)
        panel = features_1m.merge_1m(panel, f1m)
    return panel.reset_index(drop=True)