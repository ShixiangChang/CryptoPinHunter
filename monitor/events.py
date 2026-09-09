# -*- coding: utf-8 -*-
"""事件引擎：滚动窗口检测、动态监控池、告警触发。

事件类型：
- watch_enter  进入深度监控池（涨幅/资金流/爆仓/OI/费率任一触发）
- flow         资金流净流入（净主动买入占比，归一化，替代原单笔大单）
- liquidation  爆仓潮（相对该币 OI 占比）
- oi_spike     持仓量变化率异动
- funding      资金费率极值
- depth        盘口失衡（bid_imbalance 突破前兆）
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from typing import Any, Optional

from . import config
from .db import MonitorDB
from .notifier import Notifier


def _utc(ts: float) -> str:
    return time.strftime("%m-%d %H:%M:%S", time.gmtime(ts)) + " UTC"


class EventEngine:
    _COOLDOWNS = {
        "flow": config.FLOW_COOLDOWN_SEC,
        "liquidation": config.LIQ_COOLDOWN_SEC,
        "oi_spike": config.OI_COOLDOWN_SEC,
        "funding": config.FUNDING_COOLDOWN_SEC,
        "whale_transfer": config.WHALE_COOLDOWN_SEC,
        "depth": config.DEPTH_COOLDOWN_SEC,
        "watch_enter": 0,
    }

    def __init__(self, db: MonitorDB, notifier: Notifier):
        self.db = db
        self.notifier = notifier
        self.feed: Any = None  # main 里注入，避免循环依赖

        self.baseline: list[str] = []
        self.watchlist: dict[str, dict] = {}
        self.last_price: dict[str, float] = {}
        self._ticker_info: dict[str, tuple[float, float]] = {}  # sym -> (24h涨跌%, 24h成交额)

        self._liq: dict[str, deque] = defaultdict(deque)        # (ts, usd, side)
        self._oi_hist: dict[str, deque] = defaultdict(deque)    # (ts, notional)
        self._px_hist: dict[str, deque] = defaultdict(deque)    # (ts, price)
        self._taker: dict[str, deque] = defaultdict(deque)      # (ts, usd, taker_buy)
        self._last_alert: dict[tuple[str, str], float] = {}
        self._last_mark_db: dict[str, float] = {}
        self._last_oi_db: dict[str, float] = {}
        self._last_flow_flush: dict[str, float] = {}   # 每币上次净流入结算时间
        self._last_oi_notional: dict[str, float] = {}  # 每币最近 OI 名义价值（爆仓潮相对分母）
        self._flow_seq: dict[str, deque] = defaultdict(deque)  # 每币最近若干窗口方向序列
        self.depth_pool: list[str] = []              # L2 深度池：观察池里最热的 N 个
        self._ob_seq: dict[str, deque] = defaultdict(deque)  # 每币盘口失衡方向序列
        self._msg_buffer: list[tuple[str, str, list[str]]] = []
        self._flush_task: Optional[asyncio.Task] = None
        self._tasks: list[asyncio.Task] = []

    # ---------------- 生命周期 ----------------
    def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._flush_loop()))
        self._tasks.append(asyncio.create_task(self._maintenance_loop()))

    def stop(self) -> None:
        for t in self._tasks:
            t.cancel()

    # ---------------- 行情快照（每 15 分钟） ----------------
    def on_universe(self, rows: list[dict]) -> None:
        # L0 全市场快照：一次 ticker/24hr 拿到全部合约的涨跌幅/成交额
        for r in rows:
            sym = r["symbol"]
            self.last_price[sym] = float(r["lastPrice"])
            self._ticker_info[sym] = (float(r["priceChangePercent"]), float(r["quoteVolume"]))
        top = sorted(rows, key=lambda r: float(r["quoteVolume"]), reverse=True)
        self.baseline = [r["symbol"] for r in top[: config.BASELINE_TOP_N]]

        # L0→L1：扫描全市场异动（涨跌对称 + 成交额分桶），按 |涨跌幅| 强度排序截断
        hits: list[tuple[float, str, float]] = []
        for r in rows:
            sym = r["symbol"]
            change = float(r["priceChangePercent"])
            qv = float(r["quoteVolume"])
            for vol_min, chg_min in config.ENTRY_TIERS:
                if qv >= vol_min and abs(change) >= chg_min:
                    hits.append((abs(change), sym, change))
                    break
        hits.sort(key=lambda h: h[0], reverse=True)
        for _strength, sym, change in hits[: config.LOOKOUT_MAX]:
            self._add_to_watchlist(sym, f"24h {'涨' if change >= 0 else '跌'}幅 {change:+.1f}%",
                                   priority=1, sync=False, strength=_strength)
        self._sync_feed()

    # ---------------- 监控池 ----------------
    def _add_to_watchlist(self, sym: str, reason: str, priority: int = 2,
                          sync: bool = True, strength: float = 0.0) -> None:
        """加入/刷新观察池。priority=2 实时事件币，priority=1 扫描占位币。
        池满时踢掉最旧的扫描占位币，保住实时异动币的深度采集位。
        """
        now = time.time()
        entry = self.watchlist.get(sym)
        if entry is None:
            if len(self.watchlist) >= config.LOOKOUT_MAX:
                removable = [(s, v) for s, v in self.watchlist.items()
                             if v.get("priority", 2) < priority]
                if not removable:
                    return  # 全是实时事件币，放弃低优先级占位
                victim = min(removable, key=lambda sv: sv[1]["last_event"])[0]
                self.watchlist.pop(victim, None)
            self.watchlist[sym] = {"since": now, "last_event": now,
                                   "reasons": [reason], "priority": priority,
                                   "strength": strength}
            lines = [f"触发原因: {reason}"]
            info = self._ticker_info.get(sym)
            if info:
                change, qv = info
                lines.append(f"24h 涨跌 {change:+.1f}% | 24h 成交额 ${qv / 1e6:,.0f}M")
            px = self.last_price.get(sym)
            if px:
                lines.append(f"当前价格 {px:,.8g}")
            lines.append("已进入深度监控池：持续跟踪资金流、爆仓、持仓量与资金费率")
            self._fire("watch_enter", sym, "进入深度监控", lines)
            if sync:
                self._sync_feed()
        else:
            if reason not in entry["reasons"]:
                entry["reasons"].append(reason)
            entry["last_event"] = now
            entry["priority"] = max(entry.get("priority", 2), priority)

    def _heat(self, v: dict) -> float:
        # 占位币比强度（|涨跌幅|），事件币比新鲜度（最近活跃）；两者量纲不同但只在同优先级内比较
        return v.get("strength", 0.0) if v.get("priority", 2) == 1 else v["last_event"]

    def _sync_feed(self) -> None:
        if self.feed is None:
            return
        # L2 深度池：观察池里最热的 N 个（实时事件币优先、同层按热度降序）
        items = sorted(self.watchlist.items(),
                       key=lambda kv: (-kv[1].get("priority", 2), -self._heat(kv[1])))
        self.depth_pool = [s for s, _ in items[: config.DEPTH_TOP_N]]
        symbols = sorted(set(self.baseline) | set(self.watchlist))
        asyncio.create_task(self.feed.set_symbols(symbols))

    async def _maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = time.time()
            expired = [
                s for s, v in self.watchlist.items()
                if now - v["last_event"] > config.WATCHLIST_TTL_SEC
            ]
            for s in expired:
                self.watchlist.pop(s, None)
                print(f"[watch] {s} 移出深度监控池（{config.WATCHLIST_TTL_SEC // 3600}h 无新事件）")
            if expired:
                self._sync_feed()

    # ---------------- 事件检测 ----------------
    def on_aggtrade(self, sym: str, price: float, qty: float, usd: float, taker_buy: bool) -> None:
        now = time.time()
        self.last_price[sym] = price
        w = self._taker[sym]
        w.append((now, usd, taker_buy))
        cutoff = now - config.FLOW_WINDOW_SEC
        while w and w[0][0] < cutoff:
            w.popleft()

        # 明细仍落库（逐笔 >= 10 万，建模原料），但告警不再用单笔金额
        if usd >= config.LARGE_TRADE_DB_USD:
            self.db.trade(sym, price, qty, usd, taker_buy)

        # 每满一个窗口结算一次净流入；冷清币无成交自然不结算
        if now - self._last_flow_flush.get(sym, 0.0) >= config.FLOW_WINDOW_SEC:
            self._settle_flow(sym, now)

    def _settle_flow(self, sym: str, now: float) -> None:
        """把一个聚合窗口的逐笔成交折算成「净主动流入占比」，落库 + 判吸筹/派发。

        关键：flow_ratio = 净主动买入额 / 窗口成交额 ∈ [-1, 1]，只相对该币自己，
        所以 BTC 和山寨币用同一条判据可比——这正是「100 万对 BTC 是尘埃、对
        山寨币是地震」的解法：不看绝对额，看净买压占比。
        """
        window = config.FLOW_WINDOW_SEC
        w = self._taker[sym]
        if not w:
            return
        total = sum(u for _, u, _ in w)
        if total <= 0:
            return
        net = sum(u if b else -u for _, u, b in w)
        ratio = net / total
        buy_pct = sum(u for _, u, b in w if b) / total * 100.0
        self.db.flow_point(sym, window, net, total, ratio, buy_pct)

        q = self._flow_seq[sym]
        q.append(ratio >= 0)
        while len(q) > config.FLOW_CONSECUTIVE + 2:
            q.popleft()
        self._taker[sym].clear()
        self._last_flow_flush[sym] = now

        if not self._cooldown_ok(sym, "flow"):
            return
        if len(q) < config.FLOW_CONSECUTIVE:
            return
        if len(set(list(q)[-config.FLOW_CONSECUTIVE:])) != 1:
            return                        # 方向不连续，压单窗口噪声
        if abs(ratio) < config.FLOW_RATIO_THRESH:
            return
        _, qv = self._ticker_info.get(sym, (0.0, 0.0))
        avg_5m = (qv / 288.0) if qv else 0.0   # 24h 成交额 ÷ 288 个 5m 窗口
        if total < avg_5m * config.FLOW_TOTAL_MIN_RATIO:
            return                        # 窗口量相对该币太小，可信度不足

        direction = "持续吸筹（净主动买入）" if ratio >= 0 else "持续派发（净主动卖出）"
        lines = [
            f"近{config.FLOW_CONSECUTIVE}个{window // 60}分钟窗口净主动占比 {ratio:+.1%}",
            f"净额 ${net:+,.0f} / 成交额 ${total:,.0f}（主动买占比 {buy_pct:.0f}%）",
            f"解读: {direction}",
            f"时间 {_utc(now)}",
        ]
        self._add_to_watchlist(sym, f"{'吸筹' if ratio >= 0 else '派发'} {ratio:+.1%}")
        self._fire("flow", sym, direction, lines)

    def on_liquidation(self, sym: str, side: str, usd: float) -> None:
        now = time.time()
        w = self._liq[sym]
        w.append((now, usd, side))
        cutoff = now - config.LIQ_WINDOW_SEC
        while w and w[0][0] < cutoff:
            w.popleft()

        total = sum(u for _, u, _ in w)
        oi = self._last_oi_notional.get(sym, 0.0)
        if oi > 0:
            # 相对该币自身 OI：5 分钟爆仓额占 OI 的比重，跨币种可比
            if total / oi < config.LIQ_OI_RATIO and len(w) < config.LIQ_COUNT:
                return
        elif len(w) < config.LIQ_COUNT:
            return
        if not self._cooldown_ok(sym, "liquidation"):
            return

        # 强平买单 = 空头爆仓被强制买入；强平卖单 = 多头爆仓被强制卖出
        buys = sum(u for _, u, s in w if s == "BUY")
        sells = total - buys
        if buys > sells * 1.5:
            hint = "空头集中爆仓 → 抛压释放，短线偏多"
        elif sells > buys * 1.5:
            hint = "多头集中爆仓 → 踩踏风险，短线偏空"
        else:
            hint = "多空爆仓均衡，方向参考有限"
        oi_part = f"，占 OI {total / oi * 100:.1f}%" if oi > 0 else ""
        lines = [
            f"近{config.LIQ_WINDOW_SEC // 60}分钟累计爆仓 ${total:,.0f}（{len(w)} 笔{oi_part}）",
            f"空头爆仓(强平买入) ${buys:,.0f} | 多头爆仓(强平卖出) ${sells:,.0f}",
            f"解读: {hint}",
            f"时间 {_utc(now)}",
        ]
        self._add_to_watchlist(sym, f"爆仓潮占 OI {total / oi * 100:.1f}%" if oi > 0 else f"爆仓潮 ${total:,.0f}")
        self._fire("liquidation", sym, "爆仓潮", lines)

    def on_mark(self, sym: str, price: float, funding: float) -> None:
        now = time.time()
        self.last_price[sym] = price
        if now - self._last_mark_db.get(sym, 0.0) >= 60:
            self._last_mark_db[sym] = now
            self.db.mark_price(sym, price, funding)

        if funding and abs(funding) >= config.FUNDING_EXTREME and self._cooldown_ok(sym, "funding"):
            side_txt = "多头拥挤（多头付费给空头）" if funding > 0 else "空头拥挤（空头付费给多头）"
            lines = [
                f"当前资金费率 {funding * 100:.4f}% / 8h（基准 0.0100%）",
                f"解读: {side_txt}，费率越极端，反向修正风险越大",
                f"时间 {_utc(now)}",
            ]
            self._add_to_watchlist(sym, f"资金费率极值 {funding * 100:.3f}%")
            self._fire("funding", sym, "资金费率极值", lines)

    def on_oi(self, sym: str, oi_base: float) -> None:
        now = time.time()
        price = self.last_price.get(sym)
        if not price:
            return
        notional = oi_base * price
        oi_hist = self._oi_hist[sym]
        px_hist = self._px_hist[sym]
        oi_hist.append((now, notional))
        px_hist.append((now, price))
        cutoff = now - config.OI_WINDOW_SEC - 90
        while oi_hist and oi_hist[0][0] < cutoff:
            oi_hist.popleft()
        while px_hist and px_hist[0][0] < cutoff:
            px_hist.popleft()

        if now - self._last_oi_db.get(sym, 0.0) >= 60:
            self._last_oi_db[sym] = now
            self.db.oi_point(sym, oi_base, notional)
        self._last_oi_notional[sym] = notional  # 爆仓潮的相对分母

        if now - oi_hist[0][0] < config.OI_WINDOW_SEC:
            return  # 数据不足一个窗口，继续积累
        prev_notional = oi_hist[0][1]
        prev_price = px_hist[0][1]
        if prev_notional <= 0 or prev_price <= 0:
            return

        oi_pct = (notional - prev_notional) / prev_notional * 100
        px_pct = (price - prev_price) / prev_price * 100
        if abs(oi_pct) < config.OI_CHANGE_PCT or not self._cooldown_ok(sym, "oi_spike"):
            return

        if oi_pct > 0 and px_pct >= 0:
            hint = "价格上涨 + 持仓增加 → 新多资金入场，趋势可能延续"
        elif oi_pct > 0 and px_pct < 0:
            hint = "价格下跌 + 持仓增加 → 新空资金入场，下跌有承接"
        elif oi_pct < 0 and px_pct >= 0:
            hint = "价格上涨 + 持仓减少 → 空头回补推动，持续性存疑"
        else:
            hint = "价格下跌 + 持仓减少 → 多头止损离场"
        lines = [
            f"近{config.OI_WINDOW_SEC // 60}分钟持仓量 {oi_pct:+.1f}%（名义 ${notional:,.0f}）",
            f"同周期价格 {px_pct:+.2f}%",
            f"解读: {hint}",
            f"时间 {_utc(now)}",
        ]
        self._add_to_watchlist(sym, f"持仓量 {oi_pct:+.1f}%")
        self._fire("oi_spike", sym, "持仓量异动", lines)

    def on_depth(self, sym: str, imbalance: float, bid_qty: float, ask_qty: float) -> None:
        """盘口失衡信号：bid_imbalance 持续偏向一侧 = 突破/崩塌前兆。

        失衡本身是 0-1 相对量纲，跨币种天然可比（买墙占比，与币市值无关）。
        连续 DEPTH_CONFIRM 次同向才告警，压单张畸形盘口（大户挂单秒撤）噪声。
        """
        thresh = config.DEPTH_IMBALANCE_THRESH
        q = self._ob_seq[sym]
        if imbalance >= thresh:
            q.append(1)
        elif imbalance <= 1.0 - thresh:
            q.append(-1)
        else:
            q.clear()
            return
        while len(q) > config.DEPTH_CONFIRM:
            q.popleft()

        if len(q) < config.DEPTH_CONFIRM or len(set(q)) != 1:
            return
        if not self._cooldown_ok(sym, "depth"):
            return

        side = q[0]
        if side > 0:
            hint = "买盘墙厚（bid 挂单占比高）→ 突破前兆，短线偏多"
        else:
            hint = "卖盘墙厚（ask 挂单占比高）→ 抛压聚集，短线偏空"
        lines = [
            f"近{config.DEPTH_CONFIRM}次盘口快照 bid 占比 {imbalance:.1%}（{'偏多' if side > 0 else '偏空'}）",
            f"bid ${bid_qty:,.0f} / ask ${ask_qty:,.0f}",
            f"解读: {hint}",
            f"时间 {_utc(time.time())}",
        ]
        self.db.ob_point(sym, imbalance, bid_qty, ask_qty)
        self._add_to_watchlist(sym, f"盘口{'买' if side > 0 else '卖'}墙 {imbalance:.1%}")
        self._fire("depth", sym, "盘口失衡（突破前兆）", lines)

    def on_ratios(self, sym: str, global_ls: float | None,
                  top_pos_ls: float | None, taker_ls: float | None) -> None:
        if global_ls is None and top_pos_ls is None and taker_ls is None:
            return
        self.db.ratio_point(sym, global_ls, top_pos_ls, taker_ls)

    def on_chain_alert(self, type_: str, key: str, title: str, lines: list[str]) -> None:
        if not self._cooldown_ok(key, type_):
            return
        self._fire(type_, key, title, lines)

    # ---------------- 告警 ----------------
    def _cooldown_ok(self, sym: str, type_: str) -> bool:
        limit = self._COOLDOWNS.get(type_, 0)
        return time.time() - self._last_alert.get((sym, type_), 0.0) >= limit

    def _fire(self, type_: str, sym: str, title: str, lines: list[str]) -> None:
        now = time.time()
        self.db.event(sym, type_, title, {"time": _utc(now), "lines": lines})
        self._last_alert[(sym, type_)] = now
        self._msg_buffer.append((sym, title, lines))
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_loop())
        print(f"[event] {sym} {type_}: {title}")

    async def _flush_loop(self) -> None:
        """3 秒内同一币种的多条告警合并成一条发送，避免刷屏。"""
        while True:
            await asyncio.sleep(3)
            if not self._msg_buffer:
                continue
            batch, self._msg_buffer = self._msg_buffer, []
            merged: dict[str, list[str]] = {}
            titles: dict[str, str] = {}
            for sym, title, lines in batch:
                if sym in merged:
                    merged[sym].append("——")
                else:
                    merged[sym] = [f"【{title}】"]
                    titles[sym] = title
                merged[sym].extend(lines)
            for sym, lines in merged.items():
                await self.notifier.send(f"[{sym}] {titles[sym]}", lines)
