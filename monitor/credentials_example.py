# -*- coding: utf-8 -*-
"""密钥模板。复制本文件为 credentials.py 并填上真实 key（credentials.py 已被 .gitignore，不进版本库）。

安全须知：
- 币安 API key 只开「交易」权限，绝不开「提现」权限。
- 一个 key 只放在一台服务器上，泄露立即到币安后台删除重建。
"""
BINANCE_API_KEY = ""      # 币安 API Key（只勾合约交易权限）
BINANCE_API_SECRET = ""   # 币安 API Secret

# 合约交易 key（主网，真钱，上线时填；测试网用 BINANCE_TESTNET_*，不混用）
BINANCE_FUTURES_API_KEY = ""
BINANCE_FUTURES_API_SECRET = ""

# 币安合约测试网（假钱，验证执行层用，与主网完全隔离）
BINANCE_TESTNET_API_KEY = ""
BINANCE_TESTNET_API_SECRET = ""

DINGTALK_WEBHOOK = ""     # 钉钉机器人 webhook（可选，空则不推送）
DINGTALK_SECRET = ""      # 钉钉机器人加签 secret（可选）

ETHERSCAN_API_KEY = ""    # 去 https://etherscan.io 注册免费 key（链上监控可选）
BSCSCAN_API_KEY = ""      # 去 https://bscscan.com 注册免费 key（可选）
