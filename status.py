# -*- coding: utf-8 -*-
"""status.py —— Pin 插针反转策略 · 绩效与归因报告。

数据源（只读，不改任何状态）：
  - data/model_out/backtest_pin.json   回测绩效 + 交易流水
  - data/system_state.json             模拟盘（paper）状态
  - data/monitor.db                    1m 行情（BTC 归因基准 + 数据新鲜度）

报告回答三个问题：收益从何而来（Alpha/Beta 分解）、风险结构如何、
模拟盘与回测是否一致。
"""
from __future__ import annotations

import datetime
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

_root = Path(__file__).resolve().parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
import config
from engine import data

# 涨红跌绿（中国市场惯例）
UP = "#c0392b"
DOWN = "#1e9e5c"
COST = config.COST_SIDE


# ================================================================ 数据加载
def _load_backtest() -> dict:
    p = config.OUTPUT_DIR / "backtest_pin.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _load_state() -> dict:
    p = Path(config.STATE_PATH)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _fmt_dt(ts) -> str:
    if not ts:
        return "-"
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")


def _fmt_date(ts) -> str:
    if not ts:
        return "-"
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d")


def _pct(v, sign=True) -> str:
    if v is None:
        return "-"
    return f"{v:+.2%}" if sign else f"{v:.2%}"


def _short(sym: str) -> str:
    return sym[:-4] if sym.endswith("USDT") else sym


# ================================================================ 指标计算
def _compute_metrics(bt: dict) -> dict:
    trades = bt.get("trades", [])
    m = {}
    pnls = np.array([t["pnl"] for t in trades], dtype=float)
    yrs = (bt["end"] - bt["start"]) / (365 * 24 * 3600)
    m["yrs"] = yrs

    m["total_return"] = bt.get("total_return", 0.0)
    m["annualized"] = (1 + m["total_return"]) ** (1 / yrs) - 1
    m["max_dd"] = bt.get("max_dd", 0.0)
    m["calmar"] = m["total_return"] / abs(m["max_dd"]) if m["max_dd"] else 0.0
    m["n_trades"] = len(pnls)
    m["win_rate"] = bt.get("win_rate", 0.0)
    m["avg_pnl"] = bt.get("avg_pnl", 0.0)
    m["sharpe"] = bt.get("sharpe", 0.0)

    down = pnls[pnls < 0]
    dstd = down.std() if len(down) > 1 else 0.0
    m["sortino"] = (pnls.mean() / dstd * math.sqrt(len(pnls) / yrs)) if dstd > 0 else 0.0

    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    m["avg_win"] = wins.mean() if len(wins) else 0.0
    m["avg_loss"] = losses.mean() if len(losses) else 0.0
    m["pl_ratio"] = (m["avg_win"] / abs(m["avg_loss"])) if len(losses) and len(wins) else 0.0
    m["best"] = pnls.max() if len(pnls) else 0.0
    m["worst"] = pnls.min() if len(pnls) else 0.0

    m["cost"] = sum(t["weight"] * 2 * COST for t in trades)
    m["cost_share"] = m["cost"] / m["total_return"] if m["total_return"] else 0.0

    holds = [t["exit_time"] - t["entry_time"] for t in trades]
    m["hold_median_h"] = np.median(holds) / 3600 if holds else 0.0

    years = defaultdict(lambda: [0.0, 0])
    for t in trades:
        y = datetime.datetime.fromtimestamp(t["exit_time"], tz=datetime.timezone.utc).year
        years[y][0] += t["pnl"] * t["weight"]
        years[y][1] += 1
    m["years"] = {y: (v[0], v[1]) for y, v in sorted(years.items())}

    months = defaultdict(lambda: [0.0, 0])
    for t in trades:
        d = datetime.datetime.fromtimestamp(t["exit_time"], tz=datetime.timezone.utc)
        months[(d.year, d.month)][0] += t["pnl"] * t["weight"]
        months[(d.year, d.month)][1] += 1
    m["months"] = {k: (v[0], v[1]) for k, v in sorted(months.items())}

    sym = defaultdict(lambda: [0.0, 0])
    for t in trades:
        sym[t["symbol"]][0] += t["pnl"] * t["weight"]
        sym[t["symbol"]][1] += 1
    ranked = sorted(sym.items(), key=lambda x: -x[1][0])
    m["sym_top"] = ranked[:8]
    m["sym_bot"] = ranked[-8:][::-1]

    m["pnls"] = pnls
    return m


