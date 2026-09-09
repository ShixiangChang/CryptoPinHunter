# -*- coding: utf-8 -*-
"""SQLite 存储：事件与历史数据落盘。

这些历史数据是 Phase 3 训练模型、回测策略的原料——从第一天就开始积累。
写入时做基础校验（NaN/inf、非正 OI 一律拒之门外），从源头保证原料干净。
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    ts INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    type TEXT NOT NULL,
    title TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    ts INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    price REAL,
    qty REAL,
    usd REAL,
    taker_buy INTEGER
);
CREATE TABLE IF NOT EXISTS mark_prices (
    ts INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    price REAL,
    funding REAL
);
CREATE TABLE IF NOT EXISTS oi (
    ts INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    oi_base REAL,
    notional REAL
);
CREATE TABLE IF NOT EXISTS ratios (
    ts INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    global_ls REAL,
    top_pos_ls REAL,
    taker_ls REAL
);
CREATE INDEX IF NOT EXISTS idx_events_symbol ON events(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_mark_symbol ON mark_prices(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_oi_symbol ON oi(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_ratios_symbol ON ratios(symbol, ts);
CREATE TABLE IF NOT EXISTS flow (
    ts INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    window INTEGER NOT NULL,
    net_flow REAL,
    total REAL,
    flow_ratio REAL,
    buy_pct REAL
);
CREATE INDEX IF NOT EXISTS idx_flow_symbol ON flow(symbol, window, ts);
CREATE TABLE IF NOT EXISTS ob_imbalance (
    ts INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    imbalance REAL,
    bid_qty REAL,
    ask_qty REAL
);
CREATE INDEX IF NOT EXISTS idx_ob_symbol ON ob_imbalance(symbol, ts);
CREATE TABLE IF NOT EXISTS onchain_txs (
    ts INTEGER NOT NULL,
    chain TEXT NOT NULL,
    token TEXT NOT NULL,
    from_addr TEXT,
    to_addr TEXT,
    usd REAL,
    txhash TEXT
);
CREATE INDEX IF NOT EXISTS idx_onchain_chain ON onchain_txs(chain, ts);
CREATE INDEX IF NOT EXISTS idx_onchain_hash ON onchain_txs(txhash);
CREATE TABLE IF NOT EXISTS erc20_flow (
    ts INTEGER NOT NULL,
    chain TEXT NOT NULL,
    token TEXT NOT NULL,
    from_addr TEXT,
    to_addr TEXT,
    amount REAL,
    usd REAL,
    flow TEXT,
    txhash TEXT
);
CREATE INDEX IF NOT EXISTS idx_erc20_chain ON erc20_flow(chain, ts);
CREATE TABLE IF NOT EXISTS data_quality (
    ts INTEGER NOT NULL,
    table_name TEXT NOT NULL,
    symbol TEXT,
    kind TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS data_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER,
    reason TEXT,
    first_seen INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gaps_table ON data_gaps(table_name, start_ts);
CREATE TABLE IF NOT EXISTS heartbeats (
    ts INTEGER NOT NULL,
    source TEXT NOT NULL,
    version TEXT,
    uptime_s REAL
);
CREATE TABLE IF NOT EXISTS predictions (
    ts INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    direction INTEGER NOT NULL,
    ref_price REAL,
    pred_z REAL,
    horizon_h INTEGER NOT NULL,
    target TEXT NOT NULL DEFAULT 'cs',
    settled INTEGER NOT NULL DEFAULT 0,
    ret REAL,
    hit INTEGER,
    features TEXT
);
CREATE INDEX IF NOT EXISTS idx_pred_settle ON predictions(settled, ts);
CREATE TABLE IF NOT EXISTS klines (
    symbol TEXT NOT NULL,
    open_time INTEGER NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    quote_volume REAL,
    trades REAL,
    taker_buy_base REAL,
    taker_buy_quote REAL,
    PRIMARY KEY (symbol, open_time)
);
CREATE TABLE IF NOT EXISTS funding_hist (
    symbol TEXT NOT NULL,
    funding_time INTEGER NOT NULL,
    funding REAL,
    PRIMARY KEY (symbol, funding_time)
);
CREATE TABLE IF NOT EXISTS micro_1h (
    symbol TEXT NOT NULL,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    value REAL,
    PRIMARY KEY (symbol, ts, kind)
);
CREATE TABLE IF NOT EXISTS token_map (
    symbol TEXT NOT NULL,
    base TEXT,
    kind TEXT,
    chain TEXT,
    contract_address TEXT,
    source TEXT,
    price_gap REAL,
    ts INTEGER,
    PRIMARY KEY (symbol)
);
CREATE TABLE IF NOT EXISTS liquidity (
    ts INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    kind TEXT,
    liquidity_usd REAL,
    volume_24h REAL,
    depth_2pct REAL,
    source TEXT
);
CREATE INDEX IF NOT EXISTS idx_liquidity_symbol ON liquidity(symbol, ts);
CREATE TABLE IF NOT EXISTS live_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side INTEGER NOT NULL,        -- +1 多 / -1 空
    entry_price REAL NOT NULL,
    leverage REAL NOT NULL,       -- 杠杆倍数（gross*2 → 2）
    stop_price REAL,              -- 止损价
    take_profit REAL,             -- 止盈价（NULL = 不设止盈）
    size REAL NOT NULL,           -- 仓位（占总资金比例，如 0.33）
    open_ts INTEGER NOT NULL,     -- 开仓时间戳
    status TEXT NOT NULL DEFAULT 'open',   -- open / closed
    close_price REAL,
    close_ts INTEGER,
    pnl_pct REAL,                 -- 已实现盈亏（% = 方向 × 涨跌幅 × 杠杆）
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_live_trades_symbol ON live_trades(symbol, open_ts);
"""


