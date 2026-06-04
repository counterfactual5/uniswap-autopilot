#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
AMOUNT_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
ASSET_ROOT = Path(__file__).resolve().parent.parent / "data"
COMMON_TOKEN_FILE = ASSET_ROOT / "common-token-addresses.json"
TOKEN_CACHE_FILE = ASSET_ROOT / "token-cache.json"
CHAINS_FILE = ASSET_ROOT / "chains.json"
SECURE_WALLET_ENV_CANDIDATES = (
    "SECURE_WALLET_ADDRESS",
    "TRADE_SIGNER_WALLET_ADDRESS",
)
HOT_WALLET_ENV_CANDIDATES = (
    "HOT_WALLET_ADDRESS",
    "UNISWAP_WALLET_ADDRESS",
    "EXECUTOR_WALLET_ADDRESS",
    "DEFAULT_WALLET_ADDRESS",
)
DEFAULT_WALLET_ENV_CANDIDATES = SECURE_WALLET_ENV_CANDIDATES + HOT_WALLET_ENV_CANDIDATES


@dataclass(frozen=True)
class Token:
    symbol: str
    address: str
    decimals: int
    category: str | None = None
    is_stable: bool = False
    price_hint: str | None = None


@dataclass(frozen=True)
class Chain:
    key: str
    chain_id: int
    native_symbol: str
    wrapped_native_symbol: str
    url_param: str
    tokens: dict[str, Token]


def load_common_tokens() -> dict[str, dict[str, Token]]:
    payload = json.loads(COMMON_TOKEN_FILE.read_text(encoding="utf-8"))
    tokens_by_chain: dict[str, dict[str, Token]] = {}
    for chain_name, tokens in payload.items():
        if not isinstance(tokens, dict):
            raise ValueError(f"invalid token catalog for chain '{chain_name}'")
        chain_tokens: dict[str, Token] = {}
        for symbol_key, token_data in tokens.items():
            if not isinstance(token_data, dict):
                raise ValueError(f"invalid token entry '{chain_name}.{symbol_key}'")
            chain_tokens[symbol_key.upper()] = Token(
                str(token_data["symbol"]),
                str(token_data["address"]),
                int(token_data["decimals"]),
                str(token_data.get("category")) if token_data.get("category") is not None else None,
                bool(token_data.get("isStable", False)),
                str(token_data.get("priceHint")) if token_data.get("priceHint") is not None else None,
            )
        tokens_by_chain[chain_name] = chain_tokens
    return tokens_by_chain


def _load_chains(tokens_by_chain: dict[str, dict[str, Token]]) -> dict[str, Chain]:
    chains_cfg = json.loads(CHAINS_FILE.read_text(encoding="utf-8"))
    chains: dict[str, Chain] = {}
    for chain_key, cfg in chains_cfg.items():
        if not isinstance(cfg, dict):
            raise ValueError(f"invalid chain config for '{chain_key}'")
        chains[chain_key] = Chain(
            key=chain_key,
            chain_id=int(cfg["chainId"]),
            native_symbol=str(cfg["nativeSymbol"]),
            wrapped_native_symbol=str(cfg["wrappedNativeSymbol"]),
            url_param=str(cfg["urlParam"]),
            tokens=tokens_by_chain.get(chain_key, {}),
        )
    return chains


COMMON_TOKENS = load_common_tokens()

CHAINS: dict[str, Chain] = _load_chains(COMMON_TOKENS)

LP_CONTRACTS: dict[str, dict[str, str]] = {
    "ethereum": {
        "nonfungiblePositionManager": "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
        "v3Factory": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
    },
    "base": {
        "nonfungiblePositionManager": "0x03a520b32c04bf3beef7beb72e919cf822ed34f1",
        "v3Factory": "0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
    },
    "arbitrum": {
        "nonfungiblePositionManager": "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
        "v3Factory": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
    },
    "optimism": {
        "nonfungiblePositionManager": "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
        "v3Factory": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
    },
    "polygon": {
        "nonfungiblePositionManager": "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
        "v3Factory": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
    },
    "celo": {
        "nonfungiblePositionManager": "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
        "v3Factory": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
    },
    "linea": {
        "nonfungiblePositionManager": "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
        "v3Factory": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
    },
    "world_chain": {
        "nonfungiblePositionManager": "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
        "v3Factory": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
    },
    "soneium": {
        "nonfungiblePositionManager": "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
        "v3Factory": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
    },
}

