# -*- coding: utf-8 -*-
"""数据采集层。

- WebSocket：
    * {sym}@aggTrade       逐笔成交流 → 大单检测、主动买卖占比
    * {sym}@markPrice@1s   标记价格 + 资金费率（每秒）
    * !forceOrder@arr      全市场强平（爆仓）流
- REST 轮询：
    * /fapi/v1/ticker/24hr           每 15 分钟 → 基准池 + 涨幅触发
    * /fapi/v1/openInterest          每 30 秒/币种 → 持仓量
    * /futures/data/*Ratio           每 60 秒/币种 → 多空比、大户持仓比、主动买卖比
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

import aiohttp

from . import config

# IP 被 Binance 风控封禁（-1003 / 418）。区别于普通网络错误：触发后必须长退避，
# 不能立刻重试，否则只会把 IP 打得更死、封禁时间越拉越长。
class _IPBannedError(Exception):
    pass


# 一次 418/-1003 后，REST 轮询暂停的最短秒数（长退避，别硬刚风控）
_BAN_BACKOFF_SEC = 60

# ---------- 调参区（可根据需要调整）----------
# 看门狗：WS 数据超过多少秒未收到视为“断流”，触发 REST 降级（原 10s 改为 30s，避免误触）
WS_STALE_SEC = 30
# WS 恢复后需稳定多少秒才退出降级模式（原硬编码 30s，保持不变）
WS_RECOVERY_SEC = 30
# 看门狗检查间隔（原硬编码 10s，保持不变）
WATCHDOG_INTERVAL = 10
# ------------------------------------------


class DataFeed:
    def __init__(self, engine: Any):
        self.engine = engine
        self._symbols: set[str] = set()  # 当前 WS 订阅的小写 symbol
        self._running = False
        self._ws_task: Optional[asyncio.Task] = None
        self._bg_tasks: list[asyncio.Task] = []
        self._valid_cache: set[str] = set()
        self._valid_ts: float = 0.0
        # WS 降级模式（网络间歇性可达时的兜底）
        self._fallback_active = False
        self._fallback_tasks: list[asyncio.Task] = []
        self._ws_connect_time: float = 0.0
        self._ws_connected = False            # WS 当前是否已成功建立连接
        self._fb_last_id: dict[str, int] = {}  # 降级模式成交流去重
        self._last_ws_msg_ts = 0.0             # 最近一次成功收到 WS 数据的时间（看门狗用）
        self._banned_until = 0.0               # 遇 418/-1003 后 REST 静默退避到的时间戳

    # ---------------- 生命周期 ----------------
    async def start(self) -> None:
        self._running = True
        self._last_ws_msg_ts = time.time()  # 起跑前记基准，避免看门狗把冷启动误判成断流
        # 冷启动兜底：先用落盘缓存池订阅一批 symbol，别让 WS 空转等 exchangeInfo。
        # 这样即使 exchangeInfo 被 418，WS 也能先拿到实时流；ticker 成功后 set_symbols 会覆盖。
        cached = self._cached_symbols_from_db()
        if cached:
            self._symbols = {s.lower() for s in sorted(cached)}
        self._ws_task = asyncio.create_task(self._ws_loop())
        self._bg_tasks = [
            asyncio.create_task(self._ticker_loop()),
            asyncio.create_task(self._oi_loop()),
            asyncio.create_task(self._ratio_loop()),
            asyncio.create_task(self._depth_loop()),
            asyncio.create_task(self._watchdog_loop()),
        ]

    def stop(self) -> None:
        self._running = False
        for t in [self._ws_task, *self._bg_tasks, *self._fallback_tasks]:
            if t and not t.done():
                t.cancel()

    # ---------------- 订阅管理（动态监控池 → WS 重建） ----------------
    async def set_symbols(self, symbols: list[str]) -> None:
        new = {s.lower() for s in symbols}
        if new == self._symbols:
            return
        self._symbols = new
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
        self._ws_task = asyncio.create_task(self._ws_loop())
        print(f"[feed] 订阅更新: {len(new)} 个交易对")

    def _streams_url(self) -> str:
        streams = [f"{s}@aggTrade" for s in sorted(self._symbols)]
        streams += [f"{s}@markPrice@1s" for s in sorted(self._symbols)]
        streams.append("!forceOrder@arr")
        return f"{config.WS_URL}?streams={'/'.join(streams)}"

    # ---------------- WebSocket ----------------
    async def _ws_loop(self) -> None:
        backoff = 1
        consecutive_failures = 0
        while self._running:
            ok = False
            try:
                async with self._new_session() as session:
                    async with session.ws_connect(
                        self._streams_url(),
                        heartbeat=20,
                        timeout=aiohttp.ClientTimeout(total=None, sock_read=60),
                    ) as ws:
                        ok = True
                        consecutive_failures = 0
                        self._ws_connect_time = time.time()
                        self._ws_connected = True
                        backoff = 1
                        print(f"[feed] WS 已连接，订阅 {len(self._symbols)} 个交易对")
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                self._last_ws_msg_ts = time.time()
                                try:
                                    self._handle(json.loads(msg.data))
                                except Exception as exc:
                                    print(f"[feed] WS 消息处理异常: {exc}")
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
            except asyncio.CancelledError:
                return
            except Exception as exc:
                print(f"[feed] WS 异常: {exc}，{backoff}s 后重连")
            self._ws_connected = False  # 连接已结束（正常断开或异常），等待下次重连
            if not ok:
                consecutive_failures += 1
                if consecutive_failures >= 2:
                    await self._start_fallback()
            if not self._running:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    def _handle(self, payload: dict) -> None:
        data = payload.get("data") or {}
        etype = data.get("e")
        if etype == "aggTrade":
            sym = data["s"].upper()
            if sym.lower() not in self._symbols:
                return
            usd = float(data["p"]) * float(data["q"])
            self.engine.on_aggtrade(sym, float(data["p"]), float(data["q"]), usd, data["m"] is False)
        elif etype == "markPriceUpdate":
            sym = data["s"].upper()
            if sym.lower() not in self._symbols:
                return
            self.engine.on_mark(sym, float(data["p"]), float(data.get("r") or 0.0))
        elif etype == "forceOrder":
            o = data.get("o") or {}
            sym = o.get("s", "")
            if not sym:
                return
            usd = float(o.get("p") or 0) * float(o.get("q") or 0)
            self.engine.on_liquidation(sym.upper(), o.get("S", ""), usd)

    # ---------------- REST 公共 ----------------
    @staticmethod
    def _new_session() -> aiohttp.ClientSession:
        """创建带完整浏览器伪装头的 HTTP 会话，有效降低 418 风险。"""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
        }
        return aiohttp.ClientSession(
            headers=headers,
            trust_env=True,
            proxy=config.PROXY or None
        )

    @staticmethod
    async def _get_json(session: aiohttp.ClientSession, path: str,
                        params: Optional[dict] = None) -> Any:
        async with session.get(
            f"{config.BASE_URL}{path}",
            params=params,
            timeout=aiohttp.ClientTimeout(total=config.TIMEOUT),
        ) as resp:
            if resp.status == 418:
                # IP 信誉风控：强制退避，别再立刻重试把 IP 打得更死。
                # 418 的响应体里是 {"code":-1003, "msg":"...IP banned until <ts>..."}
                raise _IPBannedError(f"418 I'm a teapot: {path}")
            resp.raise_for_status()
            data = await resp.json(content_type=None)
            if isinstance(data, dict) and data.get("code", 0) < 0:
                if data.get("code") == -1003:
                    raise _IPBannedError(f"-1003 IP banned: {data.get('msg', '')[:80]}")
                raise RuntimeError(f"Binance API 错误: {data}")
            return data

    async def _valid_symbols(self, session: aiohttp.ClientSession) -> set[str]:
        if self._valid_cache and time.time() - self._valid_ts < 3600:
            return self._valid_cache
        try:
            data = await self._get_json(session, "/fapi/v1/exchangeInfo")
            self._valid_cache = {
                item["symbol"]
                for item in data["symbols"]
                if item["status"] == "TRADING"
                and item["contractType"] == "PERPETUAL"
                and item["quoteAsset"] == "USDT"
            }
            self._valid_ts = time.time()
        except Exception as exc:
            # exchangeInfo 最容易被 418/IP 风控打挂；失败时回退到落盘缓存池兜底，
            # 绝不让它卡死整个监控池初始化。缓存池为空才重新抛出。
            if not self._valid_cache:
                self._valid_cache = self._cached_symbols_from_db()
            print(f"[feed] exchangeInfo 获取失败（回退落盘缓存池 {len(self._valid_cache)} 个）: {exc}")
        return self._valid_cache

    def _cached_symbols_from_db(self) -> set[str]:
        """从 monitor.db 已采集的实时表里取 symbol 集合，作为 exchangeInfo 被 418 时的兜底池。"""
        symbols: set[str] = set()
        try:
            from .db import MonitorDB
            db = MonitorDB(config.DB_PATH)
            try:
                for table in ("mark_prices", "oi", "ratios", "events"):
                    try:
                        for r in db.query(f"SELECT DISTINCT symbol FROM {table} LIMIT 200"):
                            if r[0]:
                                symbols.add(r[0].upper())
                    except Exception:
                        pass
            finally:
                db.close()
        except Exception as exc:
            print(f"[feed] 兜底池读取失败: {exc}")
        return symbols

    # ---------------- 轮询任务 ----------------
    async def _ticker_loop(self) -> None:
        while self._running:
            try:
                async with self._new_session() as session:
                    valid = await self._valid_symbols(session)
                    tickers = await self._get_json(session, "/fapi/v1/ticker/24hr")
                rows = [t for t in tickers if t["symbol"] in valid]
                self.engine.on_universe(rows)
                print(f"[feed] 行情快照刷新: {len(rows)} 个交易对，基准池 {len(self.engine.baseline)} 个")
            except _IPBannedError as exc:
                print(f"[feed] 行情快照被 IP 风控拦下（{exc}），{_BAN_BACKOFF_SEC}s 后重试；请确认代理出口 IP 是否仍被封")
                await asyncio.sleep(_BAN_BACKOFF_SEC)
                continue
            except Exception as exc:
                print(f"[feed] 行情快照刷新失败: {exc}")
            await asyncio.sleep(config.BASELINE_REFRESH_SEC)

    async def _oi_loop(self) -> None:
        async with self._new_session() as session:
            while self._running:
                for sym in sorted(self._symbols):
                    if not self._running:
                        return
                    if time.time() < self._banned_until:
                        break  # 刚被 IP 风控拦下，整轮退避，别对着全池硬刚
                    try:
                        data = await self._get_json(
                            session, "/fapi/v1/openInterest", {"symbol": sym.upper()}
                        )
                        self.engine.on_oi(sym.upper(), float(data["openInterest"]))
                    except _IPBannedError:
                        self._banned_until = time.time() + _BAN_BACKOFF_SEC
                        break
                    except asyncio.CancelledError:
                        return
                    except Exception as exc:
                        print(f"[feed] OI 获取失败 {sym}: {exc}")
                    await asyncio.sleep(0.05)
                await asyncio.sleep(config.OI_POLL_SEC)

    async def _ratio_loop(self) -> None:
        async with self._new_session() as session:
            while self._running:
                for sym in sorted(self._symbols):
                    if not self._running:
                        return
                    if time.time() < self._banned_until:
                        break
                    try:
                        g = await self._ratio(session, "/futures/data/globalLongShortAccountRatio", sym, "longShortRatio")
                        t = await self._ratio(session, "/futures/data/topLongShortPositionRatio", sym, "longShortRatio")
                        k = await self._ratio(session, "/futures/data/takerlongshortRatio", sym, "buySellRatio")
                        self.engine.on_ratios(sym.upper(), g, t, k)
                    except _IPBannedError:
                        self._banned_until = time.time() + _BAN_BACKOFF_SEC
                        break
                    except asyncio.CancelledError:
                        return
                    except Exception as exc:
                        print(f"[feed] 多空比获取失败 {sym}: {exc}")
                    await asyncio.sleep(0.1)
                await asyncio.sleep(config.RATIO_POLL_SEC)

    @staticmethod
    async def _ratio(session: aiohttp.ClientSession, path: str, symbol: str, field: str) -> Optional[float]:
        rows = await DataFeed._get_json(
            session, path, {"symbol": symbol, "period": "5m", "limit": 1}
        )
        if not rows:
            return None
        val = rows[0].get(field)
        return float(val) if val is not None else None

    # ---------------- L2 深度池（REST 低频轮询盘口 → 失衡信号） ----------------
    async def _depth_loop(self) -> None:
        """对 depth_pool 里的最热 N 个币低频轮询盘口，算 bid_imbalance 给引擎。

        只要 engine.depth_pool 变，下一轮自然切币；盘口只算聚合失衡、不落原始盘口。
        """
        async with self._new_session() as session:
            while self._running:
                for sym in list(self.engine.depth_pool):
                    if not self._running:
                        return
                    try:
                        book = await self._get_json(
                            session, "/fapi/v1/depth",
                            {"symbol": sym.upper(), "limit": config.DEPTH_LEVELS},
                        )
                        bids = [float(b[1]) for b in book["bids"]]
                        asks = [float(a[1]) for a in book["asks"]]
                        bid_qty = sum(bids)
                        ask_qty = sum(asks)
                        tot = bid_qty + ask_qty
                        if tot > 0:
                            self.engine.on_depth(sym.upper(), bid_qty / tot, bid_qty, ask_qty)
                    except asyncio.CancelledError:
                        return
                    except Exception as exc:
                        print(f"[feed] 盘口获取失败 {sym}: {exc}")
                    await asyncio.sleep(0.2)
                await asyncio.sleep(config.DEPTH_POLL_SEC)

    # ---------------- WS 降级模式（REST 兜底） ----------------
    async def _start_fallback(self) -> None:
        if self._fallback_active:
            return
        self._fallback_active = True
        self._fb_last_id.clear()
        print("[feed] 警告: WebSocket 断流/不可用，切换到 REST 降级轮询"
              "（只保标记价/资金费率；成交明细是 limit=1000 的权重绞肉机，"
              "WS 恢复后自动补齐，不在 REST 上硬抢以免 IP 被封）")
        self._fallback_tasks = [
            asyncio.create_task(self._mark_fallback_loop()),
        ]

    def _stop_fallback(self) -> None:
        if not self._fallback_active:
            return
        self._fallback_active = False
        for t in self._fallback_tasks:
            t.cancel()
        self._fallback_tasks = []
        print("[feed] WebSocket 已恢复稳定，退出 REST 降级模式")

    async def _watchdog_loop(self) -> None:
        """WS 数据新鲜度看门狗：断流即启动 REST 降级，恢复稳定后关闭。

        不只看「连不上」，更要看「连上了却不吐数据」——被 GFW/代理半路掐断的 WS
        常常握手成功马上断流，靠连通性判断抓不住，必须看数据新鲜度。
        """
        while self._running:
            await asyncio.sleep(WATCHDOG_INTERVAL)      # 改为使用常量
            fresh = (time.time() - self._last_ws_msg_ts) < WS_STALE_SEC   # 阈值调大
            if not self._fallback_active and not fresh:
                await self._start_fallback()
            elif (self._fallback_active and self._ws_connected and fresh
                  and time.time() - self._ws_connect_time >= WS_RECOVERY_SEC):
                self._stop_fallback()

    async def _mark_fallback_loop(self) -> None:
        async with self._new_session() as session:
            while self._running and self._fallback_active:
                try:
                    data = await self._get_json(session, "/fapi/v1/premiumIndex")
                    for item in data:
                        sym = item["symbol"]
                        if sym.lower() not in self._symbols:
                            continue
                        funding = float(item.get("lastFundingRate") or 0.0)
                        self.engine.on_mark(sym, float(item["markPrice"]), funding)
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    print(f"[feed] 降级模式标记价获取失败: {exc}")
                await asyncio.sleep(5)

    async def _trades_fallback_loop(self) -> None:
        """【已停用】成交明细的 REST 兜底。

        原实现对每个 symbol 拉 aggTrades?limit=1000，是 429/418 封禁的直接元凶：
        成交额大的币 1000 条几秒刷完，REST 永远追不上，只能越拉越勤 → IP 权重爆表。
        成交明细的正路是 WS @aggTrade 逐笔流（无限量、不吃 REST 权重），WS 断了就安静
        等它恢复，不再用 REST 硬抢。本函数保留仅作记录，_start_fallback 不再调用它。
        """
        return