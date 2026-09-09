# -*- coding: utf-8 -*-
"""从币安 exchangeInfo 生成定池所需的元数据（一次抓全，三个文件）：

1. tradfi_symbols.json：underlyingType != COIN 的 TradFi 永续（股票/贵金属/ETF/大宗/指数）
2. onboard_dates.json：COIN 永续的 onboardDate（诊断用：上线时间分布）
3. no_spot_symbols.json：COIN 永续中「仅合约无现货」的 symbol（无现货盘 = 套利锚缺失）

权威来源：
- fapi: https://fapi.binance.com/fapi/v1/exchangeInfo（USD-M 永续）
- spot: https://api.binance.com/api/v3/exchangeInfo（现货交易对）

edge 本质：pin 赚「插针→回归」。回归锚三层（按强度）：现货-合约套利(最强) > 做市商再平衡 > 投机抄底。
定池筛「回归锚强」的币，三条正面条件：
  1. underlyingType == COIN（排除跳空/事件驱动的股票/贵金属/大宗/ETF）
  2. 有现货盘（排除「仅合约无现货」，无现货 = 套利锚缺失 = 插针真相归零）
  3. 流动性下限（排除无承接盘微盘）

「上线时长 onboardDate」是 2+3 的代理（新币 ≈ 无现货 + 低流动性），已被正面定义取代，onboard_dates 仅作诊断保留。

用法：
  python tools/fetch_coin_universe.py                                   # 香港服务器直连
  BINANCE_PROXY=http://127.0.0.1:7890 python tools/fetch_coin_universe.py  # 本地走代理
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
import config

FAPI_URL = config.BASE_URL + "/fapi/v1/exchangeInfo"
SPOT_URL = "https://api.binance.com/api/v3/exchangeInfo"

# 人工补充边界：underlyingType==COIN 但本质锚定外部资产的代币（PAXG 锚黄金等）。
EXTRA_BLACKLIST = ["PAXGUSDT"]


def _proxies() -> dict | None:
    if config.PROXY:
        return {"http": config.PROXY, "https": config.PROXY}
    return None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> None:
    proxies = _proxies()

    # ---- fapi 永续 ----
    print(f"GET {FAPI_URL} (proxy={config.PROXY or '直连'})")
    r = requests.get(FAPI_URL, timeout=30, proxies=proxies)
    r.raise_for_status()
    fut = r.json()["symbols"]

    # ---- spot 现货 ----
    print(f"GET {SPOT_URL}")
    r2 = requests.get(SPOT_URL, timeout=30, proxies=proxies)
    r2.raise_for_status()
    spot_syms = [s for s in r2.json()["symbols"] if s.get("status") == "TRADING"]
    spot_base = {s["baseAsset"] for s in spot_syms}

    dist: dict[str, int] = {}
    tradfi: list[str] = []
    onboard: dict[str, int] = {}
    no_spot: list[str] = []
    for s in fut:
        if s.get("contractType") not in ("PERPETUAL", "TRADIFI_PERPETUAL"):
            continue
        ut = s.get("underlyingType", "?")
        dist[ut] = dist.get(ut, 0) + 1
        sym = s["symbol"]
        if ut != "COIN":
            tradfi.append(sym)
            continue
        # COIN：记录 onboardDate（诊断）+ 判断有无现货
        od = s.get("onboardDate")
        if od:
            onboard[sym] = int(od)
        base = s.get("baseAsset", "")
        # 归一化 1000/1000000 倍合约前缀（1000PEPE → PEPE 这类历史遗留）
        norm = base
        if norm.startswith("1000000") and norm[7:] in spot_base:
            norm = norm[7:]
        elif norm.startswith("1000") and norm[4:] in spot_base:
            norm = norm[4:]
        if norm not in spot_base:
            no_spot.append(sym)

    print(f"永续合约数: {sum(dist.values())}")
    print(f"underlyingType 分布: {dist}")
    print(f"spot TRADING base asset: {len(spot_base)}")
    print(f"TradFi（!=COIN）: {len(tradfi)} 个")
    print(f"「仅合约无现货」COIN: {len(no_spot)} 个")

    # ---- 写文件 1：tradfi ----
    all_black = sorted(set(tradfi) | set(EXTRA_BLACKLIST))
    payload = {
        "generated_at": _now(),
        "source": "exchangeInfo",
        "note": "underlyingType != COIN 的 USD-M 永续全集（股票/贵金属/ETF/大宗/指数）+ 人工边界 extra（PAXG 等锚定外部资产的 COIN）。",
        "underlying_type_dist": dist,
        "tradfi_symbols": all_black,
    }
    config.TRADFI_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(config.TRADFI_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"已写入 {config.TRADFI_PATH}，共 {len(all_black)} 个 TradFi symbol")

    # ---- 写文件 2：onboard（诊断保留）----
    onboard_payload = {
        "generated_at": _now(),
        "source": "exchangeInfo",
        "note": "COIN 永续的 onboardDate（毫秒）。仅诊断用，定池已改由「有无现货」正面定义取代上线时长。",
        "onboard_dates": onboard,
    }
    with open(config.ONBOARD_PATH, "w", encoding="utf-8") as f:
        json.dump(onboard_payload, f, ensure_ascii=False, indent=2)
    print(f"已写入 {config.ONBOARD_PATH}，共 {len(onboard)} 个 COIN 上线时间")

    # ---- 写文件 3：no_spot ----
    no_spot.sort()
    no_spot_payload = {
        "generated_at": _now(),
        "source": "exchangeInfo",
        "note": "COIN 永续中「仅合约无现货」的 symbol（无现货盘 = 现货-合约套利锚缺失，插针是真相归零不是错杀）。由 fapi COIN 永续 base 与 spot TRADING base 求差集。",
        "count": len(no_spot),
        "no_spot_symbols": no_spot,
    }
    with open(config.NO_SPOT_PATH, "w", encoding="utf-8") as f:
        json.dump(no_spot_payload, f, ensure_ascii=False, indent=2)
    print(f"已写入 {config.NO_SPOT_PATH}，共 {len(no_spot)} 个「仅合约无现货」")


if __name__ == "__main__":
    main()
