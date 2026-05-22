#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any


from uniswap_autopilot.common.common import (
    dump_json,
    get_v2_factory_address,
    load_local_env,
    normalize_chain,
    resolve_token,
    resolve_wallet_address,
    sort_token_addresses,
)
from uniswap_autopilot.execute._internal.rpc import resolve_rpc_url
from uniswap_autopilot.lp.v2.pair import query_lp_balance, query_pair_address, query_reserves, query_total_supply

V2_CHAINS = ["ethereum", "optimism", "bsc", "polygon", "arbitrum", "avalanche", "world_chain"]

COMMON_PAIRS = [
    ("WETH", "USDC"), ("WETH", "USDT"), ("WETH", "DAI"),
    ("WBTC", "WETH"), ("WBTC", "USDC"),
    ("UNI", "WETH"), ("LINK", "WETH"),
    ("USDC", "USDT"),
    ("WBNB", "USDT"), ("WBNB", "USDC"), ("WBNB", "ETH"),
    ("WAVAX", "USDC"), ("WAVAX", "USDT"), ("WAVAX", "WETH"),
    ("WLD", "USDC"), ("WLD", "WETH"),
    ("OP", "WETH"), ("OP", "USDC"),
    ("ARB", "WETH"), ("ARB", "USDC"),
    ("CAKE", "WBNB"),
    ("WMATIC", "USDC"), ("WMATIC", "WETH"),
]


def _base_unit_to_human(raw: int, decimals: int) -> str:
    if raw == 0:
        return "0"
    d = Decimal(raw) / Decimal(10 ** decimals)
    return f"{d:.6f}".rstrip("0").rstrip(".")


def scan_v2_positions(
    chain_name: str,
    wallet: str | None = None,
    rpc_url: str | None = None,
    pairs: list[tuple[str, str]] | None = None,
    chain_tokens: dict[str, dict] | None = None,
) -> list[dict[str, Any]]:
    chain = normalize_chain(chain_name)
    wallet_addr = resolve_wallet_address(wallet)
    if not wallet_addr:
        raise ValueError("wallet address required")

    rpc = rpc_url or resolve_rpc_url(None, chain.chain_id)[0]
    try:
        factory = get_v2_factory_address(chain_name)
    except ValueError:
        return []

    scan_pairs = pairs or COMMON_PAIRS
    token_cache: dict[str, dict] = dict(chain_tokens or {})

    positions: list[dict[str, Any]] = []

    for sym_a, sym_b in scan_pairs:
        try:
            if sym_a not in token_cache:
                token_cache[sym_a] = resolve_token(chain, sym_a, rpc)
            if sym_b not in token_cache:
                token_cache[sym_b] = resolve_token(chain, sym_b, rpc)
        except Exception:
            continue

        tok_a = token_cache[sym_a]
        tok_b = token_cache[sym_b]

        t0, t1 = sort_token_addresses(tok_a["address"], tok_b["address"])

        pair_addr = query_pair_address(t0, t1, factory, rpc)
        if not pair_addr:
            continue

        lp_bal = query_lp_balance(pair_addr, wallet_addr, rpc)
        if lp_bal == 0:
            continue

        total_supply = query_total_supply(pair_addr, rpc)
        reserves = query_reserves(pair_addr, rpc)

        share_pct = Decimal(lp_bal) / Decimal(total_supply) * Decimal("100") if total_supply > 0 else Decimal("0")

        # Determine which reserve maps to which token
        reserve_a = reserves["reserve0"] if tok_a["address"].lower() == t0.lower() else reserves["reserve1"]
        reserve_b = reserves["reserve1"] if tok_a["address"].lower() == t0.lower() else reserves["reserve0"]

        my_a = int(Decimal(reserve_a) * Decimal(lp_bal) / Decimal(total_supply)) if total_supply > 0 else 0
        my_b = int(Decimal(reserve_b) * Decimal(lp_bal) / Decimal(total_supply)) if total_supply > 0 else 0

        positions.append({
            "chain": chain_name,
            "pair": f"{sym_a}/{sym_b}",
            "pairAddress": pair_addr,
            "lpBalance": str(lp_bal),
            "totalSupply": str(total_supply),
            "sharePct": f"{share_pct:.6f}",
            "tokenA": {"symbol": sym_a, "amount": str(my_a), "human": _base_unit_to_human(my_a, tok_a["decimals"])},
            "tokenB": {"symbol": sym_b, "amount": str(my_b), "human": _base_unit_to_human(my_b, tok_b["decimals"])},
        })

    return positions


def main() -> None:
    parser = argparse.ArgumentParser(description="查询钱包 V2 LP 仓位")
    parser.add_argument("--chain", default="", help="链名（--all-chains 时可省略）")
    parser.add_argument("--wallet", help="钱包地址")
    parser.add_argument("--rpc-url", help="RPC URL")
    parser.add_argument("--pair", action="append", help="指定交易对，如 WETH/USDC（可多次指定）")
    parser.add_argument("--all-chains", action="store_true", help="扫描所有 V2 链")
    args = parser.parse_args()

    load_local_env()

    custom_pairs: list[tuple[str, str]] | None = None
    if args.pair:
        custom_pairs = []
        for p in args.pair:
            parts = p.split("/")
            if len(parts) != 2:
                print(f"invalid pair format: {p}, expected TOKEN_A/TOKEN_B", file=sys.stderr)
                sys.exit(1)
            custom_pairs.append((parts[0].strip(), parts[1].strip()))

    if args.all_chains:
        all_positions: list[dict[str, Any]] = []
        for c in V2_CHAINS:
            try:
                positions = scan_v2_positions(c, args.wallet, args.rpc_url, custom_pairs)
                all_positions.extend(positions)
            except Exception as e:
                print(f"  {c}: {e}", file=sys.stderr)
        if not all_positions:
            print("No V2 LP positions found on any chain.")
        else:
            print(f"\nV2 LP Positions ({len(all_positions)} found):")
            print("-" * 60)
            for pos in all_positions:
                print(f"  {pos['chain']:12s} {pos['pair']:15s} share={pos['sharePct']}%")
                print(f"    tokenA: {pos['tokenA']['human']} {pos['tokenA']['symbol']}")
                print(f"    tokenB: {pos['tokenB']['human']} {pos['tokenB']['symbol']}")
                print(f"    pair:   {pos['pairAddress']}")
            print("-" * 60)
        dump_json({"action": "v2_positions_all", "positions": all_positions, "count": len(all_positions)})
    else:
        if not args.chain:
            parser.error("--chain is required when not using --all-chains")
        positions = scan_v2_positions(args.chain, args.wallet, args.rpc_url, custom_pairs)
        if not positions:
            print(f"No V2 LP positions found on {args.chain}.")
        else:
            print(f"\nV2 LP Positions on {args.chain} ({len(positions)} found):")
            print("-" * 60)
            for pos in positions:
                print(f"  {pos['pair']:15s} share={pos['sharePct']}%")
                print(f"    tokenA: {pos['tokenA']['human']} {pos['tokenA']['symbol']}")
                print(f"    tokenB: {pos['tokenB']['human']} {pos['tokenB']['symbol']}")
                print(f"    pair:   {pos['pairAddress']}")
            print("-" * 60)
        dump_json({"action": "v2_positions", "chain": args.chain, "positions": positions, "count": len(positions)})


if __name__ == "__main__":
    main()
