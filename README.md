# CryptoPinHunter — 永续合约插针回归量化交易系统

面向 **Binance USDT 本位永续合约**的自动化交易研究系统：统一策略框架（回测 / 纸面 / 实盘同源）、1 分钟级数据采集、科学验证纪律、7×24 实盘执行与看板。核心策略为 **pin（插针抄底）**——捕捉杠杆强平造成的流动性错杀，赚「错杀 → 回归」的钱。

> ⚠️ **免责声明**：本项目为量化交易研究用途，**不构成任何投资建议**。加密货币合约交易风险极高，可能损失全部本金。使用本项目产生的任何盈亏与作者无关。

---

## 项目定位（先读这段）

本项目遵循两条核心方法论，所有代码与结论都围绕它们展开：

1. **edge 从哪里来，哪里就是本质。** 一切分类、定池、选参，都从「策略凭什么赚钱」倒推，不从名字、标签、历史偶然推。`engine/strategies/pin.py` 的 edge 来自「合约流动性错杀 → 价格回归」，因此定池只筛「回归锚足够强」的资产。
2. **验证必须用科学方法，否则退化成拟合。** 认知链条：机制推演 → 可证伪预测 → 科学验证 → 数据检验。数据是「证伪」的裁判，不是「发现规律」的工具。所有回测结论必须声明**【前视】或【无前视】**档位（判定标准见下文「科学验证纪律」）。

**本项目不追求"漂亮的回测数字"。** 回测中易混入两类系统性偏差——**幸存者偏差**（归零/下架币无数据被天然过滤）与**全周期前视定池**（用回测期之后的信息选池）。结论可信的最低门槛：walk-forward（无前视，t 日只用 t 之前数据）+ point-in-time 现货判据 + 参数样本外检验。

## 架构

```
┌─────────────────────────────────────────────────────────┐
│                       scheduler.py                        │
│         统一入口：--backtest / --once / --loop             │
└───────────────┬─────────────────────────┬────────────────┘
                │                         │
   ┌────────────▼───────────┐   ┌─────────▼───────────┐
   │   engine/ 策略框架      │   │   数据层             │
   │   strategy.py 基类      │   │   monitor/ 采集      │
   │   strategies/pin.py     │   │   features/ 因子     │
   │   backtest.py 回测引擎  │   │   data/monitor.db   │
   │   attribution.py 归因   │   │   (klines_1m/1h/    │
   └────────────┬───────────┘   │    funding_hist)     │
                │               └─────────┬───────────┘
   ┌────────────▼───────────┐             │
   │   实盘执行（可选）       │◄────────────┘
   │   live_trader.py        │
   │   信号→风控→下单→结算    │
   │   live_status.py 看板   │
   └────────────────────────┘
```

- **策略与数据解耦**：一条流水线、一个 `Strategy` 契约（`generate_signals(df)`），回测和实盘喂同一份数据、跑同一份代码。
- **数据库是唯一数据源**：`monitor.db`（SQLite）集中存 1m / 1h K 线、funding 历史、决策与成交流水。

## 目录结构

```
config.py             统一配置（路径 / 网络 / 策略 / 交易参数，改参数只动这一个文件）
engine/               策略框架
  strategy.py         Strategy 基类（generate_signals 是唯一契约）
  strategies/         pin.py（插针抄底）等策略实现
  backtest.py         统一回测引擎（含前视档位强制声明）
  data.py             数据加载（monitor.db 唯一数据源）
  executor.py         币安合约执行层（下单 / 平仓 / 账户，HMAC 签名）
  state.py / attribution.py / __init__.py
monitor/              数据采集与监控（行情 / funding / 数据健康检查）
features/             特征工程（因子计算）
tools/                研究工具：定池元数据抓取、样本外验证、稳健性测试
deploy/               部署脚本（依赖安装 / 历史数据预采 / 守护进程）
live_trader.py        实盘主循环（信号→风控→执行→结算→落库→告警）
live_status.py        实盘看板生成（每 60s 刷新）
serve.py              看板 HTTP 服务（默认 8777）
scheduler.py          回测 / 纸面入口
requirements.txt
```

