<div align="center">

# ⚡ Uniswap Autopilot

### The execution layer uniswap-ai forgot to build.

**Plan swaps with AI → Execute them on-chain. Automatically.**

Pure Python · Zero Dependencies · Uniswap v2 / v3 / v4 · 6 Chains · 13K Lines

[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-99%20passing-green.svg)](tests/)
[![Zero Deps](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)](pyproject.toml)

[Installation](#installation) · [Features](#features) · [Quick Start](#quick-start) · [Architecture](#architecture) · [Comparison](#how-does-this-compare-to-uniswap-ai)

</div>

---

## The Problem

[uniswap-ai](https://github.com/Uniswap/uniswap-ai) is Uniswap Labs' official project. It teaches AI agents to plan trades and generate deep links. But when the agent actually needs to **execute**? It says:

> *"Open this link in your browser."*

That's not autonomous. That's a bookmark.

**uniswap-autopilot closes the loop.** It takes the plan and does the work — quote, approve, sign, broadcast, receipt — all in Python, no browser needed.

```
  You: "Swap 0.1 ETH for USDC on Base"

  uniswap-ai:         "Here's a deep link. Open it in your browser."
  uniswap-autopilot:  ✅ Quoted → Approved → Signed → Broadcast → 0xabc123...
```

---

## Features

### 🔄 LP Auto-Management

The largest module at 4,500 lines. Nothing like this exists in any open-source Uniswap Python library.

- **Auto-Rebalance** — v3 position goes out of range? Automatically withdraw and redeploy to a new range
- **Compound** — harvest uncollected fees and reinvest into the same position
- **v4 PoolManager** — full LP lifecycle for Uniswap v4's new architecture
- **v2 Classic LP** — add/remove liquidity for traditional AMM pools
- **Pool Comparison** — compare fee tiers, TVL, and volume to pick the best pool

### ⚡ Complete Swap Pipeline

Not just quotes. The full chain from "I want to trade" to "tx confirmed on-chain":

```
Quote → Permit/Approval → Sign → Preflight Check → Broadcast → Receipt
```

- **Uniswap Trading API** — direct integration with quote, swap, and permit2 endpoints
- **Preflight checks** — verifies you have the balance, allowance, and gas *before* signing
- **Policy engine** — define auto-trading rules in JSON (chain, pair, amount, slippage limits)
- **Paper trading** — dry-run mode: records everything, executes nothing
- **Dynamic slippage** — auto-adjusts tolerance based on real-time volatility

### 📊 Analytics Suite

Quantitative tools that help LPs make real decisions:

| Tool | What it does |
|---|---|
| IL Calculator | Exact impermanent loss at any price ratio |
| Range Suggest | Recommends concentrated liquidity ranges from volatility |
| Position Analysis | Tracks uncollected fees, PnL, and position health |
| Portfolio Valuation | Aggregate value across multiple positions |

### 🔍 Token Search & Risk Scoring

- **Multi-source** — DexScreener + GeckoTerminal, deduplicated
- **Risk assessment** — composite score from liquidity, volume, market cap, and price volatility
- **Token cache** — local cache with auto-refresh, works offline

### 🌉 Extensions

- **Cross-chain Bridge** — detect and route cross-chain swaps automatically
- **Limit Orders** — on-chain limit order logic
- **Telegram Confirmation** — human-in-the-loop approval via inline buttons

---

## How Does This Compare to uniswap-ai?

| | uniswap-ai (official) | uniswap-autopilot |
|---|:---:|:---:|
| What is it? | 11 markdown docs | 13K lines of Python |
| Swap quotes | 📄 Documentation | ✅ **Executable API** |
| Swap execution | 🔗 Generates a deep link | ✅ **Quote → Sign → Broadcast** |
| LP management | 📄 Planning docs | ✅ **Auto-rebalance + compound** |
| LP analytics | ❌ | ✅ **IL / ranges / portfolio** |
| Token risk scoring | ❌ | ✅ **Multi-factor scoring** |
| Cross-chain bridge | ❌ | ✅ |
| Limit orders | ❌ | ✅ |
| Human confirmation | ❌ | ✅ **Telegram buttons** |
| Dependencies | npm / bun / Foundry | **Zero** |
| Tests | 0 | **99** |
| Install | Complex setup | `pip install uniswap-autopilot` |

> **They're complementary.** Use uniswap-ai for planning, use this for execution.

---

## Installation

```bash
# Analysis + quotes + LP planning — zero dependencies
pip install uniswap-autopilot

# Add on-chain signing & broadcasting (eth-account)
pip install uniswap-autopilot[signer]

# Development
pip install -e ".[dev]"
```

That's it. No Foundry. No npm. No web3.py. Pure Python.

---

## Quick Start

### 📊 Analyze Impermanent Loss

```python
from uniswap_autopilot.analytics.il_calculator import calculate_il

# ETH-USDC pool, 1% fee tier, entry at $3000
il = calculate_il(
    price_entry=3000.0,
    price_current=3300.0,  # 10% up
    tick_lower=-887220, tick_upper=887220,
    decimals0=18, decimals1=6,
    liquidity=1000000000000,
)
print(f"  IL: {il['ilPct']:.2f}%")
```

```
  Price 1.1x → IL: 0.04%
  Price 1.5x → IL: 0.56%
  Price 2.0x → IL: 2.02%
  Price 3.0x → IL: 5.36%
```

### 🎯 Get LP Range Suggestions

```python
from uniswap_autopilot.analytics.range_suggest import suggest_ranges

ranges = suggest_ranges(
    chain_name="ethereum",
    token_a="ETH", token_b="USDC",
    fee_tier=3000,
)
for r in ranges:
    print(f"  ${r['lower']:,.0f} — ${r['upper']:,.0f}  ({r['profile']})")
```

```
  $2,400 — $3,600  (conservative)
  $2,100 — $4,200  (balanced)
  $1,800 — $4,800  (aggressive)
```

### 💱 Get a Swap Quote

```python
from uniswap_autopilot.swap.trading_api.quote import build_quote_payload, summarize_quote

quote = build_quote_payload(
    wallet="0x...",
    chain_id=8453,
    api_token_in="ETH",
    api_token_out="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC on Base
    base_amount="0.1",
    swap_type="EXACT_INPUT",
    slippage="0.5",
    routing_preference="CLASSIC",
)
print(summarize_quote(quote))
```

```
  Swap 0.1 ETH → ~175.42 USDC on Base
  Route: ETH → USDC (0.05% fee)
  Estimated gas: ~$0.02
```

### 🔍 Search & Score Tokens

```python
from uniswap_autopilot.search.search import search_tokens
from uniswap_autopilot.search.risk import risk_assess

results = search_tokens(query="PEPE", chain="ethereum", limit=3)
for token in results:
    risk = risk_assess(token["address"], "ethereum")
    print(f"  {token['symbol']:8s} risk={risk['level']}  ${token['price']:.8f}")
```

```
  PEPE      risk= 72/100  $0.00001234
  PEPECOIN  risk= 35/100  $0.4521
  PEPECEO   risk= 12/100  $0.00000001
```

### ⚡ Execute a Trade (with signing)

```bash
export EXECUTOR_PRIVATE_KEY=0x...   # Never written to disk
export WALLET_ADDRESS=0x...
export RPC_URL_BASE=https://mainnet.base.org
```

```python
from uniswap_autopilot.swap.flow import run_trade_flow

result = run_trade_flow(
    chain="base",
    input_token="ETH",
    output_token="USDC",
    amount="0.01",
    auto_confirm=True,      # skip human confirmation
)
print(result["swap"]["transactionHash"])
```

```
  ✅ Quoted: 0.01 ETH → 17.54 USDC
  ✅ Approved: USDC spender set
  ✅ Signed: via eth-account
  ✅ Broadcast: 0xabc123...def456
  ✅ Confirmed in block 19,847,231
```

### 🔄 Auto-Rebalance an LP Position

```python
from uniswap_autopilot.lp.v3.auto_rebalance import check_and_rebalance

result = check_and_rebalance(
    position={"token_id": 123456, "chain": "ethereum"},
    strategy="balanced",
    volatility_pct=30,
)
if result["rebalanced"]:
    print(f"  Old range: {result['old_range']}")
    print(f"  New range: {result['new_range']}")
    print(f"  TX: {result['transactionHash']}")
```

### 🧪 Paper Trade (Safe Mode)

```python
# Everything runs, nothing executes on-chain
result = run_trade_flow(
    chain="base",
    input_token="ETH",
    output_token="USDC",
    amount="0.1",
    paper_trade=True,       # <--- dry run
)
print(result["journal"])    # full trade journal without spending gas
```

---

## Architecture

### System Overview

```
                        ┌─────────────────────────────────────────┐
                        │            AI Agent / User              │
                        └──────┬──────────┬──────────┬───────────┘
                               │          │          │
                    ┌──────────▼──┐  ┌────▼─────┐  ┌─▼──────────┐
                    │   🔍 Search  │  │  📊 Ana  │  │  🔄 LP     │
                    │   & Risk     │  │ lytics   │  │ Management │
                    │   750 lines  │  │ 1500 ln  │  │ 4500 lines │
                    └──────┬───────┘  └────┬─────┘  └─┬──────────┘
                           │               │           │
                           └───────┬───────┘           │
                                   │                   │
                          ┌────────▼─────────┐         │
                          │    ⚡ Swap Flow    │◄────────┘
                          │    2,500 lines    │  (rebalance uses
                          │                   │   swap internally)
                          │  ┌─────────────┐  │
                          │  │  flow_core   │  │
                          │  │  • Policy    │  │
                          │  │  • Paper     │  │
                          │  │  • Diag      │  │
                          │  └──────┬──────┘  │
                          └─────────┼─────────┘
                                    │
                          ┌─────────▼──────────┐
                          │   🔐 Execute Layer  │
                          │     1,900 lines     │
                          │                     │
                          │  preflight → sign   │
                          │  → broadcast →      │
                          │    receipt          │
                          └─────────┬──────────┘
                                    │
                    ┌───────────────┼────────────────┐
                    │               │                │
              ┌─────▼─────┐  ┌────▼─────┐  ┌──────▼──────┐
              │  JSON-RPC  │  │  eth-acct │  │  Telegram   │
              │  (urllib)  │  │  signing  │  │  confirm    │
              └─────┬──────┘  └────┬─────┘  └─────────────┘
                    │              │
                    └──────┬───────┘
                           │
                    ┌──────▼──────┐
                    │   EVM Chain │
                    │  ETH·BASE·  │
                    │  ARB·OP·    │
                    │  POLY·UNI   │
                    └─────────────┘
```

### Data Flow: Swap Execution

```
 User Input          Uniswap API           Local Processing         On-Chain
───────────        ────────────          ─────────────────        ─────────

 "Swap 0.1          quote endpoint           Policy check            ┌──────┐
  ETH→USDC"   ──→  (GET /quote)     ──→    Slippage adjust    ──→   │      │
  on Base"                                                       │      │
                                                                  │ EVM  │
                     swap endpoint          Preflight check         │ Chain│
               ──→  (GET /swap)      ──→   balance / allowance  ──→  │      │
                                                                  │      │
                                            Sign TX               │      │
                                      ──→   (eth-account)     ──→   │      │
                                                                  │      │
                                            Broadcast             │      │
                                      ──→   (eth_sendRawTx)   ──→   └──────┘
                                                                  ↓
                                                            ✅ Receipt
```

### Package Structure

```
uniswap_autopilot/                    13,000 lines total
│
├── swap/                   2,500     Full swap pipeline
│   ├── trading_api/                  Uniswap Trading API — quote / swap / permit2
│   ├── flow_core/                    Policy engine, diagnostics, paper trading
│   ├── extensions/                   Bridge, limit orders, dynamic slippage
│   └── links/                        Deep link generation
│
├── lp/                     4,500     Liquidity provision (largest module)
│   ├── v2/                           Classic AMM — add / remove / positions
│   ├── v3/                           Concentrated liquidity — auto-rebalance, compound
│   ├── v4/                           PoolManager — v4 LP flows
│   └── compare_pools                 Fee tier & TVL comparison
│
├── analytics/              1,500     Quantitative tools
│   ├── il_calculator                 Impermanent loss at any price ratio
│   ├── range_suggest                 Concentrated liquidity range optimization
│   ├── position                      Fee tracking, PnL, health monitoring
│   └── portfolio                     Multi-position aggregation
│
├── search/                   750     Token intelligence
│   ├── search                        DexScreener + GeckoTerminal
│   └── risk                          Multi-factor risk scoring
│
├── execute/                1,900     Transaction engine
│   ├── _internal/rpc                 Pure JSON-RPC over urllib
│   ├── _internal/pure_signer         eth-account signing (optional dep)
│   ├── _internal/signer              Backend detection & wallet resolution
│   ├── _internal/submit              eth_sendRawTransaction broadcast
│   ├── _internal/preflight           Balance / allowance / gas pre-checks
│   ├── broadcast                     Main broadcast orchestrator
│   ├── detect                        Backend capability detection
│   └── telegram_confirm              Human-in-the-loop via Telegram
│
├── common/                 1,400     Shared utilities
│   └── ...                           Chain config, token resolution, gas, balances
│

```

## Supported Chains

| Chain | Chain ID | Native Token | LP Versions |
|---|---|---|---|
| Ethereum | 1 | ETH | v2 · v3 · v4 |
| Base | 8453 | ETH | v2 · v3 · v4 |
| Arbitrum | 42161 | ETH | v2 · v3 · v4 |
| Optimism | 10 | ETH | v2 · v3 · v4 |
| Polygon | 137 | MATIC | v2 · v3 |
| Unichain | 130 | ETH | v3 · v4 |

Plus 13 additional chains configurable via [`data/chains.json`](data/chains.json).

## Security Model

| Concern | How we handle it |
|---|---|
| Private keys | Environment variables only — never written to disk, never logged |
| Transaction signing | In-process via eth-account — no external CLI, no IPC, no file I/O |
| Large trades | Telegram inline buttons for human confirmation before broadcast |
| Preflight checks | Balance, allowance, and gas verified *before* signing |
| Supply chain | Zero npm, zero Foundry, zero web3.py — pure Python stdlib |
| RPC calls | Direct JSON-RPC over urllib — no middleware, no proxy |

## 🔐 Private Key Security

Your private key is the only thing standing between your funds and an attacker. This library takes a strict approach:

**How this library handles your key:**
- Read from environment variable `EXECUTOR_PRIVATE_KEY` — **never** stored in a file, config, or database
- Used in-process via `eth-account` for signing — no shell command, no IPC, no temp file
- **Never logged** — the key appears nowhere in stdout, debug output, or trade journals

**Author's setup (production-grade isolation):**

```
┌─────────────────┐       HTTPS        ┌──────────────────┐
│   Agent Process  │ ──────────────────→ │  Signing Service  │
│   (no private    │   sign request      │  (holds key in    │
│    key access)   │ ←────────────────── │   HSM / enclave)  │
└─────────────────┘   signed payload     └──────────────────┘
```

- Private key lives in a **separate signing microservice** on an isolated machine
- Agent process **never sees the key** — only sends transaction payloads to be signed
- Communication over HTTPS with Bearer token auth
- This is the most secure architecture but requires infrastructure setup

**Recommended approaches (pick your level):**

| Level | Approach | Security | Complexity |
|---|---|---|---|
| ⭐ Basic | Environment variable + `pip install uniswap-autopilot[signer]` | Good for dev/testing | Zero |
| ⭐⭐ Standard | `.env` file (gitignored) + env var | Good for personal bots | Low |
| ⭐⭐⭐ Advanced | Dedicated signing service (separate machine / Docker) | Production-grade | Medium |
| ⭐⭐⭐⭐ Maximum | HSM or AWS KMS backed signing service | Institutional-grade | High |

**Rules that apply to every level:**

1. **Never commit a private key to git** — not in code, not in config, not in `.env` (unless `.env` is in `.gitignore`)
2. **Never paste your key in an AI chat** — LLM providers may log and train on your input
3. **Use a dedicated trading wallet** — fund it with only what you're willing to lose
4. **Test on testnet first** — every chain has a faucet, use it before mainnet
5. **Set spending limits** — use `auto_trade_policy.json` to cap per-trade amounts

```bash
# Basic setup (Level ⭐)
# Step 1: Create a dedicated wallet
# Step 2: Export the private key
export EXECUTOR_PRIVATE_KEY=0x...
export WALLET_ADDRESS=0x...
export RPC_URL_BASE=https://mainnet.base.org

# Step 3: Add to your shell profile (never to git)
echo 'export EXECUTOR_PRIVATE_KEY=0x...' >> ~/.bashrc  # or ~/.zshrc
```

## For AI Agent Developers

This library is designed as the execution backend for coding agents:

```python
# 1. Research   →  search tokens, assess risk
# 2. Plan       →  get quote, calculate optimal slippage
# 3. Verify     →  preflight balance/allowance/gas check
# 4. Execute    →  sign + broadcast (with optional human confirmation)
# 5. Report     →  receipt, PnL journal, position health
```

Compatible with:
- **[uniswap-ai](https://github.com/Uniswap/uniswap-ai)** — use for planning, use this for execution
- **[Claude Code](https://docs.anthropic.com/en/docs/claude-code)** / **[Cursor](https://cursor.sh)** — import directly in agent scripts
- **[Codex](https://github.com/openai/codex)** — `pip install` in sandbox, execute trades
- **Any MCP-compatible agent** — wrap the API in an MCP tool server

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest                              # 99 tests
pytest --cov=uniswap_autopilot      # with coverage
pytest -x                           # stop at first failure

# Lint
ruff check src/ tests/
```

## Roadmap

- [ ] MCP server wrapper for plug-and-play agent integration
- [ ] PyPI package publishing
- [ ] Async support (async/await)
- [ ] More DEX aggregators (1inch, Paraswap)
- [ ] Position monitoring & alerting
- [ ] Backtesting framework

## Contributing

Contributions welcome! Areas of particular interest:

- **More chains** — add config to `data/chains.json`
- **More analytics** — fee income forecasting, Monte Carlo IL simulation
- **More extensions** — DCA, TWAP, MEV protection
- **Tests** — the more the merrier
- **Docs** — tutorials, API reference, integration guides

Please open an issue first to discuss what you'd like to change.

## License

[MIT](LICENSE) — use it however you want.

---

If this project helped you, please ⭐ star this repo — it helps others find it!

### Related Projects

- **[defi-autopilot](https://github.com/counterfactual5/defi-autopilot)** — Multi-protocol DeFi toolkit (Aave, Compound, Morpho, Moonwell, Curve, Lido, 1inch + Uniswap). If you need more than just Uniswap, check it out.