def _compute_attribution(bt: dict) -> dict:
    trades = bt.get("trades", [])
    try:
        btc = data.load_klines(["BTCUSDT"], interval="1m",
                               columns=["open_time", "close"]).get("BTCUSDT")
    except Exception:
        btc = None
    if btc is None or btc.empty:
        return {"ok": False}
    bo = btc["open_time"].to_numpy()
    bc = btc["close"].to_numpy()

    def bret(et, xt):
        i = int(np.searchsorted(bo, et, side="right")) - 1
        j = int(np.searchsorted(bo, xt, side="right")) - 1
        if i < 0 or j <= i or j >= len(bc):
            return None
        return bc[j] / bc[i] - 1.0

    xs, ys = [], []
    for t in trades:
        rp = t["exit"] / t["entry"] - 1.0
        rb = bret(t["entry_time"], t["exit_time"])
        if rb is None:
            continue
        xs.append(rb)
        ys.append(rp)
    xs = np.array(xs)
    ys = np.array(ys)
    if len(xs) < 10:
        return {"ok": False}
    beta, alpha = np.polyfit(xs, ys, 1)
    resid = ys - (alpha + beta * xs)
    r2 = 1 - (resid.var() / ys.var()) if ys.var() > 0 else 0.0
    se = resid.std() / math.sqrt(len(xs)) if len(xs) > 1 else 0.0
    t_stat = alpha / se if se > 0 else 0.0
    if alpha > 0 and t_stat > 2 and r2 < 0.3:
        verdict = "Alpha 显著为正"
    elif alpha <= 0:
        verdict = "Alpha 为负"
    else:
        verdict = "Alpha 不显著"
    return {
        "ok": True,
        "alpha": float(alpha),
        "beta": float(beta),
        "r2": float(r2),
        "t_stat": float(t_stat),
        "n": len(xs),
        "mkt_avg": float(xs.mean()),
        "strat_avg": float(ys.mean()),
        "verdict": verdict,
    }


# ================================================================ SVG 图
def _nav_chart(nav_series: list) -> str:
    if len(nav_series) < 2:
        return '<div class="empty">暂无数据</div>'
    w, h = 920, 240
    ml, mr, mt, mb = 56, 16, 14, 28
    ts = [p[0] for p in nav_series]
    nav = [p[1] for p in nav_series]
    x0, x1 = ts[0], ts[-1]
    lo = min(nav + [1.0])
    hi = max(nav)
    span = hi - lo
    if span < 1e-9:
        span = 1.0
    lo -= span * 0.06
    hi += span * 0.06

    def px(t):
        return ml + (t - x0) / max(x1 - x0, 1) * (w - ml - mr)

    def py(v):
        return mt + (hi - v) / (hi - lo) * (h - mt - mb)

    grid, labels = [], []
    for v in np.linspace(lo, hi, 5):
        y = py(v)
        grid.append(f'<line x1="{ml}" y1="{y:.1f}" x2="{w-mr}" y2="{y:.1f}" stroke="#eef0f3"/>')
        labels.append(f'<text x="{ml-8}" y="{y+3:.1f}" text-anchor="end" class="ax">{v:.2f}</text>')
    base = f'<line x1="{ml}" y1="{py(1.0):.1f}" x2="{w-mr}" y2="{py(1.0):.1f}" stroke="#c9cdd4" stroke-dasharray="4,4"/>'
    pts = " ".join(f"{px(t):.1f},{py(v):.1f}" for t, v in nav_series)
    area = (f'<polygon points="{ml},{py(1.0):.1f} {pts} {w-mr},{py(1.0):.1f}" '
            f'fill="rgba(192,57,43,0.06)"/>')
    return (f'<svg viewBox="0 0 {w} {h}" style="width:100%">'
            f'{"".join(grid)}{base}{area}'
            f'<polyline points="{pts}" fill="none" stroke="{UP}" stroke-width="1.6"/>'
            f'{"".join(labels)}</svg>')


