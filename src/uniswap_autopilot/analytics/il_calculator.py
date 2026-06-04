#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


from uniswap_autopilot.common.common import dump_json, load_local_env, normalize_chain, resolve_token
from uniswap_autopilot.analytics.position import (
    analyze_position,
    calculate_position_amounts,
    fetch_token_prices,
)
from uniswap_autopilot.lp.v3.tick import nearest_usable_tick, price_to_tick, tick_to_price
from uniswap_autopilot.analytics.range_suggest import suggest_ranges

DEFILLAMA_YIELDS_URL = "https://yields.llama.fi/pools"

DEFILLAMA_CHAIN_MAP: dict[str, str] = {
    "avalanche": "avax",
    "world_chain": "worldchain",
    "polygon": "matic",
}

DEFAULT_SCENARIOS = [-50, -30, -20, -10, -5, 5, 10, 20, 30, 50]


def calculate_il(
    price_entry: float,
    price_current: float,
    tick_lower: int,
    tick_upper: int,
    decimals0: int,
    decimals1: int,
    liquidity: int,
) -> dict[str, Any]:
    tick_entry = price_to_tick(price_entry, decimals0, decimals1)
    tick_current = price_to_tick(price_current, decimals0, decimals1)

    amount0_entry, amount1_entry = calculate_position_amounts(
        liquidity, tick_entry, tick_lower, tick_upper, decimals0, decimals1,
    )
    amount0_current, amount1_current = calculate_position_amounts(
        liquidity, tick_current, tick_lower, tick_upper, decimals0, decimals1,
    )

    hodl_value = amount0_entry * price_current + amount1_entry
    lp_value = amount0_current * price_current + amount1_current

    if hodl_value > 0:
        il_pct = (lp_value / hodl_value - 1) * 100
    else:
        il_pct = 0.0

    fee_break_even = abs(il_pct) / (1 + il_pct / 100) if il_pct > -100 else float("inf")

    in_range = tick_lower < tick_current < tick_upper

    price_change_pct = ((price_current / price_entry) - 1) * 100 if price_entry != 0 else 0.0

    return {
        "priceEntry": price_entry,
        "priceCurrent": price_current,
        "priceChangePct": price_change_pct,
        "tickEntry": tick_entry,
        "tickCurrent": tick_current,
        "tickLower": tick_lower,
        "tickUpper": tick_upper,
        "inRange": in_range,
        "amountsAtEntry": {
            "amount0": round(amount0_entry, 8),
            "amount1": round(amount1_entry, 8),
            "valueToken1": round(amount0_entry * price_entry + amount1_entry, 8),
        },
        "amountsAtCurrent": {
            "amount0": round(amount0_current, 8),
            "amount1": round(amount1_current, 8),
            "valueToken1": round(lp_value, 8),
        },
        "hodlValueToken1": round(hodl_value, 8),
        "lpValueToken1": round(lp_value, 8),
        "impermanentLossPct": round(il_pct, 4),
        "feeBreakEvenPct": round(fee_break_even, 4),
    }