def _num(v: Any) -> float | None:
    """NaN / inf 一律转 None：脏值入库会带歪 AVG/STD 等统计，从源头掐掉。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


class MonitorDB:
    """最小封装：单连接 + 写锁，足够监控场景使用。"""

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """轻量列迁移：给老库补新增列（SQLite 的 CREATE IF NOT EXISTS 对已存在表不生效）。"""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(predictions)").fetchall()}
        if "target" not in cols:
            self._conn.execute("ALTER TABLE predictions ADD COLUMN target TEXT NOT NULL DEFAULT 'cs'")
        tcols = {r[1] for r in self._conn.execute("PRAGMA table_info(token_map)").fetchall()}
        if tcols and "price_gap" not in tcols:
            self._conn.execute("ALTER TABLE token_map ADD COLUMN price_gap REAL")

    def _insert(self, table: str, columns: list[str], values: list[Any]) -> None:
        sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' * len(values))})"
        with self._lock:
            self._conn.execute(sql, values)
            self._conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        """只读查询（供健康哨兵等使用），走同一把锁，保证读写安全。"""
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def event(self, symbol: str, type_: str, title: str, detail: dict) -> None:
        self._insert(
            "events",
            ["ts", "symbol", "type", "title", "detail"],
            [int(time.time()), symbol, type_, title, json.dumps(detail, ensure_ascii=False)],
        )

    def trade(self, symbol: str, price: float, qty: float, usd: float, taker_buy: bool) -> None:
        p, q, u = _num(price), _num(qty), _num(usd)
        if p is None or p <= 0 or q is None or q <= 0 or u is None or u <= 0:
            self.data_quality("trades", "range", symbol, f"price={price} qty={qty} usd={usd}")
            return
        self._insert(
            "trades",
            ["ts", "symbol", "price", "qty", "usd", "taker_buy"],
            [int(time.time()), symbol, p, q, u, int(taker_buy)],
        )

    def mark_price(self, symbol: str, price: float, funding: float) -> None:
        p = _num(price)
        f = _num(funding)
        if p is None or p <= 0:
            self.data_quality("mark_prices", "range", symbol, f"price={price}")
            return
        if f is not None and not (-0.05 <= f <= 0.05):  # 费率 ±5% 宽裕上界，越界=单位/字段错位
            self.data_quality("mark_prices", "range", symbol, f"funding={funding}")
            f = None  # 字段级净化：费率置空，价格照留，不连坐整行
        self._insert(
            "mark_prices",
            ["ts", "symbol", "price", "funding"],
            [int(time.time()), symbol, p, f],
        )

    def oi_point(self, symbol: str, oi_base: float, notional: float) -> None:
        ob, no = _num(oi_base), _num(notional)
        if ob is None or ob <= 0 or no is None or no <= 0:
            self.data_quality("oi", "range", symbol, f"oi_base={oi_base} notional={notional}")
            return
        self._insert(
            "oi",
            ["ts", "symbol", "oi_base", "notional"],
            [int(time.time()), symbol, ob, no],
        )

    def ratio_point(self, symbol: str, global_ls: float | None,
                    top_pos_ls: float | None, taker_ls: float | None) -> None:
        cols = ("global_ls", "top_pos_ls", "taker_ls")
        vals = [_num(global_ls), _num(top_pos_ls), _num(taker_ls)]
        for i, v in enumerate(vals):
            if v is not None and not (0.0 <= v <= 100.0):  # 多空比/买卖比下限 0，100 宽裕上界
                self.data_quality("ratios", "range", symbol, f"{cols[i]}={v}")
                vals[i] = None
        if all(v is None for v in vals):
            return
        self._insert(
            "ratios",
            ["ts", "symbol", "global_ls", "top_pos_ls", "taker_ls"],
            [int(time.time()), symbol, *vals],
        )

    def flow_point(self, symbol: str, window: int, net_flow: float, total: float,
                   flow_ratio: float, buy_pct: float) -> None:
        """落一条资金流净流入（每聚合窗口一条）。flow_ratio 已跨币种归一化。"""
        tot = _num(total)
        if tot is None or tot <= 0:
            self.data_quality("flow", "range", symbol, f"total={total}")
            return
        self._insert(
            "flow",
            ["ts", "symbol", "window", "net_flow", "total", "flow_ratio", "buy_pct"],
            [int(time.time()), symbol, window, _num(net_flow), tot, _num(flow_ratio), _num(buy_pct)],
        )

    def ob_point(self, symbol: str, imbalance: float, bid_qty: float, ask_qty: float) -> None:
        """落一条盘口失衡快照（只落聚合值，不落原始盘口，防库爆）。"""
        im = _num(imbalance)
        if im is None or not (0.0 <= im <= 1.0):
            self.data_quality("ob_imbalance", "range", symbol, f"imbalance={imbalance}")
            return
        self._insert(
            "ob_imbalance",
            ["ts", "symbol", "imbalance", "bid_qty", "ask_qty"],
            [int(time.time()), symbol, im, _num(bid_qty), _num(ask_qty)],
        )

    def onchain_tx(self, chain: str, token: str, from_addr: str, to_addr: str,
                   usd: float, txhash: str) -> None:
        u = _num(usd)
        if u is None or u <= 0:
            self.data_quality("onchain_txs", "range", f"{chain}.{token}", f"usd={usd}")
            return
        self._insert(
            "onchain_txs",
            ["ts", "chain", "token", "from_addr", "to_addr", "usd", "txhash"],
            [int(time.time()), chain, token, from_addr, to_addr, u, txhash],
        )

    def erc20_flow(self, chain: str, token: str, from_addr: str, to_addr: str,
                   amount: float, usd: float, flow: str, txhash: str) -> None:
        """ERC-20 代币交易所净流入流出落库（现货链上数据）。flow: in/out/other。"""
        a = _num(amount)
        u = _num(usd)
        if a is None or a <= 0:
            self.data_quality("erc20_flow", "range", f"{chain}.{token}", f"amount={amount}")
            return
        self._insert(
            "erc20_flow",
            ["ts", "chain", "token", "from_addr", "to_addr", "amount", "usd", "flow", "txhash"],
            [int(time.time()), chain, token, from_addr, to_addr, a, u, flow, txhash],
        )

    # ---------------- 数据可靠性台账 ----------------
    def data_quality(self, table_name: str, kind: str, symbol: str, detail: str) -> None:
        """拒收/净化的计次台账：脏数据不入库，但「该扔了多少」要留痕。

        静默丢弃是 DIY 系统最阴的坑——数据没了却没人知道。这里把每次拒收都记下来，
        哨兵/日报可汇总，让你知道数据源什么时候开始变脏。
        """
        self._insert(
            "data_quality",
            ["ts", "table_name", "symbol", "kind", "detail"],
            [int(time.time()), table_name, symbol, kind, detail],
        )

    def mark_gap(self, table_name: str, reason: str, start_ts: int | None = None) -> int:
        """开一条断档（同表已有未闭合 gap 则复用，不重复开）。返回 gap id。"""
        ts = int(time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM data_gaps WHERE table_name = ? AND end_ts IS NULL "
                "ORDER BY id DESC LIMIT 1",
                (table_name,),
            ).fetchone()
            if row is not None:
                return int(row[0])
            cur = self._conn.execute(
                "INSERT INTO data_gaps (table_name, start_ts, end_ts, reason, first_seen) "
                "VALUES (?, ?, NULL, ?, ?)",
                (table_name, start_ts if start_ts is not None else ts, reason, ts),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def close_gap(self, table_name: str) -> None:
        """恢复后闭合该表所有未闭合 gap（写 end_ts）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE data_gaps SET end_ts = ? WHERE table_name = ? AND end_ts IS NULL",
                (int(time.time()), table_name),
            )
            self._conn.commit()

    def heartbeat(self, source: str, version: str = "", uptime_s: float = 0.0) -> None:
        self._insert(
            "heartbeats",
            ["ts", "source", "version", "uptime_s"],
            [int(time.time()), source, version, uptime_s],
        )

    # ---------------- 决策对账（预测 → 到期结算命中/打脸） ----------------
    def insert_prediction(self, symbol: str, direction: int, ref_price: float,
                          pred_z: float, horizon_h: int, features: str | None = None,
                          target: str = "cs") -> None:
        """落一条决策预测。direction=+1 多 / -1 空；ref_price=预测时收盘价；pred_z=置信。
        horizon_h=预测周期；target=目标函数（cs 截面z / mn 市场中性 / cls 方向分类…）。
        features=预测那一刻的特征快照(JSON)，让对账样本同时可当训练样本喂回模型。
        """
        self._insert(
            "predictions",
            ["ts", "symbol", "direction", "ref_price", "pred_z", "horizon_h", "target", "features"],
            [int(time.time()), symbol, int(direction),
             _num(ref_price), _num(pred_z), int(horizon_h), target, features],
        )

    def open_predictions(self) -> list[tuple]:
        """所有未结算预测，返回 (rowid, ts, symbol, direction, ref_price, pred_z, horizon_h, target)。"""
        with self._lock:
            return self._conn.execute(
                "SELECT rowid, ts, symbol, direction, ref_price, pred_z, horizon_h, target "
                "FROM predictions WHERE settled = 0 ORDER BY ts",
            ).fetchall()

    def settle_prediction(self, rowid: int, ret: float, hit: int) -> None:
        """结算一条：写入实际收益 ret 与命中 hit（1 命中 / 0 打脸）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE predictions SET settled = 1, ret = ?, hit = ? WHERE rowid = ?",
                (_num(ret), hit, rowid),
            )
            self._conn.commit()

    def prediction_stats(self) -> tuple[int, int, float | None]:
        """(已结算数, 命中数, 平均单笔净盈亏)。无结算记录返回 (0, 0, None)。"""
        with self._lock:
            n, hits, avg = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(hit), 0), AVG(ret) FROM predictions WHERE settled = 1",
            ).fetchone()
        return int(n or 0), int(hits or 0), avg

    def prediction_report(self, z_threshold: float = 1.0) -> dict:
        """前向对账报告（净口径）：总笔数 / 命中率 / 累计净收益 / 最大回撤 / 期望值。

        只统计 |pred_z| >= z_threshold 的真实信号仓（= 交付给用户跟单的那批）；
        全池弱信号预测只作 rank-IC / 监督学习样本，不计入验收口径。
        ret 列已存「单笔净盈亏」（= 方向×市场收益 − 双边成本 − 方向×资金费，decision 结算时算好），
        这里直接累加即得真实累计净收益，不再乘方向、不再重复扣成本。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT direction, ret, hit FROM predictions "
                "WHERE settled = 1 AND ret IS NOT NULL AND ABS(pred_z) >= ? "
                "ORDER BY ts",
                (z_threshold,),
            ).fetchall()
        if not rows:
            return {"n": 0, "hits": 0, "win_rate": 0.0, "total_ret": 0.0,
                    "max_dd": 0.0, "expectancy": 0.0}
        n = len(rows)
        hits = sum(1 for _, _, h in rows if h)
        pnls = [r for _, r, _ in rows]  # ret 已是单笔净盈亏（含方向、成本、资金费）
        eq, peak, max_dd = 1.0, 1.0, 0.0
        for p in pnls:
            eq *= (1.0 + p)
            peak = max(peak, eq)
            max_dd = min(max_dd, eq / peak - 1.0)
        return {"n": n, "hits": hits, "win_rate": hits / n,
                "total_ret": eq - 1.0, "max_dd": max_dd,
                "expectancy": sum(pnls) / n}

    def prediction_report_grid(self, z_threshold: float = 1.0) -> dict:
        """多目标 × 多 horizon 的前向对账矩阵：每个 (target, horizon) 的命中率/期望值/样本数。

        吞吐量流水线的核心报告：让多个目标函数、多个周期并行对账，数据告诉我们哪个
        组合「有效」，而不是事先拍板选唯一。返回 {target: {horizon: {n, hits, win_rate, expectancy}}}。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT target, horizon_h, ret, hit FROM predictions "
                "WHERE settled = 1 AND ret IS NOT NULL AND ABS(pred_z) >= ? "
                "ORDER BY ts",
                (z_threshold,),
            ).fetchall()
        grid: dict = {}
        for target, horizon, ret, hit in rows:
            cell = grid.setdefault(target, {}).setdefault(horizon, {"n": 0, "hits": 0, "pnls": []})
            cell["n"] += 1
            cell["hits"] += int(hit or 0)
            cell["pnls"].append(ret)
        out: dict = {}
        for target, hs in grid.items():
            out[target] = {}
            for horizon, cell in hs.items():
                n, hits, pnls = cell["n"], cell["hits"], cell["pnls"]
                out[target][horizon] = {
                    "n": n, "hits": hits,
                    "win_rate": hits / n if n else 0.0,
                    "expectancy": sum(pnls) / n if n else 0.0,
                }
        return out

    def upsert_token_map(self, symbol: str, base: str, kind: str,
                         chain: str | None, contract_address: str | None, source: str,
                         price_gap: float | None = None) -> None:
        """建/更新 token 映射（symbol → 链上身份）。price_gap=标记价交叉验证的价差比，
        NULL=权威地址/CEX（无需交叉验证）；有值且越大 = 越存疑。"""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO token_map "
                "(symbol, base, kind, chain, contract_address, source, price_gap, ts) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (symbol, base, kind, chain, contract_address, source, _num(price_gap), int(time.time())),
            )
            self._conn.commit()

    def insert_liquidity(self, symbol: str, kind: str, liquidity_usd: float | None,
                         volume_24h: float | None, depth_2pct: float | None, source: str) -> None:
        """落一条流动性快照。"""
        self._insert(
            "liquidity",
            ["ts", "symbol", "kind", "liquidity_usd", "volume_24h", "depth_2pct", "source"],
            [int(time.time()), symbol, kind, _num(liquidity_usd), _num(volume_24h),
             _num(depth_2pct), source],
        )

    # ------------------------------------------------ 实盘交易台账（长期记录）
    def open_live_trade(self, symbol: str, side: int, entry_price: float,
                        leverage: float, size: float,
                        stop_price: float | None = None,
                        take_profit: float | None = None,
                        note: str | None = None) -> int:
        """开一笔实盘仓。side=+1多/-1空；size=仓位(占总资金比例)；返回交易 id。"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO live_trades (symbol, side, entry_price, leverage, size, "
                "stop_price, take_profit, open_ts, status, note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (symbol, int(side), _num(entry_price), _num(leverage), _num(size),
                 _num(stop_price), _num(take_profit), int(time.time()), "open", note),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def open_live_trades(self) -> list[tuple]:
        """所有持仓中的实盘仓 (id, symbol, side, entry_price, leverage, stop_price, take_profit, size, open_ts)。"""
        with self._lock:
            return self._conn.execute(
                "SELECT id, symbol, side, entry_price, leverage, stop_price, take_profit, "
                "size, open_ts FROM live_trades WHERE status='open' ORDER BY open_ts",
            ).fetchall()

    def close_live_trade(self, trade_id: int, close_price: float, note: str | None = None) -> None:
        """平仓：pnl_pct = 方向 × (close/entry − 1) × 杠杆（多头涨 = 正，空头跌 = 正）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT side, entry_price, leverage FROM live_trades WHERE id=?", (trade_id,),
            ).fetchone()
            if not row:
                return
            side, entry, lev = row
            pnl = side * (float(close_price) / entry - 1.0) * lev if entry else 0.0
            self._conn.execute(
                "UPDATE live_trades SET status='closed', close_price=?, close_ts=?, pnl_pct=?, note=? "
                "WHERE id=?",
                (_num(close_price), int(time.time()), _num(pnl), note, trade_id),
            )
            self._conn.commit()

    def live_trade_history(self) -> list[tuple]:
        """全部实盘记录 (id, symbol, side, entry, lev, stop, tp, size, open_ts, status, close, pnl)。"""
        with self._lock:
            return self._conn.execute(
                "SELECT id, symbol, side, entry_price, leverage, stop_price, take_profit, "
                "size, open_ts, status, close_price, pnl_pct FROM live_trades ORDER BY open_ts",
            ).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()