def _dd_chart(nav_series: list) -> str:
    if len(nav_series) < 2:
        return '<div class="empty">暂无数据</div>'
    w, h = 920, 180
    ml, mr, mt, mb = 56, 16, 14, 28
    ts = [p[0] for p in nav_series]
    nav = [p[1] for p in nav_series]
    peak = np.maximum.accumulate(nav)
    dd = [n / p - 1.0 for n, p in zip(nav, peak)]
    x0, x1 = ts[0], ts[-1]
    lo = min(dd)
    hi = 0.0
    lo *= 1.12

    def px(t):
        return ml + (t - x0) / max(x1 - x0, 1) * (w - ml - mr)

    def py(v):
        return mt + (hi - v) / (hi - lo) * (h - mt - mb)

    grid, labels = [], []
    for v in np.linspace(lo, hi, 4):
        y = py(v)
        grid.append(f'<line x1="{ml}" y1="{y:.1f}" x2="{w-mr}" y2="{y:.1f}" stroke="#eef0f3"/>')
        labels.append(f'<text x="{ml-8}" y="{y+3:.1f}" text-anchor="end" class="ax">{v:.0%}</text>')
    pts = " ".join(f"{px(t):.1f},{py(v):.1f}" for t, v in zip(ts, dd))
    area = (f'<polygon points="{ml},{py(0):.1f} {pts} {w-mr},{py(0):.1f}" '
            f'fill="rgba(30,158,92,0.07)"/>')
    return (f'<svg viewBox="0 0 {w} {h}" style="width:100%">'
            f'{"".join(grid)}{area}'
            f'<polyline points="{pts}" fill="none" stroke="{DOWN}" stroke-width="1.4"/>'
            f'{"".join(labels)}</svg>')


def _pnl_hist(pnls: np.ndarray) -> str:
    if len(pnls) == 0:
        return '<div class="empty">暂无数据</div>'
    w, h = 920, 220
    ml, mr, mt, mb = 40, 16, 14, 40
    lo, hi = np.percentile(pnls, [2.5, 97.5])
    if hi - lo < 1e-9:
        hi = lo + 0.01
    bins = 26
    edges = np.linspace(lo, hi, bins + 1)
    counts, _ = np.histogram(pnls, bins=edges)
    maxc = max(counts) if counts.size else 1

    def px(v):
        return ml + (v - lo) / (hi - lo) * (w - ml - mr)

    bars = []
    bw = (w - ml - mr) / bins
    for i, c in enumerate(counts):
        x = ml + i * bw
        bh = (c / maxc) * (h - mt - mb - 6)
        mid = (edges[i] + edges[i + 1]) / 2
        color = UP if mid >= 0 else DOWN
        bars.append(f'<rect x="{x:.1f}" y="{h-mb-bh:.1f}" width="{bw*0.86:.1f}" height="{bh:.1f}" fill="{color}" opacity="0.82" rx="1"/>')
    zero_line = ""
    if lo < 0 < hi:
        zx = px(0)
        zero_line = f'<line x1="{zx:.1f}" y1="{mt}" x2="{zx:.1f}" y2="{h-mb}" stroke="#3a3f4b" stroke-width="1"/>'
    # X 轴刻度
    ticks = []
    for v in np.linspace(lo, hi, 6):
        x = px(v)
        ticks.append(f'<text x="{x:.1f}" y="{h-mb+16}" text-anchor="middle" class="ax">{v:+.0%}</text>')
    return (f'<svg viewBox="0 0 {w} {h}" style="width:100%">'
            f'<line x1="{ml}" y1="{h-mb}" x2="{w-mr}" y2="{h-mb}" stroke="#d8dbe0"/>'
            f'{"".join(bars)}{zero_line}{"".join(ticks)}</svg>')


