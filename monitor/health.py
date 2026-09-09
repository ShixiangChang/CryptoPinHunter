# -*- coding: utf-8 -*-
"""数据健康哨兵：定时核检 monitor.db，发现断档 / 脏值立刻推告警，每天一条健康心跳。

守护的是"长期积累的原料质量"——数据静默变质是 DIY 量化系统的头号死法，
等建模那天才发现就晚了。哨兵天天看着，出问题第一时间提醒，而不是攒了十天二十天回头傻眼。

两层防线：
1. 写入时校验（db.py）：NaN/inf、非正 OI 这些脏值压根不进库 —— 治未病
2. 定时核检（本模块）：断档 / 脏值 / 币种覆盖 —— 体检

告警策略：只在"好→坏"状态切换时告警（按稳定 key 去重），避免断档期间每个周期刷屏；
恢复后再发一条"已恢复"。健康心跳每天一条，让沉默 = 确认干净，而不是"可能死了"。
"""
from __future__ import annotations

import asyncio
import time

from . import config
from .db import MonitorDB

# 需要持续写入、断档即异常的表（轮询驱动，无论行情冷热都应该分钟级更新）
ALWAYS_FRESH = ["oi", "ratios", "mark_prices"]
# 事件驱动表：有大单/爆仓/鲸鱼才有写入，行情冷清时可能长时间空白，只进日报不硬告警
EVENT_TABLES = ["events", "trades", "onchain_txs"]
ALL_TABLES = ALWAYS_FRESH + EVENT_TABLES


class DataHealth:
    def __init__(self, db: MonitorDB, notifier):
        self.db = db
        self.notifier = notifier
        self._bad: set[str] = set()
        self._last_heartbeat = time.monotonic()
        self._started = time.monotonic()

    # ---------- 核检（纯查询，无副作用） ----------
    def _check(self) -> list[tuple[str, str]]:
        """返回 [(稳定 key, 描述)]，空 = 健康。key 稳定是告警去重的关键。"""
        issues: list[tuple[str, str]] = []
        now = int(time.time())

        for t in ALL_TABLES:
            try:
                max_ts = self.db.query(f"SELECT MAX(ts) FROM {t}")[0][0]
            except Exception as exc:
                issues.append((f"err:{t}", f"表 {t} 查询失败: {exc}"))
                continue
            if max_ts is None:
                issues.append((f"empty:{t}", f"表 {t} 还没有任何数据"))
            elif t in ALWAYS_FRESH and now - max_ts > config.HEALTH_STALE_SEC:
                hrs = (now - max_ts) / 3600
                issues.append((f"stale:{t}", f"表 {t} 断档 {hrs:.1f} 小时"))

        # OI 非正/空 = 脏（写入时已保证正数，出现说明有问题）
        for col in ("oi_base", "notional"):
            try:
                n = self.db.query(
                    f"SELECT COUNT(*) FROM oi WHERE ts > ? AND ({col} IS NULL OR {col} <= 0)",
                    (now - 86400,),
                )[0][0]
                if n:
                    issues.append((f"bad:oi.{col}", f"oi.{col} 近 24h 有 {n} 条非正值"))
            except Exception as exc:
                issues.append((f"err:oi.{col}", f"oi.{col} 检查失败: {exc}"))

        # 多空比负值 = 脏（账户比/买卖比不可能为负；NULL 是端点偶发缺失，不算脏）
        for col in ("global_ls", "top_pos_ls", "taker_ls"):
            try:
                n = self.db.query(
                    f"SELECT COUNT(*) FROM ratios WHERE ts > ? AND {col} < 0",
                    (now - 86400,),
                )[0][0]
                if n:
                    issues.append((f"bad:ratios.{col}", f"ratios.{col} 近 24h 有 {n} 条负值"))
            except Exception as exc:
                issues.append((f"err:ratios.{col}", f"ratios.{col} 检查失败: {exc}"))

        # 币种覆盖：oi 近 24h 至少要有一点点数据（全空 = 明确坏了）
        try:
            n = self.db.query("SELECT COUNT(DISTINCT symbol) FROM oi WHERE ts > ?", (now - 86400,))[0][0]
            if n < config.HEALTH_MIN_SYMBOLS:
                issues.append((f"cover:oi", f"oi 近 24h 只有 {n} 个币种（阈值 {config.HEALTH_MIN_SYMBOLS}）"))
        except Exception as exc:
            issues.append(("err:oi.cover", f"oi 覆盖检查失败: {exc}"))

        return issues

    def _summary(self) -> list[str]:
        now = int(time.time())
        lines = []
        for t in ALL_TABLES:
            try:
                n, max_ts = self.db.query(f"SELECT COUNT(*), MAX(ts) FROM {t}")[0]
                age = (now - max_ts) / 3600 if max_ts else float("nan")
                lines.append(f"{t}: {n:,} 条，最新 {age:.1f}h 前")
            except Exception:
                lines.append(f"{t}: 查询失败")
        return lines

    def _sync_gap_ledger(self) -> None:
        """断档台账：把核检发现的断档写进 data_gaps 落库，恢复后闭合。

        与只发一条告警不同，gap 落库是「这段数据不可信」的永久记录 ——
        下游建模/回测读它就知道哪里是盲区，而不是被脏数据静默带偏。
        """
        now = int(time.time())
        for t in ALWAYS_FRESH:
            try:
                max_ts = self.db.query(f"SELECT MAX(ts) FROM {t}")[0][0]
            except Exception:
                max_ts = None
            stale = (max_ts is None) or (now - max_ts > config.HEALTH_STALE_SEC)
            if stale:
                self.db.mark_gap(t, "哨兵核检 · 断档", start_ts=max_ts or now)
            else:
                self.db.close_gap(t)

    # ---------- 主循环 ----------
    async def run(self) -> None:
        while True:
            self.db.heartbeat("monitor::health", uptime_s=time.monotonic() - self._started)
            issues = self._check()
            keys = {k for k, _ in issues}
            new_bad = keys - self._bad
            recovered = self._bad - keys
            self._bad = keys
            self._sync_gap_ledger()

            if issues:
                print(f"[health] 核检发现 {len(issues)} 项问题")
                for _, msg in issues:
                    print(f"[health]   ⚠ {msg}")
            else:
                print("[health] 核检：健康")

            if new_bad:
                msgs = [msg for k, msg in issues if k in new_bad]
                await self.notifier.send(f"⚠️ 数据健康告警（{len(msgs)} 项）", msgs)
            elif recovered and not keys:
                await self.notifier.send("✅ 数据恢复健康", ["之前的问题已消除，数据恢复正常积累"])

            # 每日健康心跳：让"沉默" = 确认干净，而不是"可能死了"
            if time.monotonic() - self._last_heartbeat >= config.HEALTH_HEARTBEAT_SEC:
                self._last_heartbeat = time.monotonic()
                await self.notifier.send("✅ 数据健康日报", self._summary())

            await asyncio.sleep(config.HEALTH_CHECK_SEC)