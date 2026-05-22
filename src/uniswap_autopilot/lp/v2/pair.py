#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


from uniswap_autopilot.common.common import (
    dump_json,
    get_v2_factory_address,
    load_local_env,
    normalize_chain,
    resolve_token,
    sort_token_addresses,
)
from uniswap_autopilot.execute._internal.rpc import (
    decode_address, decode_uint, encode_address, encode_selector, encode_uint,
    eth_call, resolve_rpc_url,
)

ZERO_ADDR = "0x0000000000000000000000000000000000000000"


def query_pair_address(token0: str, token1: str, factory: str, rpc_url: str) -> str:
    sel = encode_selector("getPair(address,address)")
    data = sel + encode_address(token0).replace("0x", "") + encode_address(token1).replace("0x", "")
    raw = eth_call(factory, data, rpc_url)
    addr = decode_address(raw)
    if not addr or addr == ZERO_ADDR:
        return ""
    return addr



def query_reserves(pair_address: str, rpc_url: str) -> dict[str, Any]:
    sel = encode_selector("getReserves()")
    raw = eth_call(pair_address, sel, rpc_url)
    clean = raw.replace("0x", "")
    if len(clean) < 128:
        return {"reserve0": 0, "reserve1": 0}
    reserve0 = decode_uint("0x" + clean[0:64])
    reserve1 = decode_uint("0x" + clean[64:128])
    return {"reserve0": reserve0, "reserve1": reserve1}


def query_total_supply(pair_address: str, rpc_url: str) -> int:
    sel = encode_selector("totalSupply()")
    raw = eth_call(pair_address, sel, rpc_url)
    return decode_uint(raw)


def query_lp_balance(pair_address: str, owner: str, rpc_url: str) -> int:
    sel = encode_selector("balanceOf(address)")
    data = sel + encode_address(owner).replace("0x", "")
    raw = eth_call(pair_address, data, rpc_url)
    return decode_uint(raw)


def query_pair_tokens(pair_address: str, rpc_url: str) -> tuple[str, str]:
    sel0 = encode_selector("token0()")
    raw0 = eth_call(pair_address, sel0, rpc_url)
    token0 = decode_address(raw0)
    sel1 = encode_selector("token1()")
    raw1 = eth_call(pair_address, sel1, rpc_url)
    token1 = decode_address(raw1)
    return token0, token1


def query_pair_full_info(
    chain_name: str,
    token_a: str,
    token_b: str,
    rpc_url: str | None = None,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    rpc = rpc_url or resolve_rpc_url(None, chain.chain_id)[0]
    if not rpc:
        raise RuntimeError(f"RPC URL not configured for {chain_name}")

    factory = get_v2_factory_address(chain_name)

    tok_a = resolve_token(chain, token_a, rpc)
    tok_b = resolve_token(chain, token_b, rpc)

    token0, token1 = sort_token_addresses(tok_a["address"], tok_b["address"])

    pair_address = query_pair_address(token0, token1, factory, rpc)
    if not pair_address:
        return {
            "action": "v2_pair_info",
            "chain": {"key": chain.key, "chainId": chain.chain_id},
            "tokenA": tok_a,
            "tokenB": tok_b,
            "token0": token0,
            "token1": token1,
            "pairAddress": None,
            "exists": False,
        }

    reserves = query_reserves(pair_address, rpc)
    total_supply = query_total_supply(pair_address, rpc)

    return {
        "action": "v2_pair_info",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "tokenA": tok_a,
        "tokenB": tok_b,
        "token0": token0,
        "token1": token1,
        "pairAddress": pair_address,
        "exists": True,
        "reserves": reserves,
        "totalSupply": str(total_supply),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="查询 Uniswap V2 Pair 信息")
    parser.add_argument("--chain", required=True, help="链名")
    parser.add_argument("--token-a", required=True, help="代币 A symbol 或地址")
    parser.add_argument("--token-b", required=True, help="代币 B symbol 或地址")
    parser.add_argument("--rpc-url", help="RPC URL")
    parser.add_argument("--output", help="输出 JSON 文件路径")
    args = parser.parse_args()

    load_local_env()
    result = query_pair_full_info(args.chain, args.token_a, args.token_b, args.rpc_url)
    dump_json(result)
    if args.output:
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
