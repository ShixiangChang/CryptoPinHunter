# 🎯 CryptoPinHunter

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Platform: Binance USDT-M Futures](https://img.shields.io/badge/platform-Binance%20USDT--M%20Futures-orange.svg)](https://www.binance.com/en/futures)
[![CI](https://github.com/ShixiangChang/CryptoPinHunter/actions/workflows/ci.yml/badge.svg)](https://github.com/ShixiangChang/CryptoPinHunter/actions/workflows/ci.yml)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/ShixiangChang/CryptoPinHunter/pulls)

A research-grade algorithmic trading system for **Binance USDT-M perpetual futures**, built around one idea: liquidation cascades throw prices briefly below fair value — and that dislocation is tradeable.

**CryptoPinHunter** detects these wicks ("pins"), filters out the ones that are genuine information shocks, and enters mean-reversion positions that capture the snap-back to fair value. Backtesting, paper trading and live execution share a single code path and a single validation discipline — no strategy is trusted until it survives out-of-sample testing.

> ⚠️ **Disclaimer**
>
> This software is for **research and educational purposes only**. It is **not investment advice**. Cryptocurrency derivatives trading involves substantial risk of loss, including loss of your entire capital. **Never trade money you cannot afford to lose.** The authors accept no responsibility for any trading results. Always validate any strategy in paper mode before risking real funds.

---

## How it works

Perpetual-futures markets are structurally long-biased: leveraged longs dominate, funding is usually positive, and crowd positions build up over time. When the price drops sharply, leveraged longs are liquidated in a cascade. The cascade forces mechanical, price-insensitive selling that drives the price **below fair value** in a matter of minutes — a "pin".

That pin is a **dislocation, not a signal**. Fair value reasserts itself through two mechanisms:

1. **Spot-futures arbitrage** — the futures price below the spot price is an arbitrage opportunity; arbitrageurs buy the future and sell the spot, pulling the price back.
2. **Market-maker inventory rebalancing** — market makers that sold into the cascade rebalance their inventory by buying back.

CryptoPinHunter only acts when the dislocation is *mechanical* rather than *informational*. Its entry conditions answer one question: **"Is this crash forced selling of crowded longs (mean-reverting), or is it new information (trend-changing)?"**

| Entry condition | What it detects | The question it answers |
|---|---|---|
| Sharp wick (≈ -5% in 15 min) that recovers | Pin morphology | "Is this a liquidity squeeze?" |
| Positive funding before the pin | Crowded longs | "Were forced sellers leveraged longs — or informed sellers?" |
| Price near its 90-day high | Intact trend | "Is this a dip in an uptrend — or a falling knife?" |
| Liquid, spot-backed instrument | Reversion anchor exists | "Is there a mechanism to pull price back?" |

The position is held for a fixed 12-hour window, then closed. No price stop-loss is used: the tail risk is managed *ex ante* by never entering instruments without a strong reversion anchor, not by stops that cannot fill during a cascade.

## Features

- [x] **One code path** — backtest, paper and live execution consume the same signals from the same `Strategy` contract
- [x] **Wick-first detection** — pins are detected on the low (the wick tip), not the close, so deep momentary dislocations are not missed
- [x] **Funding filter** — only enters when funding is positive (crowded longs → cascade is forced selling)
- [x] **Trend filter** — skips coins that have broken down deeply from their 90-day high (falling knives)
- [x] **Spot-backed universe** — instruments without a spot market are excluded (no arbitrage anchor, no edge)
- [x] **Survivorship-bias-aware** — the universe is re-selected on a rolling window and instrument availability is evaluated point-in-time, never with hindsight
- [x] **Look-ahead-safe backtesting** — every decision at time *t* uses only data available before *t* (walk-forward validation)
- [x] **1-minute data pipeline** — 1m/1h klines and funding history collected from the Binance API into SQLite
- [x] **Live dashboard** — self-hosted web status board (equity curve, positions, decisions)
- [x] **Real trading** — production order execution on Binance USDT-M futures with HMAC-signed requests

## Validation methodology

Backtest results are easy to fake. This project treats the following as the *minimum bar* before any result may be quoted as evidence:

- **No-look-ahead walk-forward** — at time *t*, only data before *t* is used for any decision (universe selection, filters, parameters)
- **Point-in-time availability** — a coin is tradeable only after it actually listed spot / futures, never retroactively
- **Out-of-sample parameter checks** — parameters are validated on data they were not fit on
- **Survivorship awareness** — delisted / dead coins leave no data, which silently inflates returns; this bias is bounded explicitly rather than ignored
- **Explicit bias labeling** — every backtest output is declared 【look-ahead】 or 【no-look-ahead】; only the latter may support conclusions about profitability

## Known limitations

- **Low frequency by design** — pins cluster in liquidation cascades; quiet markets can produce weeks of zero signals
- **Regime-dependent** — the edge lives in volatile / bull-phase markets. There is **no bull/bear regime gate** in this system; in a trend reversal it can lose persistently. Evaluate carefully before using it with real capital
- **Survivorship floor is qualitative** — the residual bias from dead coins is bounded in mechanism but cannot be fully quantified

## Quick start

```bash
# 1. Install (Python 3.10+)
pip install -r requirements.txt

# 2. Backtest-only? Skip credentials. Paper / live? Create the template.
cp monitor/credentials_example.py monitor/credentials.py
#    Fill in Binance API key — grant "Futures trading" only, never "Withdraw"

# 3. Pre-fetch history (universe ranking needs ~90d volume, trend filter needs 1h, funding filter needs funding)
bash deploy/fetch_history.sh

# 4. Backtest
python scheduler.py --backtest

# 5. Paper-trade a single pass / run continuously
python scheduler.py --once
python scheduler.py --loop

# 6. Live (DRY_RUN=1 is paper; switch off only after you understand the risks)
export DRY_RUN=0
python live_trader.py --loop
```

> **Network note:** Binance API is geo-restricted in some regions. Connect directly if possible; otherwise set `BINANCE_PROXY=http://host:port` as an environment variable — never hardcode a proxy into the code.

## Repository layout

```
engine/             Strategy framework — signals, backtest engine, execution, state
  strategies/       pin.py (the core strategy), momentum.py, beta.py
monitor/            Data collection — 1m/1h klines, funding history, health checks
tools/              Research utilities — universe metadata, out-of-sample validation
deploy/             Deployment — dependency setup, history pre-fetch, daemon
live_trader.py      Live trading loop (signal → risk → execute → settle → persist)
live_status.py      Dashboard generator (refreshes every 60s)
serve.py            Dashboard HTTP server (port 8777)
scheduler.py        Backtest / paper entry point
config.py           Central configuration
tests/              Hermetic unit tests (no network, no live credentials)
```

## Documentation

- **[DEPLOY.md](DEPLOY.md)** — standalone server deployment guide (Ubuntu, setup, verification checklist)
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — development setup, test discipline, PR guidance
- **[LICENSE](LICENSE)** — MIT

## Contributing

Issues and pull requests are welcome. For bugs, please [open an issue](https://github.com/ShixiangChang/CryptoPinHunter/issues) with the log output and a minimal reproduction. For changes, open a PR — keep the existing validation discipline in mind: no look-ahead, results labeled honestly.
