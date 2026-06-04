# Changelog

## [0.5.6] — 2026-06-05

### Added
- **Pair-age risk signal**: DexScreener `pairCreatedAt` feeds risk scoring
  (<24h +25, <7d +15, <30d +5); `pairAgeDays` exposed in market data.

## [0.1.1] — 2026-05-23

### Fixed
- Data files moved into package (`src/uniswap_autopilot/data/`) for correct pip install
- ASSET_ROOT path fix for post-install file discovery

## [0.1.0] — 2026-05-23

### Added
- Swap execution: quote, build, sign, and broadcast for Uniswap V2/V3/V4
- LP management: position analysis, rebalancing, compound strategies
- Analytics: impermanent loss calculator, LP range suggestions, risk scoring
- Price feed: CoinGecko integration with batch price queries
- Token search and risk assessment
- Multi-chain: Ethereum, Base, Arbitrum, Optimism, Polygon
- Trade confirmation via Telegram (optional)
- Paper trading mode with journaling
- 99 tests covering swap, LP, analytics, and execution gates
- GitHub Actions CI (Python 3.10, 3.11, 3.12)
- MIT License

[0.1.1]: https://github.com/counterfactual5/uniswap-autopilot/releases/tag/v0.1.1
[0.1.0]: https://github.com/counterfactual5/uniswap-autopilot/releases/tag/v0.1.0