V2_CONTRACTS: dict[str, dict[str, str]] = {
    "ethereum": {
        "factory": "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f",
        "router02": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
    },
    "optimism": {
        "factory": "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f",
        "router02": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
    },
    "bsc": {
        "factory": "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f",
        "router02": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
    },
    "polygon": {
        "factory": "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f",
        "router02": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
    },
    "arbitrum": {
        "factory": "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f",
        "router02": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
    },
    "avalanche": {
        "factory": "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f",
        "router02": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
    },
    "world_chain": {
        "factory": "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f",
        "router02": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
    },
}

V3_FEE_TIERS = {100, 500, 3000, 10000}

PUBLIC_RPC_URLS: dict[str, str] = {
    "ethereum": "https://eth.llamarpc.com",
    "base": "https://mainnet.base.org",
    "arbitrum": "https://arb1.arbitrum.io/rpc",
    "optimism": "https://mainnet.optimism.io",
    "polygon": "https://polygon-rpc.com",
    "bsc": "https://bsc-dataseed.binance.org",
    "avalanche": "https://api.avax.network/ext/bc/C/rpc",
    "celo": "https://forno.celo.org",
    "unichain": "https://mainnet.unichain.org",
    "linea": "https://rpc.linea.build",
    "blast": "https://rpc.blast.io",
    "zora": "https://rpc.zora.energy",
    "world_chain": "https://worldchain-mainnet.g.alchemy.com/public",
    "soneium": "https://rpc.soneium.org",
    "monad": "https://rpc.monad.xyz",
    "x_layer": "https://rpc.xlayer.tech",
    "zksync": "https://mainnet.era.zksync.io",
    "tempo": "https://api.avax.network/ext/bc/C/rpc",
}


def get_position_manager_address(chain_name: str) -> str:
    chain_key = chain_name.strip().lower()
    if chain_key not in LP_CONTRACTS:
        raise ValueError(f"LP not supported on chain '{chain_name}'")
    return LP_CONTRACTS[chain_key]["nonfungiblePositionManager"]


def get_v3_factory_address(chain_name: str) -> str:
    chain_key = chain_name.strip().lower()
    if chain_key not in LP_CONTRACTS:
        raise ValueError(f"LP not supported on chain '{chain_name}'")
    return LP_CONTRACTS[chain_key]["v3Factory"]


def get_v2_factory_address(chain_name: str) -> str:
    chain_key = chain_name.strip().lower()
    if chain_key not in V2_CONTRACTS:
        raise ValueError(f"V2 LP not supported on chain '{chain_name}'")
    return V2_CONTRACTS[chain_key]["factory"]


def get_v2_router02_address(chain_name: str) -> str:
    chain_key = chain_name.strip().lower()
    if chain_key not in V2_CONTRACTS:
        raise ValueError(f"V2 LP not supported on chain '{chain_name}'")
    return V2_CONTRACTS[chain_key]["router02"]


PERMIT2_ADDRESS = "0x000000000022D473030F116dDEE9F6B43aC78BA3"

UNIVERSAL_ROUTER_ADDRESSES: dict[str, str] = {
    "ethereum": "0x3fC91A3afd70395Cd496C647d5a6CC9D4B2b7FAD",
    "base": "0x198EF79F1F515F02dFE9e3115eD9fC07183f02fC",
    "arbitrum": "0x4D9079Bb4165aeb4084c526a3C95e761A5DABb07",
    "optimism": "0xc3e7cF3A5F74717452f52BEdE06991c50F960F7e",
    "polygon": "0x1095692a6237d83c6a72f3f5efedb9a670c49223",
    "bsc": "0x4D9079Bb4165aeb4084c526a3C95e761A5DABb07",
    "avalanche": "0x4D9079Bb4165aeb4084c526a3C95e761A5DABb07",
    "celo": "0x4D9079Bb4165aeb4084c526a3C95e761A5DABb07",
    "world_chain": "0x4D9079Bb4165aeb4084c526a3C95e761A5DABb07",
    "soneium": "0x4D9079Bb4165aeb4084c526a3C95e761A5DABb07",
    "linea": "0x4D9079Bb4165aeb4084c526a3C95e761A5DABb07",
}


