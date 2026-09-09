# -*- coding: utf-8 -*-
"""ERC-20 代币交易所净流入流出监控（现货链上数据，免费档）。

背景：
- Etherscan tokenholderlist（持仓集中度：前100持有者算 top10/top100）是 API Pro 端点，
  免费档拿不到（实测返回 "API Pro endpoint"）。之前记录里「免费」是错的。
- 免费可行的替代：tokentx 接口监控 UNIVERSE 里的 ERC-20 代币（LINK/UNI/AAVE）在
  交易所的净流入流出 —— 代币流入交易所 = 潜在抛压，流出 = 囤币/提现离场。
  这是现货 exchange netflow 的散户版。

定位：慢变量、长期积累（与鲸鱼转账监控 onchain.py 互补）。短期不进模型，
先落库攒序列，攒够再做「净流入流出 → 未来收益」的截面验证。
建议每 10 分钟跑一次，或挂到定时任务。可重复跑，按 txhash 幂等去重。
"""
from __future__ import annotations

import time

import requests

from . import config, credentials


def _exchange_map() -> dict[str, dict[str, str]]:
    """chain -> {小写地址: 交易所名}"""
    return {
        chain: {a.lower(): name for a, name in addrs.items()}
        for chain, addrs in config.EXCHANGE_ADDRS.items()
    }


def _prices(db, tokens: list[str]) -> dict[str, float]:
    """从本地 klines_1m 读最新 close 价（不依赖外部行情 API，免受限流影响）。"""
    out: dict[str, float] = {}
    for t in tokens:
        row = db.query(
            "SELECT close FROM klines_1m WHERE symbol=? ORDER BY open_time DESC LIMIT 1",
            (f"{t}USDT",),
        )
        out[t] = float(row[0][0]) if row and row[0][0] else 0.0
    return out


def _api(chain: str, module: str, action: str, key: str, **params) -> list:
    cc = config.ONCHAIN_CHAINS[chain]
    q = {
        "chainid": cc["chainid"], "module": module, "action": action,
        "page": 1, "offset": 100, "sort": "desc", "apikey": key, **params,
    }
    px = {"http": config.PROXY, "https": config.PROXY}
    r = requests.get(f"{cc['url']}/v2/api", params=q, proxies=px, timeout=config.TIMEOUT)
    data = r.json()
    if str(data.get("status")) != "1":
        print(f"[erc20_flow] {chain} {action} 返回异常: {data.get('message')} | {str(data.get('result'))[:100]}")
        return []
    res = data.get("result")
    return res if isinstance(res, list) else []


def collect(db=None) -> dict:
    """采集一轮 ERC-20 交易所净流入流出（幂等，可重复调用）。

    返回本轮汇总：{token: {in, out}}。db 为空时自建连接。
    """
    from .db import MonitorDB
    own = db is None
    if own:
        db = MonitorDB(config.DB_PATH)
    try:
        return _collect(db)
    finally:
        if own:
            db.close()


def _collect(db) -> dict:
    exch = _exchange_map()

    # 已知 txhash 集合（幂等去重）
    known = {r[0] for r in db.query("SELECT txhash FROM erc20_flow WHERE txhash IS NOT NULL")}

    prices = _prices(db, [t for tokens in config.ERC20_TOKENS.values() for t in tokens])
    total_in = 0
    total_out = 0
    token_flow: dict[str, dict[str, float]] = {}

    for chain, tokens in config.ERC20_TOKENS.items():
        key = config.ONCHAIN_CHAINS[chain]["key"]
        if not key:
            print(f"[erc20_flow] {chain} 无 key，跳过")
            continue
        emap = exch.get(chain, {})
        for token, contract in tokens.items():
            rows = _api(chain, "account", "tokentx", key, contractaddress=contract)
            px_tok = prices.get(token, 0.0)
            for tx in rows:
                h = tx.get("hash") or ""
                if not h or h in known:
                    continue
                known.add(h)
                fr = (tx.get("from") or "").lower()
                to = (tx.get("to") or "").lower()
                amount = int(tx.get("value") or 0) / 1e18
                usd = amount * px_tok
                # 分类：to 是交易所=流入(in)，from 是交易所=流出(out)
                if to in emap:
                    flow = "in"
                elif fr in emap:
                    flow = "out"
                else:
                    flow = "other"
                if usd >= config.ERC20_FLOW_MIN_USD:
                    db.erc20_flow(chain, token, fr, to, amount, usd, flow, h)
                if flow == "in":
                    total_in += usd
                elif flow == "out":
                    total_out += usd
                f = token_flow.setdefault(token, {})
                f[flow] = f.get(flow, 0.0) + usd
            time.sleep(0.15)  # 免费档限速，稳一点

    print(f"[erc20_flow] 本轮：流入交易所 ≈ ${total_in:,.0f}，流出 ≈ ${total_out:,.0f}，净流入 ≈ ${total_in - total_out:,.0f}")
    # 汇总各代币在交易所的累计净流向（近 24h）
    cutoff = int(time.time()) - 24 * 3600
    rows = db.query(
        "SELECT token, flow, SUM(usd) FROM erc20_flow WHERE ts >= ? GROUP BY token, flow",
        (cutoff,),
    )
    agg: dict[str, dict[str, float]] = {}
    for token, flow, usd in rows:
        agg.setdefault(token, {})[flow] = float(usd or 0)
    for token, f in sorted(agg.items()):
        inn = f.get("in", 0.0)
        out = f.get("out", 0.0)
        print(f"  近24h {token:6s}: 流入 ${inn:>12,.0f}  流出 ${out:>12,.0f}  净流入 ${inn - out:>+12,.0f}")
    return token_flow


def main() -> None:
    collect()


if __name__ == "__main__":
    main()
