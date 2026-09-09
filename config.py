# -*- coding: utf-8 -*-
"""统一配置：所有可调参数集中在这里，改参数只动这一个文件。"""
from pathlib import Path

# ================================================================ 路径
ROOT = Path(__file__).resolve().parent
DB_PATH = str(ROOT / "data" / "monitor.db")
CACHE_DIR = ROOT / "data" / "model_cache"
OUTPUT_DIR = ROOT / "data" / "model_out"
STATE_PATH = ROOT / "data" / "system_state.json"

# 确保数据目录存在（干净环境首次运行自动创建，避免 sqlite 因目录缺失报错）
for _d in (CACHE_DIR, OUTPUT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ================================================================ 网络
import os as _os
PROXY = _os.environ.get("BINANCE_PROXY", "")   # 直连留空；需代理时设置环境变量 BINANCE_PROXY=http://host:port
TIMEOUT = 15

# 交易环境：测试网（假钱，验证执行层）vs 主网（真钱）。
# 上线时设环境变量 BINANCE_TESTNET=0，或填真实合约交易 key 自动切主网。
# 行情 + 交易统一走同一环境：测试网 testnet.binancefuture.com，主网 fapi.binance.com。
# （否则会像之前一样：交易连测试网、行情死磕主网 fapi，本地被 418 封锁后数据断更。）
USE_TESTNET = _os.environ.get("BINANCE_TESTNET", "1") == "1"
BASE_URL = "https://testnet.binancefuture.com" if USE_TESTNET else "https://fapi.binance.com"
WS_URL = "wss://stream.binancefuture.com" if USE_TESTNET else "wss://fstream.binance.com/stream"
TRADE_BASE_URL = BASE_URL

# ================================================================ 告警
NOTIFY_CHANNEL = "dingtalk"
try:
    from monitor import credentials as _sec
    DINGTALK_WEBHOOK = getattr(_sec, "DINGTALK_WEBHOOK", "")
    DINGTALK_SECRET = getattr(_sec, "DINGTALK_SECRET", "")
except ImportError:
    DINGTALK_WEBHOOK = ""
    DINGTALK_SECRET = ""
DRY_RUN = _os.environ.get("DRY_RUN", "1") == "1"   # 默认 1=只记录不下单；测试网实测下单设 0

# ================================================================ 数据采集
INTERVAL = "1h"
DAYS = 730
KLINE_LIMIT = 1500
MIN_HISTORY_BARS = 300
USE_FUNDING = True
USE_MICRO = True

# 默认交易池（老牌流动性好 USDT 永续）
UNIVERSE = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
    "LTCUSDT", "TRXUSDT", "ETCUSDT", "FILUSDT", "UNIUSDT",
    "ATOMUSDT", "NEARUSDT", "ARBUSDT",
]

# 预测周期
HORIZON = 96
HORIZONS = [1, 4, 8, 12, 24, 48, 72, 96, 168]

# ================================================================ 监控池
BASELINE_TOP_N = 10
BASELINE_REFRESH_SEC = 900
WATCHLIST_TTL_SEC = 6 * 3600
LOOKOUT_MAX = 30
DEPTH_TOP_N = 0
ENTRY_TIERS = [
    (100_000_000, 15.0),
    (20_000_000, 20.0),
    (0, 25.0),
]

# ================================================================ 事件阈值
LARGE_TRADE_DB_USD = 100_000
FLOW_WINDOW_SEC = 300
FLOW_RATIO_THRESH = 0.30
FLOW_CONSECUTIVE = 2
FLOW_TOTAL_MIN_RATIO = 1.0
FLOW_COOLDOWN_SEC = 1800
LIQ_WINDOW_SEC = 300
LIQ_OI_RATIO = 0.05
LIQ_COUNT = 10
LIQ_COOLDOWN_SEC = 900
OI_WINDOW_SEC = 300
OI_CHANGE_PCT = 3.0
OI_COOLDOWN_SEC = 1800
DEPTH_LEVELS = 20
DEPTH_IMBALANCE_THRESH = 0.70
DEPTH_CONFIRM = 3
DEPTH_POLL_SEC = 10
DEPTH_COOLDOWN_SEC = 1800
FUNDING_EXTREME = 0.0005
FUNDING_COOLDOWN_SEC = 14400
OI_POLL_SEC = 30
RATIO_POLL_SEC = 60
WS_STALE_SEC = 90

