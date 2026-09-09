# -*- coding: utf-8 -*-
"""补拉 UNIVERSE 缺 1m 的 8 币，让主模型 18 币全覆盖 1m 因子。

背景：eval_1m 已证实 1m 因子（尤其 rv_7d_1m）比 1h 波动率因子更强（IC 翻倍 + 第一特征），
但 UNIVERSE 18 币里只有 10 币有完整 1m。补拉这 8 币后主模型可全员接入。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from monitor.fetch_1m import (
    fetch_symbol, init_table, START_TS, load_progress, save_progress,
)

SYMBOLS = ["AVAXUSDT", "DOTUSDT", "LTCUSDT", "TRXUSDT",
           "ETCUSDT", "FILUSDT", "ATOMUSDT", "ARBUSDT"]


def main():
    init_table()
    now_ms = int(time.time() * 1000)
    progress = load_progress()
    for s in SYMBOLS:          # 清除旧进度，强制从起点重拉补齐（INSERT OR REPLACE 幂等）
        progress.pop(s, None)
    save_progress(progress)

    print(f"补拉 {len(SYMBOLS)} 币 1m，起点 2024-08-29", flush=True)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fetch_symbol, s, START_TS, now_ms): s for s in SYMBOLS}
        for fut in as_completed(futs):
            s = futs[fut]
            try:
                n = fut.result()
                progress[s] = now_ms
                save_progress(progress)
                print(f"[{s}] +{n:,} 条（{time.time()-t0:.0f}s）", flush=True)
            except Exception as e:
                print(f"[{s}] 失败: {e}", flush=True)
    print("补拉完成", flush=True)


if __name__ == "__main__":
    main()
