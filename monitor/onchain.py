# -*- coding: utf-8 -*-
"""链上数据层（Phase 2）：Etherscan / BSCScan V2 接口轮询。

两类监控：
- 动态层：扫描主流稳定币（USDT/USDC/BUSD）的最新大额转账，无需预设地址，自动发现鲸鱼。
- 固定层：轮询已知鲸鱼钱包（交易所热钱包、稳定币金库）的转账，判断资金流入/流出。

所有 >= WHALE_TRANSFER_DB_USD 的转账写库（Phase 3 建模原料）；>= WHALE_TRANSFER_USD 触发告警。

已知限制：这是 REST 轮询，不是全量流。极高频时段若两次轮询之间出现超过
ONCHAIN_LATEST_N 笔转账，可能漏掉个别大单；免费档做不到全量覆盖（需要付费
webhook/stream）。作为鲸鱼发现是"尽力而为"的启发式信号。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import aiohttp

from . import config


def _short(addr: str) -> str:
    if not addr:
        return "?"
    return addr[:6] + "…" + addr[-4:] if len(addr) > 12 else addr


class OnchainFeed:
    def __init__(self, engine: Any, db: Any):
        self.engine = engine
        self.db = db
        self._session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._seen: set[str] = set()          # 已处理的 "chain:txhash" 去重
        self._stable_map: dict[str, dict[str, str]] = {
            chain: {addr.lower(): sym for sym, addr in tokens.items()}
            for chain, tokens in config.STABLE_TOKENS.items()
        }
        self._price: dict[str, float] = {}    # chain -> 原生币美元价（缓存）
        self._price_ts: dict[str, float] = {}

    # ---------------- 生命周期 ----------------
    async def start(self) -> None:
        self._running = True
        self._session = aiohttp.ClientSession(trust_env=True, proxy=config.PROXY or None)
        self._task = asyncio.create_task(self._loop())
        active = [c for c, cc in config.ONCHAIN_CHAINS.items() if cc.get("key")]
        print(f"[onchain] 链上监控启动，启用链: {', '.join(active) or '无（未配置 key）'}")

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        if self._session and not self._session.closed:
            asyncio.create_task(self._session.close())

    # ---------------- 主循环 ----------------
    async def _loop(self) -> None:
        while self._running:
            try:
                for chain, cc in config.ONCHAIN_CHAINS.items():
                    if not cc.get("key"):
                        continue
                    await self._scan_dynamic(chain)
                    await self._scan_wallets(chain)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[onchain] 扫描异常（跳过本轮，下轮重试）: {exc}")
            await asyncio.sleep(config.ONCHAIN_POLL_SEC)

    async def _scan_dynamic(self, chain: str) -> None:
        """动态层：稳定币全链扫描，自动发现大额转账地址。"""
        for token, contract in config.STABLE_TOKENS.get(chain, {}).items():
            rows = await self._api(chain, "account", "tokentx", contractaddress=contract)
            if not rows:
                continue
            dec = self._decimals(chain, token)
            for tx in rows:
                amount = int(tx.get("value") or 0) / (10 ** dec)
                self._handle(chain, token, tx, usd=amount, amount=amount)
            await asyncio.sleep(0.1)

    async def _scan_wallets(self, chain: str) -> None:
        """固定层：交易所热钱包 / 金库的资金流入流出。"""
        stable_map = self._stable_map.get(chain, {})
        for name, w in config.WHALE_WALLETS.items():
            addr = w.get(chain)
            if not addr:
                continue
            # 稳定币流入/流出
            rows = await self._api(chain, "account", "tokentx", address=addr)
            if rows:
                for tx in rows:
                    token = stable_map.get((tx.get("contractAddress") or "").lower())
                    if token is None:
                        continue
                    dec = self._decimals(chain, token)
                    amount = int(tx.get("value") or 0) / (10 ** dec)
                    self._handle(chain, token, tx, usd=amount, amount=amount)
                await asyncio.sleep(0.1)
            # 原生币（ETH/BNB）流入/流出
            rows = await self._api(chain, "account", "txlist", address=addr)
            if rows:
                price = await self._native_price(chain)
                coin = "ETH" if chain == "eth" else "BNB"
                for tx in rows:
                    raw = int(tx.get("value") or 0)
                    if raw <= 0:  # 跳过纯调用/零值交易
                        continue
                    amount = raw / 1e18
                    self._handle(chain, coin, tx, usd=amount * price, amount=amount)
                await asyncio.sleep(0.1)

    # ---------------- 处理 ----------------
    def _handle(self, chain: str, token: str, tx: dict, usd: float, amount: float) -> None:
        h = tx.get("hash") or ""
        if not h:
            return
        key = f"{chain}:{h}"
        if key in self._seen:
            return
        self._seen.add(key)
        if len(self._seen) > 100_000:
            self._seen.clear()  # 到上限清空，宁可偶尔重报也不无界增长

        if usd < config.WHALE_TRANSFER_DB_USD:
            return
        self.db.onchain_tx(chain, token, tx.get("from", ""), tx.get("to", ""), usd, h)
        if usd < config.WHALE_TRANSFER_USD:
            return
        self._alert(chain, token, usd, amount, tx)

    def _alert(self, chain: str, token: str, usd: float, amount: float, tx: dict) -> None:
        from_a, to_a = tx.get("from", ""), tx.get("to", "")
        from_name, from_role = self._lookup(chain, from_a)
        to_name, to_role = self._lookup(chain, to_a)

        if to_role == "exchange":
            hint = "大额流入交易所 → 潜在抛压"
        elif from_role == "exchange":
            hint = "大额流出交易所 → 囤币/提现离场"
        elif to_role == "treasury" or from_role == "treasury":
            hint = "稳定币金库操作（铸币/销毁），关注流动性变化"
        else:
            hint = "地址间大额转移，疑似鲸鱼调仓"

        native = token in ("ETH", "BNB")
        qty = f"{amount:,.4f} {token}" if native else f"{amount:,.0f} {token}"
        chain_name = "ETH" if chain == "eth" else "BSC"
        explorer = "etherscan" if chain == "eth" else "bscscan"
        h = tx.get("hash", "")
        lines = [
            f"单笔 {qty}（≈ ${usd:,.0f}）",
            f"从 {from_name or _short(from_a)} → 到 {to_name or _short(to_a)}",
            f"解读: {hint}",
            f"{chain_name} 链 | https://{explorer}.io/tx/{h[:16]}…",
        ]
        self.engine.on_chain_alert("whale_transfer", f"{chain}.{token}", f"[{chain_name}] {token} 大额转账", lines)

    # ---------------- 工具 ----------------
    async def _api(self, chain: str, module: str, action: str, **params) -> Optional[list]:
        cc = config.ONCHAIN_CHAINS[chain]
        q = {
            "chainid": cc["chainid"], "module": module, "action": action,
            "page": 1, "offset": config.ONCHAIN_LATEST_N, "sort": "desc",
            "apikey": cc["key"], **params,
        }
        try:
            async with self._session.get(
                f"{cc['url']}/v2/api", params=q,
                timeout=aiohttp.ClientTimeout(total=config.TIMEOUT),
            ) as resp:
                data = await resp.json(content_type=None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[onchain] {chain} 请求失败: {exc}")
            return None
        if str(data.get("status")) != "1":
            print(f"[onchain] {chain} API 返回异常: {data.get('message')} | {str(data.get('result'))[:120]}")
            return None
        res = data.get("result")
        return res if isinstance(res, list) else None

    async def _native_price(self, chain: str) -> float:
        if time.time() - self._price_ts.get(chain, 0.0) < 600:
            return self._price.get(chain, 0.0)
        symbol = "ETHUSDT" if chain == "eth" else "BNBUSDT"
        try:
            async with self._session.get(
                f"{config.BASE_URL}/fapi/v1/ticker/price", params={"symbol": symbol},
                timeout=aiohttp.ClientTimeout(total=config.TIMEOUT),
            ) as resp:
                data = await resp.json(content_type=None)
            self._price[chain] = float(data["price"])
            self._price_ts[chain] = time.time()
        except Exception as exc:
            print(f"[onchain] 原生币价格获取失败 {symbol}: {exc}")
        return self._price.get(chain, 0.0)

    def _decimals(self, chain: str, token: str) -> int:
        # 稳定币小数位：USDT/USDC=6，BUSD=18
        return 6 if token in ("USDT", "USDC") else 18

    def _lookup(self, chain: str, addr: str) -> tuple[Optional[str], str]:
        a = (addr or "").lower()
        for name, w in config.WHALE_WALLETS.items():
            if (w.get(chain) or "").lower() == a:
                return name, w.get("role", "")
        return None, ""