# ================================================================ 数据健康
HEALTH_CHECK_SEC = 900
HEALTH_STALE_SEC = 3 * 3600
HEALTH_MIN_SYMBOLS = 1
HEALTH_HEARTBEAT_SEC = 24 * 3600
HEARTBEAT_SEC = 60
EXTERNAL_PING_URL = ""

# ================================================================ 链上
ONCHAIN_ENABLED = True
try:
    from monitor.credentials import ETHERSCAN_API_KEY, BSCSCAN_API_KEY
except ImportError:
    ETHERSCAN_API_KEY = ""
    BSCSCAN_API_KEY = ""
ONCHAIN_CHAINS = {
    "eth": {"url": "https://api.etherscan.io", "chainid": 1, "key": ETHERSCAN_API_KEY},
    "bsc": {"url": "https://api.bscscan.com", "chainid": 56, "key": BSCSCAN_API_KEY},
}
WHALE_TRANSFER_USD = 5_000_000
WHALE_TRANSFER_DB_USD = 1_000_000
WHALE_COOLDOWN_SEC = 1800
ONCHAIN_POLL_SEC = 10
ONCHAIN_LATEST_N = 1000
STABLE_TOKENS = {
    "eth": {
        "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
        "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    },
    "bsc": {
        "USDT": "0x55d398326f99059fF775485246999027B3197955",
        "BUSD": "0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56",
    },
}
ERC20_TOKENS = {
    "eth": {
        "LINK": "0x514910771AF9Ca656af840dff83E8264EcF986CA",
        "UNI": "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984",
        "AAVE": "0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9",
    },
}
ERC20_FLOW_MIN_USD = 100_000
ERC20_FLOW_POLL_SEC = 600
EXCHANGE_ADDRS = {
    "eth": {
        "0x28c6c06298d514db089934071355e5743bf21d60": "Binance",
        "0x267be1c1d684f78cb4f6a176c4911b741e4ffdc0": "Kraken",
    },
    "bsc": {
        "0x28c6c06298d514db089934071355e5743bf21d60": "Binance",
    },
}
WHALE_WALLETS = {
    "Binance 热钱包": {
        "role": "exchange",
        "eth": "0x28C6c06298d514Db089934071355E5743bf21d60",
        "bsc": "0x28C6c06298d514Db089934071355E5743bf21d60",
    },
    "Tether 金库": {
        "role": "treasury",
        "eth": "0x5754284f345afc66a98fbB0a0Afe71e0F007B949",
        "bsc": "",
    },
}

# ================================================================ 回测
COST_SIDE = 0.0006
TRAIN_DAYS = 90
TEST_DAYS = 30
STEP_DAYS = 30
MIN_TRAIN_ROWS = 5000
HOLD_HOURS = 96

# ================================================================ 策略：插针抄底
# 注：策略的权威参数在 engine/strategies/pin.py 的类属性里，此处仅为文档性镜像。
PIN_TH = -0.05            # 针尖跌超 -5% 触发
PIN_LOOKBACK = 15         # 15 分钟窗口
PIN_HOLD_MIN = 720        # 持有 12h
PIN_POS = 0.05            # 单笔基础仓位（占总资金 5%）
PIN_WEIGHT_EXP = 2        # 深度平方加权
PIN_WEIGHT_CAP = 3.0      # 深度封顶（单笔最大 3×5%=15%）
PIN_TOP_N = 50            # 币池：流动性前 50（Calmar 拐点扫出的自然边界）

# ================================================================ 币池定池规则（edge 本质：插针→回归，回归的锚有多强）
# 回归的锚分三层（按强度降序）：
#   ① 现货-合约套利锚（无风险，最强）：合约价低于现货价时套利者买合约卖现货，把价拉回 → 前提「有现货盘」
#   ② 做市商库存再平衡锚：做市商被动接单后主动对冲 → 前提「有活跃流动性」
#   ③ 投机抄底锚（最弱，纯赌 V 反）
# pin 的 edge 成立 = 回归锚足够强 = 至少有 ①。定池 = 筛「回归锚强」的币，三条正面条件：
#   1. underlyingType == COIN：是加密资产（排除跳空/事件驱动的股票/贵金属/大宗/ETF）
#   2. 有现货盘：现货-合约套利锚存在（排除「仅合约无现货」的 Alpha 币/新 meme，它们插针=真相归零）
#   3. 流动性下限 LIQUID_MIN_ADV（可选）：挡「无承接盘」微盘；None=不启用，靠 top_n 控制
# 「上线时长 onboardDate」是 2+3 的代理（新币≈无现货+低流动性），已被正面定义取代，不再用于定池。
TRADFI_PATH = ROOT / "data" / "tradfi_symbols.json"   # TradFi 黑名单（underlyingType != COIN，由 fetch 生成）
NO_SPOT_PATH = ROOT / "data" / "no_spot_symbols.json"  # 仅合约无现货名单（无现货=套利锚缺失，由 fetch 生成；旧静态版，被 point-in-time 取代）
ONBOARD_PATH = ROOT / "data" / "onboard_dates.json"    # COIN 上线时间（诊断保留，定池不再用）
SPOT_ONBOARD_PATH = ROOT / "data" / "spot_onboard_dates.json"  # 现货上线时间（klines 最早日线兜底，point-in-time「有现货」判断用）
LIQUID_WINDOW_DAYS = 90   # 定池滚动窗口天数
LIQUID_MIN_ADV = None     # 流动性下限（USD/分钟平均成交额；None=关闭）

# ================================================================ 策略：慢动量
MOM_LOOKBACK_H = 30 * 24
MOM_SKIP_H = 24
MOM_TOP_N = 20
MOM_POS_MAX = 0.05
MOM_TREND_OFFSET = 1.0
ATR_MULT = 3.0
TREND_MA_HOURS = 720

# ================================================================ 调度器
LOOP_MINUTES = 60
POLL_SEC = 60

# ================================================================ 实盘交易（live_trader.py）
# 资金管理（严格忠实回测仓位模型：单笔 5% 基础 × 深度加权封顶 15%）
LIVE_LEVERAGE = 1             # 杠杆 1x（无杠杆，无强平风险）
LIVE_SINGLE_POSITION = True   # 单仓模式：同时只持 1 仓，单笔满仓（小额账户，低频插针无需分仓）
LIVE_POSITION_FRACTION = 0.95 # 满仓占用比例，留 5% 手续费/资金费率余量
LIVE_MAX_CONCURRENT = 1       # 并发持仓上限（单仓模式 = 1；分仓模式改回 20）
LIVE_MAX_POSITION = 1.0       # 总仓位硬顶 100%
LIVE_MAX_DRAWDOWN = 0.70      # 熔断线：权益跌破初始资金 70% 暂停
LIVE_STALE_SEC = 3 * 24 * 3600  # 数据新鲜度：1m 数据停更超此值视为断更
LIVE_INITIAL_USDT = float(_os.environ.get("LIVE_INITIAL_USDT", "1000"))  # dry_run 纸面初始资金

# 交易成本（真实入账）：合约市价 taker 费率 + 资金费率（每 8h 结算一次）
TAKER_FEE = 0.0005            # 市价单 taker 费率（VIP0 ≈ 0.05%）
FUNDING_RATE_EST = 0.0001     # 资金费率估算（每 8h 0.01%，仅回测兜底；实盘从 income 拉真实值）

# 看板鉴权（serve.py）：默认只绑本机 127.0.0.1；DASH_PASS 非空时启用 Basic Auth
DASH_HOST = _os.environ.get("DASH_HOST", "127.0.0.1")
DASH_USER = _os.environ.get("DASH_USER", "admin")
DASH_PASS = _os.environ.get("DASH_PASS", "")