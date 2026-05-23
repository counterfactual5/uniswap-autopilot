# Contributing to uniswap-autopilot

## Setup

```bash
git clone https://github.com/counterfactual5/uniswap-autopilot.git
cd uniswap-autopilot
uv pip install -e ".[dev]"
```

## Running Tests

```bash
uv run pytest tests/ -v
```

99 tests covering swap execution, LP management, analytics, price feeds, trading evaluation, and security gates.

## Code Style

- Python 3.10+ compatible
- 120-char line length
- Public functions have docstrings
- Contract addresses verified against Uniswap official deploy-addresses repo

## Project Structure

```
src/uniswap_autopilot/
├── analytics/     # IL analysis, position suggestions, risk/health
├── common/        # RPC helpers, tokens, chains, security utilities
├── data/          # Token address catalogs
├── execute/       # Trade execution, signing, Telegram confirmation
├── lp/            # LP position management (v3 + v4)
├── price_feed.py  # CoinGecko price oracle
├── search/        # Token search and risk scoring
└── swap/          # Swap quoting and execution flow
```

## Adding a New Chain

1. Add chain config to `common/common.py`
2. Verify contract addresses for UniversalRouter and PoolManager
3. Add token catalog to `data/`

## Pull Requests

1. Fork → feature branch → changes + tests → PR to `master`
2. Keep PRs focused — one concern per PR