def get_permit2_address() -> str:
    return PERMIT2_ADDRESS


def get_universal_router_address(chain_name: str) -> str | None:
    chain_key = chain_name.strip().lower()
    return UNIVERSAL_ROUTER_ADDRESSES.get(chain_key)


V4_CONTRACTS: dict[str, dict[str, str]] = {
    "ethereum": {
        "poolManager": "0x000000000004444c5dc75cB358380D2e3dE08A90",
        "positionManager": "0xbD216513d74C8cf14Cf4747E6AaA6420ff64EE9e",
        "stateView": "0x7ffe42c4a5deea5b0fec41c94c136cf115597227",
        "quoter": "0x52f0e24d1c21c8a0cb1e5a5dd6198556bd9e1203",
    },
    "base": {
        "poolManager": "0x498581ff718922c3f8e6a244956af099b2652b2b",
        "positionManager": "0x7C5f5a4bBd8fd63184577525326123b519429Bdc",
        "stateView": "0xA3c0c9b65bAd0b08107aa264b0F3Db444b867a71",
        "quoter": "0x0d5e0f971ed27fbff6c2837bf31316121532048d",
    },
    "arbitrum": {
        "poolManager": "0x360e68faccca8ca495c1b759fd9eee466db9fb32",
        "positionManager": "0xd88f38f930b7952f2db2432cb002e7abbf3dd869",
        "stateView": "0x76fd297e2d437cd7f76d50f01afe6160f86e9990",
        "quoter": "0x3972c00f7ed4885e145823eb7c655375d275a1c5",
    },
    "optimism": {
        "poolManager": "0x9a13f98cb987694c9f086b1f5eb990eea8264ec3",
        "positionManager": "0x3c3ea4b57a46241e54610e5f022e5c45859a1017",
        "stateView": "0xc18a3169788f4f75a170290584eca6395c75ecdb",
        "quoter": "0x1f3131a13296fb91c90870043742c3cdbff1a8d7",
    },
    "polygon": {
        "poolManager": "0x67366782805870060151383f4bbff9dab53e5cd6",
        "positionManager": "0x1ec2ebf4f37e7363fdfe3551602425af0b3ceef9",
        "stateView": "0x5ea1bd7974c8a611cbab0bdcafcb1d9cc9b3ba5a",
        "quoter": "0xb3d5c3dfc3a7aebff71895a7191796bffc2c81b9",
    },
    "celo": {
        "poolManager": "0x288dc841A52FCA2707c6947B3A777c5E56cd87BC",
        "positionManager": "0xf7965f3981e4d5bc383bfbcb61501763e9068ca9",
        "stateView": "0xbc21f8720babf4b20d195ee5c6e99c52b76f2bfb",
        "quoter": "0x28566da1093609182dff2cb2a91cfd72e61d66cd",
    },
    "world_chain": {
        "poolManager": "0xb1860d529182ac3bc1f51fa2abd56662b7d13f33",
        "positionManager": "0xc585e0f504613b5fbf874f21af14c65260fb41fa",
        "stateView": "0x51d394718bc09297262e368c1a481217fdeb71eb",
        "quoter": "0x55d235b3ff2daf7c3ede0defc9521f1d6fe6c5c0",
    },
    "soneium": {
        "poolManager": "0x360e68faccca8ca495c1b759fd9eee466db9fb32",
        "positionManager": "0x1b35d13a2e2528f192637f14b05f0dc0e7deb566",
        "stateView": "0x76fd297e2d437cd7f76d50f01afe6160f86e9990",
        "quoter": "0x3972c00f7ed4885e145823eb7c655375d275a1c5",
    },
    "bsc": {
        "poolManager": "0x28e2ea090877bf75740558f6bfb36a5ffee9e9df",
        "positionManager": "0x7a4a5c919ae2541aed11041a1aeee68f1287f95b",
        "stateView": "0xd13dd3d6e93f276fafc9db9e6bb47c1180aee0c4",
        "quoter": "0x9f75dd27d6664c475b90e105573e550ff69437b0",
    },
    "avalanche": {
        "poolManager": "0x06380c0e0912312b5150364b9dc4542ba0dbbc85",
        "positionManager": "0xb74b1f14d2754acfcbbe1a221023a5cf50ab8acd",
        "stateView": "0xc3c9e198c735a4b97e3e683f391ccbdd60b69286",
        "quoter": "0xbe40675bb704506a3c2ccfb762dcfd1e979845c2",
    },
    "blast": {
        "poolManager": "0x1631559198a9e474033433b2958dabc135ab6446",
        "positionManager": "0x4ad2f4cca2682cbb5b950d660dd458a1d3f1baad",
        "stateView": "0x12a88ae16f46dce4e8b15368008ab3380885df30",
        "quoter": "0x6f71cdcb0d119ff72c6eb501abceb576fbf62bcf",
    },
    "zora": {
        "poolManager": "0x0575338e4c17006ae181b47900a84404247ca30f",
        "positionManager": "0xf66c7b99e2040f0d9b326b3b7c152e9663543d63",
        "stateView": "0x385785af07d63b50d0a0ea57c4ff89d06adf7328",
        "quoter": "0x5edaccc0660e0a2c44b06e07ce8b915e625dc2c6",
    },
    "unichain": {
        "poolManager": "0x1f98400000000000000000000000000000000004",
        "positionManager": "0x4529a01c7a0410167c5740c487a8de60232617bf",
        "stateView": "0x86e8631a016f9068c3f085faf484ee3f5fdee8f2",
        "quoter": "0x333e3c607b141b18ff6de9f258db6e77fe7491e0",
    },
}


