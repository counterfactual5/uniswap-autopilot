#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from typing import Any


from uniswap_autopilot.common.common import dump_json, load_local_env, normalize_chain, resolve_token
from uniswap_autopilot.execute._internal.rpc import (
    decode_uint, eth_call, encode_selector, resolve_rpc_url,
)
from uniswap_autopilot.lp.v3.pool import query_slot0
from uniswap_autopilot.lp.v3.position import query_position, query_positions_by_owner


# Price feed integration


# ---------------------------------------------------------------------------
# V3 position math helpers
# ---------------------------------------------------------------------------

def _sqrt_price(tick: int) -> float:
    """Calculate sqrt(1.0001^tick) = 1.0001^(tick/2)."""
    return 1.0001 ** (tick / 2)


def calculate_position_amounts(
    liquidity: int,
    current_tick: int,
    tick_lower: int,
    tick_upper: int,
    decimals0: int,
    decimals1: int,
) -> tuple[float, float]:
    """Return (amount0_human, amount1_human) for a V3 position.

    Uses the standard Uniswap V3 liquidity math:
    - currentTick <= tickLower: 100 % token0
    - currentTick >= tickUpper: 100 % token1
    - in-range:              both tokens
    """
    L = float(liquidity)
    if L == 0:
        return 0.0, 0.0

    sqrt_lower = _sqrt_price(tick_lower)
    sqrt_upper = _sqrt_price(tick_upper)

    if current_tick <= tick_lower:
        # Entirely token0: amount0 = L * (1/sqrtLower - 1/sqrtUpper)
        amount0_raw = L * (sqrt_upper - sqrt_lower) / (sqrt_lower * sqrt_upper)
        amount1_raw = 0.0
    elif current_tick >= tick_upper:
        # Entirely token1: amount1 = L * (sqrtUpper - sqrtLower)
        amount0_raw = 0.0
        amount1_raw = L * (sqrt_upper - sqrt_lower)
    else:
        # In range: amount0 = L * (1/sqrtCurrent - 1/sqrtUpper), amount1 = L * (sqrtCurrent - sqrtLower)
        sqrt_current = _sqrt_price(current_tick)
        amount0_raw = L * (sqrt_upper - sqrt_current) / (sqrt_current * sqrt_upper)
        amount1_raw = L * (sqrt_current - sqrt_lower)

    amount0_human = amount0_raw / (10 ** decimals0)
    amount1_human = amount1_raw / (10 ** decimals1)
    return amount0_human, amount1_human


# ---------------------------------------------------------------------------
# Fee growth queries & fee estimation
# ---------------------------------------------------------------------------

def query_fee_growth_global(pool_address: str, rpc_url: str) -> tuple[int, int]:
    """Query feeGrowthGlobal0X128 and feeGrowthGlobal1X128 from the pool."""
    sel0 = encode_selector("feeGrowthGlobal0X128()")
    raw0 = eth_call(pool_address, sel0, rpc_url)
    fg0 = decode_uint(raw0)

    sel1 = encode_selector("feeGrowthGlobal1X128()")
    raw1 = eth_call(pool_address, sel1, rpc_url)
    fg1 = decode_uint(raw1)
    return fg0, fg1


def estimate_uncollected_fees(
    position: dict,
    fee_growth_global0: int,
    fee_growth_global1: int,
    decimals0: int,
    decimals1: int,
) -> tuple[float, float]:
    """Simple approximation of uncollected fees.

    fee_token = liquidity * (feeGrowthGlobal - feeGrowthInsideLast) / 2^128

    This is an upper-bound approximation because feeGrowthInsideLast accounts
    for tick-range-specific growth, but we use the global value as a rough
    estimate when per-tick data is not available.
    """
    L = int(position.get("liquidity", "0"))
    if L == 0:
        return 0.0, 0.0

    fg_inside_last0 = int(position.get("feeGrowthInside0LastX128", "0"))
    fg_inside_last1 = int(position.get("feeGrowthInside1LastX128", "0"))

    Q128 = 2 ** 128

    delta0 = fee_growth_global0 - fg_inside_last0
    delta1 = fee_growth_global1 - fg_inside_last1

    # Handle potential underflow (position was last updated when global was higher
    # due to cross-tick movements) by taking absolute value.
    fee0_raw = abs(L * delta0) / Q128
    fee1_raw = abs(L * delta1) / Q128

    fee0_human = float(fee0_raw) / (10 ** decimals0)
    fee1_human = float(fee1_raw) / (10 ** decimals1)
    return fee0_human, fee1_human


# ---------------------------------------------------------------------------
# Token prices via DefiLlama
# ---------------------------------------------------------------------------

def fetch_token_prices(
    chain: str,
    addr0: str,
    addr1: str,
) -> tuple[float | None, float | None]:
    """Fetch current USD prices via price-feed (multi-source with fallback)."""
    from price_feed import get_prices_batch
    results = get_prices_batch([(chain, addr0), (chain, addr1)], tier="normal")
    price0 = results.get(f"{chain}:{addr0.lower()}", {}).get("price")
    price1 = results.get(f"{chain}:{addr1.lower()}", {}).get("price")
    return price0, price1


