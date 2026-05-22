"""Price feed — multi-source token price lookup via CoinGecko free API.

Provides:
- ``get_price(chain, address, tier)`` — single token price
- ``get_prices_batch(pairs, tier)`` — batch price lookup
- ``get_eth_price()`` — current ETH/USD price

All functions are designed as drop-in replacements for the price_feed module
expected by ``analytics/position.py``, ``execute/_internal/tx.py``, and ``search/risk.py``.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any
from urllib.error import HTTPError, URLError

# ---------------------------------------------------------------------------
# Chain name → CoinGecko platform id map
# ---------------------------------------------------------------------------
CHAIN_TO_PLATFORM: dict[str, str] = {
    "ethereum": "ethereum",
    "base": "base",
    "arbitrum": "arbitrum-one",
    "optimism": "optimistic-ethereum",
    "polygon": "polygon-pos",
    "bsc": "binance-smart-chain",
    "avalanche": "avalanche",
    "celo": "celo",
    "world_chain": "world-chain",
    "soneium": "soneium",
    "linea": "linea",
    "blast": "blast",
    "zora": "zora",
    "unichain": "unichain",
}

COINGECKO_API = "https://api.coingecko.com/api/v3"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetch(url: str, timeout: int = 15) -> Any:
    """GET a JSON endpoint with retry logic."""
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "uniswap-autopilot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (HTTPError, URLError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_price(chain: str, address: str, tier: str = "normal") -> dict[str, Any] | None:
    """Get current USD price for a single token.

    Returns ``{"price": float, "symbol": str, ...}`` or ``None`` on failure.
    """
    platform = CHAIN_TO_PLATFORM.get(chain.lower())
    if not platform:
        return None

    url = f"{COINGECKO_API}/simple/token_price/{platform}?contract_addresses={address}&vs_currencies=usd"
    data = _fetch(url)
    if not data or address.lower() not in data:
        return None

    price = data[address.lower()].get("usd")
    if price is None:
        return None

    return {"price": float(price), "symbol": "", "source": "coingecko"}


def get_prices_batch(pairs: list[tuple[str, str]], tier: str = "normal") -> dict[str, dict[str, Any]]:
    """Batch fetch prices for multiple (chain, address) pairs.

    Returns ``{"chain:address": {"price": float}, ...}``.
    """
    results: dict[str, dict[str, Any]] = {}
    for chain, address in pairs:
        price_data = get_price(chain, address, tier)
        if price_data:
            results[f"{chain}:{address.lower()}"] = price_data
    return results


def get_eth_price() -> float | None:
    """Get current ETH/USD price."""
    url = f"{COINGECKO_API}/simple/price?ids=ethereum&vs_currencies=usd"
    data = _fetch(url)
    if not data:
        return None
    return float(data.get("ethereum", {}).get("usd", 0)) or None
