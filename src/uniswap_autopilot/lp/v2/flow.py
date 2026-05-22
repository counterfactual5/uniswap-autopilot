#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


from uniswap_autopilot.common.common import (
    dump_json,
    get_v2_router02_address,
    load_local_env,
    normalize_chain,
    resolve_token,
    resolve_wallet_address,
    sort_token_addresses,
)
from uniswap_autopilot.execute._internal.rpc import resolve_rpc_url
from uniswap_autopilot.lp.v2.approve import build_v2_approval_tx, check_v2_approvals
from uniswap_autopilot.lp.v2.build_tx import build_add_liquidity_tx, build_remove_liquidity_tx
from uniswap_autopilot.lp.v2.pair import query_lp_balance, query_pair_full_info


def run_v2_add_liquidity_flow(
    chain_name: str,
    token_a: str,
    token_b: str,
    amount_a: str,
    amount_b: str,
    slippage: float = 0.5,
    wallet: str | None = None,
    rpc_url: str | None = None,
    output_dir: str | None = None,
    broadcast_args: argparse.Namespace | None = None,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    wallet_addr = resolve_wallet_address(wallet)
    if not wallet_addr:
        raise ValueError("wallet address required (--wallet or configure wallet env)")

    rpc = rpc_url or resolve_rpc_url(None, chain.chain_id)[0]
    if not rpc:
        raise RuntimeError(f"RPC URL not configured for {chain_name}; set an RPC env var or pass --rpc-url")

    # 1. Query pair info
    pair_info = query_pair_full_info(chain_name, token_a, token_b, rpc)

    # 2. Check approvals
    if pair_info.get("exists"):
        token0 = pair_info["token0"]
        token1 = pair_info["token1"]
    else:
        tok_a = resolve_token(chain, token_a, rpc)
        tok_b = resolve_token(chain, token_b, rpc)
        token0, token1 = sort_token_addresses(tok_a["address"], tok_b["address"])

    approval_status = check_v2_approvals(token0, token1, wallet_addr, chain_name, rpc)

    # 3. Build add liquidity tx
    add_result = build_add_liquidity_tx(
        chain_name, token_a, token_b, amount_a, amount_b,
        slippage=slippage, recipient=wallet_addr, rpc_url=rpc,
    )

    # 4. Build approval txs if needed
    approval_txs = []
    for label in ["token0", "token1"]:
        tok_info = approval_status.get(label, {})
        if tok_info.get("needsApproval"):
            approval_txs.append(build_v2_approval_tx(tok_info["address"], wallet_addr, chain_name))

    output = {
        "action": "v2_add_liquidity_flow",
        "pair": pair_info,
        "approval": approval_status,
        "addLiquidity": add_result,
        "approvalTxs": approval_txs,
        "nextActions": ([] if not approval_txs else ["broadcast-approvals"]) + ["broadcast-add-liquidity"],
    }

    dump_json(output)
    if output_dir:
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        out_path.joinpath("v2_add_liquidity_flow.json").write_text(
            json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    return output


def run_v2_remove_liquidity_flow(
    chain_name: str,
    token_a: str,
    token_b: str,
    liquidity_pct: float = 100.0,
    slippage: float = 0.5,
    wallet: str | None = None,
    rpc_url: str | None = None,
    output_dir: str | None = None,
    broadcast_args: argparse.Namespace | None = None,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    wallet_addr = resolve_wallet_address(wallet)
    if not wallet_addr:
        raise ValueError("wallet address required (--wallet or configure wallet env)")

    rpc = rpc_url or resolve_rpc_url(None, chain.chain_id)[0]
    if not rpc:
        raise RuntimeError(f"RPC URL not configured for {chain_name}; set an RPC env var or pass --rpc-url")

    # 1. Query pair info
    pair_info = query_pair_full_info(chain_name, token_a, token_b, rpc)
    if not pair_info.get("exists"):
        raise ValueError(f"V2 pair {token_a}/{token_b} does not exist on {chain_name}")

    pair_address = pair_info["pairAddress"]

    # 2. Query LP balance
    lp_balance = query_lp_balance(pair_address, wallet_addr, rpc)
    if lp_balance == 0:
        raise ValueError(f"No LP tokens for {token_a}/{token_b} in wallet {wallet_addr}")

    # Calculate liquidity to remove based on percentage
    from decimal import Decimal
    liquidity_to_remove = str(int(Decimal(lp_balance) * Decimal(str(liquidity_pct)) / Decimal("100")))
    if int(liquidity_to_remove) == 0:
        raise ValueError("Calculated liquidity to remove is 0")

    # 3. Build remove liquidity tx
    remove_result = build_remove_liquidity_tx(
        chain_name, token_a, token_b, liquidity_to_remove,
        slippage=slippage, recipient=wallet_addr, rpc_url=rpc,
    )

    output = {
        "action": "v2_remove_liquidity_flow",
        "pair": pair_info,
        "lpBalance": str(lp_balance),
        "liquidityToRemove": liquidity_to_remove,
        "removePct": liquidity_pct,
        "removeLiquidity": remove_result,
        "nextActions": ["broadcast-remove-liquidity"],
    }

    dump_json(output)
    if output_dir:
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        out_path.joinpath("v2_remove_liquidity_flow.json").write_text(
            json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Uniswap V2 LP 全流程编排")
    sub = parser.add_subparsers(dest="command")

    add = sub.add_parser("add", help="添加 V2 流动性")
    add.add_argument("--chain", required=True, help="链名")
    add.add_argument("--token-a", required=True, help="代币 A")
    add.add_argument("--token-b", required=True, help="代币 B")
    add.add_argument("--amount-a", required=True, help="代币 A 数量")
    add.add_argument("--amount-b", required=True, help="代币 B 数量")
    add.add_argument("--slippage", type=float, default=0.5, help="滑点百分比")
    add.add_argument("--wallet", help="钱包地址")
    add.add_argument("--rpc-url", help="RPC URL")
    add.add_argument("--output-dir", help="输出目录")

    remove = sub.add_parser("remove", help="移除 V2 流动性")
    remove.add_argument("--chain", required=True, help="链名")
    remove.add_argument("--token-a", required=True, help="代币 A")
    remove.add_argument("--token-b", required=True, help="代币 B")
    remove.add_argument("--liquidity-pct", type=float, default=100.0, help="移除百分比，默认 100%%")
    remove.add_argument("--slippage", type=float, default=0.5, help="滑点百分比")
    remove.add_argument("--wallet", help="钱包地址")
    remove.add_argument("--rpc-url", help="RPC URL")
    remove.add_argument("--output-dir", help="输出目录")

    args = parser.parse_args()
    load_local_env()

    if args.command == "add":
        run_v2_add_liquidity_flow(
            args.chain, args.token_a, args.token_b,
            args.amount_a, args.amount_b,
            args.slippage, args.wallet, args.rpc_url, args.output_dir,
        )
    elif args.command == "remove":
        run_v2_remove_liquidity_flow(
            args.chain, args.token_a, args.token_b,
            args.liquidity_pct, args.slippage,
            args.wallet, args.rpc_url, args.output_dir,
        )
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