# ---------------------------------------------------------------------------
# Resolve token decimals from position data
# ---------------------------------------------------------------------------

def _resolve_token_decimals(
    chain_name: str,
    token_address: str,
    rpc_url: str,
) -> int:
    """Resolve decimals for a token address using the token catalog or on-chain."""
    try:
        chain = normalize_chain(chain_name)
        token_info = resolve_token(chain, token_address, rpc_url)
        return token_info["decimals"]
    except (ValueError, RuntimeError):
        return 18


def _resolve_token_symbol(
    chain_name: str,
    token_address: str,
    rpc_url: str,
) -> str:
    """Resolve symbol for a token address."""
    try:
        chain = normalize_chain(chain_name)
        token_info = resolve_token(chain, token_address, rpc_url)
        return token_info.get("symbol", token_address)
    except (ValueError, RuntimeError):
        return token_address


# ---------------------------------------------------------------------------
# Pool address from factory (needed when we only have the position)
# ---------------------------------------------------------------------------

def _get_pool_address_from_position(
    position: dict,
    chain_name: str,
    rpc_url: str,
) -> str:
    """Derive the pool address from position data by querying the factory."""
    from uniswap_autopilot.common.common import get_v3_factory_address, sort_token_addresses

    token0 = position["token0"]
    token1 = position["token1"]
    fee = position["fee"]

    token0_addr, token1_addr = sort_token_addresses(token0, token1)
    factory = get_v3_factory_address(chain_name)

    from uniswap_autopilot.lp.v3.pool import query_pool_address
    return query_pool_address(token0_addr, token1_addr, fee, factory, rpc_url)


# ---------------------------------------------------------------------------
# Core analytics functions
# ---------------------------------------------------------------------------

def analyze_position(
    chain_name: str,
    token_id: int,
    rpc_url: str | None = None,
) -> dict[str, Any]:
    """Analyze a single V3 LP position by token ID.

    Returns a comprehensive dict with position amounts, USD values,
    fee estimates, and range status.
    """
    chain = normalize_chain(chain_name)
    rpc, _ = resolve_rpc_url(rpc_url, chain.chain_id)
    if not rpc:
        raise RuntimeError(f"RPC URL not configured for {chain_name}")

    # 1. Query position data
    position = query_position(token_id, chain_name, rpc)

    liquidity = int(position.get("liquidity", "0"))
    tick_lower = position["tickLower"]
    tick_upper = position["tickUpper"]
    fee_tier = position["fee"]
    addr0 = position["token0"]
    addr1 = position["token1"]

    # 2. Derive pool address and query current tick
    pool_address = _get_pool_address_from_position(position, chain_name, rpc)
    if not pool_address or pool_address == "0x" + "0" * 40:
        raise RuntimeError(
            f"Pool not found for token0={addr0} token1={addr1} fee={fee_tier}"
        )

    slot0 = query_slot0(pool_address, rpc)
    current_tick = slot0["tick"]

    # 3. Resolve token metadata
    decimals0 = _resolve_token_decimals(chain_name, addr0, rpc)
    decimals1 = _resolve_token_decimals(chain_name, addr1, rpc)
    symbol0 = _resolve_token_symbol(chain_name, addr0, rpc)
    symbol1 = _resolve_token_symbol(chain_name, addr1, rpc)

    # 4. Calculate position amounts
    amount0, amount1 = calculate_position_amounts(
        liquidity, current_tick, tick_lower, tick_upper, decimals0, decimals1,
    )

    # 5. Fetch USD prices
    price0, price1 = fetch_token_prices(chain.key, addr0, addr1)

    # 6. Estimate uncollected fees
    fg0, fg1 = query_fee_growth_global(pool_address, rpc)
    fee0, fee1 = estimate_uncollected_fees(
        position, fg0, fg1, decimals0, decimals1,
    )

    # 7. Compute USD values
    amount0_usd = amount0 * price0 if price0 is not None and amount0 > 0 else 0.0
    amount1_usd = amount1 * price1 if price1 is not None and amount1 > 0 else 0.0
    total_value_usd = amount0_usd + amount1_usd

    fee0_usd = fee0 * price0 if price0 is not None and fee0 > 0 else 0.0
    fee1_usd = fee1 * price1 if price1 is not None and fee1 > 0 else 0.0
    total_fees_usd = fee0_usd + fee1_usd

    in_range = tick_lower < current_tick < tick_upper

    # 8. Build position result
    pos_result: dict[str, Any] = {
        "tokenId": token_id,
        "token0": {
            "symbol": symbol0,
            "address": addr0,
            "amount": f"{amount0:.6f}".rstrip("0").rstrip("."),
            "amountUsd": round(amount0_usd, 2),
        },
        "token1": {
            "symbol": symbol1,
            "address": addr1,
            "amount": f"{amount1:.6f}".rstrip("0").rstrip("."),
            "amountUsd": round(amount1_usd, 2),
        },
        "totalValueUsd": round(total_value_usd, 2),
        "uncollectedFees": {
            "token0": f"{fee0:.6f}".rstrip("0").rstrip("."),
            "token1": f"{fee1:.6f}".rstrip("0").rstrip("."),
            "totalUsd": round(total_fees_usd, 2),
        },
        "inRange": in_range,
        "feeTier": fee_tier,
        "tickLower": tick_lower,
        "tickUpper": tick_upper,
        "currentTick": current_tick,
        "liquidity": str(liquidity),
        "poolAddress": pool_address,
    }

    if price0 is None:
        pos_result["token0"]["priceUsd"] = None
    else:
        pos_result["token0"]["priceUsd"] = price0
    if price1 is None:
        pos_result["token1"]["priceUsd"] = None
    else:
        pos_result["token1"]["priceUsd"] = price1

    return pos_result


