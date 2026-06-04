#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


from uniswap_autopilot.common.common import (
    dump_json,
    get_v4_pool_manager_address,
    get_v4_state_view_address,
    load_local_env,
    normalize_chain,
    resolve_token,
    sort_token_addresses,
)
from uniswap_autopilot.execute._internal.rpc import (
    decode_int256, decode_uint, encode_address, encode_bytes32, encode_selector,
    encode_uint, eth_call, resolve_rpc_url,
)
from uniswap_autopilot.lp.v3.tick import tick_to_price

import hashlib

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def _encode_parameters(tick_spacing: int, hooks_registration: int = 0) -> int:
    return (tick_spacing << 24) | hooks_registration


def compute_pool_id(
    currency0: str,
    currency1: str,
    hooks: str,
    pool_manager: str,
    fee: int,
    tick_spacing: int,
) -> str:
    c0 = currency0.lower()
    c1 = currency1.lower()
    if c0 > c1:
        c0, c1 = c1, c0
    params = _encode_parameters(tick_spacing)
    # abi.encode(address,address,address,address,uint24,uint256)
    encoded = (
        encode_address(c0)
        + encode_address(c1)
        + encode_address(hooks)
        + encode_address(pool_manager)
        + encode_uint(fee)
        + encode_uint(params)
    )
    raw_bytes = bytes.fromhex(encoded)
    return "0x" + hashlib.sha3_256(raw_bytes).hexdigest()


def query_v4_slot0(
    state_view: str,
    pool_id: str,
    rpc_url: str,
) -> dict[str, Any]:
    sel = encode_selector("getSlot0(bytes32)")
    data = sel + encode_bytes32(pool_id).replace("0x", "")
    raw = eth_call(state_view, data, rpc_url)
    clean = raw.replace("0x", "")
    # returns (uint160, int24, uint24, uint24)
    sqrt_price_x96 = decode_uint("0x" + clean[0:64])
    tick = decode_int256("0x" + clean[64:128])
    protocol_fee = decode_uint("0x" + clean[128:192])
    lp_fee = decode_uint("0x" + clean[192:256])
    return {
        "sqrtPriceX96": str(sqrt_price_x96),
        "tick": tick,
        "protocolFee": protocol_fee,
        "lpFee": lp_fee,
    }


def query_v4_liquidity(
    state_view: str,
    pool_id: str,
    rpc_url: str,
) -> int:
    sel = encode_selector("getLiquidity(bytes32)")
    data = sel + encode_bytes32(pool_id).replace("0x", "")
    raw = eth_call(state_view, data, rpc_url)
    return decode_uint(raw)


def query_v4_pool_full_info(
    chain_name: str,
    token_a: str,
    token_b: str,
    fee: int,
    tick_spacing: int,
    hooks: str = ZERO_ADDRESS,
    rpc_url: str | None = None,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    rpc, _ = resolve_rpc_url(rpc_url, chain.chain_id)
    if not rpc:
        raise RuntimeError(f"RPC URL not configured for {chain_name}")

    pool_manager = get_v4_pool_manager_address(chain_name)
    state_view = get_v4_state_view_address(chain_name)

    tok_a = resolve_token(chain, token_a, rpc)
    tok_b = resolve_token(chain, token_b, rpc)
    c0, c1 = sort_token_addresses(tok_a["address"], tok_b["address"])
    decimals0 = tok_a["decimals"] if c0.lower() == tok_a["address"].lower() else tok_b["decimals"]
    decimals1 = tok_b["decimals"] if c1.lower() == tok_b["address"].lower() else tok_a["decimals"]

    pool_id = compute_pool_id(c0, c1, hooks, pool_manager, fee, tick_spacing)

    slot0 = query_v4_slot0(state_view, pool_id, rpc)
    liquidity = query_v4_liquidity(state_view, pool_id, rpc)

    current_tick = slot0["tick"]
    current_price = tick_to_price(current_tick, decimals0, decimals1)

    return {
        "action": "v4_pool_info",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "currency0": c0,
        "currency1": c1,
        "fee": fee,
        "tickSpacing": tick_spacing,
        "hooks": hooks,
        "poolId": pool_id,
        "poolManager": pool_manager,
        "slot0": slot0,
        "currentTick": current_tick,
        "currentPrice": current_price,
        "liquidity": str(liquidity),
        "exists": slot0["sqrtPriceX96"] != "0",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Uniswap V4 pool state queries via StateView")
    parser.add_argument("--chain", required=True)
    parser.add_argument("--token-a", required=True)
    parser.add_argument("--token-b", required=True)
    parser.add_argument("--fee", type=int, required=True, help="Pool fee (e.g. 3000)")
    parser.add_argument("--tick-spacing", type=int, required=True, help="Tick spacing (e.g. 60)")
    parser.add_argument("--hooks", default=ZERO_ADDRESS, help="Hooks contract address (default: zero address)")
    parser.add_argument("--rpc-url")
    parser.add_argument("--output")
    args = parser.parse_args()

    load_local_env()

    result = query_v4_pool_full_info(
        args.chain, args.token_a, args.token_b,
        args.fee, args.tick_spacing, args.hooks, args.rpc_url,
    )
    if result["exists"]:
        print(f"V4 Pool: {args.token_a}/{args.token_b} fee={args.fee} tickSpacing={args.tick_spacing}")
        print(f"  PoolId: {result['poolId']}")
        print(f"  Tick: {result['currentTick']}  Price: {result['currentPrice']:.6f}")
        print(f"  Liquidity: {result['liquidity']}")
    else:
        print(f"V4 Pool not found: {args.token_a}/{args.token_b} fee={args.fee} tickSpacing={args.tick_spacing}")

    if args.output:
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    dump_json(result)


if __name__ == "__main__":
    main()
