# -*- coding: utf-8 -*-
"""monitor 配置：从根 config 继承，只保留 monitor 特有的 secrets 导入路径。"""
import sys
from pathlib import Path

# 把项目根目录加入 sys.path，确保能 import config
_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from config import *  # noqa: F401 F403 — 统一配置，所有参数从根 config 继承

# 只保留 monitor 特有的 secrets 导入（相对路径）
try:
    from . import credentials as _sec
    FEISHU_WEBHOOK = getattr(_sec, "FEISHU_WEBHOOK", "")
except ImportError:
    FEISHU_WEBHOOK = ""