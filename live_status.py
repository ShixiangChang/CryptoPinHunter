# -*- coding: utf-8 -*-
"""live_status.py —— 实盘看板生成器。

读 live_state.json + live_trades 表 + 币安账户余额，生成 live_dashboard.html。
--loop 模式每 60 秒刷新一次（配合 serve.py 托管，浏览器刷新即可看最新）。

用法：
  python live_status.py           # 生成一次
  python live_status.py --loop    # 常驻，每 60s 刷新
"""
from __future__ import annotations

import argparse
import datetime
import html
import json
import sqlite3
import sys
import time
from pathlib import Path

_root = Path(__file__).resolve().parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
import config

STATE_PATH = config.ROOT / "data" / "live_state.json"
# serve.py 托管 data/ 目录、访问 / 重定向到 /live_dashboard.html；
# 所以 html 必须生成到 data/ 下，不能放 OUTPUT_DIR(data/model_out)，否则看板 404。
OUT_HTML = config.ROOT / "data" / "live_dashboard.html"

# 涨红跌绿（中国习惯）
RED = "#c0392b"
GREEN = "#1e8449"


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"initial_usdt": 0.0, "positions": {}, "trades": [], "history": []}


def load_trades(limit: int = 50) -> list[dict]:
    try:
        conn = sqlite3.connect(config.DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM live_pin_trades ORDER BY exit_time DESC, id DESC LIMIT ?",
            (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def try_account() -> tuple[float, float, float, float] | None:
    """连币安读账户权益（含浮亏）。返回 (equity, wallet, unreal, available) 或 None。

    P0-2/P0-4 修复：equity 用 totalMarginBalance（钱包+未实现盈亏），浮盈用
    totalUnrealizedProfit，不再是硬编码 0。
    """
    try:
        from monitor import credentials as _sec
        if config.USE_TESTNET:
            key = getattr(_sec, "BINANCE_TESTNET_API_KEY", "")
            sec = getattr(_sec, "BINANCE_TESTNET_API_SECRET", "")
        else:
            key = getattr(_sec, "BINANCE_FUTURES_API_KEY", "")
            sec = getattr(_sec, "BINANCE_FUTURES_API_SECRET", "")
        if not (key and sec):
            return None
        from engine.executor import BinanceFutures
        ex = BinanceFutures(key, sec, base_url=config.TRADE_BASE_URL)
        a = ex.account()
        equity = float(a.get("totalMarginBalance", 0))
        wallet = float(a.get("totalWalletBalance", 0))
        unreal = float(a.get("totalUnrealizedProfit", 0))
        avail = float(a.get("availableBalance", 0))
        return equity, wallet, unreal, avail
    except Exception:
        return None


def pct_color(v: float) -> str:
    return RED if v >= 0 else GREEN


def fmt_pct(v: float) -> str:
    return f"{'+' if v >= 0 else ''}{v:.2%}"


def fmt_usd(v: float) -> str:
    return f"${v:,.2f}"


def render(state: dict, trades: list[dict]) -> str:
    positions = state.get("positions", {})
    history = state.get("history", [])
    initial = state.get("initial_usdt", 0.0)

    # 已实现盈亏
    realized = sum(t.get("pnl", 0.0) for t in state.get("trades", []))
    # 账户权益（含浮亏）+ 总浮盈
    acc = try_account()
    if acc:
        equity, _wallet, unreal, avail = acc
    else:
        unreal = 0.0
        avail = max(initial + realized, 0.0)
        equity = avail + sum(p.get("notional", 0.0) for p in positions.values())

    total_pnl = equity - initial if initial > 0 else realized
    occupied = sum(p.get("weight", 0.0) for p in positions.values())

    # 回撤（从 history）
    max_dd = 0.0
    if history:
        eqs = [h[1] for h in history]
        peak = 0.0
        for e in eqs:
            peak = max(peak, e)
            if peak > 0:
                max_dd = min(max_dd, e / peak - 1.0)

    # 熔断状态
    halted = initial > 0 and equity < initial * config.LIVE_MAX_DRAWDOWN

    # 状态徽章
    dry = config.DRY_RUN
    mode_badge = ("DRY-RUN · 未下单" if dry else "实盘运行中") + (
        " · 已熔断" if halted else "")

    # ---- 净值曲线 SVG ----
    svg = _nav_svg(history)

    # ---- 持仓表 ----
    pos_rows = ""
    for sym, p in positions.items():
        age_h = (int(time.time()) - p.get("open_t", 0)) / 3600
        left_h = max(config.PIN_HOLD_MIN / 60 - age_h, 0)
        pos_rows += (
            f"<tr><td style='font-weight:500'>{html.escape(sym)}</td>"
            f"<td>${p.get('entry', 0):,.4f}</td>"
            f"<td>{p.get('qty', 0):.6g}</td>"
            f"<td>{fmt_usd(p.get('notional', 0))}</td>"
            f"<td>{p.get('weight', 0):.1%}</td>"
            f"<td>{left_h:.1f}h</td></tr>"
        )
    if not pos_rows:
        pos_rows = "<tr><td colspan='6' style='color:var(--color-text-tertiary)'>无持仓</td></tr>"

    # ---- 资金流水表 ----
    flow_rows = ""
    for t in trades:
        sym = html.escape(t.get("symbol", ""))
        ts = datetime.datetime.fromtimestamp(t.get("exit_time", 0)).strftime("%m-%d %H:%M")
        pnl = t.get("pnl", 0.0)
        pnl_pct = t.get("pnl_pct", 0.0)
        flow_rows += (
            f"<tr><td>{ts}</td><td>{sym}</td>"
            f"<td>${t.get('entry_price', 0):,.4f}→${t.get('exit_price', 0):,.4f}</td>"
            f"<td style='color:{pct_color(pnl)}'>{fmt_pct(pnl_pct)}</td>"
            f"<td style='color:{pct_color(pnl)}'>{fmt_usd(pnl)}</td>"
            f"<td>{html.escape(t.get('reason', ''))}</td></tr>"
        )
    if not flow_rows:
        flow_rows = "<tr><td colspan='6' style='color:var(--color-text-tertiary)'>暂无平仓记录</td></tr>"

    return _html_shell(mode_badge, equity, avail, occupied, total_pnl, realized, unreal,
                       max_dd, halted, len(positions), svg, pos_rows, flow_rows, len(trades))


def _nav_svg(history: list) -> str:
    if len(history) < 2:
        return "<div style='color:var(--color-text-tertiary);font-size:12px'>净值数据不足</div>"
    W, H = 640, 180
    pad = 30
    ts = [h[0] for h in history]
    eqs = [h[1] for h in history]
    t0, t1 = ts[0], ts[-1]
    lo, hi = min(eqs), max(eqs)
    if hi - lo < 1e-9:
        hi = lo + 1
    span = hi - lo
    pts = []
    for t, e in zip(ts, eqs):
        x = pad + (t - t0) / max(t1 - t0, 1) * (W - 2 * pad)
        y = H - pad - (e - lo) / span * (H - 2 * pad)
        pts.append(f"{x:.1f},{y:.1f}")
    # polyline 的 points 属性只接受纯坐标对，不能用 M/L path 语法（那是 <path d=...> 的）
    path = " ".join(pts)
    color = RED if eqs[-1] >= eqs[0] else GREEN
    return (
        f"<svg viewBox='0 0 {W} {H}' width='100%'>"
        f"<line x1='{pad}' y1='{H-pad}' x2='{W-pad}' y2='{H-pad}' stroke='#ccc' stroke-width='0.5'/>"
        f"<line x1='{pad}' y1='{pad}' x2='{pad}' y2='{H-pad}' stroke='#ccc' stroke-width='0.5'/>"
        f"<polyline points='{path}' fill='none' stroke='{color}' stroke-width='1.5'/>"
        f"<text x='{pad-4}' y='{pad+4}' font-size='10' fill='#999' text-anchor='end'>{hi:.2f}</text>"
        f"<text x='{pad-4}' y='{H-pad+4}' font-size='10' fill='#999' text-anchor='end'>{lo:.2f}</text>"
        f"<text x='{pad}' y='{H-pad+18}' font-size='11' fill='#999'>{datetime.datetime.fromtimestamp(t0).strftime('%m-%d')}</text>"
        f"<text x='{W-pad}' y='{H-pad+18}' font-size='11' fill='#999' text-anchor='end'>{datetime.datetime.fromtimestamp(t1).strftime('%m-%d')}</text>"
        f"</svg>"
    )


def _html_shell(mode_badge, equity, avail, occupied, total_pnl, realized, unreal,
                max_dd, halted, n_pos, svg, pos_rows, flow_rows, n_trades) -> str:
    badge_color = RED if "实盘" in mode_badge else "#8a6d1a"
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>实盘资金看板</title>
<style>
:root{{--bg:#f5f6f8;--card:#fff;--line:#e3e5e8;--txt:#1a1a1a;--muted:#888;--accent:#1f3a5f}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,'Segoe UI','PingFang SC',sans-serif;background:var(--bg);color:var(--txt);padding:20px}}
.wrap{{max-width:980px;margin:0 auto}}
.topbar{{background:var(--accent);color:#fff;border-radius:10px;padding:16px 20px;display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}}
.topbar h1{{font-size:16px;font-weight:600}}
.badge{{font-size:12px;background:rgba(255,255,255,.15);padding:4px 10px;border-radius:12px}}
.kpis{{display:grid;grid-template-columns:repeat(5,1fr);gap:1px;background:var(--line);border:1px solid var(--line);border-radius:8px;overflow:hidden;margin-bottom:16px}}
.kpi{{background:var(--card);padding:14px 16px}}
.kpi .l{{font-size:12px;color:var(--muted)}}
.kpi .v{{font-size:22px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 20px;margin-bottom:16px}}
.card h2{{font-size:14px;font-weight:600;margin-bottom:12px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{text-align:left;font-weight:500;color:var(--muted);padding:6px 8px;border-bottom:1px solid var(--line)}}
td{{padding:8px;border-bottom:1px solid #f0f1f3}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
.meter{{height:8px;background:#eee;border-radius:4px;overflow:hidden;margin-top:6px}}
.meter i{{display:block;height:100%;background:{RED}}}
.risk{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}}
.risk .k{{font-size:12px;color:var(--muted)}}
.risk .v{{font-size:18px;font-weight:600;margin-top:2px;font-variant-numeric:tabular-nums}}
.warn{{background:#fff8e6;border:1px solid #f0d88a;color:#8a6d1a;border-radius:8px;padding:10px 14px;font-size:13px;margin-bottom:16px}}
.foot{{text-align:center;color:var(--muted);font-size:11px;margin-top:8px}}
</style></head><body>
<div class="wrap">
<div class="topbar"><h1>Pin 插针策略 · 实盘资金看板</h1><span class="badge">{mode_badge}</span></div>
{warn_bar(halted)}
<div class="kpis">
<div class="kpi"><div class="l">账户权益</div><div class="v">{fmt_usd(equity)}</div></div>
<div class="kpi"><div class="l">可用余额</div><div class="v">{fmt_usd(avail)}</div></div>
<div class="kpi"><div class="l">累计盈亏</div><div class="v" style="color:{pct_color(total_pnl)}">{fmt_usd(total_pnl)}</div></div>
<div class="kpi"><div class="l">仓位占用</div><div class="v">{occupied:.1%}</div></div>
<div class="kpi"><div class="l">已结算笔数</div><div class="v">{n_trades}</div></div>
</div>
<div class="card"><h2>净值曲线</h2>{svg}</div>
<div class="card"><h2>实时持仓（{n_pos}）</h2>
<table><tr><th>币</th><th>开仓价</th><th>数量</th><th>名义</th><th>权重</th><th>剩余</th></tr>{pos_rows}</table></div>
<div class="card"><h2>资金流水（最近 {n_trades} 笔平仓）</h2>
<table><tr><th>时间</th><th>币</th><th>开→平</th><th>收益率</th><th>盈亏</th><th>原因</th></tr>{flow_rows}</table></div>
<div class="card"><h2>风险仪表</h2><div class="risk">
<div><div class="k">已实现盈亏</div><div class="v" style="color:{pct_color(realized)}">{fmt_usd(realized)}</div></div>
<div><div class="k">未实现盈亏</div><div class="v" style="color:{pct_color(unreal)}">{fmt_usd(unreal)}</div></div>
<div><div class="k">最大回撤</div><div class="v" style="color:{GREEN}">{max_dd:.2%}</div></div>
<div><div class="k">总仓位上限</div><div class="v">{config.LIVE_MAX_POSITION:.0%}</div></div>
<div><div class="k">熔断线</div><div class="v">{config.LIVE_MAX_DRAWDOWN:.0%}</div></div>
</div></div>
<div class="foot">数据源 live_state.json + live_pin_trades 表 · 刷新时间 {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
</div></body></html>"""


def warn_bar(halted: bool) -> str:
    if halted:
        return "<div class='warn'>已触发熔断：账户权益跌破初始资金的 70%，系统已暂停下单。</div>"
    if config.DRY_RUN:
        return "<div class='warn'>当前为 DRY-RUN 模式：只记录信号不下单。填入 API key 并关闭 DRY_RUN 后开始实盘。</div>"
    return ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    args = ap.parse_args()

    while True:
        state = load_state()
        trades = load_trades()
        OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
        OUT_HTML.write_text(render(state, trades), encoding="utf-8")
        print(f"[live_status] 已生成 {OUT_HTML.name} @ "
              f"{datetime.datetime.now().strftime('%H:%M:%S')}")
        if not args.loop:
            return
        time.sleep(60)


if __name__ == "__main__":
    main()
