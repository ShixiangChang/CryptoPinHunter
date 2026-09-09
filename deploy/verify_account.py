# -*- coding: utf-8 -*-
"""只读验证：连主网查账户余额，验证 key 有效 + 看真实到账金额。绝不下单。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
from engine.executor import BinanceFutures
from monitor import credentials as c

ex = BinanceFutures(c.BINANCE_FUTURES_API_KEY, c.BINANCE_FUTURES_API_SECRET,
                    base_url=config.TRADE_BASE_URL)
a = ex.account()
print("环境:", config.TRADE_BASE_URL)
print("钱包余额 totalWalletBalance:", a.get("totalWalletBalance"))
print("未实现盈亏 totalUnrealizedProfit:", a.get("totalUnrealizedProfit"))
print("权益 totalMarginBalance:", a.get("totalMarginBalance"))
print("可用 availableBalance:", a.get("availableBalance"))
print("=== 各资产 ===")
for b in a.get("assets", []):
    wb = float(b.get("walletBalance", 0))
    if wb != 0:
        print("  %s: 余额 %s 可用 %s" % (b.get("asset"), b.get("walletBalance"),
                                          b.get("availableBalance")))
print("=== 当前持仓 ===")
for p in a.get("positions", []):
    amt = float(p.get("positionAmt", 0))
    if amt != 0:
        print("  %s: amt=%s entry=%s" % (p.get("symbol"), amt, p.get("entryPrice")))
print("=== 杠杆/保证金模式 ===")
print("multiAssetsMargin:", a.get("multiAssetsMargin"))
