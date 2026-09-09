#!/usr/bin/env bash
# 一键初始化：建 venv、装依赖、建目录、初始化 credentials（密钥文件）
# 用 venv 隔离（兼容 Ubuntu 24.04+ 的 PEP 668 限制），后续脚本自动复用 .venv。
set -euo pipefail
cd "$(dirname "$0")/.."

PY=$(command -v python3 || command -v python)
if [ -z "$PY" ]; then
  echo "错误：需要 Python 3.10+，请先安装"
  exit 1
fi
echo "==> Python: $($PY --version)"

if [ ! -d .venv ]; then
  echo "==> 创建 venv"
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate

echo "==> 安装依赖"
pip install --upgrade pip >/dev/null
pip install -r requirements.txt

echo "==> 建目录"
mkdir -p data/model_out logs

echo "==> 初始化 secrets"
if [ ! -f monitor/credentials.py ]; then
  cp monitor/credentials_example.py monitor/credentials.py
  echo "已生成 monitor/credentials.py —— 请填币安 API key"
fi

echo ""
echo "安装完成。接下来三步："
echo "  1. vi monitor/credentials.py   填币安 API key（只开『合约交易』权限，不开提现）"
echo "  2. bash deploy/fetch_history.sh   预采 90 天历史上下文（1m 定池 + 1h 趋势 + funding）"
echo "  3. bash deploy/start.sh   启动实盘 + 看板"
