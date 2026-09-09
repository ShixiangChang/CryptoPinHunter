# 部署指南

在 Linux 服务器上部署本系统（实盘 / 纸面运行）的步骤。

## 部署内容与数据

- 仓库含全部代码、部署脚本、定池元数据（`data/*.json`，已随仓库提供）。
- **行情数据不随仓库分发**，由部署后的预采步骤从 Binance API 拉取：
  - 1m K 线（近 3 个月）——插针检测与定池成交额统计
  - 1h K 线（近 90 天）——趋势过滤（滚动高点）
  - funding 历史（近 90 天）——funding 过滤
  - 趋势过滤与 funding 过滤依赖上述历史窗口；未预采就启动，这两个过滤条件无数据可用。

## 前置条件

- Linux 服务器，Python 3.10+
- 网络可达 Binance API（部分地区需代理，见下文环境变量）
- 磁盘 ≥ 10G（代码约几 MB + 90 天行情数据约数百 MB + 虚拟环境）

## 部署步骤

### 1. 获取代码

```bash
git clone <repo-url> && cd <repo-dir>
# 或解压打包文件：unzip <archive>.zip -d <dir> && cd <dir>
```

### 2. 安装依赖

```bash
bash deploy/setup.sh
```

### 3. 配置 API 凭据

```bash
cp monitor/credentials_example.py monitor/credentials.py
vi monitor/credentials.py
```

填入 Binance 合约 API key / secret。安全要求：**只授予「合约交易」权限，不要授予「提现」权限。** 该文件已被 .gitignore 排除，不会进入版本库。

### 4. 预采历史数据（首次部署必需）

```bash
bash deploy/fetch_history.sh
```

### 5. 配置运行模式（环境变量）

```bash
export BINANCE_TESTNET=1    # 1=测试网（默认）；0=主网
export DRY_RUN=1            # 1=纸面，仅记录不下单（默认）；0=真实下单
export LIVE_INITIAL_USDT=10000   # 纸面模式的初始资金（DRY_RUN=1 时使用）
export BINANCE_PROXY=       # 直连留空；需要代理时设为 http://host:port
export DASH_PASS=           # 看板访问密码；非空时启用
```

生产环境建议将这些写入 `.env` 文件或 systemd 环境配置，而非交互式 export。

### 6. 启动

```bash
bash deploy/start.sh        # 后台启动（守护各进程）
bash deploy/start.sh fg     # 前台调试
bash deploy/start.sh stop   # 停止
```

状态看板：`http://<server-ip>:8777`（`DASH_PASS` 非空时需密码）。

## 启动后验证

```bash
# 1. 1m K 线在持续更新（最新 open_time 距当前 < 60s）
sqlite3 data/monitor.db "SELECT MAX(open_time) FROM klines_1m;"

# 2. 三张核心表都有数据
sqlite3 data/monitor.db "SELECT 'klines_1m', COUNT(*) FROM klines_1m
  UNION ALL SELECT 'klines', COUNT(*) FROM klines
  UNION ALL SELECT 'funding_hist', COUNT(*) FROM funding_hist;"

# 3. 日志无异常
tail -50 logs/live_feed.log
tail -50 logs/live_trader.log
```

数据新鲜度指标：1m 最新时间 < 60s、1h 与 funding 行数 > 0。不达标说明采集未正常工作。

## 风控与停止

- 熔断：权益跌破初始资金的一定比例（`LIVE_MAX_DRAWDOWN`，默认 0.7）自动暂停交易。
- 手动停止：`bash deploy/start.sh stop`。
- 紧急处理：先在交易所后台手动平仓，再停止进程。
- 凭据泄露：立即在交易所后台删除并重建 API key。
