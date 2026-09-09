# -*- coding: utf-8 -*-
"""抓现货上线时间（point-in-time「有现货」判据的数据源）。

背景：现货 exchangeInfo 没有 onboardDate 字段（该字段是期货专属），故用兜底方案——
对每个 USDT 报价、TRADING 状态的现货，查其「最早可得日线 openTime」，近似现货上线时间。
对老币（BTC/ETH 等）能回到币安 2017-08 上线日，对新币即首根 K 线，精度满足 point-in-time 判断。

产出：data/spot_onboard_dates.json，{base_asset: 上线毫秒}，供 engine/data.has_spot_at() 使用。

用法：
  python tools/fetch_spot_onboard.py                              # 走 config.PROXY（或本地默认 7890）
  BINANCE_PROXY=http://127.0.0.1:7890 python tools/fetch_spot_onboard.py
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
import config

SPOT_URL = "https://api.binance.com/api/v3/exchangeInfo"
KLINE_URL = "https://api.binance.com/api/v3/klines"


def main() -> None:
    import os
    proxy = config.PROXY or os.environ.get("BINANCE_PROXY", "").strip()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    print(f"代理: {proxy or '(直连)'}")

    # 1. 现货 exchangeInfo → USDT 报价 + TRADING 的现货
    r = requests.get(SPOT_URL, timeout=30, proxies=proxies)
    r.raise_for_status()
    syms = r.json()["symbols"]
    usdt_syms = [s for s in syms if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"]
    print(f"USDT 现货数: {len(usdt_syms)}")

    # 2. 逐个查最早日线 openTime
    onboard: dict[str, int] = {}
    fail: list[str] = []
    for i, s in enumerate(usdt_syms):
        sym = s["symbol"]
        base = s["baseAsset"]
        try:
            kr = requests.get(
                KLINE_URL,
                params={"symbol": sym, "interval": "1d", "startTime": 0, "limit": 1},
                timeout=20, proxies=proxies,
            )
            kr.raise_for_status()
            data = kr.json()
            if data:
                onboard[base] = int(data[0][0])  # openTime 毫秒
            else:
                fail.append(sym)
        except Exception as e:  # noqa: BLE001
            fail.append(f"{sym}({type(e).__name__})")
        if (i + 1) % 50 == 0:
            print(f"  进度 {i + 1}/{len(usdt_syms)}")
        time.sleep(0.12)  # 限频保护

    print(f"成功获取现货上线时间: {len(onboard)}/{len(usdt_syms)}")
    if fail:
        print(f"失败/无数据 {len(fail)} 个: {fail[:20]}")

    # 3. 写文件
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "spot klines 最早日线 openTime（现货 exchangeInfo 无 onboardDate 字段，klines 兜底）",
        "note": "USDT 报价现货的「最早可得日线 openTime」≈ 现货上线时间（毫秒）。"
                "用于 point-in-time「有现货」判断，替代静态 no_spot 名单。",
        "count": len(onboard),
        "spot_onboard_dates": onboard,
    }
    config.SPOT_ONBOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(config.SPOT_ONBOARD_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"已写入 {config.SPOT_ONBOARD_PATH}")


if __name__ == "__main__":
    main()
