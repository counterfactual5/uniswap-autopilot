#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


from uniswap_autopilot.common.common import (
    dump_json,
    load_local_env,
    resolve_wallet_address,
)
from uniswap_autopilot.lp.v3.approve import build_approval_tx, check_lp_approvals
from uniswap_autopilot.lp.v3.build_tx import (
    build_collect_transaction,
    build_decrease_liquidity_transaction,
    build_increase_liquidity_transaction,
    build_mint_transaction,
)
from uniswap_autopilot.lp.v3.pool import query_pool_full_info


def _broadcast_tx(tx: dict[str, Any], broadcast_args: argparse.Namespace, confirm_phrase: str) -> dict[str, Any]:
    from uniswap_autopilot.execute._internal.submit import broadcast_with_backend
    return broadcast_with_backend(
        tx=tx,
        explicit_rpc_url=None,
        confirm=confirm_phrase,
        signer_args_source=broadcast_args,
    )


def run_lp_mint_flow(
    chain_name: str,
    token_a: str, token_b: str,
    fee_tier: int,
    tick_lower: int, tick_upper: int,
    amount_a: str, amount_b: str,
    slippage: float,
    deadline_seconds: int,
    wallet: str | None,
    output_dir: str,
    rpc_url: str | None,
    request_only: bool = False,
    broadcast: bool = False,
    broadcast_args: argparse.Namespace | None = None,
) -> dict[str, Any]:
    wallet_addr = resolve_wallet_address(wallet)
    if not wallet_addr:
        raise ValueError("wallet is required; pass --wallet or set wallet env")

    pool_info = query_pool_full_info(chain_name, token_a, token_b, fee_tier, rpc_url)
    if not pool_info.get("exists"):
        raise ValueError(f"pool does not exist for {token_a}/{token_b} fee={fee_tier} on {chain_name}")

    approval_status = check_lp_approvals(
        pool_info["token0"], pool_info["token1"],
        wallet_addr, chain_name, rpc_url,
    )

    mint_result = build_mint_transaction(
        chain_name=chain_name, token_a=token_a, token_b=token_b,
        fee_tier=fee_tier, tick_lower=tick_lower, tick_upper=tick_upper,
        amount_a=amount_a, amount_b=amount_b,
        slippage_pct=slippage, recipient=wallet_addr,
        deadline_seconds=deadline_seconds, rpc_url=rpc_url,
        request_only=request_only,
    )

    output: dict[str, Any] = {
        "action": "lp_mint_flow",
        "pool": pool_info,
        "approval": approval_status,
        "mint": mint_result,
    }

    approval_txs = []
    if approval_status["token0"]["needsApproval"]:
        approval_txs.append(build_approval_tx(pool_info["token0"], wallet_addr, chain_name))
    if approval_status["token1"]["needsApproval"]:
        approval_txs.append(build_approval_tx(pool_info["token1"], wallet_addr, chain_name))
    output["approvalTxs"] = approval_txs

    if request_only:
        output["nextActions"] = ["broadcast-approvals", "broadcast-mint"] if approval_txs else ["broadcast-mint"]
        return output

    chain = pool_info["chain"]
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    if broadcast and broadcast_args:
        for i, atx in enumerate(approval_txs):
            confirm = f"BROADCAST APPROVAL {chain['chainId']} {atx['to']}"
            result = _broadcast_tx(atx, broadcast_args, confirm)
            output[f"approval{i}_broadcast"] = result

        mint_tx = mint_result["transaction"]
        confirm = f"BROADCAST LP_MINT {chain['chainId']} {mint_tx['to']}"
        result = _broadcast_tx(mint_tx, broadcast_args, confirm)
        output["mintBroadcast"] = result
    else:
        output["nextActions"] = ["broadcast-approvals", "broadcast-mint"] if approval_txs else ["broadcast-mint"]

    (output_path / "lp_mint_flow.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return output


def run_lp_increase_flow(
    chain_name: str, token_id: int,
    amount0: str, amount1: str,
    slippage: float, deadline_seconds: int,
    wallet: str | None, output_dir: str,
    rpc_url: str | None,
    broadcast: bool = False,
    broadcast_args: argparse.Namespace | None = None,
) -> dict[str, Any]:
    wallet_addr = resolve_wallet_address(wallet)
    if not wallet_addr:
        raise ValueError("wallet is required")

    inc_result = build_increase_liquidity_transaction(
        chain_name=chain_name, token_id=token_id,
        amount0=amount0, amount1=amount1,
        slippage_pct=slippage, deadline_seconds=deadline_seconds,
        rpc_url=rpc_url,
    )

    pos = inc_result["position"]
    approval_status = check_lp_approvals(
        pos["token0"], pos["token1"],
        wallet_addr, chain_name, rpc_url,
    )

    output: dict[str, Any] = {"action": "lp_increase_flow", "increase": inc_result, "approval": approval_status}

    approval_txs = []
    if approval_status["token0"]["needsApproval"]:
        approval_txs.append(build_approval_tx(pos["token0"], wallet_addr, chain_name))
    if approval_status["token1"]["needsApproval"]:
        approval_txs.append(build_approval_tx(pos["token1"], wallet_addr, chain_name))

    if broadcast and broadcast_args:
        for i, atx in enumerate(approval_txs):
            _broadcast_tx(atx, broadcast_args, f"BROADCAST APPROVAL {inc_result['chain']['chainId']} {atx['to']}")
        result = _broadcast_tx(inc_result["transaction"], broadcast_args, f"BROADCAST LP_INCREASE {inc_result['chain']['chainId']} {inc_result['transaction']['to']}")
        output["broadcast"] = result
    else:
        output["nextActions"] = ["broadcast-approvals", "broadcast-increase"] if approval_txs else ["broadcast-increase"]

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    (output_path / "lp_increase_flow.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return output


def run_lp_decrease_flow(
    chain_name: str, token_id: int,
    liquidity_pct: float, slippage: float,
    deadline_seconds: int, wallet: str | None,
    output_dir: str, rpc_url: str | None,
    broadcast: bool = False,
    broadcast_args: argparse.Namespace | None = None,
) -> dict[str, Any]:
    wallet_addr = resolve_wallet_address(wallet)
    if not wallet_addr:
        raise ValueError("wallet is required")

    dec_result = build_decrease_liquidity_transaction(
        chain_name=chain_name, token_id=token_id,
        liquidity_pct=liquidity_pct, slippage_pct=slippage,
        deadline_seconds=deadline_seconds, rpc_url=rpc_url,
    )

    output: dict[str, Any] = {"action": "lp_decrease_flow", "decrease": dec_result}

    if broadcast and broadcast_args:
        result = _broadcast_tx(dec_result["transaction"], broadcast_args, f"BROADCAST LP_DECREASE {dec_result['chain']['chainId']} {dec_result['transaction']['to']}")
        output["broadcast"] = result
    else:
        output["nextActions"] = ["broadcast-decrease"]

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    (output_path / "lp_decrease_flow.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return output


def run_lp_collect_flow(
    chain_name: str, token_id: int,
    recipient: str | None, wallet: str | None,
    output_dir: str, rpc_url: str | None,
    broadcast: bool = False,
    broadcast_args: argparse.Namespace | None = None,
) -> dict[str, Any]:
    wallet_addr = resolve_wallet_address(wallet)
    if not wallet_addr:
        raise ValueError("wallet is required")

    col_result = build_collect_transaction(
        chain_name=chain_name, token_id=token_id, recipient=recipient or wallet_addr,
    )

    output: dict[str, Any] = {"action": "lp_collect_flow", "collect": col_result}

    if broadcast and broadcast_args:
        result = _broadcast_tx(col_result["transaction"], broadcast_args, f"BROADCAST LP_COLLECT {col_result['chain']['chainId']} {col_result['transaction']['to']}")
        output["broadcast"] = result
    else:
        output["nextActions"] = ["broadcast-collect"]

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    (output_path / "lp_collect_flow.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Uniswap v3 LP 全流程编排")
    sub = parser.add_subparsers(dest="command", required=True)

    # mint
    mp = sub.add_parser("mint", help="创建新 LP 仓位")
    mp.add_argument("--chain", required=True)
    mp.add_argument("--token-a", required=True)
    mp.add_argument("--token-b", required=True)
    mp.add_argument("--fee-tier", type=int, required=True)
    mp.add_argument("--tick-lower", type=int, required=True)
    mp.add_argument("--tick-upper", type=int, required=True)
    mp.add_argument("--amount-a", required=True)
    mp.add_argument("--amount-b", required=True)
    mp.add_argument("--slippage", type=float, default=0.5)
    mp.add_argument("--deadline", type=int, default=600)
    mp.add_argument("--wallet")
    mp.add_argument("--output-dir", default="")
    mp.add_argument("--rpc-url")
    mp.add_argument("--request-only", action="store_true")
    mp.add_argument("--broadcast", action="store_true")
    mp.add_argument("--confirm")
    mp.add_argument("--private-key-env")
    mp.add_argument("--keystore")
    mp.add_argument("--account")
    mp.add_argument("--trade-signer-url")
    mp.add_argument("--trade-signer-token-env")

    # increase
    ip = sub.add_parser("increase", help="增加流动性")
    ip.add_argument("--chain", required=True)
    ip.add_argument("--token-id", type=int, required=True)
    ip.add_argument("--amount0", required=True)
    ip.add_argument("--amount1", required=True)
    ip.add_argument("--slippage", type=float, default=0.5)
    ip.add_argument("--deadline", type=int, default=600)
    ip.add_argument("--wallet")
    ip.add_argument("--output-dir", default="")
    ip.add_argument("--rpc-url")
    ip.add_argument("--broadcast", action="store_true")
    ip.add_argument("--confirm")
    ip.add_argument("--private-key-env")
    ip.add_argument("--keystore")
    ip.add_argument("--account")
    ip.add_argument("--trade-signer-url")
    ip.add_argument("--trade-signer-token-env")

    # decrease
    dp = sub.add_parser("decrease", help="减少流动性")
    dp.add_argument("--chain", required=True)
    dp.add_argument("--token-id", type=int, required=True)
    dp.add_argument("--liquidity-pct", type=float, required=True)
    dp.add_argument("--slippage", type=float, default=0.5)
    dp.add_argument("--deadline", type=int, default=600)
    dp.add_argument("--wallet")
    dp.add_argument("--output-dir", default="")
    dp.add_argument("--rpc-url")
    dp.add_argument("--broadcast", action="store_true")
    dp.add_argument("--confirm")
    dp.add_argument("--private-key-env")
    dp.add_argument("--keystore")
    dp.add_argument("--account")
    dp.add_argument("--trade-signer-url")
    dp.add_argument("--trade-signer-token-env")

    # collect
    cp = sub.add_parser("collect", help="收取手续费")
    cp.add_argument("--chain", required=True)
    cp.add_argument("--token-id", type=int, required=True)
    cp.add_argument("--recipient")
    cp.add_argument("--wallet")
    cp.add_argument("--output-dir", default="")
    cp.add_argument("--rpc-url")
    cp.add_argument("--broadcast", action="store_true")
    cp.add_argument("--confirm")
    cp.add_argument("--private-key-env")
    cp.add_argument("--keystore")
    cp.add_argument("--account")
    cp.add_argument("--trade-signer-url")
    cp.add_argument("--trade-signer-token-env")

    args = parser.parse_args()

    try:
        load_local_env()
        if args.command == "mint":
            result = run_lp_mint_flow(
                chain_name=args.chain, token_a=args.token_a, token_b=args.token_b,
                fee_tier=args.fee_tier, tick_lower=args.tick_lower, tick_upper=args.tick_upper,
                amount_a=args.amount_a, amount_b=args.amount_b,
                slippage=args.slippage, deadline_seconds=args.deadline,
                wallet=args.wallet, output_dir=args.output_dir,
                rpc_url=args.rpc_url, request_only=args.request_only,
                broadcast=args.broadcast, broadcast_args=args if args.broadcast else None,
            )
        elif args.command == "increase":
            result = run_lp_increase_flow(
                chain_name=args.chain, token_id=args.token_id,
                amount0=args.amount0, amount1=args.amount1,
                slippage=args.slippage, deadline_seconds=args.deadline,
                wallet=args.wallet, output_dir=args.output_dir,
                rpc_url=args.rpc_url,
                broadcast=args.broadcast, broadcast_args=args if args.broadcast else None,
            )
        elif args.command == "decrease":
            result = run_lp_decrease_flow(
                chain_name=args.chain, token_id=args.token_id,
                liquidity_pct=args.liquidity_pct, slippage=args.slippage,
                deadline_seconds=args.deadline, wallet=args.wallet,
                output_dir=args.output_dir, rpc_url=args.rpc_url,
                broadcast=args.broadcast, broadcast_args=args if args.broadcast else None,
            )
        elif args.command == "collect":
            result = run_lp_collect_flow(
                chain_name=args.chain, token_id=args.token_id,
                recipient=args.recipient, wallet=args.wallet,
                output_dir=args.output_dir, rpc_url=args.rpc_url,
                broadcast=args.broadcast, broadcast_args=args if args.broadcast else None,
            )
        else:
            parser.print_help()
            sys.exit(1)
        dump_json(result)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
