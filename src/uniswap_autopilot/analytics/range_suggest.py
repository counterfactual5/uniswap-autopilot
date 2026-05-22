#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any


from uniswap_autopilot.common.common import (
    dump_json,
    load_local_env,
    normalize_chain,
    resolve_token,
    validate_fee_tier,
    V3_FEE_TIERS,
)
from uniswap_autopilot.lp.v3.pool import query_pool_full_info
from uniswap_autopilot.lp.v3.tick import price_to_tick, tick_to_price, nearest_usable_tick, fee_tier_to_tick_spacing
from uniswap_autopilot.search.search import _ds_lookup


# ---------------------------------------------------------------------------
# Pair-type detection
# ---------------------------------------------------------------------------

WRAPPED_NATIVE_SYMBOLS = {"WETH", "WBTC", "WMATIC", "WBNB", "WAVAX", "WGLMR", "WSTAID"}

MAJOR_TOKEN_SYMBOLS = {
    "WETH", "WBTC", "WMATIC", "WBNB", "WAVAX",
    "ETH", "BTC", "USDC", "USDT", "DAI",
    "ARB", "OP", "UNI", "LINK", "AAVE",
    "CBETH", "RETH", "WEETH",
}


def detect_pair_type(token_a_info: dict[str, Any], token_b_info: dict[str, Any]) -> str:
    """Classify the token pair to determine appropriate range widths."""
    a_stable = bool(token_a_info.get("isStable"))
    b_stable = bool(token_b_info.get("isStable"))
    a_category = token_a_info.get("category")
    b_category = token_b_info.get("category")

    # Both tokens are stablecoins
    if a_stable and b_stable:
        return "stable_stable"
    if a_category == "stablecoin" and b_category == "stablecoin":
        return "stable_stable"

    a_symbol = token_a_info.get("symbol", "").upper()
    b_symbol = token_b_info.get("symbol", "").upper()

    # Both are major tokens (WETH, WBTC, etc.)
    if a_symbol in MAJOR_TOKEN_SYMBOLS and b_symbol in MAJOR_TOKEN_SYMBOLS:
        return "correlated"

    # One wrapped native + one non-stable
    a_is_wrapped = a_symbol in WRAPPED_NATIVE_SYMBOLS
    b_is_wrapped = b_symbol in WRAPPED_NATIVE_SYMBOLS
    if (a_is_wrapped and not b_stable) or (b_is_wrapped and not a_stable):
        return "major_volatile"

    return "volatile"


# ---------------------------------------------------------------------------
# Range width presets (percentage offset from current price)
# ---------------------------------------------------------------------------

_RANGE_WIDTHS: dict[str, dict[str, float]] = {
    "stable_stable": {"CONSERVATIVE": 0.5, "MODERATE": 1.0, "AGGRESSIVE": 2.0},
    "correlated":    {"CONSERVATIVE": 5.0, "MODERATE": 10.0, "AGGRESSIVE": 20.0},
    "major_volatile": {"CONSERVATIVE": 10.0, "MODERATE": 20.0, "AGGRESSIVE": 40.0},
    "volatile":      {"CONSERVATIVE": 15.0, "MODERATE": 30.0, "AGGRESSIVE": 60.0},
}


# ---------------------------------------------------------------------------
# Range suggestion calculator
# ---------------------------------------------------------------------------