def analyze_positions_by_owner(
    chain_name: str,
    owner: str,
    rpc_url: str | None = None,
) -> dict[str, Any]:
    """Analyze all V3 LP positions owned by an address.

    Iterates over every token ID returned by query_positions_by_owner
    and runs analyze_position on each.
    """
    chain = normalize_chain(chain_name)
    rpc, _ = resolve_rpc_url(rpc_url, chain.chain_id)

    token_ids = query_positions_by_owner(owner, chain_name, rpc)

    positions: list[dict[str, Any]] = []
    total_value = 0.0
    total_fees = 0.0
    errors: list[dict[str, str]] = []

    for tid in token_ids:
        try:
            pos = analyze_position(chain_name, tid, rpc)
            positions.append(pos)
            total_value += pos.get("totalValueUsd", 0.0)
            total_fees += pos.get("uncollectedFees", {}).get("totalUsd", 0.0)
        except Exception as exc:
            errors.append({"tokenId": str(tid), "error": str(exc)})

    result: dict[str, Any] = {
        "action": "v3_position_analytics",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "owner": owner,
        "positions": positions,
        "totalValueUsd": round(total_value, 2),
        "totalFeesUsd": round(total_fees, 0),
        "positionCount": len(positions),
    }
    if errors:
        result["errors"] = errors
    return result


# ---------------------------------------------------------------------------
# Human-readable output
# ---------------------------------------------------------------------------

def _print_position_summary(pos: dict) -> None:
    """Print a human-readable summary of a single position."""
    tid = pos.get("tokenId", "?")
    t0 = pos.get("token0", {})
    t1 = pos.get("token1", {})
    symbol0 = t0.get("symbol", "?")
    symbol1 = t1.get("symbol", "?")
    amt0 = t0.get("amount", "0")
    amt1 = t1.get("amount", "0")
    total_usd = pos.get("totalValueUsd", 0)
    fees = pos.get("uncollectedFees", {})
    fees_usd = fees.get("totalUsd", 0)
    in_range = pos.get("inRange", False)
    fee_tier = pos.get("feeTier", "?")
    current_tick = pos.get("currentTick", "?")
    tick_lower = pos.get("tickLower", "?")
    tick_upper = pos.get("tickUpper", "?")

    range_status = "IN RANGE" if in_range else "OUT OF RANGE"
    print(f"  Position #{tid}:")
    print(f"    Pair: {symbol0}/{symbol1}  Fee: {fee_tier / 10000:.2f}%")
    print(f"    {symbol0}: {amt0}  ({t0.get('amountUsd', 0)} USD)")
    print(f"    {symbol1}: {amt1}  ({t1.get('amountUsd', 0)} USD)")
    print(f"    Total Value: {total_usd:.2f} USD")
    print(f"    Uncollected Fees: {fees.get('token0', '0')} {symbol0} + {fees.get('token1', '0')} {symbol1} = {fees_usd:.2f} USD")
    print(f"    Range: {range_status}  [tick {tick_lower}, {tick_upper}]  current={current_tick}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Uniswap V3 LP position analytics")
    parser.add_argument("--chain", required=True, help="Chain name, e.g. base, ethereum")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--token-id", type=int, help="Single LP NFT token ID")
    source.add_argument("--owner", help="Wallet address to analyze all positions")
    parser.add_argument("--rpc-url", help="RPC URL (reads from env if not provided)")
    args = parser.parse_args()

    try:
        load_local_env()

        chain = normalize_chain(args.chain)

        if args.token_id is not None:
            pos = analyze_position(args.chain, args.token_id, args.rpc_url)
            result: dict[str, Any] = {
                "action": "v3_position_analytics",
                "chain": {"key": chain.key, "chainId": chain.chain_id},
                "positions": [pos],
                "totalValueUsd": pos.get("totalValueUsd", 0),
                "totalFeesUsd": pos.get("uncollectedFees", {}).get("totalUsd", 0),
            }

            _print_position_summary(pos)
            print()
            dump_json(result)
        else:
            result = analyze_positions_by_owner(args.chain, args.owner, args.rpc_url)
            positions = result.get("positions", [])
            if positions:
                print(f"Found {len(positions)} position(s) for {args.owner}:")
                for pos in positions:
                    _print_position_summary(pos)
                    print()
            print(f"Total Value: {result.get('totalValueUsd', 0):.2f} USD")
            print(f"Total Fees:  {result.get('totalFeesUsd', 0):.2f} USD")
            print()
            dump_json(result)

    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
