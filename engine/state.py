# -*- coding: utf-8 -*-
"""状态管理：唯一 system_state.json。

全系统只有一个运行时状态文件，替代旧版的六个落盘点。结构：
{
  "started": ts,
  "strategies": {
    "<name>": {
      "nav": 1.0,                    # 纸面净值
      "status": "active",            # active / stopped
      "positions": {sym: {...}},     # 当前持仓
      "history": [[ts, nav], ...],   # 净值曲线
      "trades": [ {...}, ...]        # 已结算交易流水（复盘用）
    }
  }
}
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import sys
from pathlib import Path as _Path

_root = _Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
import config


class StateManager:
    def __init__(self, path=None):
        self.path = Path(path) if path else Path(config.STATE_PATH)

    def load(self) -> dict:
        if not self.path.exists():
            return {"started": int(time.time()), "strategies": {}}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"started": int(time.time()), "strategies": {}}

    def save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def strategy(state: dict, name: str) -> dict:
        """取某猎犬的状态（不存在则初始化）。"""
        return state["strategies"].setdefault(name, {
            "nav": 1.0,
            "status": "active",
            "positions": {},
            "history": [],
            "trades": [],
        })
