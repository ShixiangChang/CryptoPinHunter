#!/usr/bin/env bash
# 部署时一次性预采：三类历史数据（pin 策略三条过滤的运行时上下文）
#   1. 1m 近 3 个月   —— 定池（滚动窗口成交额 top50）
#   2. 1h 近 90 天    —— 趋势过滤（过去 90 天最高 high）
#   3. funding 近 90 天 —— funding 过滤（针前最近一次已结算 funding）
# 池子取 ticker/24hr 成交额前 150（比定池 top50 大，留余量给 TradFi + 无现货剔除）。
# 之后启动 daemon，live_feed.py 负责持续增量。
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY=$(command -v python3 || command -v python)
fi
TOP=150

# ---- 1. 1m 近 3 个月（走币安官方月度 zip，不碰 REST 限流）----
MONTHS=$($PY -c "
from datetime import datetime, timezone
now = datetime.now(timezone.utc)
months = []
y, m = now.year, now.month
for _ in range(3):
    m -= 1
    if m == 0:
        m = 12
        y -= 1
    months.append(f'{y}-{m:02d}')
print(','.join(reversed(months)))
")

echo "==> [1/3] 采 1m 近 3 个月: $MONTHS"
$PY monitor/fetch_1m_monthly.py --top "$TOP" --months "$MONTHS"

# ---- 2+3. 1h + funding 近 90 天（REST 分页）----
echo "==> [2/3] 采 1h + funding 近 90 天"
$PY monitor/fetch_1h_funding.py --top "$TOP" --days 90

echo "==> [3/3] 预采完成"
