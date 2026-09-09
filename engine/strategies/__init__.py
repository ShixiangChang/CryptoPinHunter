# -*- coding: utf-8 -*-
"""活跃策略注册表。

策略通过统一的 Strategy 接口实现，回测/纸面/实盘共用同一套信号。
RETIRED 列表存放已退役策略（样本外验证无正收益），代码保留供参考。
"""
from .pin import PinStrategy
from .momentum import MomentumStrategy
from .beta import BetaStrategy

ALL = [PinStrategy]

# 退役策略（样本外验证无正收益，代码保留作参考）
RETIRED = [MomentumStrategy, BetaStrategy]

__all__ = ["PinStrategy", "MomentumStrategy", "BetaStrategy", "ALL", "RETIRED"]
