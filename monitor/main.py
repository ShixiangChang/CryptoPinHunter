# -*- coding: utf-8 -*-
"""quantum monitor 入口。

用法（在 quantum 目录下运行）:
    python -m monitor.main              正常运行
    python -m monitor.main --dry-run    只打印告警到控制台，不发 webhook
    python -m monitor.main --test       向 webhook 发一条测试消息后退出
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from . import config
from .db import MonitorDB
from .events import EventEngine
from .feed import DataFeed
from .health import DataHealth
from .notifier import Notifier
from .onchain import OnchainFeed


async def _erc20_flow_loop(db: MonitorDB) -> None:
    """ERC-20 现货净流入采集（慢变量，每 10 分钟一轮，幂等）。"""
    from . import erc20_flow
    while True:
        try:
            await asyncio.to_thread(erc20_flow.collect, db)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[erc20_flow] 采集异常（跳过本轮）: {exc}")
        await asyncio.sleep(config.ERC20_FLOW_POLL_SEC)


async def _run(dry_run: bool) -> None:
    config.DRY_RUN = dry_run
    db = MonitorDB(config.DB_PATH)
    notifier = Notifier()
    engine = EventEngine(db, notifier)
    feed = DataFeed(engine)
    engine.feed = feed
    onchain = OnchainFeed(engine, db)

    await feed.start()
    engine.start()
    erc20_task = None
    if config.ONCHAIN_ENABLED:
        await onchain.start()
        # ERC-20 现货净流入采集：慢变量长期积累，独立线程跑（requests 阻塞调用）
        erc20_task = asyncio.create_task(_erc20_flow_loop(db))

    # 数据健康哨兵：定时核检断档/脏值，出问题推告警，每天一条健康日报
    health = DataHealth(db, notifier)
    health_task = asyncio.create_task(health.run())

    print(f"[main] 监控已启动 | DRY_RUN={config.DRY_RUN} | 告警渠道={config.NOTIFY_CHANNEL}")
    print("[main] 数据落盘位置: " + config.DB_PATH)
    print("[main] 数据健康哨兵已上线（每 15 分钟核检，异常即时告警）")
    if config.ONCHAIN_ENABLED:
        print(f"[main] ERC-20 现货净流入采集已上线（每 {config.ERC20_FLOW_POLL_SEC // 60} 分钟一轮）")
    print("[main] 按 Ctrl+C 退出")
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        health_task.cancel()
        if erc20_task is not None:
            erc20_task.cancel()
        try:
            await health_task
        except BaseException:
            pass
        if erc20_task is not None:
            try:
                await erc20_task
            except BaseException:
                pass
        onchain.stop()
        feed.stop()
        engine.stop()
        db.close()
        print("[main] 已停止")


async def _send_test() -> None:
    notifier = Notifier()
    await notifier.send(
        "✅ quantum monitor 测试",
        ["webhook 配置成功，可以正常接收告警了",
         f"告警渠道: {config.NOTIFY_CHANNEL}",
         "现在可以 Ctrl+C 关掉测试窗口，正常运行: python -m monitor.main"],
    )


def main() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Binance USDT 永续合约实时监控")
    parser.add_argument("--dry-run", action="store_true", help="只打印告警，不发 webhook")
    parser.add_argument("--test", action="store_true", help="发送一条测试消息后退出")
    args = parser.parse_args()

    if args.test:
        try:
            asyncio.run(_send_test())
        except KeyboardInterrupt:
            pass
        return

    try:
        asyncio.run(_run(args.dry_run or config.DRY_RUN))
    except KeyboardInterrupt:
        print("\n[main] 已退出")


if __name__ == "__main__":
    main()