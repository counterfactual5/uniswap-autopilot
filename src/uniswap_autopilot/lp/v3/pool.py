#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


from uniswap_autopilot.common.common import (
    dump_json,
    get_v3_factory_address,
    load_local_env,
    normalize_chain,
    resolve_token,
    sort_token_addresses,
    validate_fee_tier,
)
from uniswap_autopilot.execute._internal.rpc import (
    decode_address, decode_int256, decode_uint, encode_address, encode_selector,
    encode_uint, eth_call, resolve_rpc_url,
)
from uniswap_autopilot.lp.v3.tick import fee_tier_to_tick_spacing, tick_to_price


def query_pool_address(token0: str, token1: str, fee: int, factory: str, rpc_url: str) -> str:
    sel = encode_selector("getPool(address,address,uint24)")
    data = sel + encode_address(token0).replace("0x", "") + encode_address(token1).replace("0x", "") + encode_uint(fee).replace("0x", "")
    raw = eth_call(factory, data, rpc_url)
    addr = decode_address(raw)
    if addr == "0x" + "0" * 40:
        return "0x0000000000000000000000000000000000000000"
    return addr


def query_slot0(pool_address: str, rpc_url: str) -> dict[str, Any]:
    sel = encode_selector("slot0()")
    raw = eth_call(pool_address, sel, rpc_url)
    clean = raw.replace("0x", "")
    # slot0 returns 7 values: uint160, int24, uint16, uint16, uint16, uint8, bool
    if len(clean) < 7 * 64:
        raise RuntimeError(f"slot0 returned unexpected output: {raw}")
    sqrt_price_x96 = decode_uint("0x" + clean[0:64])
    tick = decode_int256("0x" + clean[64:128])
    observation_index = decode_uint("0x" + clean[128:192])
    observation_cardinality = decode_uint("0x" + clean[192:256])
    observation_cardinality_next = decode_uint("0x" + clean[256:320])
    fee_protocol = decode_uint("0x" + clean[320:384])
    unlocked = decode_uint("0x" + clean[384:448]) != 0
    return {
        "sqrtPriceX96": sqrt_price_x96,
        "tick": tick,
        "observationIndex": observation_index,
        "observationCardinality": observation_cardinality,
        "observationCardinalityNext": observation_cardinality_next,
        "feeProtocol": fee_protocol,
        "unlocked": unlocked,
    }


def query_pool_liquidity(pool_address: str, rpc_url: str) -> int:
    sel = encode_selector("liquidity()")
    raw = eth_call(pool_address, sel, rpc_url)
    return decode_uint(raw)


def query_pool_tokens(pool_address: str, rpc_url: str) -> tuple[str, str]:
    sel0 = encode_selector("token0()")
    raw0 = eth_call(pool_address, sel0, rpc_url)
    t0 = decode_address(raw0)
    sel1 = encode_selector("token1()")
    raw1 = eth_call(pool_address, sel1, rpc_url)
    t1 = decode_address(raw1)
    return t0, t1


def query_pool_fee(pool_address: str, rpc_url: str) -> int:
    sel = encode_selector("fee()")
    raw = eth_call(pool_address, sel, rpc_url)
    return decode_uint(raw)


def query_pool_full_info(
    chain_name: str,
    token_a: str,
    token_b: str,
    fee_tier: int,
    rpc_url: str | None = None,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    rpc, _ = resolve_rpc_url(rpc_url, chain.chain_id)
    token_a_info = resolve_token(chain, token_a, rpc)
    token_b_info = resolve_token(chain, token_b, rpc)
    fee = validate_fee_tier(fee_tier)

    def _resolve_addr(info: dict[str, Any]) -> str:
        if info["address"] != "NATIVE":
            return info["address"]
        wrapped = chain.tokens.get(chain.wrapped_native_symbol.upper())
        if not wrapped:
            raise ValueError(f"wrapped native token not configured for chain '{chain.key}'")
        return wrapped.address

    addr_a = _resolve_addr(token_a_info)
    addr_b = _resolve_addr(token_b_info)
    token0_addr, token1_addr = sort_token_addresses(addr_a, addr_b)

    factory = get_v3_factory_address(chain_name)
    if not rpc:
        raise RuntimeError(f"RPC URL not configured for {chain_name}; set {chain_name.upper()}_RPC_URL")

    pool_address = query_pool_address(token0_addr, token1_addr, fee, factory, rpc)
    if pool_address == "0x0000000000000000000000000000000000000000":
        return {
            "action": "pool_info",
            "chain": {"key": chain.key, "chainId": chain.chain_id},
            "tokenA": token_a_info,
            "tokenB": token_b_info,
            "token0": token0_addr,
            "token1": token1_addr,
            "feeTier": fee,
            "poolAddress": None,
            "exists": False,
        }

    slot0 = query_slot0(pool_address, rpc)
    liquidity = query_pool_liquidity(pool_address, rpc)
    tick_spacing = fee_tier_to_tick_spacing(fee)

    decimals0 = token_a_info["decimals"] if token0_addr == addr_a else token_b_info["decimals"]
    decimals1 = token_b_info["decimals"] if token1_addr == addr_b else token_a_info["decimals"]
    current_price = tick_to_price(slot0["tick"], decimals0, decimals1)

    return {
        "action": "pool_info",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "tokenA": token_a_info,
        "tokenB": token_b_info,
        "token0": token0_addr,
        "token1": token1_addr,
        "feeTier": fee,
        "tickSpacing": tick_spacing,
        "poolAddress": pool_address,
        "exists": True,
        "slot0": slot0,
        "currentTick": slot0["tick"],
        "currentPrice": current_price,
        "liquidity": str(liquidity),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="查询 Uniswap v3 池子状态")
    parser.add_argument("--chain", required=True, help="链名，例如 base / ethereum")
    parser.add_argument("--token-a", required=True, help="代币 symbol 或地址")
    parser.add_argument("--token-b", required=True, help="代币 symbol 或地址")
    parser.add_argument("--fee-tier", type=int, required=True, help="手续费等级: 100 / 500 / 3000 / 10000")
    parser.add_argument("--rpc-url", help="RPC URL，不提供则从环境变量读取")
    parser.add_argument("--output", help="输出 JSON 文件路径")
    args = parser.parse_args()

    try:
        load_local_env()
        result = query_pool_full_info(args.chain, args.token_a, args.token_b, args.fee_tier, args.rpc_url)
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dump_json(result)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
