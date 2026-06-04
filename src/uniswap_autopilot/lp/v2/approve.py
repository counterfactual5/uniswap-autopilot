#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


from uniswap_autopilot.common.common import (
    dump_json,
    get_v2_router02_address,
    load_local_env,
    normalize_chain,
    resolve_token,
    sort_token_addresses,
)
from uniswap_autopilot.execute._internal.rpc import (
    build_calldata, encode_address, encode_uint, query_erc20_allowance,
)


def check_v2_approvals(
    token0_address: str,
    token1_address: str,
    owner: str,
    chain_name: str,
    rpc_url: str | None = None,
) -> dict[str, Any]:
    _chain = normalize_chain(chain_name)
    router02 = get_v2_router02_address(chain_name)

    result = {"action": "v2_approval_check", "owner": owner, "spender": router02}

    for label, token_addr in [("token0", token0_address), ("token1", token1_address)]:
        try:
            allowance = query_erc20_allowance(token_addr, owner, router02, rpc_url)
        except Exception as exc:
            allowance = 0
            result[label] = {"queryError": str(exc)}
        result[label] = {
            "address": token_addr,
            "allowance": str(allowance),
            "needsApproval": allowance == 0,
        }

    return result


def build_v2_approval_tx(
    token_address: str,
    owner: str,
    chain_name: str,
    amount: str | None = None,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    router02 = get_v2_router02_address(chain_name)
    approve_amount = amount or "115792089237316195423570985008687907853269984665640564039457584007913129639935"

    calldata = build_calldata(
        "approve(address,uint256)",
        encode_address(router02),
        encode_uint(int(approve_amount)),
    )

    return {
        "kind": "approval",
        "to": token_address,
        "data": calldata,
        "value": "0",
        "chainId": chain.chain_id,
        "from": owner,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="检查 V2 LP approval 状态")
    parser.add_argument("--chain", required=True, help="链名")
    parser.add_argument("--token-a", required=True, help="代币 A")
    parser.add_argument("--token-b", required=True, help="代币 B")
    parser.add_argument("--owner", required=True, help="钱包地址")
    parser.add_argument("--rpc-url", help="RPC URL")
    parser.add_argument("--output", help="输出 JSON 文件路径")
    args = parser.parse_args()

    load_local_env()
    chain = normalize_chain(args.chain)

    tok_a = resolve_token(chain, args.token_a)
    tok_b = resolve_token(chain, args.token_b)
    token0, token1 = sort_token_addresses(tok_a["address"], tok_b["address"])

    result = check_v2_approvals(token0, token1, args.owner, args.chain, args.rpc_url)
    dump_json(result)
    if args.output:
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
