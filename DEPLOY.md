# 香港服务器部署清单

pin 实盘策略从本地迁移到香港服务器的完整步骤。核心原则：**只迁代码 + 定池元数据，不迁历史数据**（6.6G 的 `monitor.db` 是回测资产，服务器重新采集）。

---

## 一、为什么「历史数据」分两类（迁移前必须分清）

pin 策略用到的「历史数据」有两个完全不同的用途，迁移只带前者：

| 用途 | 数据 | 粒度/窗口 | 迁移 |
|---|---|---|---|
| **运行时上下文**（策略实时决策的输入） | 1m K 线、1h K 线、funding | 90 天 | ✅ 服务器重新采集（几百 MB） |
| **回测资产**（验证策略能否赚钱） | `monitor.db` 全量 | 2 年 | ❌ 不迁（6.6G，使命已结束） |

三条过滤条件的运行时数据依赖（缺一个，策略就空转/失效）：

| 过滤条件 | 读的表 | 需要的历史 | 用途 |
|---|---|---|---|
| 插针检测 | `klines_1m` | 最近 15 分钟 | 判断「当下是否插针」 |
| **趋势过滤** | `klines`(1h) | 过去 90 天最高 high | 判断「币是不是阴跌中的僵尸」 |
| **funding 过滤** | `funding_hist` | 针前最近一次已结算 funding | 判断「砸盘的是不是被强平的杠杆多头」 |
| 定池 | `klines_1m` | 近 90 天成交额 | 选流动性前 50 + 剔除 TradFi/无现货 |

**实时监控 ≠ 不需要历史。** WebSocket/轮询只推「当下」，推不了「过去 90 天」。策略每做一个决策，都要回查 90 天的历史上下文才能判断「这个针是不是错杀」。

---

## 二、迁移内容

**带：**
- 全部代码（`engine/` `monitor/` `tools/` `deploy/` + 根目录 `.py`）
- 定池元数据（`data/tradfi_symbols.json` `data/spot_onboard_dates.json` `data/spot_base.json`）——三层过滤的输入
- 部署脚本（`deploy/setup.sh` `fetch_history.sh` `start.sh` `daemon.py`）
- `requirements.txt` `config.py`

**不带：**
- `monitor/credentials.py`（密钥，服务器重新填）
- `monitor.db` 及其 wal/shm（6.6G 回测资产）
- `.workbuddy/`（内部记忆）、`.git/`、`__pycache__/`、`data/*.csv` `*.html` 快照

---

## 三、部署步骤

### 0. 前置条件
- 香港服务器，Ubuntu 20.04+，Python 3.10+（币安支持香港，直连无 418）
- 至少 10G 磁盘（代码几 MB + 90 天数据约 300-500MB + venv 约 200MB）

### 1. 本地打包 → 传服务器
```bash
# 本地（Windows，在 quantum 目录）
python pack.py
# 生成 ./quantum_live.zip（代码 + 部署脚本 + 定池元数据，不含密钥/数据库）
```
上传 `quantum_live.zip` 到服务器（scp / 宝塔 / 任意方式），解压：
```bash
unzip quantum_live.zip -d ~/quantum && cd ~/quantum
```

> 或者走 git：`git push` 后服务器 `git clone`。但注意定池元数据在 `data/*.json`，已改 `.gitignore` 让它们进版本库，两种方式都可。

### 2. 装依赖 + 建目录 + 初始化 secrets
```bash
bash deploy/setup.sh
```

### 3. 填密钥（关键）
```bash
vi monitor/credentials.py
```
填 `BINANCE_FUTURES_API_KEY` / `BINANCE_FUTURES_API_SECRET`（币安合约 API key）。
**只开「合约交易」权限，绝不开「提现」权限。**

### 4. 预采 90 天历史上下文（约 5-15 分钟）
```bash
bash deploy/fetch_history.sh
```
这一步采三类：1m 近 3 个月（定池）、1h 近 90 天（趋势过滤）、funding 近 90 天（funding 过滤）。
**不跑这步就启动，趋势过滤和 funding 过滤无数据，策略空转。**

### 5. 配置环境变量（决定真钱还是假钱）
```bash
export BINANCE_TESTNET=0    # 0=主网（真钱）；1=测试网（假钱，默认）
export DRY_RUN=0            # 0=真实下单；1=只记录不下单（默认）
export LIVE_INITIAL_USDT=10000   # 纸面初始资金（dry_run 用）
# 香港直连，BINANCE_PROXY 留空（默认就是空）
```

### 6. 启动
```bash
bash deploy/start.sh        # 后台启动（守护 4 进程）
bash deploy/start.sh fg     # 前台调试
bash deploy/start.sh stop   # 停止
```
看板：`http://服务器IP:8777`（`DASH_PASS` 非空时启用密码，建议公网设密码）

---

## 四、启动后验证清单（缺一不可）

```bash
# 1. 数据新鲜度（1m 应该在 1 分钟内更新）
sqlite3 data/monitor.db "SELECT MAX(open_time) FROM klines_1m;"

# 2. 三张表都有数据
sqlite3 data/monitor.db "SELECT 'klines_1m', COUNT(*) FROM klines_1m
  UNION ALL SELECT 'klines(1h)', COUNT(*) FROM klines
  UNION ALL SELECT 'funding_hist', COUNT(*) FROM funding_hist;"

# 3. 日志无异常
tail -50 logs/live_feed.log
tail -50 logs/live_trader.log
```

**三个数字都要对：** 1m 新鲜 < 60s、klines(1h) > 0、funding_hist > 0。否则策略在瞎跑。

---

## 五、回滚 / 止损

- 熔断：权益跌破初始资金 70% 自动暂停（`LIVE_MAX_DRAWDOWN`）
- 手动停：`bash deploy/start.sh stop`
- 紧急撤所有单：币安后台手动平仓 + `bash deploy/start.sh stop`
- 泄露密钥：立刻去币安后台删除重建 key
