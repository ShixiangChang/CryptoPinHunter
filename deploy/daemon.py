# -*- coding: utf-8 -*-
"""deploy/daemon.py —— 进程守护：四个子进程崩溃自动重启。

守护的进程：
  live_feed    1m 数据常驻采集
  live_trader  实盘交易主循环
  live_status  实盘看板生成（每 60s 刷新）
  serve        看板 HTTP 服务

用法：python deploy/daemon.py     （常驻，Ctrl+C 退出并回收所有子进程）
"""
from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

PROCS = [
    ("live_feed",   [PY, str(ROOT / "monitor" / "live_feed.py")]),
    ("live_trader", [PY, str(ROOT / "live_trader.py"), "--loop"]),
    ("live_status", [PY, str(ROOT / "live_status.py"), "--loop"]),
    ("serve",       [PY, str(ROOT / "serve.py")]),
]

LOG_DIR = ROOT / "logs"


def main() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    procs: dict[str, subprocess.Popen | None] = {name: None for name, _ in PROCS}

    def stop_all(*_):
        print("\n[daemon] 正在停止所有子进程...")
        for name, p in procs.items():
            if p is not None and p.poll() is None:
                p.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, stop_all)
    signal.signal(signal.SIGTERM, stop_all)

    print(f"[daemon] 守护 {len(PROCS)} 个进程：{', '.join(n for n, _ in PROCS)}")
    while True:
        for name, cmd in PROCS:
            p = procs[name]
            if p is None or p.poll() is not None:
                if p is not None:
                    print(f"[daemon] {name} 退出(code={p.returncode})，3 秒后重启")
                    time.sleep(3)
                log = open(LOG_DIR / f"{name}.log", "a", encoding="utf-8")
                print(f"[daemon] 启动 {name}")
                procs[name] = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                               cwd=str(ROOT))
        time.sleep(5)


if __name__ == "__main__":
    main()