def _get_v4_contract(chain_name: str, contract_key: str) -> str:
    chain_key = chain_name.strip().lower()
    if chain_key not in V4_CONTRACTS:
        raise ValueError(f"V4 not supported on chain '{chain_name}'")
    return V4_CONTRACTS[chain_key][contract_key]


def get_v4_pool_manager_address(chain_name: str) -> str:
    return _get_v4_contract(chain_name, "poolManager")


def get_v4_position_manager_address(chain_name: str) -> str:
    return _get_v4_contract(chain_name, "positionManager")


def get_v4_state_view_address(chain_name: str) -> str:
    return _get_v4_contract(chain_name, "stateView")


def get_v4_quoter_address(chain_name: str) -> str:
    return _get_v4_contract(chain_name, "quoter")


def validate_fee_tier(fee: int) -> int:
    if fee not in V3_FEE_TIERS:
        raise ValueError(f"invalid fee tier {fee}; valid: {sorted(V3_FEE_TIERS)}")
    return fee


def sort_token_addresses(token_a: str, token_b: str) -> tuple[str, str]:
    a = token_a.lower()
    b = token_b.lower()
    if a == b:
        raise ValueError("token0 and token1 must be different")
    return (token_a, token_b) if a < b else (token_b, token_a)


def normalize_chain(chain_name: str) -> Chain:
    key = chain_name.strip().lower()
    if key not in CHAINS:
        supported = ", ".join(sorted(CHAINS))
        raise ValueError(f"unsupported chain '{chain_name}', supported: {supported}")
    return CHAINS[key]


# ─── Token Cache ────────────────────────────────────────────────

