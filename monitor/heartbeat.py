# -*- coding: utf-8 -*-
"""外部存活心跳（独立进程）：与 monitor 解耦，证明「这台机器还活着、数据层还能写」。

monitor 崩了、整机断电、磁盘满 —— 如果只有 monitor 自己知道，等于没人知道。这个进程
不依赖 monitor，独立常驻，每隔 HEARTBEAT_SEC 秒：① 写一条心跳进库；②（若配置了
EXTERNAL_PING_URL）GET 一个外部心跳端点（UptimeRobot 之类）。外部 watchdog 发现心跳
停了就给你发短信/邮件 —— 这就是「进程死了也有人知道」，解决 todo #10 的外部存活心跳。

能否区分故障层级：
- 心跳进程也停 + monitor::health 停 → 整机/断电级，外部 ping 兜底
- 只有 monitor::health 停、心跳还在 → monitor 进程死了但机器活着，重启 monitor 即可

用法:
    python -m monitor.heartbeat        # 前台
    # 生产（云服务器）:
    #   nohup python -m monitor.heartbeat >> data/heartbeat.log 2>&1 &
    #   或配成 systemd 服务 / 开机自启脚本
"""
from __future__ import annotations

import asyncio
import sys
import time

import aiohttp

from . import config
from .db import MonitorDB

START = time.monotonic()
VERSION = "1.0"


async def _ping(session: aiohttp.ClientSession, url: str) -> None:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=config.TIMEOUT)) as resp:
            await resp.read()
    except Exception as exc:
        print(f"[heartbeat] 外部 ping 失败: {exc}")


async def main() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    db = MonitorDB(config.DB_PATH)  # 顺带建表 + 开 WAL（独立进程也能先于 monitor 建立库结构）
    session = aiohttp.ClientSession(trust_env=True, proxy=config.PROXY or None)
    print(f"[heartbeat] 独立心跳进程启动，每 {config.HEARTBEAT_SEC}s 一跳，"
          f"外部 ping {'开启' if config.EXTERNAL_PING_URL else '关闭'}")
    try:
        while True:
            db.heartbeat("heartbeat", version=VERSION, uptime_s=time.monotonic() - START)
            if config.EXTERNAL_PING_URL:
                await _ping(session, config.EXTERNAL_PING_URL)
            await asyncio.sleep(config.HEARTBEAT_SEC)
    finally:
        await session.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())