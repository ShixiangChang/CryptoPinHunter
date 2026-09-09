# Contributing

Thanks for considering contributing to CryptoPinHunter.

## Project status

This is a research-grade automated trading framework, actively developed
against live Binance USDT-M perpetual markets. The signal engine and live
daemon share one code path; changes there affect real order flow if deployed
with `DRY_RUN=0`.

## Development setup

```bash
git clone https://github.com/ShixiangChang/CryptoPinHunter.git
cd CryptoPinHunter
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Run the tests:

```bash
pytest
```

The unit tests in `tests/` are hermetic — they mock the database access layer
and never touch the Binance API or a live database. Keep it that way: a
contribution that requires network or real credentials to run tests will not
pass review.

## What makes a good change

- **One change per PR.** If a bug fix and a refactor are mixed, split them.
- **A failing test first** for bug fixes. The pin trigger logic in
  `tests/test_pin_strategy.py` is the template.
- **Neutral, reader-facing comments.** Comments explain what the code does and
  why it is designed that way. Research-process notes (parameter history,
  personal context) belong elsewhere, not in committed code.
- **No lookahead.** Anything that makes a decision at time `t` must only read
  data with `open_time <= t`. Walk-forward validation is a core discipline of
  this project; a signal change without an out-of-sample claim will not be merged.

## Code layout

```
engine/      Strategy interface, backtest engine, data access, execution
monitor/     Data collection (klines, funding, health)
features/    Feature engineering
tools/       Research utilities (validation scripts)
deploy/      Deployment (dependency setup, history pre-fetch, process daemon)
tests/       Hermetic unit tests
```

## Reporting issues

Include the Python version, the commit or version you are on, and a minimal
reproduction. Do not include live API keys or account details in issues.

## License

By contributing you agree that your contributions are licensed under the MIT
License — see [LICENSE](LICENSE).
