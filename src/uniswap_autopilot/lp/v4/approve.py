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
    resolve_token,
    sort_token_addresses,
)
from uniswap_autopilot.execute._internal.rpc import (
    build_calldata, encode_address, encode_uint, query_erc20_allowance, resolve_rpc_url,
)


def check_v4_approvals(
    token0_address: str,
    token1_address: str,
    owner: str,
    chain_name: str,
    rpc_url: str | None = None,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    pm = get_v4_position_manager_address(chain_name)
    rpc, _ = resolve_rpc_url(rpc_url, chain.chain_id)
    if not rpc:
        raise RuntimeError(f"RPC URL not configured for {chain_name}")

    allowance0 = query_erc20_allowance(token0_address, owner, pm, rpc)
    allowance1 = query_erc20_allowance(token1_address, owner, pm, rpc)

    return {
        "action": "v4_approval_check",
        "owner": owner,
        "spender": pm,
        "token0": {"address": token0_address, "allowance": str(allowance0), "needsApproval": allowance0 == 0},
        "token1": {"address": token1_address, "allowance": str(allowance1), "needsApproval": allowance1 == 0},
    }


def build_approval_calldata(spender: str, amount: str | None = None) -> str:
    approve_amount = amount or str(2**256 - 1)
    return build_calldata(
        "approve(address,uint256)",
        encode_address(spender),
        encode_uint(int(approve_amount)),
    )


def build_approval_tx(
    token: str,
    owner: str,
    chain_name: str,
    amount: str | None = None,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    pm = get_v4_position_manager_address(chain_name)
    calldata = build_approval_calldata(pm, amount)
    return {
        "kind": "approval",
        "to": token,
        "data": calldata,
        "value": "0",
        "chainId": chain.chain_id,
        "from": owner,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Check ERC-20 approvals for V4 PositionManager")
    parser.add_argument("--chain", required=True)
    parser.add_argument("--token-a", required=True)
    parser.add_argument("--token-b", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--rpc-url")
    parser.add_argument("--output")
    args = parser.parse_args()

    load_local_env()
    chain = normalize_chain(args.chain)
    token_a = resolve_token(chain, args.token_a)
    token_b = resolve_token(chain, args.token_b)
    from uniswap_autopilot.common.common import sort_token_addresses
    t0, t1 = sort_token_addresses(token_a["address"], token_b["address"])

    result = check_v4_approvals(t0, t1, args.owner, args.chain, args.rpc_url)
    for key in ("token0", "token1"):
        if result[key]["needsApproval"]:
            result[key]["approvalTx"] = build_approval_tx(
                result[key]["address"], args.owner, args.chain,
            )

    if args.output:
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    dump_json(result)


if __name__ == "__main__":
    main()
