# -*- coding: utf-8 -*-
"""scheduler.py —— 唯一入口。替代旧版三个各自为政的 --loop。

用法：
  python scheduler.py --backtest        # 回测各策略，落 backtest_{name}.json
  python scheduler.py --once            # 跑一次纸面（结算 → 换仓 → 归因 → 推送 → 落盘）
  python scheduler.py --loop            # 常驻：每 LOOP_MINUTES 跑一次纸面

与回测共用同一套 Strategy.generate_signals —— 纸面信号和回测信号是同一只狗。
结算规则（止损价、到期、双边成本）参数都来自 config，与回测引擎一致。
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import time

import config
from engine import data
from engine.attribution import summarize, should_stop
from engine.backtest import COST_SIDE, _normalize, run_backtest
from engine.state import StateManager
from engine.strategies import ALL


def build_strategies():
    return [S() for S in ALL]


# ---------------------------------------------------------------- 纸面单步
def _close(strat, st: dict, sym: str, exit_px: float, reason: str, now: int) -> None:
    """平仓：复利更新已实现净值，记交易流水。"""
    p = st["positions"].pop(sym)
    pnl = p["side"] * (exit_px / p["entry"] - 1.0) - 2 * COST_SIDE
    st["nav"] *= (1.0 + p["weight"] * pnl)
    st["trades"].append({
        "symbol": sym, "side": p["side"], "entry_time": p["open_t"], "entry": p["entry"],
        "exit_time": now, "exit": exit_px, "pnl": pnl, "weight": p["weight"], "reason": reason,
    })


def _display_nav(st: dict, dm: dict) -> float:
    """展示净值 = 已实现净值 × (1 + Σ 持仓权重 × 浮盈率)。"""
    realized = st.get("nav", 1.0)
    unreal = 0.0
    for sym, p in st["positions"].items():
        df = dm.get(sym)
        if df is None or df.empty:
            continue
        close = float(df["close"].iloc[-1])
        unreal += p["weight"] * p["side"] * (close / p["entry"] - 1.0)
    return realized * (1.0 + unreal)


def paper_step(strat, st: dict, dm: dict, now: int) -> None:
    """一只策略的一步：结算到期/止损仓 → 重算信号换仓。"""
    hold_sec = strat.hold_hours * 3600

    # 1) 结算：止损（做多 close 跌破 stop）+ 到期
    for sym in list(st["positions"].keys()):
        p = st["positions"][sym]
        df = dm.get(sym)
        if df is None or df.empty:
            continue
        low = float(df["low"].iloc[-1])
        high = float(df["high"].iloc[-1])
        close = float(df["close"].iloc[-1])
        hit_stop = False
        if p["stop"] is not None:
            hit_stop = (low <= p["stop"]) if p["side"] == 1 else (high >= p["stop"])
        if hit_stop:
            _close(strat, st, sym, p["stop"], "stop", now)
        elif now - p["open_t"] >= hold_sec:
            _close(strat, st, sym, close, "expire", now)

    # 2) 换仓：目标信号 vs 当前持仓
    signals = _normalize(strat.generate_signals(dm, now))
    target = {s["symbol"]: s for s in signals}
    for sym in list(st["positions"].keys()):
        if sym not in target:
            df = dm.get(sym)
            close = float(df["close"].iloc[-1]) if df is not None and not df.empty else st["positions"][sym]["entry"]
            _close(strat, st, sym, close, "rotate", now)
    for sym, s in target.items():
        if sym in st["positions"]:
            continue
        df = dm.get(sym)
        if df is None or df.empty:
            continue
        entry = s["entry"] if s["entry"] else float(df["close"].iloc[-1])
        if entry <= 0:
            continue
        st["positions"][sym] = {"side": s["side"], "weight": s["weight"],
                                "entry": entry, "stop": s["stop"], "open_t": now}


def paper_once(strategies, sm: StateManager, notifier, now: int | None = None) -> tuple[dict, list[str]]:
    """跑一轮纸面，返回 (state, 推送消息)。"""
    state = sm.load()
    now = now if now is not None else int(time.time())
    msgs: list[str] = []

    for strat in strategies:
        st = StateManager.strategy(state, strat.name)
        if st.get("status") == "stopped":
            continue
        # 纸面只加载决策所需的历史：1m 插针只需最近 3 天（15min 触发 + 12h 持有）
        if strat.interval == "1m":
            dm = data.load_klines(strat.universe, interval="1m", start=now - 3 * 24 * 3600)
        else:
            dm = data.load_klines(strat.universe, interval=strat.interval)
        paper_step(strat, st, dm, now)
        disp = _display_nav(st, dm)
        st.setdefault("history", []).append([now, round(disp, 6)])

        summ = summarize(st["trades"])
        if should_stop(st["trades"]):
            st["status"] = "stopped"
            msgs.append(f"【{strat.name}】判定淘汰：样本{summ['n']} 期望{summ['avg_pnl']:+.2%}")
        msgs.append(f"【{strat.name}】展示净值 {disp:.4f} | 持仓 {len(st['positions'])} | "
                    f"已结算 {summ['n']} 胜率 {summ['win_rate']:.0%} 期望 {summ['avg_pnl']:+.2%}")

    sm.save(state)
    return state, msgs


# ---------------------------------------------------------------- 回测
def _parse_dt(s: str) -> int:
    return int(datetime.datetime.strptime(s, "%Y-%m-%d")
               .replace(tzinfo=datetime.timezone.utc).timestamp())


def backtest_all(start: int, end: int | None = None) -> None:
    strategies = build_strategies()
    for strat in strategies:
        if end is None:
            end = data.latest_open_time(strat.interval) or start + 365 * 24 * 3600
        r = run_backtest(strat, start, end)
        r.save(config.OUTPUT_DIR / f"backtest_{strat.name}.json")
        print(f"[{strat.name}] 收益 {r.total_return:+.2%} | Sharpe {r.sharpe:.2f} | "
              f"回撤 {r.max_dd:.2%} | 交易 {r.n_trades} | 胜率 {r.win_rate:.1%} | "
              f"均笔 {r.avg_pnl:+.2%}")


# ---------------------------------------------------------------- 入口
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--start", default="2024-09-01")
    args = ap.parse_args()

    notifier = None
    from monitor.notifier import Notifier
    notifier = Notifier()

    if args.backtest:
        backtest_all(_parse_dt(args.start))
        return

    sm = StateManager()
    strategies = build_strategies()

    if args.once:
        _, msgs = paper_once(strategies, sm, notifier)
        if msgs:
            asyncio.run(notifier.send("量化决策", msgs))
        return

    if args.loop:
        print(f"[scheduler] 常驻循环，每 {config.LOOP_MINUTES} 分钟一轮，Ctrl+C 退出")
        while True:
            try:
                _, msgs = paper_once(strategies, sm, notifier)
                if msgs:
                    asyncio.run(notifier.send("量化决策", msgs))
                time.sleep(config.LOOP_MINUTES * 60)
            except KeyboardInterrupt:
                print("[scheduler] 退出")
                break
        return

    ap.print_help()


if __name__ == "__main__":
    main()