## 快速开始

```bash
# 1. 依赖（Python 3.10+）
pip install -r requirements.txt

# 2. 准备密钥模板（实盘 / 测试网需要；纯回测不需要）
cp monitor/credentials_example.py monitor/credentials.py
#    填入 Binance API key（只授予「合约交易」权限，勿授予「提现」权限）

# 3. 预采数据（定池需要近 90 天成交额、趋势过滤需要 1h、funding 需要 funding_hist）
bash deploy/fetch_history.sh        # 或手动 python monitor/fetch_1m_monthly.py 等

# 4. 跑回测
python scheduler.py --backtest      # 生成 data/model_out/backtest_*.json

# 5. 纸面跑一轮 / 常驻
python scheduler.py --once
python scheduler.py --loop

# 6. 实盘（DRY_RUN=1 是纸面，确认无误后关掉）
export DRY_RUN=0
python live_trader.py --loop
```

> 网络说明：Binance API 在某些地区需要代理。直连无需设置；需代理时设环境变量 `BINANCE_PROXY=http://host:port`（**不要硬编码代理进代码**）。

## 插针策略（pin）说明

**信号**（全部同时满足才触发，参数见 `engine/strategies/pin.py` 类属性）：

| 条件 | 含义 | 回答的问题 |
|---|---|---|
| 15 分钟内针尖跌超阈值（约 -5%）且收盘反弹 | 插针形态 | 「这是一次流动性挤兑」 |
| 针前 funding 为正 | 多头拥挤 | 「砸盘的是被强平的杠杆多头（错杀）还是真利空」 |
| 距 90 天高点跌幅有限 | 趋势未坏 | 「跌的是浅回调还是阴跌中的僵尸」 |
| 池内流动性 / 现货齐全 | 回归锚存在 | 「价格有没有被拉回的机制」 |

**为什么有 edge（机制推演）**：杠杆多头拥挤 → 下跌触发连环强平 → 价格被砸到公允值之下 → 现货-合约套利 / 做市商库存再平衡把价格拉回。赚的是「错杀修复」，不是方向预测。

**已知局限（诚实声明）**：
- 高频但低频：只在插针潮出现，平静期可能数周零触发；
- **regime 依赖**：该策略是波动期 / 多头市场策略，牛熊转换期可能持续亏损——系统不含牛熊 regime 闸门，请知悉后再决定是否实盘使用；
- 归零币（无数据）造成的幸存者偏差无法完全消除，结论只能定性声明、不能定量封顶。

## 数据

- `klines_1m`：1 分钟 K 线（插针检测、定池成交额）
- `klines`：1 小时 K 线（趋势过滤）
- `funding_hist`：资金费率历史（funding 过滤）
- `live_pin_decisions` / `live_pin_trades`：实盘决策与成交流水（每笔可复盘）

定池元数据（白名单进版本库，可被 `tools/fetch_coin_universe.py` / `tools/fetch_spot_onboard.py` 重新生成）：
- `data/tradfi_symbols.json` — 非加密资产（股票/贵金属等）永续黑名单
- `data/no_spot_symbols.json` — 仅合约无现货名单
- `data/spot_onboard_dates.json` — 现货上线时间（point-in-time「有现货」判据）

## 科学验证纪律

本项目在回测与验证上执行以下纪律，防止「验证」退化成「拟合」：

- 回测输出必须声明【前视】或【无前视】档位，未声明不得作为收益结论；
- 【前视】结果只用于机制筛选（比较开关某变量前后的差值），不得用于「赚多少」结论；
- 只有 walk-forward（t 日只用 t 之前数据）+ point-in-time 判据 + 参数样本外的结果，才有资格支撑实盘决策；
- 判定只三档：证实 / 证伪 / 不确定，结论必须落在三态之一；
- 多重比较需校正（Bonferroni / FDR）；结论须可复现（数据哈希 + 代码版本 + 一条命令重跑）。

## 相关文档

- `DEPLOY.md` — Linux 服务器部署与启动后验证清单

## License

[MIT](LICENSE)