def estimate_il_for_position(
    chain_name: str,
    token_id: int,
    price_change_pct: float,
    rpc_url: str | None = None,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    pos = analyze_position(chain_name, token_id, rpc_url)

    addr0 = pos["token0"]["address"]
    addr1 = pos["token1"]["address"]

    # Resolve decimals from token catalog since analyze_position may not include them
    try:
        tok0 = resolve_token(chain, addr0)
        decimals0 = tok0["decimals"]
    except Exception:
        decimals0 = 18
    try:
        tok1 = resolve_token(chain, addr1)
        decimals1 = tok1["decimals"]
    except Exception:
        decimals1 = 18

    current_tick = pos["currentTick"]
    current_price = tick_to_price(current_tick, decimals0, decimals1)
    hypothetical_price = current_price * (1 + price_change_pct / 100)
    liquidity = int(pos["liquidity"])

    il_result = calculate_il(
        price_entry=current_price,
        price_current=hypothetical_price,
        tick_lower=pos["tickLower"],
        tick_upper=pos["tickUpper"],
        decimals0=decimals0,
        decimals1=decimals1,
        liquidity=liquidity,
    )

    price0, price1 = fetch_token_prices(chain.key, addr0, addr1)
    if price0 is not None and price1 is not None:
        il_result["hodlValueUsd"] = round(il_result["hodlValueToken1"] * price1, 2)
        il_result["lpValueUsd"] = round(il_result["lpValueToken1"] * price1, 2)
        il_result["price0Usd"] = price0
        il_result["price1Usd"] = price1

    return {
        "action": "il_position",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "tokenId": token_id,
        "token0": pos["token0"]["symbol"],
        "token1": pos["token1"]["symbol"],
        "feeTier": pos["feeTier"],
        "currentPrice": current_price,
        "hypotheticalPrice": hypothetical_price,
        "priceChangePct": price_change_pct,
        "currentLiquidity": str(liquidity),
        **il_result,
    }


def compare_il_across_ranges(
    chain_name: str,
    token_a: str,
    token_b: str,
    fee_tier: int,
    price_change_pct: float,
    rpc_url: str | None = None,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    tok_a = resolve_token(chain, token_a)
    tok_b = resolve_token(chain, token_b)

    ranges = suggest_ranges(chain_name, token_a, token_b, fee_tier, rpc_url)
    current_price = float(ranges["currentPrice"])
    hypothetical_price = current_price * (1 + price_change_pct / 100)
    decimals0 = ranges["tokenA"].get("decimals", 18)
    decimals1 = ranges["tokenB"].get("decimals", 18)

    comparisons = []
    for suggestion in ranges.get("suggestions", []):
        il = calculate_il(
            price_entry=current_price,
            price_current=hypothetical_price,
            tick_lower=suggestion["tickLower"],
            tick_upper=suggestion["tickUpper"],
            decimals0=decimals0,
            decimals1=decimals1,
            liquidity=10**18,
        )
        comparisons.append({
            "profile": suggestion["profile"],
            "rangeWidthPct": suggestion.get("rangeWidthPct"),
            "tickLower": suggestion["tickLower"],
            "tickUpper": suggestion["tickUpper"],
            "impermanentLossPct": il["impermanentLossPct"],
            "inRange": il["inRange"],
            "feeBreakEvenPct": il["feeBreakEvenPct"],
        })

    return {
        "action": "il_ranges_compare",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "tokenA": ranges["tokenA"]["symbol"],
        "tokenB": ranges["tokenB"]["symbol"],
        "feeTier": ranges["feeTier"],
        "currentPrice": current_price,
        "hypotheticalPrice": hypothetical_price,
        "priceChangePct": price_change_pct,
        "comparisons": comparisons,
    }


def _fetch_pool_apy(
    chain_name: str,
    pool_address: str,
) -> float | None:
    chain = normalize_chain(chain_name)
    dl_chain = DEFILLAMA_CHAIN_MAP.get(chain.key, chain.key).capitalize()
    try:
        request = urllib.request.Request(DEFILLAMA_YIELDS_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, json.JSONDecodeError):
        return None

    target = pool_address.lower()
    for pool in data.get("data", []):
        if pool.get("project") != "uniswap-v3":
            continue
        if (pool.get("chain") or "").lower() != dl_chain.lower():
            continue
        if (pool.get("pool") or "").lower() == target:
            apy = pool.get("apy")
            return float(apy) if apy is not None else None
    return None


def _get_pool_address_for_pair(
    chain_name: str,
    token_a: str,
    token_b: str,
    fee_tier: int,
    rpc_url: str | None = None,
) -> str | None:
    from uniswap_autopilot.lp.v3.pool import query_pool_full_info
    try:
        info = query_pool_full_info(chain_name, token_a, token_b, fee_tier, rpc_url)
        if info.get("exists"):
            return info.get("poolAddress")
    except Exception:
        pass
    return None


def simulate_il(
    chain_name: str,
    token_a: str,
    token_b: str,
    fee_tier: int,
    scenarios: list[float] | None = None,
    rpc_url: str | None = None,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    effective_scenarios = scenarios or DEFAULT_SCENARIOS

    ranges = suggest_ranges(chain_name, token_a, token_b, fee_tier, rpc_url)
    current_price = float(ranges["currentPrice"])
    decimals0 = ranges["tokenA"].get("decimals", 18)
    decimals1 = ranges["tokenB"].get("decimals", 18)

    pool_addr = _get_pool_address_for_pair(chain_name, token_a, token_b, fee_tier, rpc_url)
    pool_apy = _fetch_pool_apy(chain_name, pool_addr) if pool_addr else None

    matrix: list[dict[str, Any]] = []
    for pct in effective_scenarios:
        hypothetical_price = current_price * (1 + pct / 100)
        row: dict[str, Any] = {
            "priceChangePct": pct,
            "hypotheticalPrice": round(hypothetical_price, 8),
            "profiles": [],
        }
        for suggestion in ranges.get("suggestions", []):
            il = calculate_il(
                price_entry=current_price,
                price_current=hypothetical_price,
                tick_lower=suggestion["tickLower"],
                tick_upper=suggestion["tickUpper"],
                decimals0=decimals0,
                decimals1=decimals1,
                liquidity=10**18,
            )
            break_even = abs(il["feeBreakEvenPct"])
            apy_covers = pool_apy is not None and pool_apy >= break_even if il["impermanentLossPct"] < 0 else True
            row["profiles"].append({
                "profile": suggestion["profile"],
                "impermanentLossPct": il["impermanentLossPct"],
                "feeBreakEvenPct": il["feeBreakEvenPct"],
                "inRange": il["inRange"],
                "apyCoversIL": apy_covers,
            })
        matrix.append(row)

    return {
        "action": "il_simulate",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "tokenA": ranges["tokenA"]["symbol"],
        "tokenB": ranges["tokenB"]["symbol"],
        "feeTier": ranges["feeTier"],
        "currentPrice": current_price,
        "poolAddress": pool_addr,
        "poolApy": pool_apy,
        "scenarios": effective_scenarios,
        "matrix": matrix,
    }


def simulate_position(
    chain_name: str,
    token_id: int,
    scenarios: list[float] | None = None,
    rpc_url: str | None = None,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    effective_scenarios = scenarios or DEFAULT_SCENARIOS

    pos = analyze_position(chain_name, token_id, rpc_url)
    addr0 = pos["token0"]["address"]
    addr1 = pos["token1"]["address"]

    try:
        tok0 = resolve_token(chain, addr0)
        decimals0 = tok0["decimals"]
    except Exception:
        decimals0 = 18
    try:
        tok1 = resolve_token(chain, addr1)
        decimals1 = tok1["decimals"]
    except Exception:
        decimals1 = 18

    current_tick = pos["currentTick"]
    current_price = tick_to_price(current_tick, decimals0, decimals1)
    tick_lower = pos["tickLower"]
    tick_upper = pos["tickUpper"]
    liquidity = int(pos["liquidity"])

    pool_addr = pos.get("poolAddress")
    pool_apy = _fetch_pool_apy(chain_name, pool_addr) if pool_addr else None

    price0, price1 = fetch_token_prices(chain.key, addr0, addr1)

    matrix: list[dict[str, Any]] = []
    for pct in effective_scenarios:
        hypothetical_price = current_price * (1 + pct / 100)
        il = calculate_il(
            price_entry=current_price,
            price_current=hypothetical_price,
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            decimals0=decimals0,
            decimals1=decimals1,
            liquidity=liquidity,
        )
        break_even = abs(il["feeBreakEvenPct"])
        apy_covers = pool_apy is not None and pool_apy >= break_even if il["impermanentLossPct"] < 0 else True

        lp_usd = il["lpValueToken1"] * (price1 or 0)
        hodl_usd = il["hodlValueToken1"] * (price1 or 0)

        matrix.append({
            "priceChangePct": pct,
            "hypotheticalPrice": round(hypothetical_price, 8),
            "impermanentLossPct": il["impermanentLossPct"],
            "feeBreakEvenPct": il["feeBreakEvenPct"],
            "inRange": il["inRange"],
            "lpValueUsd": round(lp_usd, 2) if price1 else None,
            "hodlValueUsd": round(hodl_usd, 2) if price1 else None,
            "apyCoversIL": apy_covers,
        })

    return {
        "action": "il_simulate_position",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "tokenId": token_id,
        "token0": pos["token0"]["symbol"],
        "token1": pos["token1"]["symbol"],
        "feeTier": pos["feeTier"],
        "currentPrice": current_price,
        "tickLower": tick_lower,
        "tickUpper": tick_upper,
        "currentLiquidity": str(liquidity),
        "poolAddress": pool_addr,
        "poolApy": pool_apy,
        "scenarios": effective_scenarios,
        "matrix": matrix,
    }


def quick_il(
    price_entry: float,
    price_current: float,
    range_pct: float,
    decimals0: int = 18,
    decimals1: int = 6,
    tick_spacing: int = 60,
) -> dict[str, Any]:
    tick_entry = price_to_tick(price_entry, decimals0, decimals1)
    tick_lower = nearest_usable_tick(
        price_to_tick(price_entry * (1 - range_pct / 200), decimals0, decimals1),
        tick_spacing,
    )
    tick_upper = nearest_usable_tick(
        price_to_tick(price_entry * (1 + range_pct / 200), decimals0, decimals1),
        tick_spacing,
    )

    il = calculate_il(
        price_entry=price_entry,
        price_current=price_current,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        decimals0=decimals0,
        decimals1=decimals1,
        liquidity=10**18,
    )
    il["rangePct"] = range_pct
    il["tickSpacing"] = tick_spacing
    return {
        "action": "il_quick",
        **il,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Uniswap V3 Impermanent Loss Calculator")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("position", help="Estimate IL for an on-chain position given a hypothetical price change")
    p.add_argument("--chain", required=True)
    p.add_argument("--token-id", type=int, required=True)
    p.add_argument("--price-change", type=float, required=True, help="Hypothetical price change %% (e.g. -20 for -20%%)")
    p.add_argument("--rpc-url")
    p.add_argument("--output")

    r = sub.add_parser("ranges", help="Compare IL across CONSERVATIVE/MODERATE/AGGRESSIVE ranges")
    r.add_argument("--chain", required=True)
    r.add_argument("--token-a", required=True)
    r.add_argument("--token-b", required=True)
    r.add_argument("--fee-tier", type=int, required=True)
    r.add_argument("--price-change", type=float, required=True, help="Hypothetical price change %%")
    r.add_argument("--rpc-url")
    r.add_argument("--output")

    q = sub.add_parser("quick", help="Quick IL calculation without on-chain data")
    q.add_argument("--price-entry", type=float, required=True, help="Entry price (token1 per token0)")
    q.add_argument("--price-current", type=float, required=True, help="Current/hypothetical price")
    q.add_argument("--range-pct", type=float, required=True, help="Range width %% (e.g. 20 = +/-10%% each side)")
    q.add_argument("--decimals0", type=int, default=18)
    q.add_argument("--decimals1", type=int, default=6)
    q.add_argument("--tick-spacing", type=int, default=60)
    q.add_argument("--output")

    sim = sub.add_parser("simulate", help="Multi-scenario IL simulation with fee yield comparison")
    sim.add_argument("--chain", required=True)
    sim.add_argument("--token-a", required=True)
    sim.add_argument("--token-b", required=True)
    sim.add_argument("--fee-tier", type=int, required=True)
    sim.add_argument("--scenarios", help=f"Comma-separated price change %% (default: {','.join(str(s) for s in DEFAULT_SCENARIOS)})")
    sim.add_argument("--rpc-url")
    sim.add_argument("--output")

    sp = sub.add_parser("simulate-position", help="Multi-scenario IL simulation for an existing position")
    sp.add_argument("--chain", required=True)
    sp.add_argument("--token-id", type=int, required=True)
    sp.add_argument("--scenarios", help=f"Comma-separated price change %% (default: {','.join(str(s) for s in DEFAULT_SCENARIOS)})")
    sp.add_argument("--rpc-url")
    sp.add_argument("--output")

    args = parser.parse_args()
    load_local_env()

    if args.command == "position":
        result = estimate_il_for_position(args.chain, args.token_id, args.price_change, args.rpc_url)
        print(f"IL for position #{args.token_id} at {args.price_change:+.1f}% price change: {result['impermanentLossPct']:.4f}%")
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dump_json(result)
    elif args.command == "ranges":
        result = compare_il_across_ranges(args.chain, args.token_a, args.token_b, args.fee_tier, args.price_change, args.rpc_url)
        for c in result["comparisons"]:
            print(f"  {c['profile']:13s}: IL={c['impermanentLossPct']:+.4f}%  inRange={c['inRange']}")
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dump_json(result)
    elif args.command == "quick":
        result = quick_il(args.price_entry, args.price_current, args.range_pct, args.decimals0, args.decimals1, args.tick_spacing)
        print(f"IL at {result['priceChangePct']:+.1f}% price change, range ±{args.range_pct/2}%: {result['impermanentLossPct']:.4f}%")
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dump_json(result)
    elif args.command == "simulate":
        scenarios = None
        if args.scenarios:
            scenarios = [float(s.strip()) for s in args.scenarios.split(",") if s.strip()]
        result = simulate_il(args.chain, args.token_a, args.token_b, args.fee_tier, scenarios, args.rpc_url)
        apy_str = f"{result['poolApy']:.2f}%" if result["poolApy"] is not None else "N/A"
        print(f"IL Simulation: {result['tokenA']}/{result['tokenB']} fee={result['feeTier']} pool_apy={apy_str}")
        print(f"  {'Price%':>8s}  {'Profile':13s}  {'IL%':>10s}  {'BreakEven%':>10s}  {'InRng':>5s}  {'APY>IL':>6s}")
        for row in result["matrix"]:
            for pr in row["profiles"]:
                print(f"  {row['priceChangePct']:>+7.0f}%  {pr['profile']:13s}  {pr['impermanentLossPct']:>+9.4f}%  {pr['feeBreakEvenPct']:>+9.4f}%  {str(pr['inRange']):>5s}  {str(pr['apyCoversIL']):>6s}")
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dump_json(result)
    elif args.command == "simulate-position":
        scenarios = None
        if args.scenarios:
            scenarios = [float(s.strip()) for s in args.scenarios.split(",") if s.strip()]
        result = simulate_position(args.chain, args.token_id, scenarios, args.rpc_url)
        apy_str = f"{result['poolApy']:.2f}%" if result["poolApy"] is not None else "N/A"
        print(f"IL Simulation: position #{args.token_id} {result['token0']}/{result['token1']} pool_apy={apy_str}")
        print(f"  {'Price%':>8s}  {'IL%':>10s}  {'BreakEven%':>10s}  {'InRng':>5s}  {'APY>IL':>6s}  {'LP USD':>12s}")
        for row in result["matrix"]:
            lp_usd_str = f"${row['lpValueUsd']:.2f}" if row.get("lpValueUsd") is not None else "-"
            print(f"  {row['priceChangePct']:>+7.0f}%  {row['impermanentLossPct']:>+9.4f}%  {row['feeBreakEvenPct']:>+9.4f}%  {str(row['inRange']):>5s}  {str(row['apyCoversIL']):>6s}  {lp_usd_str:>12s}")
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dump_json(result)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