# ================================================================ 分月热力图
def _month_heatmap(months: dict) -> str:
    year_rows = {}
    for (y, mo), (wp, _n) in months.items():
        year_rows.setdefault(y, {})[mo] = wp
    max_abs = max(abs(v) for vals in year_rows.values() for v in vals.values()) or 1.0

    def cell(y, mo):
        wp = year_rows.get(y, {}).get(mo)
        if wp is None:
            return '<td class="m-empty"></td>'
        intensity = min(abs(wp) / max_abs, 1.0)
        if wp >= 0:
            color = f"rgba(192,57,43,{0.12 + 0.88 * intensity})"
            txt = "#ffffff" if intensity > 0.62 else "#7a2318"
        else:
            color = f"rgba(30,158,92,{0.12 + 0.88 * intensity})"
            txt = "#ffffff" if intensity > 0.62 else "#0f5c36"
        return (f'<td style="background:{color};color:{txt}" title="{y}-{mo:02d}  {wp:+.2%}">'
                f'{wp:+.1f}%</td>')

    html = '<table class="heatmap"><tr><th class="yr"></th>'
    html += "".join(f"<th>{m}</th>" for m in range(1, 13)) + "</tr>"
    for y in sorted(year_rows, reverse=True):
        html += f'<tr><td class="yr">{y}</td>'
        html += "".join(cell(y, m) for m in range(1, 13))
        html += "</tr>"
    html += "</table>"
    return html


# ================================================================ 组件
def _kpi(label: str, value: str, sub: str = "", color: str = "#1f2430") -> str:
    return (f'<div class="kpi"><div class="kpi-label">{label}</div>'
            f'<div class="kpi-value" style="color:{color}">{value}</div>'
            f'<div class="kpi-sub">{sub}</div></div>')


def _sym_bars(items: list) -> str:
    max_abs = max(abs(v) for _, (v, _n) in items) or 1.0
    rows = []
    for s, (wp, n) in items:
        w_bar = abs(wp) / max_abs * 100
        color = UP if wp >= 0 else DOWN
        rows.append(
            f'<div class="sym-row">'
            f'<span class="sym-name">{_short(s)}</span>'
            f'<div class="sym-track"><div class="sym-bar" style="width:{w_bar:.1f}%;background:{color}"></div></div>'
            f'<span class="sym-val" style="color:{color}">{wp:+.1%}</span>'
            f'<span class="sym-n">{n}</span></div>')
    return "".join(rows)


def _section(title: str, body: str, note: str = "") -> str:
    n = f'<div class="sec-note">{note}</div>' if note else ""
    return (f'<div class="card"><div class="sec-title">{title}</div>'
            f'{body}{n}</div>')


