#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


from uniswap_autopilot.common.common import (
    dump_json,
    get_position_manager_address,
    load_local_env,
    normalize_chain,
)
from uniswap_autopilot.execute._internal.rpc import (
    decode_address, decode_int256, decode_uint, encode_address, encode_selector,
    encode_uint, eth_call, resolve_rpc_url,
)


def query_position(token_id: int, chain_name: str, rpc_url: str | None = None) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    pm = get_position_manager_address(chain_name)
    rpc, _ = resolve_rpc_url(rpc_url, chain.chain_id)
    if not rpc:
        raise RuntimeError(f"RPC URL not configured for {chain_name}")

    sel = encode_selector("positions(uint256)")
    data = sel + encode_uint(token_id).replace("0x", "")
    raw = eth_call(pm, data, rpc)
    clean = raw.replace("0x", "")
    # positions returns 12 uint256/int256/address values
    if len(clean) < 12 * 64:
        raise RuntimeError(f"positions() returned unexpected output: {raw}")

    def _u(offset: int) -> int:
        return decode_uint("0x" + clean[offset:offset + 64])

    def _i(offset: int) -> int:
        return decode_int256("0x" + clean[offset:offset + 64])

    def _a(offset: int) -> str:
        return decode_address("0x" + clean[offset:offset + 64])

    return {
        "tokenId": token_id,
        "nonce": _u(0),
        "operator": _a(64),
        "token0": _a(128),
        "token1": _a(192),
        "fee": _u(256),
        "tickLower": _i(320),
        "tickUpper": _i(384),
        "liquidity": str(_u(448)),
        "feeGrowthInside0LastX128": str(_u(512)),
        "feeGrowthInside1LastX128": str(_u(576)),
        "tokensOwed0": str(_u(640)),
        "tokensOwed1": str(_u(704)),
    }


def query_positions_by_owner(owner: str, chain_name: str, rpc_url: str | None = None) -> list[int]:
    chain = normalize_chain(chain_name)
    pm = get_position_manager_address(chain_name)
    rpc, _ = resolve_rpc_url(rpc_url, chain.chain_id)
    if not rpc:
        raise RuntimeError(f"RPC URL not configured for {chain_name}")

    bal_sel = encode_selector("balanceOf(address)")
    bal_data = bal_sel + encode_address(owner).replace("0x", "")
    raw_bal = eth_call(pm, bal_data, rpc)
    balance = decode_uint(raw_bal)

    token_ids: list[int] = []
    tobi_sel = encode_selector("tokenOfOwnerByIndex(address,uint256)")
    for i in range(balance):
        data = tobi_sel + encode_address(owner).replace("0x", "") + encode_uint(i).replace("0x", "")
        raw_tid = eth_call(pm, data, rpc)
        token_ids.append(decode_uint(raw_tid))
    return token_ids


def main() -> None:
    parser = argparse.ArgumentParser(description="查询 Uniswap v3 LP 仓位信息")
    parser.add_argument("--chain", required=True, help="链名")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--token-id", type=int, help="LP NFT token ID")
    source.add_argument("--owner", help="钱包地址，列出所有仓位")
    parser.add_argument("--rpc-url", help="RPC URL")
    parser.add_argument("--output", help="输出 JSON 文件路径")
    args = parser.parse_args()

    try:
        load_local_env()
        if args.token_id is not None:
            result = {"action": "position_info", "position": query_position(args.token_id, args.chain, args.rpc_url)}
        else:
            token_ids = query_positions_by_owner(args.owner, args.chain, args.rpc_url)
            positions = [query_position(tid, args.chain, args.rpc_url) for tid in token_ids]
            result = {"action": "positions_by_owner", "owner": args.owner, "count": len(positions), "positions": positions}
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dump_json(result)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
