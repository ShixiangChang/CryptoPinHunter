# -*- coding: utf-8 -*-
"""回溯补历史：把「链上鲸鱼」+「官方短期 OI / 多空比」填进 monitor.db，减少"从零攒"的等待。

用法（在 quantum 目录下）：
    python -m monitor.backfill                     # 默认回溯 14 天链上 + 官方短期 OI/多空比
    python -m monitor.backfill --days 30          # 链上回溯 30 天
    python -m monitor.backfill --skip-onchain     # 只补 OI/多空比

独立于实时监控运行，走 config.PROXY 代理；与监控共用 SQLite（WAL 支持并发）。
"""
from __future__ import annotations

import argparse
import sqlite3
import time

import requests

from . import config as mcfg

# 从统一 config 继承（monitor/config 已 re-export 根 config）
SYMBOLS = mcfg.UNIVERSE


def _proxies():
    return {"http": mcfg.PROXY, "https": mcfg.PROXY} if mcfg.PROXY else None


def _conn():
    c = sqlite3.connect(mcfg.DB_PATH)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    return c


def _binance(path, params):
    r = requests.get(mcfg.BASE_URL + path, params=params, timeout=30, proxies=_proxies())
    r.raise_for_status()
    return r.json()


def _etherscan(chain, module, action, offset, **params):
    cc = mcfg.ONCHAIN_CHAINS[chain]
    q = {"chainid": cc["chainid"], "module": module, "action": action,
         "page": 1, "offset": offset, "sort": "desc", "apikey": cc["key"], **params}
    r = requests.get(f"{cc['url']}/v2/api", params=q, timeout=30, proxies=_proxies())
    r.raise_for_status()
    data = r.json()
    if str(data.get("status")) != "1":
        return None
    res = data.get("result")
    return res if isinstance(res, list) else None


def _max_offset(chain: str) -> int:
    """探测免费档允许的最大 offset（10000 不行就退到 1000）。"""
    cc = mcfg.ONCHAIN_CHAINS[chain]
    contract = list(mcfg.STABLE_TOKENS.get(chain, {}).values())
    if not contract:
        return 1000
    for off in (10000, 1000):
        try:
            rows = _etherscan(chain, "account", "tokentx", off, contractaddress=contract[0])
            if rows is not None:
                return off
        except Exception:
            pass
    return 1000


# ---------------- 链上鲸鱼回溯 ----------------
def backfill_onchain(days: int = 14) -> int:
    conn = _conn()
    existing = {r[0] for r in conn.execute("SELECT txhash FROM onchain_txs")}
    cutoff = int(time.time()) - days * 86400
    written = 0
    for chain, cc in mcfg.ONCHAIN_CHAINS.items():
        if not cc.get("key"):
            print(f"[backfill] 链上 {chain}: 无 key，跳过")
            continue
        offset = _max_offset(chain)
        print(f"[backfill] 链上 {chain} 回溯 {days} 天，offset={offset}")
        for token, contract in mcfg.STABLE_TOKENS.get(chain, {}).items():
            dec = 6 if token in ("USDT", "USDC") else 18
            page = 1
            while True:
                try:
                    rows = _etherscan(chain, "account", "tokentx", offset,
                                      contractaddress=contract, page=page)
                except Exception as exc:
                    print(f"[backfill] {chain}.{token} 第{page}页失败: {exc}")
                    break
                if not rows:
                    break
                oldest = float("inf")
                for tx in rows:
                    t = int(tx.get("timeStamp") or 0)
                    oldest = min(oldest, t)
                    if t < cutoff:
                        break
                    h = tx.get("hash") or ""
                    if not h or h in existing:
                        continue
                    amount = int(tx.get("value") or 0) / (10 ** dec)
                    if amount < mcfg.WHALE_TRANSFER_DB_USD:
                        continue
                    conn.execute(
                        "INSERT INTO onchain_txs (ts,chain,token,from_addr,to_addr,usd,txhash)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (t, chain, token, tx.get("from", ""), tx.get("to", ""), amount, h))
                    existing.add(h)
                    written += 1
                if oldest < cutoff or len(rows) < offset:
                    break
                page += 1
                time.sleep(0.25)  # 免费档限速
    conn.commit()
    conn.close()
    print(f"[backfill] 链上鲸鱼补入 {written} 条")
    return written


# ---------------- OI 短期历史 ----------------
def backfill_oi() -> int:
    conn = _conn()
    written = fail = 0
    for sym in SYMBOLS:
        try:
            data = _binance("/futures/data/openInterestHist",
                            {"symbol": sym, "period": "5m", "limit": 500})
        except Exception as exc:
            fail += 1
            print(f"[backfill] OI {sym} 历史端点不可用: {exc}")
            continue
        if not data:
            continue
        for d in data:
            t = int(d["timestamp"]) // 1000
            cur = conn.execute("SELECT 1 FROM oi WHERE symbol=? AND ts=?", (sym, t)).fetchone()
            if cur:
                continue
            conn.execute("INSERT INTO oi (ts,symbol,oi_base,notional) VALUES (?,?,?,?)",
                         (t, sym, float(d["sumOpenInterest"]), float(d["sumOpenInterestValue"])))
            written += 1
    conn.commit()
    conn.close()
    if fail == len(SYMBOLS):
        print("[backfill] OI 历史端点整体不可用（USD-T 已不再免费提供长历史），跳过")
    print(f"[backfill] OI 补入 {written} 条")
    return written


# ---------------- 多空比历史 ----------------
def backfill_ratios() -> int:
    conn = _conn()
    written = fail = 0
    for sym in SYMBOLS:
        try:
            gls = _binance("/futures/data/globalLongShortAccountRatio",
                           {"symbol": sym, "period": "1h", "limit": 500})
            tpl = _binance("/futures/data/topLongShortPositionRatio",
                           {"symbol": sym, "period": "1h", "limit": 500})
            tlr = _binance("/futures/data/takerlongshortRatio",
                           {"symbol": sym, "period": "1h", "limit": 500})
        except Exception as exc:
            fail += 1
            print(f"[backfill] ratios {sym} 失败: {exc}")
            continue
        g = {int(d["timestamp"]) // 1000: float(d["longShortRatio"]) for d in gls}
        p = {int(d["timestamp"]) // 1000: float(d["longShortRatio"]) for d in tpl}
        t = {int(d["timestamp"]) // 1000: float(d["buySellRatio"]) for d in tlr}
        for ts in sorted(set(g) & set(p) & set(t)):
            cur = conn.execute("SELECT 1 FROM ratios WHERE symbol=? AND ts=?", (sym, ts)).fetchone()
            if cur:
                continue
            conn.execute("INSERT INTO ratios (ts,symbol,global_ls,top_pos_ls,taker_ls)"
                         " VALUES (?,?,?,?,?)", (ts, sym, g[ts], p[ts], t[ts]))
            written += 1
    conn.commit()
    conn.close()
    print(f"[backfill] 多空比补入 {written} 条")
    return written


def main() -> None:
    p = argparse.ArgumentParser(description="回溯补历史数据")
    p.add_argument("--days", type=int, default=14, help="链上回溯天数")
    p.add_argument("--skip-onchain", action="store_true")
    p.add_argument("--skip-oi", action="store_true")
    p.add_argument("--skip-ratios", action="store_true")
    a = p.parse_args()

    print("[backfill] 开始回溯（DYDX 走 config.PROXY）")
    if not a.skip_onchain:
        backfill_onchain(a.days)
    if not a.skip_oi:
        backfill_oi()
    if not a.skip_ratios:
        backfill_ratios()
    print("[backfill] 完成")


if __name__ == "__main__":
    main()