def _load_token_cache() -> dict[str, dict[str, dict[str, Any]]]:
    """Load token-cache.json; return {} on missing / corrupt."""
    if not TOKEN_CACHE_FILE.exists():
        return {}
    try:
        data = json.loads(TOKEN_CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_token_cache(cache: dict[str, dict[str, dict[str, Any]]]) -> None:
    """Persist token cache with sorted keys for diff-friendliness."""
    TOKEN_CACHE_FILE.write_text(
        json.dumps(cache, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _cache_get(chain_key: str, symbol_or_address: str) -> dict[str, Any] | None:
    """Look up cache by (chain, upper(symbol)) or (chain, lower(address))."""
    cache = _load_token_cache()
    chain_bucket = cache.get(chain_key, {})
    key = symbol_or_address.upper()
    if key in chain_bucket:
        return dict(chain_bucket[key])  # shallow copy
    # try address lookup
    lower = symbol_or_address.lower()
    for entry in chain_bucket.values():
        if entry.get("address", "").lower() == lower:
            return dict(entry)
    return None


def _cache_put(chain_key: str, symbol: str, entry: dict[str, Any]) -> None:
    """Write one entry into the token cache. Symbol is upper-cased."""
    cache = _load_token_cache()
    chain_bucket = cache.setdefault(chain_key, {})
    entry_copy = dict(entry)
    entry_copy["cachedAt"] = __import__("time").time()
    chain_bucket[symbol.upper()] = entry_copy
    _save_token_cache(cache)


def cache_token_from_search(
    chain_key: str, symbol: str, address: str, decimals: int,
    category: str | None = None, is_stable: bool = False,
) -> None:
    """Public API: write a token entry (e.g. from search results) into cache."""
    _cache_put(chain_key, symbol, {
        "kind": "erc20",
        "symbol": symbol,
        "address": address,
        "decimals": decimals,
        "category": category,
        "isStable": is_stable,
        "priceHint": None,
    })


# ─── End Token Cache ────────────────────────────────────────────


def _auto_search_token(chain_key: str, symbol: str) -> dict[str, Any] | None:
    """Try to find a token via DexScreener and cache the result."""
    try:
        from uniswap_autopilot.search.search import search_tokens  # type: ignore
    except ImportError:
        return None
    try:
        results = search_tokens(symbol, chain=chain_key, limit=5)
    except Exception:
        return None
    if not results:
        return None
    # Pick the best match: prefer exact symbol match, then highest liquidity
    best = None
    for r in results:
        if r.get("symbol", "").upper() == symbol.upper():
            best = r
            break
    if best is None:
        best = results[0]
    addr = best.get("address", "")
    if not addr:
        return None
    entry = {
        "kind": "erc20",
        "symbol": best.get("symbol", symbol),
        "address": addr,
        "decimals": best.get("decimals") or 18,
        "category": None,
        "isStable": False,
        "priceHint": None,
    }
    _cache_put(chain_key, symbol, entry)
    return entry


def read_erc20_metadata(token_address: str, rpc_url: str) -> dict[str, Any]:
    """Read ERC-20 decimals and symbol from chain via JSON-RPC."""
    from uniswap_autopilot.execute._internal.rpc import read_erc20_decimals, read_erc20_symbol
    decimals = 18
    try:
        decimals = read_erc20_decimals(token_address, rpc_url)
    except Exception:
        pass

    symbol = token_address
    try:
        sym = read_erc20_symbol(token_address, rpc_url)
        if sym and not sym.startswith("0x"):
            symbol = sym
    except Exception:
        pass

    return {"symbol": symbol, "decimals": decimals}


def is_native(chain: Chain, token: str) -> bool:
    normalized = token.strip().upper()
    if normalized == "NATIVE":
        return True
    return normalized == chain.native_symbol.upper()


def resolve_token(chain: Chain, token: str, rpc_url: str | None = None) -> dict[str, Any]:
    normalized = token.strip()
    upper = normalized.upper()
    if is_native(chain, normalized):
        return {
            "kind": "native",
            "symbol": chain.native_symbol,
            "address": "NATIVE",
            "decimals": 18,
        }
    if ADDRESS_RE.fullmatch(normalized):
        # Check cache first for address-based lookups
        cached = _cache_get(chain.key, normalized)
        if cached and cached.get("address", "").lower() == normalized.lower():
            return cached
        # Cache miss: read from chain and cache the result
        meta = {}
        effective_rpc = rpc_url or PUBLIC_RPC_URLS.get(chain.key)
        if effective_rpc:
            meta = read_erc20_metadata(normalized, effective_rpc)
        result = {
            "kind": "erc20",
            "symbol": meta.get("symbol", normalized),
            "address": normalized,
            "decimals": meta.get("decimals", 18),
            "category": None,
            "isStable": False,
            "priceHint": None,
        }
        # Cache by symbol if we got a readable symbol
        sym = result["symbol"]
        if sym and not sym.startswith("0x"):
            _cache_put(chain.key, sym, result)
        return result
    if upper in chain.tokens:
        token_cfg = chain.tokens[upper]
        return {
            "kind": "erc20",
            "symbol": token_cfg.symbol,
            "address": token_cfg.address,
            "decimals": token_cfg.decimals,
            "category": token_cfg.category,
            "isStable": token_cfg.is_stable,
            "priceHint": token_cfg.price_hint,
        }
    # Symbol not in built-in list — check cache
    cached = _cache_get(chain.key, upper)
    if cached:
        return cached
    # Cache miss: try auto-search via DexScreener / GeckoTerminal
    resolved = _auto_search_token(chain.key, normalized)
    if resolved:
        return resolved
    raise ValueError(
        f"unknown token '{token}' on {chain.key}; "
        f"use a known symbol or a token address, or search first"
    )


def resolve_quote_token(chain: Chain, token: dict[str, Any]) -> dict[str, Any]:
    if token["address"] != "NATIVE":
        return token
    wrapped = chain.tokens.get(chain.wrapped_native_symbol.upper())
    if not wrapped:
        raise ValueError(f"wrapped native token is not configured for chain '{chain.key}'")
    return {
        "kind": "erc20",
        "symbol": wrapped.symbol,
        "address": wrapped.address,
        "decimals": wrapped.decimals,
        "category": wrapped.category,
        "isStable": wrapped.is_stable,
        "priceHint": wrapped.price_hint,
        "wrappedFromNative": True,
    }


def parse_amount(amount: str) -> Decimal:
    if not AMOUNT_RE.fullmatch(amount.strip()):
        raise ValueError(f"invalid amount '{amount}'")
    try:
        parsed = Decimal(amount)
    except InvalidOperation as exc:
        raise ValueError(f"invalid amount '{amount}'") from exc
    if parsed <= 0:
        raise ValueError("amount must be greater than 0")
    return parsed


def decimal_to_base_units(amount: Decimal, decimals: int) -> str:
    scaled = amount * (Decimal(10) ** decimals)
    if scaled != scaled.to_integral_value():
        raise ValueError(
            f"amount {amount} has too many decimal places for token decimals={decimals}"
        )
    return str(int(scaled))


def validate_address(value: str, field_name: str) -> str:
    if not ADDRESS_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a valid EVM address")
    return value


def resolve_wallet_address(
    value: str | None,
    field_name: str = "wallet",
    preference: str = "any",
) -> str | None:
    if value:
        return validate_address(value, field_name)
    if preference == "secure":
        candidates = SECURE_WALLET_ENV_CANDIDATES
    elif preference == "hot":
        candidates = HOT_WALLET_ENV_CANDIDATES
    else:
        candidates = DEFAULT_WALLET_ENV_CANDIDATES
    for env_name in candidates:
        env_value = os.environ.get(env_name, "").strip()
        if env_value:
            return validate_address(env_value, env_name)
    return None


NATIVE_LINK_ADDRESS = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"


def _link_address(token: dict[str, Any]) -> str:
    if token.get("address") == "NATIVE":
        return NATIVE_LINK_ADDRESS
    return token["address"]


def build_swap_link(
    chain: Chain,
    token_in: dict[str, Any],
    token_out: dict[str, Any],
    amount: str,
    field: str,
) -> str:
    params = {
        "chain": chain.url_param,
        "inputCurrency": _link_address(token_in),
        "outputCurrency": _link_address(token_out),
        "value": amount,
        "field": field,
    }
    return f"https://app.uniswap.org/swap?{urlencode(params)}"


def dump_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--chain", required=True, help="链名，例如 base / ethereum")
    parser.add_argument("--token-in", required=True, help="输入 token symbol、地址或 NATIVE")
    parser.add_argument("--token-out", required=True, help="输出 token symbol、地址或 NATIVE")
    parser.add_argument("--amount", required=True, help="人类可读数量，例如 1 或 250.5")
    parser.add_argument(
        "--token-in-decimals",
        type=int,
        help="当 --token-in 直接传地址且不是内置 symbol 时，显式指定 decimals",
    )
    parser.add_argument(
        "--token-out-decimals",
        type=int,
        help="当 --token-out 直接传地址且不是内置 symbol 时，显式指定 decimals",
    )


def override_decimals(token: dict[str, Any], decimals: int | None) -> dict[str, Any]:
    if decimals is None:
        return token
    if decimals < 0 or decimals > 255:
        raise ValueError("token decimals must be between 0 and 255")
    updated = dict(token)
    updated["decimals"] = decimals
    return updated


def native_currency_address() -> str:
    return "0x0000000000000000000000000000000000000000"


def resolve_api_token(chain: Chain, token: dict[str, Any]) -> dict[str, Any]:
    if token["address"] != "NATIVE":
        return token
    return {
        "kind": "native",
        "symbol": chain.native_symbol,
        "address": native_currency_address(),
        "decimals": 18,
        "nativeInput": True,
    }


API_BASE = "https://trade-api.gateway.uniswap.org/v1"
BROWSER_LIKE_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
)


def require_api_key() -> str:
    api_key = os.environ.get("UNISWAP_API_KEY")
    if not api_key:
        raise RuntimeError("UNISWAP_API_KEY is not set")
    return api_key


def post_json(endpoint: str, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    uses_native = False
    for field in ("tokenIn", "tokenOut"):
        if payload.get(field) == native_currency_address():
            uses_native = True
            break
    if not uses_native:
        quote = payload.get("quote") or {}
        for field in ("input", "output"):
            section = quote.get(field) or {}
            if section.get("token") == native_currency_address():
                uses_native = True
                break
    url = f"{API_BASE}/{endpoint.lstrip('/')}"
    data = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "Accept": "application/json",
            "x-universal-router-version": "2.0",
            "User-Agent": BROWSER_LIKE_USER_AGENT,
            "Origin": "https://app.uniswap.org",
            "Referer": "https://app.uniswap.org/",
            **({"x-erc20eth-enabled": "true"} if uses_native else {}),
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Trading API HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"Trading API request failed: {exc.reason}") from exc


def skill_root() -> Path:
    """Return the project root directory."""
    return Path(__file__).resolve().parent.parent.parent.parent


def load_local_env() -> dict[str, str]:
    loaded: dict[str, str] = {}
    candidates = [skill_root() / ".env.local", skill_root() / ".env"]
    for path in candidates:
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if not key:
                continue
            os.environ.setdefault(key, value)
            loaded[key] = value
    return loaded


def check_balance(
    chain: Chain,
    token: dict[str, Any],
    amount_base_units: str,
    owner: str,
    rpc_url: str | None = None,
) -> dict[str, Any]:
    if not rpc_url:
        return {"checked": False, "ok": None, "reason": "RPC URL not configured"}
    try:
        from uniswap_autopilot.execute._internal.rpc import query_erc20_balance, query_native_balance
        if token.get("address") == "NATIVE" or token.get("kind") == "native":
            raw = query_native_balance(owner, rpc_url)
        else:
            raw = query_erc20_balance(owner, token["address"], rpc_url)
        required = int(amount_base_units)
        ok = raw >= required
        return {
            "checked": True,
            "ok": ok,
            "balance": str(raw),
            "required": str(required),
            "shortfall": str(required - raw) if not ok else "0",
        }
    except Exception as exc:
        return {"checked": False, "ok": None, "reason": str(exc)}
