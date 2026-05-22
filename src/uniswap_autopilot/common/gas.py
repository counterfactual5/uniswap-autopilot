#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


from uniswap_autopilot.common.common import dump_json, load_local_env, normalize_chain, PUBLIC_RPC_URLS
from uniswap_autopilot.execute._internal.rpc import (
    build_calldata, encode_uint, eth_fee_history, query_gas_price, resolve_rpc_url,
)

L2_UNWRAP_CHAINS = {
    "base", "arbitrum", "optimism", "polygon", "unichain",
    "linea", "blast", "zora", "world_chain", "soneium",
}

SPEED_PERCENTILE = {"slow": 15, "standard": 50, "fast": 85}


def estimate_gas_price(
    chain_name: str,
    speed: str = "standard",
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    rpc_url = PUBLIC_RPC_URLS.get(chain.key) or resolve_rpc_url(None, chain.chain_id)[0]
    if not rpc_url:
        raise RuntimeError(f"RPC URL not configured for {chain_name}")

    result: dict[str, Any] = {
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "speed": speed,
    }

    # Try EIP-1559 fee history
    try:
        data = eth_fee_history(4, "latest", rpc_url, reward_percentiles=[25, 50, 75])
        base_fee_hex = (data.get("baseFeePerGas") or [])[-1]
        base_fee = int(base_fee_hex, 16) if base_fee_hex else None
        result["baseFee"] = base_fee

        rewards = data.get("reward") or []
        if rewards and rewards[0] and rewards[-1]:
            pct = SPEED_PERCENTILE.get(speed, 50)
            idx = min(pct // 25, len(rewards[0]) - 1)
            priority_hex = rewards[-1][idx] if idx < len(rewards[-1]) else "0x0"
            priority_fee = int(priority_hex, 16)
        else:
            priority_fee = 1_500_000_000  # 1.5 gwei default
        result["priorityFee"] = priority_fee
        result["totalEstimate"] = (base_fee or 0) + priority_fee
        result["mode"] = "eip1559"
    except Exception:
        # Fallback to legacy gas price
        try:
            gas_price = query_gas_price(rpc_url)
            result["gasPriceLegacy"] = gas_price
            result["totalEstimate"] = gas_price
            result["mode"] = "legacy"
        except Exception as exc:
            result["error"] = str(exc)
            result["totalEstimate"] = 0

    return result


def check_weth_unwrap_needed(
    chain_name: str,
    token_out_info: dict[str, Any],
    original_token_out: str,
) -> bool:
    if chain_name not in L2_UNWRAP_CHAINS:
        return False
    chain = normalize_chain(chain_name)
    wrapped = chain.tokens.get(chain.wrapped_native_symbol.upper())
    if not wrapped:
        return False
    if token_out_info.get("address", "").lower() != wrapped.address.lower():
        return False
    upper = original_token_out.strip().upper()
    return upper in {"NATIVE", chain.native_symbol.upper()}


def build_weth_unwrap_tx(
    chain_name: str,
    amount_wei: str,
    wallet: str,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    wrapped = chain.tokens.get(chain.wrapped_native_symbol.upper())
    if not wrapped:
        raise ValueError(f"wrapped native not configured for {chain_name}")

    calldata = build_calldata("withdraw(uint256)", encode_uint(int(amount_wei)))

    return {
        "action": "weth_unwrap",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "wethAddress": wrapped.address,
        "amountWei": amount_wei,
        "transaction": {
            "kind": "weth_unwrap",
            "to": wrapped.address,
            "data": calldata,
            "value": "0",
            "chainId": chain.chain_id,
            "from": wallet,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Gas price estimation and WETH unwrap utilities")
    sub = parser.add_subparsers(dest="command")

    g = sub.add_parser("estimate", help="Estimate gas price for a chain")
    g.add_argument("--chain", required=True)
    g.add_argument("--speed", choices=["slow", "standard", "fast"], default="standard")

    w = sub.add_parser("unwrap", help="Build WETH.unwrap transaction")
    w.add_argument("--chain", required=True)
    w.add_argument("--amount-wei", required=True, help="Amount in wei to unwrap")
    w.add_argument("--wallet", required=True)

    args = parser.parse_args()
    load_local_env()

    if args.command == "estimate":
        result = estimate_gas_price(args.chain, args.speed)
        total_gwei = result["totalEstimate"] / 1e9
        print(f"Gas estimate ({args.speed}): {total_gwei:.2f} gwei ({result.get('mode', 'unknown')})")
        dump_json(result)
    elif args.command == "unwrap":
        result = build_weth_unwrap_tx(args.chain, args.amount_wei, args.wallet)
        dump_json(result)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
