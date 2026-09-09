# -*- coding: utf-8 -*-
"""live_trader.py —— 实盘主循环（唯一实盘入口）。

流程（每 POLL_SEC 一轮）：
  对账(启动) → 熔断检查 → 结算到期平仓 → 权益检查(含浮亏) → 跑信号 → 风控开仓 → 落库 → 推送。

安全设计（上线前审计，覆盖 7 项 P0）：
- 熔断真停：halted 写进 state，触发后只平仓不开仓，直到人工解除。
- 权益含浮亏：用 /fapi/v2/account totalMarginBalance（钱包+未实现盈亏），不是钱包余额。
- 孤儿仓 reconcile：启动时拉交易所真实持仓，本地缺失的仓认回，杜绝崩溃后无人管。
- 下单幂等：clientOrderId 唯一 + 超时查单，杜绝重复下单。
- 复利：下单名义锚定当前权益，与回测 nav 复利一致。
- 费率入账：平仓扣真实手续费 + 资金费率，账能对上。
- DRY_RUN：默认不下单；USE_TESTNET 下 DRY_RUN=0 是假钱实测，上线时 BINANCE_TESTNET=0。

用法：
  python live_trader.py --once     # 跑一轮（调试）
  python live_trader.py --loop     # 常驻循环
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

_root = Path(__file__).resolve().parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import config
from engine import data
from engine.executor import BinanceFutures
from engine.strategies import PinStrategy

STATE_PATH = config.ROOT / "data" / "live_state.json"
DB_PATH = config.DB_PATH


def _connect(db_path=DB_PATH):
    """统一数据库连接：WAL + busy_timeout，避免 live_feed/live_trader 并发写锁冲突。"""
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _write(sql: str, params: tuple) -> bool:
    """落库（带锁冲突重试）。失败不抛异常——下单是既成事实，落库失败绝不能中断交易流程。"""
    for attempt in range(3):
        conn = None
        try:
            conn = _connect()
            conn.execute(sql, params)
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            if attempt == 2:
                print(f"[live] 落库失败(放弃): {e} | {sql[:60]}")
                return False
            time.sleep(0.3 * (attempt + 1))
    return False


# ---------------------------------------------------------------- 状态
def _env_fingerprint() -> str:
    """环境指纹：纸面↔真钱、测试网↔主网切换时 state 必须重置，否则旧 initial_usdt/halted 污染新环境。"""
    return f"{config.USE_TESTNET}:{config.DRY_RUN}"


def load_state() -> dict:
    fp = _env_fingerprint()
    if STATE_PATH.exists():
        try:
            st = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if st.get("env_fingerprint") == fp:
                return st
            # 指纹不匹配（切了环境）：重置运行态，但保留已平仓 trades（可复盘资产）
            print(f"[live] 环境切换（指纹 {st.get('env_fingerprint')} -> {fp}），"
                  f"重置运行态，保留 {len(st.get('trades', []))} 笔历史交易")
            return {
                "started": int(time.time()),
                "initial_usdt": 0.0,
                "halted": False,
                "positions": {},
                "trades": st.get("trades", []),
                "history": [],
                "env_fingerprint": fp,
            }
        except Exception:
            pass
    return {
        "started": int(time.time()),
        "initial_usdt": 0.0,
        "halted": False,            # 熔断标志：True=只平仓不开仓
        "positions": {},            # sym -> {qty, entry, notional, weight, open_t, orphan?, client_id?}
        "trades": [],               # 已平仓流水
        "history": [],              # [[ts, equity], ...]
        "env_fingerprint": fp,
    }


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 落库
def _add_column_if_missing(conn: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db() -> None:
    conn = _connect()
    # 用独立表名 live_pin_*，避免与 monitor/db.py 旧多腿策略的 live_trades/decision_snapshots
    # 撞表（旧表结构不同，IF NOT EXISTS 不会重建，撞表会导致落库失败 + 重复平仓）。
    conn.execute("""CREATE TABLE IF NOT EXISTS live_pin_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT, side INTEGER,
        qty REAL, entry_price REAL, exit_price REAL,
        notional_usdt REAL, pnl REAL, pnl_pct REAL,
        open_time INTEGER, exit_time INTEGER, reason TEXT,
        commission REAL DEFAULT 0.0, funding REAL DEFAULT 0.0)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS live_pin_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER, strategy TEXT, symbol TEXT, action TEXT,
        weight REAL, depth REAL, entry_price REAL, notional_usdt REAL,
        detail TEXT)""")
    conn.commit()
    conn.close()


def log_trade(sym, side, qty, entry, exit_px, notional, pnl, pnl_pct,
              open_t, exit_t, reason, commission=0.0, funding=0.0):
    _write("INSERT INTO live_pin_trades (symbol, side, qty, entry_price, exit_price, "
           "notional_usdt, pnl, pnl_pct, open_time, exit_time, reason, commission, funding) "
           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
           (sym, side, qty, entry, exit_px, notional, pnl, pnl_pct,
            open_t, exit_t, reason, commission, funding))


def log_decision(ts, strategy, sym, action, weight, depth, entry, notional, detail):
    _write("INSERT INTO live_pin_decisions (ts, strategy, symbol, action, weight, "
           "depth, entry_price, notional_usdt, detail) VALUES (?,?,?,?,?,?,?,?,?)",
           (ts, strategy, sym, action, weight, depth, entry, notional, detail))


# ---------------------------------------------------------------- 实盘核心
class LiveTrader:
    def __init__(self, executor: BinanceFutures, strat: PinStrategy,
                 notifier, dry_run: bool):
        self.ex = executor
        self.strat = strat
        self.notifier = notifier
        self.dry_run = dry_run

    def equity(self, state: dict, dm: dict | None = None) -> float:
        """账户权益。实盘用交易所 totalMarginBalance（含浮亏）；dry_run 用纸面账。

        P0-2 修复：绝不能用 usdt_balance() 的钱包余额——它不含持仓浮亏，
        浮亏最需要熔断的时刻恰恰看不见。
        """
        if not self.dry_run:
            try:
                return self.ex.equity()
            except Exception:
                pass  # 查失败降级到本地账
        realized = sum(t.get("pnl", 0.0) for t in state["trades"])
        unreal = 0.0
        if dm:
            for sym, p in state["positions"].items():
                df = dm.get(sym)
                if df is not None and not df.empty:
                    unreal += p["notional"] * (float(df["close"].iloc[-1]) / p["entry"] - 1.0)
        base = state["initial_usdt"] if state["initial_usdt"] > 0 else config.LIVE_INITIAL_USDT
        return base + realized + unreal

    def reconcile(self, state: dict, now: int) -> list[str]:
        """启动对账：交易所真实持仓 vs 本地 state，本地缺失的仓认回（孤儿仓）。

        P0-3 修复：崩溃导致「下单成功但 state 没记」，重启后这些仓本地不知道，
        不认回就永远无人平仓。认回后 open_t 从 now 重新计时 12h，标记 orphan。
        """
        if self.dry_run:
            return []
        try:
            live = self.ex.positions()
        except Exception as e:
            return [f"对账失败（本轮跳过）: {e}"]
        msgs = []
        for p in live:
            amt = float(p.get("positionAmt", 0))
            if amt <= 0:
                continue  # 只做多，空仓忽略
            sym = p["symbol"]
            if sym in state["positions"]:
                continue
            entry = float(p.get("entryPrice", 0))
            notional = amt * entry
            weight = (notional / state["initial_usdt"]
                      if state["initial_usdt"] > 0 else 0.0)
            state["positions"][sym] = {
                "qty": amt, "entry": entry, "notional": notional,
                "weight": min(weight, config.LIVE_MAX_POSITION),
                "open_t": now, "orphan": True,
            }
            # 认回孤儿仓也补记 open 决策，保证复盘数据完整（open 决策不得缺失）
            log_decision(now, self.strat.name, sym, "open",
                         state["positions"][sym]["weight"], 0.0, entry, notional,
                         "孤儿仓认回(补记 open)")
            msgs.append(f"孤儿仓认回 {sym} qty={amt} 名义${notional:.0f} 仓位{weight:.1%}")
        return msgs

    def settle_expired(self, state: dict, now: int, dm: dict | None = None) -> list[str]:
        """平掉持有满 hold_hours 的仓，扣手续费 + 资金费率。"""
        msgs = []
        hold_sec = self.strat.hold_hours * 3600
        for sym in list(state["positions"].keys()):
            p = state["positions"][sym]
            if now - p["open_t"] < hold_sec:
                continue
            exit_px = self._close(sym, p, "expire", now, dm)
            if exit_px is None:
                msgs.append(f"平仓失败 {sym}（下轮重试）")
                continue
            gross = p["notional"] * (exit_px / p["entry"] - 1.0)
            # P0-7：扣真实手续费 + 资金费率
            if not self.dry_run:
                try:
                    comm, fund = self.ex.realized_costs(sym, p["open_t"], now)
                except Exception:
                    comm = p["notional"] * 2 * config.TAKER_FEE
                    fund = 0.0
            else:
                comm = p["notional"] * 2 * config.TAKER_FEE
                fund = 0.0
            net = gross - comm - fund
            pnl_pct = net / p["notional"] if p["notional"] > 0 else 0.0
            trade = {"symbol": sym, "side": 1, "qty": p["qty"], "entry": p["entry"],
                     "exit": exit_px, "notional": p["notional"], "pnl": net,
                     "pnl_pct": pnl_pct, "commission": comm, "funding": fund,
                     "open_t": p["open_t"], "exit_t": now, "reason": "expire"}
            state["trades"].append(trade)
            log_trade(sym, 1, p["qty"], p["entry"], exit_px, p["notional"], net,
                      pnl_pct, p["open_t"], now, "expire", comm, fund)
            log_decision(now, self.strat.name, sym, "close", p["weight"], 0.0,
                         exit_px, p["notional"],
                         f"到期平仓 pnl={pnl_pct:+.2%} 费{comm+fund:.4f}")
            del state["positions"][sym]
            msgs.append(f"平仓 {sym} @ {exit_px} 盈亏 {pnl_pct:+.2%}")
        return msgs

    def _close(self, sym: str, p: dict, reason: str, now: int, dm: dict | None = None):
        """平仓：dry_run 用最新 close 模拟，否则市价卖出。返回成交价，失败返回 None。"""
        if self.dry_run:
            df = dm.get(sym) if dm else None
            if df is None or df.empty:
                return None
            return float(df["close"].iloc[-1])
        try:
            cid = self.ex._gen_cid(sym)
            r = self.ex.close_long(sym, p["qty"], client_order_id=cid)
            return float(r.get("avgPrice") or r.get("price") or p["entry"])
        except Exception as e:
            print(f"[live] 平仓失败 {sym}: {e}")
            return None

    def open_position(self, state: dict, sig: dict, price: float, now: int,
                      equity: float) -> None:
        """按信号开仓。notional 锚定当前权益（复利），entry 用真实成交价。"""
        weight = sig["weight"]
        if config.LIVE_SINGLE_POSITION:
            weight = config.LIVE_POSITION_FRACTION   # 单仓满仓：一次押满（留手续费余量）
        notional = equity * weight          # P0-5：复利，不是 initial_usdt
        if notional <= 0:
            return
        entry = price
        qty = notional / price if price > 0 else 0.0
        cid = None
        if not self.dry_run:
            try:
                qty = self.ex.usdt_to_qty(sig["symbol"], notional, price)
                cid = self.ex._gen_cid(sig["symbol"])
                r = self.ex.open_long(sig["symbol"], qty,
                                      leverage=config.LIVE_LEVERAGE,
                                      client_order_id=cid)
                entry = float(r.get("avgPrice") or price)  # 真实成交价，非参考价
            except Exception as e:
                print(f"[live] 开仓失败 {sig['symbol']}: {e}")
                return
        state["positions"][sig["symbol"]] = {
            "qty": qty, "entry": entry, "notional": notional,
            "weight": weight, "open_t": now, "client_id": cid,
        }
        depth = sig.get("depth", 0.0)
        log_decision(now, self.strat.name, sig["symbol"], "open", weight, depth,
                     entry, notional, f"开仓 仓位{weight:.1%}")
        print(f"[live{'/DRY' if self.dry_run else ''}] 开仓 {sig['symbol']} "
              f"qty={qty} 名义${notional:.0f} 仓位{weight:.1%}")


# ---------------------------------------------------------------- 单轮
def run_once(trader: LiveTrader, state: dict, now: int | None = None) -> list[str]:
    now = now if now is not None else int(time.time())
    msgs: list[str] = []

    # 0) 熔断：已 halted 只平仓不开仓（P0-1）
    if state.get("halted"):
        msgs += trader.settle_expired(state, now)
        msgs.append("已熔断：只平仓不开仓，人工解除前不再进场")
        save_state(state)
        return msgs

    # 1) 拉数据（结算 + 信号 + 浮盈共用同一份）
    dm = data.load_klines(trader.strat.universe, interval="1m",
                          start=now - 3 * 24 * 3600)

    # 2) 结算到期（含手续费/费率）
    msgs += trader.settle_expired(state, now, dm)

    # 3) 权益检查（含浮亏，开仓前判断熔断）
    equity = trader.equity(state, dm)
    if state["initial_usdt"] <= 0:
        state["initial_usdt"] = equity
    if equity < state["initial_usdt"] * config.LIVE_MAX_DRAWDOWN:
        state["halted"] = True
        state["history"].append([now, round(equity, 4)])
        save_state(state)
        msgs.append(f"熔断触发：权益 ${equity:.0f} 跌破初始 ${state['initial_usdt']:.0f} "
                    f"的 {config.LIVE_MAX_DRAWDOWN:.0%}，已停开仓")
        return msgs

    # 4) 跑信号 + 风控开仓
    signals = trader.strat.generate_signals(dm, now)
    total_weight = sum(p["weight"] for p in state["positions"].values())
    opened = 0
    blocked = 0
    for sig in signals:
        if sig["symbol"] in state["positions"]:
            log_decision(now, trader.strat.name, sig["symbol"], "blocked",
                         sig["weight"], sig.get("depth", 0.0), 0.0, 0.0,
                         "已在持仓，跳过")
            continue
        if len(state["positions"]) >= config.LIVE_MAX_CONCURRENT:
            log_decision(now, trader.strat.name, sig["symbol"], "blocked",
                         sig["weight"], sig.get("depth", 0.0), 0.0, 0.0,
                         "单仓满仓，无法开新仓")
            blocked += 1
            break
        if total_weight + sig["weight"] > config.LIVE_MAX_POSITION:
            log_decision(now, trader.strat.name, sig["symbol"], "blocked",
                         sig["weight"], sig.get("depth", 0.0), 0.0, 0.0,
                         "总仓位超限，跳过")
            blocked += 1
            continue
        df = dm.get(sig["symbol"])
        if df is None or df.empty:
            continue
        price = float(df["close"].iloc[-1])
        trader.open_position(state, sig, price, now, equity)
        total_weight += sig["weight"]
        opened += 1
    if opened:
        msgs.append(f"新开仓 {opened} 笔，总仓位 {total_weight:.1%}")
    if blocked:
        msgs.append(f"满仓/持仓挡住 {blocked} 个信号（单仓模式正常现象）")

    # 5) 落 history
    state["history"].append([now, round(equity, 4)])
    save_state(state)
    return msgs


# ---------------------------------------------------------------- 入口
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    args = ap.parse_args()

    # 密钥选择：测试网（假钱）vs 主网（真钱）
    api_key = api_secret = ""
    try:
        from monitor import credentials as _sec
        if config.USE_TESTNET:
            api_key = getattr(_sec, "BINANCE_TESTNET_API_KEY", "")
            api_secret = getattr(_sec, "BINANCE_TESTNET_API_SECRET", "")
        else:
            api_key = (getattr(_sec, "BINANCE_FUTURES_API_KEY", "")
                       or os.environ.get("BINANCE_API_KEY", ""))
            api_secret = (getattr(_sec, "BINANCE_FUTURES_API_SECRET", "")
                          or os.environ.get("BINANCE_API_SECRET", ""))
    except ImportError:
        pass
    if not api_key:
        api_key = os.environ.get("BINANCE_API_KEY", "")
        api_secret = os.environ.get("BINANCE_API_SECRET", "")

    dry_run = config.DRY_RUN or not (api_key and api_secret)
    ex = BinanceFutures(api_key, api_secret, base_url=config.TRADE_BASE_URL)
    strat = PinStrategy()
    from monitor.notifier import Notifier
    trader = LiveTrader(ex, strat, Notifier(), dry_run)

    env_name = "测试网(假钱)" if config.USE_TESTNET else "主网(真钱)"
    pos_desc = ("单仓满仓" if config.LIVE_SINGLE_POSITION
                else f"单笔 {config.PIN_POS:.0%}~{config.PIN_POS * config.PIN_WEIGHT_CAP:.0%}")
    print(f"[live_trader] 环境={env_name} | 模式={'DRY-RUN(不下单)' if dry_run else '实盘下单'} | "
          f"池子 {len(strat.universe)} 币 | 杠杆 {config.LIVE_LEVERAGE}x | {pos_desc}")

    init_db()
    state = load_state()

    # 启动对账（孤儿仓认回）
    msgs = trader.reconcile(state, int(time.time()))

    if args.once:
        msgs += run_once(trader, state)
        print("\n".join(msgs) if msgs else "[live] 本轮无动作")
        return

    if args.loop:
        print(f"[live] 常驻循环，每 {config.POLL_SEC}s 一轮，Ctrl+C 退出")
        while True:
            try:
                round_msgs = run_once(trader, state)
                if round_msgs:
                    print("\n".join(round_msgs))
                    asyncio.run(trader.notifier.send("实盘决策", round_msgs))
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"[live] 本轮异常: {e}")
            time.sleep(config.POLL_SEC)
        return

    ap.print_help()


if __name__ == "__main__":
    main()
