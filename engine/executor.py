# -*- coding: utf-8 -*-
"""币安 USDT 本位永续合约执行层。

只做「下单 / 平仓 / 查余额 / 查持仓 / 数量精度」五件事，不产生任何信号。
信号来自 engine.strategies，风控在 live_trader.py。

安全设计：
- API key 只应开「交易」权限，绝不开「提现」权限——本模块也根本不实现提现。
- 单向持仓模式（ONE_WAY）：BUY=开多，SELL=平多。
- 默认 1x 杠杆（全保证金，无强平风险，最坏亏本金不倒欠）。
"""
from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from urllib.parse import urlencode

import requests

import sys
from pathlib import Path as _Path

_root = _Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
import config


class BinanceError(Exception):
    """币安 API 返回的业务错误。"""


class BinanceFutures:
    """币安合约 REST 客户端（USDT 本位永续）。"""

    def __init__(self, api_key: str = "", api_secret: str = "",
                 base_url: str | None = None, proxy: str | None = None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = (base_url or config.BASE_URL).rstrip("/")
        self.proxy = proxy if proxy is not None else config.PROXY
        self.session = requests.Session()
        self._step_cache: dict[str, tuple[float, float]] = {}
        self._time_offset_ms = 0  # 本地时钟 vs 币安服务器的毫秒偏移
        if self.api_key:
            self._sync_time()

    def _sync_time(self) -> None:
        """同步币安服务器时间，算本地偏移，避免本地时钟不准导致 -1021 Timestamp ahead。"""
        try:
            r = self.session.get(self.base_url + "/fapi/v1/time",
                                 proxies=self._proxies(), timeout=config.TIMEOUT)
            if r.status_code == 200:
                server = int(r.json().get("serverTime", 0))
                if server:
                    self._time_offset_ms = server - int(time.time() * 1000)
        except Exception:
            pass

    def _now_ms(self) -> int:
        """接近币安服务器时间的毫秒时间戳（本地时间 + 服务器偏移）。"""
        return int(time.time() * 1000) + self._time_offset_ms

    # ------------------------------------------------------------ 基础
    def _proxies(self) -> dict | None:
        return {"http": self.proxy, "https": self.proxy} if self.proxy else None

    def _signed(self, params: dict) -> str:
        p = dict(params)
        p["timestamp"] = self._now_ms()   # 用服务器时间基准，不是本地裸时间
        # recvWindow 放宽到币安最大 60000ms：容忍本地时钟与服务器最多 60s 偏差，
        # 避免 -1021 Timestamp ahead 拒单（自用低频交易无重放风险）
        p["recvWindow"] = 60000
        qs = urlencode(p)
        sig = hmac.new(self.api_secret.encode("utf-8"), qs.encode("utf-8"),
                       hashlib.sha256).hexdigest()
        return qs + "&signature=" + sig

    def _headers(self, signed: bool) -> dict:
        return {"X-MBX-APIKEY": self.api_key} if signed else {}

    def _get(self, path: str, params: dict | None = None, signed: bool = False) -> dict:
        url = self.base_url + path
        if signed:
            url += "?" + self._signed(params or {})
        elif params:
            url += "?" + urlencode(params)
        r = self.session.get(url, proxies=self._proxies(), timeout=config.TIMEOUT,
                             headers=self._headers(signed))
        return self._check(r)

    def _post(self, path: str, params: dict) -> dict:
        url = self.base_url + path + "?" + self._signed(params)
        r = self.session.post(url, proxies=self._proxies(), timeout=config.TIMEOUT,
                              headers=self._headers(True))
        return self._check(r)

    @staticmethod
    def _check(r: requests.Response) -> dict:
        if r.status_code in (418, 429):
            raise BinanceError(f"被限流 HTTP {r.status_code}：{r.text[:120]}")
        if r.status_code != 200:
            raise BinanceError(f"HTTP {r.status_code}：{r.text[:200]}")
        data = r.json()
        if isinstance(data, dict) and data.get("code") not in (None, 0, 200):
            raise BinanceError(f"币安业务错误 {data.get('code')}: {data.get('msg')}")
        return data

    # ------------------------------------------------------------ 账户
    def account(self) -> dict:
        """账户概要：totalWalletBalance / totalUnrealizedProfit / totalMarginBalance。"""
        return self._get("/fapi/v2/account", signed=True)

    def equity(self) -> float:
        """账户权益（USDT）= totalMarginBalance = 钱包余额 + 未实现盈亏。

        关键：必须用这个而不是 usdt_balance() 的 balance——balance 不含持仓浮亏，
        浮亏最需要熔断的时刻恰恰看不到。"""
        a = self._get("/fapi/v2/account", signed=True)
        return float(a.get("totalMarginBalance", 0))

    def usdt_balance(self) -> tuple[float, float]:
        """返回 (总余额, 可用余额)，USDT 计价。"""
        for b in self._get("/fapi/v2/balance", signed=True):
            if b.get("asset") == "USDT":
                return float(b.get("balance", 0)), float(b.get("availableBalance", 0))
        return 0.0, 0.0

    def positions(self) -> list[dict]:
        """返回当前非零持仓列表。positionAmt>0 为多，<0 为空。"""
        return [p for p in self._get("/fapi/v2/positionRisk", signed=True)
                if float(p.get("positionAmt", 0)) != 0]

    # ------------------------------------------------------------ 交易
    def set_leverage(self, symbol: str, leverage: int = 1) -> dict:
        return self._post("/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})

    @staticmethod
    def _gen_cid(symbol: str) -> str:
        """唯一 clientOrderId：幂等下单的锚，超时后据此查单避免重复成交。"""
        return f"pin-{symbol}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"

    def query_order(self, symbol: str, client_order_id: str) -> dict:
        """按 clientOrderId 查单状态。"""
        return self._get("/fapi/v1/order",
                         {"symbol": symbol, "origClientOrderId": client_order_id},
                         signed=True)

    def market_order(self, symbol: str, side: str, quantity: float,
                     client_order_id: str | None = None) -> dict:
        """市价单。side: BUY(开多)/SELL(平多)。带 clientOrderId 幂等 + 超时查单。

        幂等语义：同一 clientOrderId 重复提交，币安拒单(-4015)，不会重复成交。
        请求超时（实际可能已成交但客户端不知）时，先按 clientOrderId 查单，
        查到成交就返回该成交，避免重试造成重复下单。
        """
        cid = client_order_id or self._gen_cid(symbol)
        params = {"symbol": symbol, "side": side, "type": "MARKET",
                  "quantity": quantity, "newClientOrderId": cid}
        try:
            r = self._post("/fapi/v1/order", params)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            return self.query_order(symbol, cid)
        # 市价单提交可能返回 status=NEW（已受理但成交信息未同步，executedQty=0）。
        # 轮询查询直到 FILLED，拿真实 executedQty/avgPrice，否则平仓会因 qty=0 报 -4003。
        for _ in range(10):
            if r.get("status") == "FILLED":
                break
            time.sleep(0.3)
            try:
                r = self.query_order(symbol, cid)
            except Exception:
                break
        return r

    def open_long(self, symbol: str, quantity: float, leverage: int = 1,
                  client_order_id: str | None = None) -> dict:
        """开多：设杠杆 + 市价买入。"""
        self.set_leverage(symbol, leverage)
        return self.market_order(symbol, "BUY", quantity, client_order_id)

    def close_long(self, symbol: str, quantity: float,
                   client_order_id: str | None = None) -> dict:
        """平多：市价卖出。"""
        return self.market_order(symbol, "SELL", quantity, client_order_id)

    def income(self, symbol: str, start_ts: int, end_ts: int,
               income_type: str | None = None, limit: int = 1000) -> list[dict]:
        """资金流水（手续费/资金费率/划转）。时间单位秒。"""
        params = {"symbol": symbol, "startTime": start_ts * 1000,
                  "endTime": end_ts * 1000, "limit": limit}
        if income_type:
            params["incomeType"] = income_type
        return self._get("/fapi/v1/income", params, signed=True)

    def realized_costs(self, symbol: str, start_ts: int, end_ts: int) -> tuple[float, float]:
        """(start, end] 内某币的已实现成本，返回 (佣金, 资金费率)，单位 USDT。

        两者均为「净支出」：佣金恒为正（付出去）；资金费率付为正、收为负。
        """
        rows = self.income(symbol, start_ts, end_ts)
        commission = sum(-float(r["income"]) for r in rows
                         if r.get("incomeType") == "COMMISSION")
        funding = sum(-float(r["income"]) for r in rows
                      if r.get("incomeType") == "FUNDING_FEE")
        return commission, funding

    # ------------------------------------------------------------ 精度
    def lot_size(self, symbol: str) -> tuple[float, float]:
        """返回 (stepSize, minQty)。"""
        if symbol not in self._step_cache:
            info = self._get("/fapi/v1/exchangeInfo")
            for s in info.get("symbols", []):
                if s["symbol"] == symbol:
                    for f in s.get("filters", []):
                        if f["filterType"] == "LOT_SIZE":
                            self._step_cache[symbol] = (float(f["stepSize"]), float(f["minQty"]))
                            break
                    break
        return self._step_cache.get(symbol, (1e-8, 0.0))

    def round_qty(self, symbol: str, quantity: float) -> float:
        """按 stepSize / minQty 取整下单数量，避免精度拒单。"""
        step, minq = self.lot_size(symbol)
        q = max(quantity, minq)
        q = int(q / step) * step
        precision = len(str(step).rstrip("0").split(".")[-1]) if "." in str(step) else 0
        return round(q, precision)

    def usdt_to_qty(self, symbol: str, usdt_amount: float, price: float) -> float:
        """把 USDT 金额换算成合约数量（1x 杠杆下名义=金额），并按精度取整。"""
        if price <= 0:
            return 0.0
        return self.round_qty(symbol, usdt_amount / price)
