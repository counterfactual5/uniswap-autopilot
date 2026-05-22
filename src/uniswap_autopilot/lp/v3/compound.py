#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


from uniswap_autopilot.common.common import dump_json, load_local_env, normalize_chain, resolve_wallet_address, resolve_token, decimal_to_base_units
from uniswap_autopilot.lp.v3.position import query_position, query_positions_by_owner
from uniswap_autopilot.analytics.position import analyze_position
from uniswap_autopilot.lp.v3.build_tx import (
    build_decrease_liquidity_transaction,
    build_collect_transaction,
    build_mint_transaction,
)


def scan_compound_candidates(
    chain_name: str,
    wallet: str,
    min_fee_usd: float = 10.0,
    rpc_url: str | None = None,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    token_ids = query_positions_by_owner(wallet, chain_name, rpc_url)

    positions: list[dict[str, Any]] = []
    candidates: list[int] = []

    for tid in token_ids:
        try:
            pos = analyze_position(chain_name, tid, rpc_url)
            fees_usd = pos.get("uncollectedFees", {}).get("totalUsd", 0.0)
            entry = {
                "tokenId": tid,
                "token0": pos.get("token0", {}).get("symbol", "?"),
                "token1": pos.get("token1", {}).get("symbol", "?"),
                "feeTier": pos.get("feeTier"),
                "uncollectedFeesUsd": fees_usd,
                "totalValueUsd": pos.get("totalValueUsd", 0.0),
                "inRange": pos.get("inRange"),
                "tickLower": pos.get("tickLower"),
                "tickUpper": pos.get("tickUpper"),
            }
            positions.append(entry)
            if fees_usd >= min_fee_usd:
                candidates.append(tid)
        except Exception as exc:
            positions.append({"tokenId": tid, "error": str(exc)})

    return {
        "action": "compound_scan",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "wallet": wallet,
        "minFeeUsd": min_fee_usd,
        "totalPositions": len(positions),
        "candidatesForCompound": candidates,
        "positions": positions,
    }


def execute_compound(
    chain_name: str,
    token_id: int,
    slippage_pct: float = 0.5,
    wallet: str | None = None,
    rpc_url: str | None = None,
    request_only: bool = True,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    owner = resolve_wallet_address(wallet) or ""
    if not owner:
        raise ValueError("wallet address required")

    # Step 1: query current position
    pos = query_position(token_id, chain_name, rpc_url)
    tick_lower = pos["tickLower"]
    tick_upper = pos["tickUpper"]
    fee_tier = pos["fee"]
    tok0_addr = pos["token0"]
    tok1_addr = pos["token1"]
    current_liq = int(pos["liquidity"])
    if current_liq == 0:
        raise ValueError(f"position {token_id} has no liquidity")

    # Get analyzed position for human-readable amounts + fees
    analyzed = analyze_position(chain_name, token_id, rpc_url)
    tok0_amount_human = float(analyzed["token0"]["amount"])
    tok1_amount_human = float(analyzed["token1"]["amount"])
    fee0_human = float(analyzed["uncollectedFees"]["token0"] or "0")
    fee1_human = float(analyzed["uncollectedFees"]["token1"] or "0")

    # Step 2: decrease 100%
    decrease_result = build_decrease_liquidity_transaction(
        chain_name=chain_name,
        token_id=token_id,
        liquidity_pct=100.0,
        slippage_pct=slippage_pct,
        wallet=owner,
        rpc_url=rpc_url,
    )

    # Step 3: collect all
    collect_result = build_collect_transaction(
        chain_name=chain_name,
        token_id=token_id,
        recipient=owner,
    )

    # Step 4: mint new position with same range using collected amounts
    # Convert human amounts + fees to base units for the mint
    from decimal import Decimal as D
    tok0_info = resolve_token(chain, tok0_addr)
    tok1_info = resolve_token(chain, tok1_addr)
    total0 = D(str(tok0_amount_human)) + D(str(fee0_human))
    total1 = D(str(tok1_amount_human)) + D(str(fee1_human))
    amount0 = decimal_to_base_units(total0, tok0_info["decimals"])
    amount1 = decimal_to_base_units(total1, tok1_info["decimals"])

    mint_result = build_mint_transaction(
        chain_name=chain_name,
        token_a=tok0_addr,
        token_b=tok1_addr,
        fee_tier=fee_tier,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        amount0=amount0,
        amount1=amount1,
        slippage_pct=slippage_pct,
        wallet=owner,
        rpc_url=rpc_url,
        request_only=request_only,
    )

    return {
        "action": "compound",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "tokenId": token_id,
        "feeTier": fee_tier,
        "tickLower": tick_lower,
        "tickUpper": tick_upper,
        "currentLiquidity": str(current_liq),
        "steps": {
            "decrease": decrease_result,
            "collect": collect_result,
            "mint": mint_result,
        },
        "broadcastReady": not request_only,
    }


def batch_compound(
    chain_name: str,
    wallet: str,
    min_fee_usd: float = 10.0,
    slippage_pct: float = 0.5,
    rpc_url: str | None = None,
    request_only: bool = True,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    scan = scan_compound_candidates(chain_name, wallet, min_fee_usd, rpc_url)
    candidates = scan["candidatesForCompound"]

    succeeded: list[int] = []
    failed: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    for tid in candidates:
        try:
            result = execute_compound(
                chain_name=chain_name,
                token_id=tid,
                slippage_pct=slippage_pct,
                wallet=wallet,
                rpc_url=rpc_url,
                request_only=request_only,
            )
            succeeded.append(tid)
            results.append({"tokenId": tid, "status": "success", "steps": result["steps"]})
        except Exception as exc:
            failed.append({"tokenId": tid, "error": str(exc)})
            results.append({"tokenId": tid, "status": "error", "error": str(exc)})

    return {
        "action": "batch_compound",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "wallet": wallet,
        "minFeeUsd": min_fee_usd,
        "totalAttempted": len(candidates),
        "succeeded": succeeded,
        "failed": failed,
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="V3 LP auto-compound: harvest fees and reinvest")
    sub = parser.add_subparsers(dest="command")

    s = sub.add_parser("scan", help="Find positions with uncollected fees above threshold")
    s.add_argument("--chain", required=True)
    s.add_argument("--wallet", required=True)
    s.add_argument("--min-fee-usd", type=float, default=10.0)
    s.add_argument("--rpc-url")
    s.add_argument("--output")

    e = sub.add_parser("execute", help="Execute compound: decrease + collect + mint")
    e.add_argument("--chain", required=True)
    e.add_argument("--token-id", type=int, required=True)
    e.add_argument("--wallet")
    e.add_argument("--slippage", type=float, default=0.5)
    e.add_argument("--rpc-url")
    e.add_argument("--output")

    b = sub.add_parser("batch", help="Batch compound all positions with fees above threshold")
    b.add_argument("--chain", required=True)
    b.add_argument("--wallet", required=True)
    b.add_argument("--min-fee-usd", type=float, default=10.0)
    b.add_argument("--slippage", type=float, default=0.5)
    b.add_argument("--rpc-url")
    b.add_argument("--output")

    args = parser.parse_args()
    load_local_env()

    if args.command == "scan":
        result = scan_compound_candidates(args.chain, args.wallet, args.min_fee_usd, args.rpc_url)
        n = len(result["candidatesForCompound"])
        print(f"Found {n}/{result['totalPositions']} positions with fees >= ${args.min_fee_usd}")
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dump_json(result)
    elif args.command == "execute":
        result = execute_compound(
            args.chain, args.token_id, args.slippage,
            args.wallet, args.rpc_url,
        )
        print(f"Compound plan for position #{args.token_id}: decrease → collect → mint")
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dump_json(result)
    elif args.command == "batch":
        result = batch_compound(
            args.chain, args.wallet, args.min_fee_usd,
            args.slippage, args.rpc_url,
        )
        print(f"Batch compound: {len(result['succeeded'])} succeeded, {len(result['failed'])} failed / {result['totalAttempted']} attempted")
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dump_json(result)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