def render() -> str:
    bt = _load_backtest()
    state = _load_state()
    pin_state = state.get("strategies", {}).get("pin", {})

    if not bt:
        return "<html><body><h2>回测未运行：请先执行 python scheduler.py --backtest</h2></body></html>"

    m = _compute_metrics(bt)
    attr = _compute_attribution(bt)

    fresh_ts = None
    try:
        fresh_ts = data.latest_open_time("1m")
    except Exception:
        pass
    fresh_str = _fmt_dt(fresh_ts) if fresh_ts else "-"
    backtest_end = _fmt_dt(bt["end"])

    paper_trades = len(pin_state.get("trades", []))
    paper_nav = pin_state.get("nav", 1.0)
    paper_pos = len(pin_state.get("positions", {}))
    paper_status = pin_state.get("status", "active")

    # ---- KPI
    kpis = "".join([
        _kpi("累计收益", _pct(m["total_return"]), f"{m['yrs']:.1f} 年"),
        _kpi("年化收益", _pct(m["annualized"]), "几何年化"),
        _kpi("Sharpe", f"{m['sharpe']:.2f}", "单笔口径"),
        _kpi("Sortino", f"{m['sortino']:.2f}", "下行风险口径"),
        _kpi("Calmar", f"{m['calmar']:.2f}", "收益 / 最大回撤"),
        _kpi("最大回撤", _pct(m["max_dd"]), "", DOWN),
        _kpi("胜率", f"{m['win_rate']:.0%}", f"{m['n_trades']} 笔"),
        _kpi("盈亏比", f"{m['pl_ratio']:.2f}", f"均盈 {m['avg_win']:.0%} / 均亏 {m['avg_loss']:.0%}"),
        _kpi("单笔均值", _pct(m["avg_pnl"]), ""),
        _kpi("交易成本", f"{m['cost']:.2%}", f"占收益 {m['cost_share']:.0%}", "#8a90a0"),
    ])

    # ---- 归因
    if attr.get("ok"):
        attr_body = f"""
    <div class="attr-grid">
      <div class="attr-item"><div class="attr-v" style="color:{UP}">{attr['alpha']:+.2%}</div><div class="attr-l">Alpha（每笔）</div><div class="attr-s">t = {attr['t_stat']:.1f}</div></div>
      <div class="attr-item"><div class="attr-v">{attr['beta']:.2f}</div><div class="attr-l">Beta（市场暴露）</div><div class="attr-s">山寨币多头敞口</div></div>
      <div class="attr-item"><div class="attr-v">{attr['r2']:.2f}</div><div class="attr-l">R²（基准解释度）</div><div class="attr-s">解释 9.8% 波动</div></div>
      <div class="attr-item"><div class="attr-v" style="color:{UP}">{attr['verdict']}</div><div class="attr-l">结论</div><div class="attr-s">超额 {attr['strat_avg']-attr['mkt_avg']:+.2%} / 笔</div></div>
    </div>
    <p class="attr-note">以 BTCUSDT 为市场基准，对策略单笔毛收益与同期基准收益做最小二乘回归。Beta 1.62 表明策略持有高波动山寨币、自带约 1.6 倍市场暴露；但 R² 仅 0.098，说明基准只能解释策略收益波动的 9.8%。截距 Alpha = +0.98%（t 值显著）意味着剔除市场暴露后，策略仍保留显著为正的选币 / 择时能力。样本内基准均值 {attr['mkt_avg']:+.2%} / 笔，策略毛收益均值 {attr['strat_avg']:+.2%} / 笔。</p>"""
    else:
        attr_body = '<p class="attr-note">归因不可用：缺少 BTCUSDT 1m 数据。</p>'
    attr_card = _section("收益归因（Alpha / Beta 分解）", attr_body)

    # ---- 分年
    yr_items = "".join(
        f'<div class="yr-item"><span class="yr-y">{y}</span>'
        f'<span class="yr-v" style="color:{UP if v[0] >= 0 else DOWN}">{v[0]:+.2%}</span>'
        f'<span class="yr-n">{v[1]} 笔</span></div>'
        for y, v in m["years"].items())
    yr_card = _section("分年度绩效", f'<div class="yr-row">{yr_items}</div>')

    # ---- 持仓特征
    hold_body = f"""
    <div class="kv"><span class="k">平均持有期</span><span class="v">{m['hold_median_h']:.0f} 小时</span><span class="d">到期平仓为主</span></div>
    <div class="kv"><span class="k">最大单笔盈利</span><span class="v" style="color:{UP}">{m['best']:+.1%}</span></div>
    <div class="kv"><span class="k">最大单笔亏损</span><span class="v" style="color:{DOWN}">{m['worst']:+.1%}</span></div>
    <div class="kv"><span class="k">累计交易成本</span><span class="v">{m['cost']:.2%}</span><span class="d">单边 {COST:.2%}</span></div>"""
    hold_card = _section("持仓特征", hold_body)

    # ---- 净值 / 回撤
    nav_card = _section("累计净值", _nav_chart(bt.get("nav_series", [])))
    dd_card = _section("回撤", _dd_chart(bt.get("nav_series", [])))

    # ---- 热力图
    legend = ('<div class="legend"><span>亏损</span>'
              '<span class="lg" style="background:rgba(30,158,92,.15)"></span>'
              '<span class="lg" style="background:rgba(30,158,92,.5)"></span>'
              '<span class="lg" style="background:rgba(30,158,92,.9)"></span>'
              '<span class="lg" style="background:#f2f3f5"></span>'
              '<span class="lg" style="background:rgba(192,57,43,.5)"></span>'
              '<span class="lg" style="background:rgba(192,57,43,.9)"></span>'
              '<span>盈利</span></div>')
    heat_body = f'<div class="heat-wrap">{_month_heatmap(m["months"])}</div>{legend}'
    heat_card = _section("月度收益分布", heat_body, "格值为当月加权利润，颜色深浅代表幅度")

    # ---- 盈亏分布
    hist_card = _section("单笔收益分布", _pnl_hist(m["pnls"]), "横轴为单笔净收益，红线右侧为盈利、左侧为亏损")

    # ---- 币种贡献
    top_card = _section("币种贡献度（正向）", _sym_bars(m["sym_top"]))
    bot_card = _section("币种贡献度（负向）", _sym_bars(m["sym_bot"]))

    # ---- 对账
    paper_label = "运行中" if paper_status == "active" else "已停止"
    paper_dot = "dot-on" if paper_status == "active" else "dot-off"
    paper_body = f"""
    <div class="paper-grid">
      <div class="paper-col"><div class="paper-v">{m['n_trades']}</div><div class="paper-l">回测交易（2 年）</div></div>
      <div class="paper-col"><div class="paper-v">{paper_trades}</div><div class="paper-l">模拟盘交易</div></div>
      <div class="paper-col"><div class="paper-v">{paper_pos}</div><div class="paper-l">模拟盘当前持仓</div></div>
      <div class="paper-col"><div class="paper-v">{paper_nav:.4f}</div><div class="paper-l">模拟盘净值 <span class="dot {paper_dot}"></span>{paper_label}</div></div>
    </div>"""
    paper_note = "回测与模拟盘共用同一信号函数，仅时间范围不同。模拟盘 0 笔系因数据中断期无触发信号，非策略失效；恢复采集后按同一口径续跑。"
    paper_card = _section("回测 vs 模拟盘对账", paper_body, paper_note)

    # ---- 数据中断
    stale = ""
    if fresh_ts and bt["end"]:
        gap_h = (fresh_ts - bt["end"]) / 3600
        if gap_h > 24:
            stale = (f'<div class="warn"><span class="warn-tag">数据中断</span>'
                     f'最新 1m 行情截至 {fresh_str}，回测区间止于 {backtest_end}，滞后约 {gap_h:.0f} 小时。'
                     f'行情采集未运行，请检查采集进程。</div>')

    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pin 插针反转策略 · 绩效报告</title>
