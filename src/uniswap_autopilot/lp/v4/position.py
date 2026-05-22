#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


from uniswap_autopilot.common.common import (
    dump_json,
    get_v4_position_manager_address,
    load_local_env,
    normalize_chain,
)
from uniswap_autopilot.execute._internal.rpc import (
    decode_address, decode_int256, decode_uint, encode_address, encode_selector,
    encode_uint, eth_call, resolve_rpc_url,
)
from uniswap_autopilot.lp.v3.tick import tick_to_price


def _parse_position_info(packed: int) -> dict[str, Any]:
    # V4 PositionInfo packing: bits 0-23 = tickUpper, bits 24-47 = tickLower, bit 56 = hasSubscriber
    tick_upper = packed & 0xFFFFFF
    if tick_upper >= 0x800000:
        tick_upper -= 0x1000000
    tick_lower = (packed >> 24) & 0xFFFFFF
    if tick_lower >= 0x800000:
        tick_lower -= 0x1000000
    has_subscriber = bool((packed >> 56) & 0x1)
    return {"tickLower": tick_lower, "tickUpper": tick_upper, "hasSubscriber": has_subscriber}


def query_v4_position(
    token_id: int,
    chain_name: str,
    rpc_url: str | None = None,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    rpc, _ = resolve_rpc_url(rpc_url, chain.chain_id)
    if not rpc:
        raise RuntimeError(f"RPC URL not configured for {chain_name}")

    pm = get_v4_position_manager_address(chain_name)

    # getPositionLiquidity(uint256)
    liq_sel = encode_selector("getPositionLiquidity(uint256)")
    liq_data = liq_sel + encode_uint(token_id).replace("0x", "")
    raw_liq = eth_call(pm, liq_data, rpc)
    liquidity = decode_uint(raw_liq)

    # getPoolAndPositionInfo(uint256) returns (address,address,address,address,uint24,uint256,uint256)
    info_sel = encode_selector("getPoolAndPositionInfo(uint256)")
    info_data = info_sel + encode_uint(token_id).replace("0x", "")
    raw_info = eth_call(pm, info_data, rpc)
    clean = raw_info.replace("0x", "")

    currency0 = decode_address("0x" + clean[0:64])
    currency1 = decode_address("0x" + clean[64:128])
    hooks = decode_address("0x" + clean[128:192])
    pool_manager = decode_address("0x" + clean[192:256])
    fee = decode_uint("0x" + clean[256:320])
    parameters = decode_uint("0x" + clean[320:384])
    position_info_packed = decode_uint("0x" + clean[384:448])
    tick_spacing = parameters >> 24

    pos_info = _parse_position_info(position_info_packed)

    from uniswap_autopilot.common.common import resolve_token
    try:
        tok0 = resolve_token(chain, currency0, rpc)
        decimals0 = tok0["decimals"]
        symbol0 = tok0.get("symbol", currency0)
    except Exception:
        decimals0 = 18
        symbol0 = currency0[:10]
    try:
        tok1 = resolve_token(chain, currency1, rpc)
        decimals1 = tok1["decimals"]
        symbol1 = tok1.get("symbol", currency1)
    except Exception:
        decimals1 = 18
        symbol1 = currency1[:10]

    current_tick = None
    try:
        from uniswap_autopilot.lp.v4.pool import compute_pool_id, query_v4_slot0
        from uniswap_autopilot.common.common import get_v4_state_view_address
        state_view = get_v4_state_view_address(chain_name)
        pool_id = compute_pool_id(currency0, currency1, hooks, pool_manager, fee, tick_spacing)
        slot0 = query_v4_slot0(state_view, pool_id, rpc)
        current_tick = slot0["tick"]
    except Exception:
        pass

    current_price = tick_to_price(current_tick, decimals0, decimals1) if current_tick is not None else None
    in_range = pos_info["tickLower"] < current_tick < pos_info["tickUpper"] if current_tick is not None else None

    return {
        "action": "v4_position",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "tokenId": token_id,
        "currency0": {"address": currency0, "symbol": symbol0, "decimals": decimals0},
        "currency1": {"address": currency1, "symbol": symbol1, "decimals": decimals1},
        "fee": fee,
        "tickSpacing": tick_spacing,
        "hooks": hooks,
        "tickLower": pos_info["tickLower"],
        "tickUpper": pos_info["tickUpper"],
        "liquidity": str(liquidity),
        "currentTick": current_tick,
        "currentPrice": current_price,
        "inRange": in_range,
    }


def query_v4_positions_by_owner(
    owner: str,
    chain_name: str,
    rpc_url: str | None = None,
) -> list[int]:
    chain = normalize_chain(chain_name)
    rpc, _ = resolve_rpc_url(rpc_url, chain.chain_id)
    if not rpc:
        raise RuntimeError(f"RPC URL not configured for {chain_name}")

    pm = get_v4_position_manager_address(chain_name)

    bal_sel = encode_selector("balanceOf(address)")
    bal_data = bal_sel + encode_address(owner).replace("0x", "")
    raw_bal = eth_call(pm, bal_data, rpc)
    count = decode_uint(raw_bal)

    token_ids: list[int] = []
    tobi_sel = encode_selector("tokenOfOwnerByIndex(address,uint256)")
    for i in range(count):
        data = tobi_sel + encode_address(owner).replace("0x", "") + encode_uint(i).replace("0x", "")
        raw_tid = eth_call(pm, data, rpc)
        token_ids.append(decode_uint(raw_tid))
    return token_ids


def main() -> None:
    parser = argparse.ArgumentParser(description="Uniswap V4 position queries")
    sub = parser.add_subparsers(dest="command")

    i = sub.add_parser("info", help="Query V4 position by token ID")
    i.add_argument("--chain", required=True)
    i.add_argument("--token-id", type=int, required=True)
    i.add_argument("--rpc-url")
    i.add_argument("--output")

    o = sub.add_parser("owner", help="List V4 position token IDs by owner")
    o.add_argument("--chain", required=True)
    o.add_argument("--owner", required=True)
    o.add_argument("--rpc-url")
    o.add_argument("--output")

    args = parser.parse_args()
    load_local_env()

    if args.command == "info":
        result = query_v4_position(args.token_id, args.chain, args.rpc_url)
        t0 = result["currency0"]["symbol"]
        t1 = result["currency1"]["symbol"]
        ir = "in-range" if result.get("inRange") else "out-of-range" if result.get("inRange") is False else "?"
        print(f"V4 Position #{args.token_id}: {t0}/{t1} fee={result['fee']} {ir}")
        print(f"  Range: [{result['tickLower']}, {result['tickUpper']}]  Liquidity: {result['liquidity']}")
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dump_json(result)
    elif args.command == "owner":
        chain = normalize_chain(args.chain)
        token_ids = query_v4_positions_by_owner(args.owner, args.chain, args.rpc_url)
        print(f"Found {len(token_ids)} V4 positions for {args.owner} on {chain.key}")
        for tid in token_ids:
            print(f"  #{tid}")
        dump_json({"action": "v4_positions_by_owner", "chain": chain.key, "owner": args.owner, "tokenIds": token_ids})
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
