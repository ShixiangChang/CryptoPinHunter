# -*- coding: utf-8 -*-
"""打包脚本：生成可部署的 quantum_live.zip，排除凭据与运行时数据。

用法：python pack.py   （在项目根目录运行，输出 ./quantum_live.zip）

含：代码 + 部署脚本 + 定池元数据（tradfi / 现货上线时间）。
排除：monitor/credentials.py（凭据）、monitor.db 及运行时数据（目标机部署后重新采集）。
"""
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "quantum_live.zip"

# 根目录要打包的文件
ROOT_FILES = [
    "config.py", "scheduler.py", "status.py", "live_trader.py", "live_status.py",
    "serve.py", "requirements.txt", "README.md", "DEPLOY.md",
    "LICENSE", ".gitignore",
]
# 要打包的目录（只收 .py / .sh / .conf）
PACK_DIRS = ["engine", "monitor", "deploy", "tools"]
# 定池元数据（三层过滤的运行时输入，服务器开箱即用，无需先联网抓）
DATA_META_FILES = [
    "data/tradfi_symbols.json",        # TradFi 黑名单（剔除股票/贵金属/ETF 代币）
    "data/spot_onboard_dates.json",    # 现货上线时间（point-in-time「有现货」判断）
    "data/spot_base.json",             # 现货 base 映射（1000 系列前缀归一化）
    "data/onboard_dates.json",         # COIN 上线时间（诊断保留）
]
# 明确排除（凭据 + 缓存 + 运行时数据）
EXCLUDE = {"monitor/credentials.py"}

count = 0
with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
    for f in ROOT_FILES:
        p = ROOT / f
        if p.exists():
            z.write(p, f)
            count += 1
    for f in DATA_META_FILES:
        p = ROOT / f
        if p.exists():
            z.write(p, f)
            count += 1
        else:
            print(f"[pack] 警告：元数据缺失 {f}（定池过滤会降级，建议先跑 tools/fetch_coin_universe.py 补齐）")
    for d in PACK_DIRS:
        for p in (ROOT / d).rglob("*"):
            if not p.is_file():
                continue
            rel = str(p.relative_to(ROOT)).replace("\\", "/")
            if rel in EXCLUDE:
                continue
            if "__pycache__" in rel or p.suffix == ".pyc":
                continue
            if p.suffix in (".py", ".sh", ".conf"):
                z.write(p, rel)
                count += 1

size_mb = OUT.stat().st_size / 1024 / 1024
print(f"[pack] 打包完成：{OUT}")
print(f"[pack] 共 {count} 个文件，{size_mb:.2f} MB")