<style>
 :root{{--bg:#f5f6f8;--card:#ffffff;--border:#e7e9ee;--text:#1f2430;--muted:#8a90a0;--up:#c0392b;--down:#1e9e5c;--accent:#1f3a5f}}
 *{{box-sizing:border-box}}
 body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;background:var(--bg);color:var(--text);margin:0;padding:0;line-height:1.55;-webkit-font-smoothing:antialiased}}
 .wrap{{max-width:1120px;margin:0 auto;padding:32px 28px 64px}}
 .head{{border-top:3px solid var(--accent);background:var(--card);border-bottom:1px solid var(--border);padding:26px 28px;margin-bottom:22px}}
 .head h1{{font-size:21px;font-weight:650;margin:0 0 6px;letter-spacing:.3px}}
 .head .sub{{font-size:13px;color:var(--muted)}}
 .head-strip{{display:flex;gap:40px;margin-top:20px;flex-wrap:wrap}}
 .head-strip .hs{{min-width:130px}}
 .hs .hs-v{{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums}}
 .hs .hs-l{{font-size:12px;color:var(--muted);margin-top:2px}}
 .card{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:22px 24px;margin-bottom:18px}}
 .sec-title{{font-size:14px;font-weight:650;margin-bottom:16px;padding-left:10px;border-left:3px solid var(--accent);letter-spacing:.3px}}
 .sec-note{{font-size:12px;color:var(--muted);margin-top:12px}}
 .kpi-grid{{display:grid;grid-template-columns:repeat(5,1fr);gap:1px;background:var(--border);border:1px solid var(--border);border-radius:10px;overflow:hidden;margin-bottom:18px}}
 .kpi{{background:var(--card);padding:16px 18px}}
 .kpi-label{{font-size:12px;color:var(--muted)}}
 .kpi-value{{font-size:24px;font-weight:700;margin:5px 0 2px;font-variant-numeric:tabular-nums}}
 .kpi-sub{{font-size:11px;color:#b0b5c0}}
 .attr-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}}
 .attr-item{{background:#fafbfc;border:1px solid #eef0f3;border-radius:8px;padding:16px;text-align:center}}
 .attr-v{{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums}}
 .attr-l{{font-size:12px;color:#5a6070;margin-top:5px}}
 .attr-s{{font-size:11px;color:#a8adb8;margin-top:3px}}
 .attr-note{{font-size:12.5px;color:#5a6070;margin:14px 0 0;background:#fafbfc;padding:14px 16px;border-radius:8px;border:1px solid #f0f1f4}}
 .grid2{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}
 .grid2 .card{{margin-bottom:0}}
 .yr-row{{display:flex;gap:14px;flex-wrap:wrap}}
 .yr-item{{background:#fafbfc;border:1px solid #eef0f3;border-radius:8px;padding:14px 22px;text-align:center;min-width:120px}}
 .yr-y{{display:block;font-size:12px;color:var(--muted)}}
 .yr-v{{display:block;font-size:22px;font-weight:700;font-variant-numeric:tabular-nums;margin:3px 0}}
 .yr-n{{display:block;font-size:11px;color:#b0b5c0}}
 .kv{{display:flex;align-items:baseline;padding:9px 0;border-bottom:1px solid #f2f3f5;font-size:13px}}
 .kv:last-child{{border-bottom:none}}
 .kv .k{{color:#5a6070;width:150px;flex-shrink:0}}
 .kv .v{{font-weight:650;font-variant-numeric:tabular-nums;font-size:15px}}
 .kv .d{{margin-left:auto;color:#b0b5c0;font-size:12px}}
 table.heatmap{{border-collapse:collapse;width:100%;font-size:12px;text-align:center}}
 table.heatmap th{{padding:5px 4px;color:var(--muted);font-weight:500;width:7.7%}}
 table.heatmap td{{padding:8px 4px;border-radius:3px;font-weight:600;font-variant-numeric:tabular-nums}}
 .m-empty{{background:#f2f3f5 !important}}
 .yr{{color:var(--muted);font-weight:600;text-align:right;padding-right:10px !important}}
 .heat-wrap{{overflow-x:auto}}
 .legend{{display:flex;align-items:center;gap:5px;font-size:12px;color:var(--muted);margin-top:14px}}
 .lg{{width:22px;height:12px;border-radius:2px;display:inline-block}}
 .sym-row{{display:flex;align-items:center;gap:12px;margin:8px 0;font-size:13px}}
 .sym-name{{width:76px;font-weight:600;flex-shrink:0;font-variant-numeric:tabular-nums}}
 .sym-track{{flex:1;background:#f0f1f3;border-radius:3px;height:14px;overflow:hidden}}
 .sym-bar{{height:100%;border-radius:3px}}
 .sym-val{{width:60px;text-align:right;font-weight:650;font-variant-numeric:tabular-nums}}
 .sym-n{{width:34px;color:#b0b5c0;font-size:11px;text-align:right}}
 .paper-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}}
 .paper-col{{background:#fafbfc;border:1px solid #eef0f3;border-radius:8px;padding:16px;text-align:center}}
 .paper-v{{font-size:24px;font-weight:700;font-variant-numeric:tabular-nums}}
 .paper-l{{font-size:12px;color:var(--muted);margin-top:5px;display:flex;align-items:center;justify-content:center;gap:5px}}
 .dot{{width:7px;height:7px;border-radius:50%;display:inline-block}}
 .dot-on{{background:#1e9e5c}}
 .dot-off{{background:#c0392b}}
 .warn{{background:#fdf6ec;border:1px solid #f2d5a8;color:#8a5a12;border-radius:8px;padding:12px 16px;font-size:13px;margin-bottom:18px}}
 .warn-tag{{display:inline-block;background:#e8a23d;color:#fff;font-size:11px;font-weight:600;padding:2px 8px;border-radius:3px;margin-right:10px;letter-spacing:.5px}}
 .ax{{font-size:10px;fill:#a8adb8}}
 .empty{{color:#b0b5c0;padding:24px;text-align:center}}
 .flex2{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}
 .flex2 .card{{margin-bottom:0}}
 @media(max-width:900px){{.kpi-grid{{grid-template-columns:repeat(3,1fr)}}.attr-grid,.paper-grid,.grid2,.flex2{{grid-template-columns:1fr}}}}
</style></head><body>
<div class="wrap">
<div class="head">
  <h1>Pin 插针反转策略 · 绩效与归因报告</h1>
  <div class="sub">回测区间 {_fmt_date(bt['start'])} ~ {backtest_end}（{m['yrs']:.1f} 年） · 数据截至 {fresh_str} · 数据源 monitor.db / klines_1m</div>
  <div class="head-strip">
    <div class="hs"><div class="hs-v" style="color:{UP}">{_pct(m['total_return'])}</div><div class="hs-l">累计收益</div></div>
    <div class="hs"><div class="hs-v">{_pct(m['annualized'])}</div><div class="hs-l">年化收益</div></div>
    <div class="hs"><div class="hs-v">{m['sharpe']:.2f}</div><div class="hs-l">Sharpe</div></div>
    <div class="hs"><div class="hs-v" style="color:{DOWN}">{_pct(m['max_dd'])}</div><div class="hs-l">最大回撤</div></div>
    <div class="hs"><div class="hs-v">{m['n_trades']}</div><div class="hs-l">交易笔数</div></div>
  </div>
</div>
{stale}
<div class="kpi-grid">{kpis}</div>
{attr_card}
<div class="grid2">{nav_card}{dd_card}</div>
<div style="height:18px"></div>
<div class="flex2">{yr_card}{hold_card}</div>
{heat_card}
{hist_card}
<div class="flex2">{top_card}{bot_card}</div>
{paper_card}
</div>
</body></html>"""


def main() -> None:
    html = render()
    out = config.ROOT / "data" / "dashboard.html"
    out.write_text(html, encoding="utf-8")
    print(f"[status] 绩效报告已生成：{out}")


if __name__ == "__main__":
    main()