def calculate_range_suggestions(
    current_tick: int,
    tick_spacing: int,
    decimals0: int,
    decimals1: int,
    pair_type: str,
    price_change_24h: float | None = None,
) -> list[dict[str, Any]]:
    """Generate tick ranges for CONSERVATIVE / MODERATE / AGGRESSIVE profiles."""
    current_price = tick_to_price(current_tick, decimals0, decimals1)
    widths = _RANGE_WIDTHS.get(pair_type, _RANGE_WIDTHS["volatile"])

    # If 24h price change exceeds a profile's width, widen it by the change magnitude
    change_factor = abs(price_change_24h) if price_change_24h is not None else 0.0

    suggestions: list[dict[str, Any]] = []
    for profile, base_width in widths.items():
        # Widen the range if recent volatility exceeds the preset width
        effective_width = max(base_width, change_factor * 1.2)

        price_lower = current_price * (1 - effective_width / 100)
        price_upper = current_price * (1 + effective_width / 100)

        tick_lower_raw = price_to_tick(price_lower, decimals0, decimals1)
        tick_upper_raw = price_to_tick(price_upper, decimals0, decimals1)

        tick_lower = nearest_usable_tick(tick_lower_raw, tick_spacing)
        tick_upper = nearest_usable_tick(tick_upper_raw, tick_spacing)

        # Convert aligned ticks back to human-readable prices
        price_lower_aligned = tick_to_price(tick_lower, decimals0, decimals1)
        price_upper_aligned = tick_to_price(tick_upper, decimals0, decimals1)

        suggestions.append({
            "profile": profile,
            "tickLower": tick_lower,
            "tickUpper": tick_upper,
            "priceLower": f"{price_lower_aligned:.12g}",
            "priceUpper": f"{price_upper_aligned:.12g}",
            "rangeWidthPct": round(effective_width, 1),
        })

    return suggestions


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def suggest_ranges(
    chain_name: str,
    token_a: str,
    token_b: str,
    fee_tier: int,
    rpc_url: str | None = None,
) -> dict[str, Any]:
    """Resolve pool state, detect pair type, and return range suggestions."""
    load_local_env()

    chain = normalize_chain(chain_name)
    fee = validate_fee_tier(fee_tier)

    # Resolve tokens (needs RPC for on-chain metadata of raw addresses)
    from uniswap_autopilot.execute._internal.rpc import resolve_rpc_url
    rpc, _ = resolve_rpc_url(rpc_url, chain.chain_id)
    token_a_info = resolve_token(chain, token_a, rpc)
    token_b_info = resolve_token(chain, token_b, rpc)

    # Query pool state
    pool_info = query_pool_full_info(chain_name, token_a, token_b, fee, rpc_url)
    if not pool_info.get("exists"):
        raise RuntimeError(
            f"No V3 pool found for {token_a}/{token_b} with fee tier {fee_tier} on {chain_name}"
        )

    current_tick: int = pool_info["currentTick"]
    current_price = pool_info["currentPrice"]
    tick_spacing = fee_tier_to_tick_spacing(fee)

    # Determine decimals for token0 / token1 ordering
    token0_addr = pool_info["token0"]
    addr_a = token_a_info.get("address", "")
    addr_b = token_b_info.get("address", "")
    if token_a_info.get("address") == "NATIVE":
        wrapped = chain.tokens.get(chain.wrapped_native_symbol.upper())
        addr_a = wrapped.address if wrapped else resolve_token(chain, chain.wrapped_native_symbol, rpc)["address"]
    if token_b_info.get("address") == "NATIVE":
        wrapped = chain.tokens.get(chain.wrapped_native_symbol.upper())
        addr_b = wrapped.address if wrapped else resolve_token(chain, chain.wrapped_native_symbol, rpc)["address"]

    if token0_addr.lower() == addr_a.lower():
        decimals0 = token_a_info["decimals"]
        decimals1 = token_b_info["decimals"]
    else:
        decimals0 = token_b_info["decimals"]
        decimals1 = token_a_info["decimals"]

    # Fetch 24h price change from DexScreener
    price_change_24h: float | None = None
    try:
        ds_token_a = _ds_lookup(chain.key, addr_a)
        if ds_token_a and ds_token_a.get("priceChange"):
            price_change_24h = ds_token_a["priceChange"].get("h24")
    except Exception:
        pass

    # Detect pair type
    pair_type = detect_pair_type(token_a_info, token_b_info)

    # Calculate suggestions
    suggestions = calculate_range_suggestions(
        current_tick=current_tick,
        tick_spacing=tick_spacing,
        decimals0=decimals0,
        decimals1=decimals1,
        pair_type=pair_type,
        price_change_24h=price_change_24h,
    )

    return {
        "action": "range_suggestions",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "tokenA": token_a_info,
        "tokenB": token_b_info,
        "feeTier": fee,
        "currentTick": current_tick,
        "currentPrice": f"{current_price:.12g}",
        "pairType": pair_type,
        "priceChange24h": price_change_24h,
        "suggestions": suggestions,
    }


# ---------------------------------------------------------------------------
# Human-readable table
# ---------------------------------------------------------------------------

def _print_suggestion_table(result: dict[str, Any]) -> None:
    """Print a human-readable table of range suggestions."""
    token_a_sym = result["tokenA"].get("symbol", "?")
    token_b_sym = result["tokenB"].get("symbol", "?")
    pair = f"{token_a_sym}/{token_b_sym}"
    chain = result["chain"]["key"]
    fee = result["feeTier"]
    pair_type = result["pairType"]
    current_price = result["currentPrice"]
    pct_24h = result.get("priceChange24h")
    pct_str = f"{pct_24h:+.2f}%" if pct_24h is not None else "N/A"

    print(f"  Range Suggestions: {pair} on {chain} (fee={fee})")
    print(f"  Pair type: {pair_type}  |  Current price: {current_price}  |  24h change: {pct_str}")
    print()
    print(f"  {'Profile':<14} {'TickLower':>10} {'TickUpper':>10} {'PriceLower':>18} {'PriceUpper':>18} {'Width':>7}")
    print(f"  {'-'*14} {'-'*10} {'-'*10} {'-'*18} {'-'*18} {'-'*7}")
    for s in result["suggestions"]:
        print(
            f"  {s['profile']:<14} {s['tickLower']:>10} {s['tickUpper']:>10} "
            f"{s['priceLower']:>18} {s['priceUpper']:>18} {s['rangeWidthPct']:>6.1f}%"
        )
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Suggest V3 price ranges for a Uniswap pool based on pair type and recent volatility"
    )
    parser.add_argument("--chain", required=True, help="Chain name, e.g. base / ethereum")
    parser.add_argument("--token-a", required=True, help="Token symbol or address")
    parser.add_argument("--token-b", required=True, help="Token symbol or address")
    parser.add_argument("--fee-tier", type=int, required=True, help="Fee tier: 100 / 500 / 3000 / 10000")
    parser.add_argument("--rpc-url", help="RPC URL (reads from env if omitted)")
    parser.add_argument("--output", help="Output JSON file path")
    args = parser.parse_args()

    try:
        result = suggest_ranges(args.chain, args.token_a, args.token_b, args.fee_tier, args.rpc_url)
        _print_suggestion_table(result)
        if args.output:
            Path(args.output).write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        dump_json(result)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
