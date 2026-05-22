#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


from uniswap_autopilot.common.common import dump_json, load_local_env, normalize_chain, resolve_wallet_address, resolve_token, decimal_to_base_units
from uniswap_autopilot.lp.v3.position import query_position, query_positions_by_owner
from uniswap_autopilot.analytics.position import analyze_position, analyze_positions_by_owner
from uniswap_autopilot.analytics.range_suggest import suggest_ranges
from uniswap_autopilot.lp.v3.build_tx import (
    build_decrease_liquidity_transaction,
    build_collect_transaction,
    build_mint_transaction,
)

REBALANCE_PROFILES = {"CONSERVATIVE", "MODERATE", "AGGRESSIVE"}


def scan_rebalance_candidates(
    chain_name: str,
    wallet: str,
    rpc_url: str | None = None,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    analysis = analyze_positions_by_owner(chain_name, wallet, rpc_url)

    candidates: list[dict[str, Any]] = []
    in_range: list[dict[str, Any]] = []

    for pos in analysis.get("positions", []):
        entry = {
            "tokenId": pos.get("tokenId"),
            "token0": pos.get("token0", {}).get("symbol", "?"),
            "token1": pos.get("token1", {}).get("symbol", "?"),
            "feeTier": pos.get("feeTier"),
            "inRange": pos.get("inRange"),
            "tickLower": pos.get("tickLower"),
            "tickUpper": pos.get("tickUpper"),
            "totalValueUsd": pos.get("totalValueUsd", 0.0),
            "uncollectedFeesUsd": pos.get("uncollectedFees", {}).get("totalUsd", 0.0),
        }
        if not pos.get("inRange", True):
            candidates.append(entry)
        else:
            in_range.append(entry)

    return {
        "action": "rebalance_scan",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "wallet": wallet,
        "totalPositions": len(analysis.get("positions", [])),
        "outOfRange": candidates,
        "inRange": in_range,
        "candidatesForRebalance": [c["tokenId"] for c in candidates],
    }


def execute_rebalance(
    chain_name: str,
    token_id: int,
    profile: str = "MODERATE",
    slippage_pct: float = 0.5,
    wallet: str | None = None,
    rpc_url: str | None = None,
    request_only: bool = True,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    owner = resolve_wallet_address(wallet) or ""
    if not owner:
        raise ValueError("wallet address required")

    profile = profile.upper()
    if profile not in REBALANCE_PROFILES:
        raise ValueError(f"profile must be one of {REBALANCE_PROFILES}, got '{profile}'")

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

    # Step 2: get suggested ranges
    ranges = suggest_ranges(chain_name, tok0_addr, tok1_addr, fee_tier, rpc_url)
    suggestions = ranges.get("suggestions", [])
    target = None
    for s in suggestions:
        if s["profile"] == profile:
            target = s
            break
    if not target:
        raise ValueError(f"profile '{profile}' not found in range suggestions")

    new_tick_lower = target["tickLower"]
    new_tick_upper = target["tickUpper"]

    # Step 3: decrease 100%
    decrease_result = build_decrease_liquidity_transaction(
        chain_name=chain_name,
        token_id=token_id,
        liquidity_pct=100.0,
        slippage_pct=slippage_pct,
        wallet=owner,
        rpc_url=rpc_url,
    )

    # Step 4: collect all
    collect_result = build_collect_transaction(
        chain_name=chain_name,
        token_id=token_id,
        recipient=owner,
    )

    # Step 5: mint new position with suggested range
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
        tick_lower=new_tick_lower,
        tick_upper=new_tick_upper,
        amount0=amount0,
        amount1=amount1,
        slippage_pct=slippage_pct,
        wallet=owner,
        rpc_url=rpc_url,
        request_only=request_only,
    )

    return {
        "action": "rebalance",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "tokenId": token_id,
        "profile": profile,
        "feeTier": fee_tier,
        "oldRange": {"tickLower": tick_lower, "tickUpper": tick_upper},
        "newRange": {"tickLower": new_tick_lower, "tickUpper": new_tick_upper},
        "newRangeWidthPct": target.get("rangeWidthPct"),
        "currentLiquidity": str(current_liq),
        "steps": {
            "decrease": decrease_result,
            "collect": collect_result,
            "mint": mint_result,
        },
        "broadcastReady": not request_only,
    }


def batch_rebalance(
    chain_name: str,
    wallet: str,
    profile: str = "MODERATE",
    slippage_pct: float = 0.5,
    rpc_url: str | None = None,
    request_only: bool = True,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    scan = scan_rebalance_candidates(chain_name, wallet, rpc_url)
    candidates = scan["candidatesForRebalance"]

    succeeded: list[int] = []
    failed: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    for tid in candidates:
        try:
            result = execute_rebalance(
                chain_name=chain_name,
                token_id=tid,
                profile=profile,
                slippage_pct=slippage_pct,
                wallet=wallet,
                rpc_url=rpc_url,
                request_only=request_only,
            )
            succeeded.append(tid)
            results.append({
                "tokenId": tid,
                "status": "success",
                "newRange": result["newRange"],
                "steps": result["steps"],
            })
        except Exception as exc:
            failed.append({"tokenId": tid, "error": str(exc)})
            results.append({"tokenId": tid, "status": "error", "error": str(exc)})

    return {
        "action": "batch_rebalance",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "wallet": wallet,
        "profile": profile,
        "totalAttempted": len(candidates),
        "succeeded": succeeded,
        "failed": failed,
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="V3 LP auto-rebalance: close out-of-range and open new range")
    sub = parser.add_subparsers(dest="command")

    s = sub.add_parser("scan", help="Find out-of-range positions eligible for rebalance")
    s.add_argument("--chain", required=True)
    s.add_argument("--wallet", required=True)
    s.add_argument("--rpc-url")
    s.add_argument("--output")

    e = sub.add_parser("execute", help="Execute rebalance: decrease + collect + mint with new range")
    e.add_argument("--chain", required=True)
    e.add_argument("--token-id", type=int, required=True)
    e.add_argument("--profile", choices=["CONSERVATIVE", "MODERATE", "AGGRESSIVE"], default="MODERATE")
    e.add_argument("--wallet")
    e.add_argument("--slippage", type=float, default=0.5)
    e.add_argument("--rpc-url")
    e.add_argument("--output")

    b = sub.add_parser("batch", help="Batch rebalance all out-of-range positions")
    b.add_argument("--chain", required=True)
    b.add_argument("--wallet", required=True)
    b.add_argument("--profile", choices=["CONSERVATIVE", "MODERATE", "AGGRESSIVE"], default="MODERATE")
    b.add_argument("--slippage", type=float, default=0.5)
    b.add_argument("--rpc-url")
    b.add_argument("--output")

    args = parser.parse_args()
    load_local_env()

    if args.command == "scan":
        result = scan_rebalance_candidates(args.chain, args.wallet, args.rpc_url)
        n = len(result["candidatesForRebalance"])
        print(f"Found {n}/{result['totalPositions']} out-of-range positions eligible for rebalance")
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dump_json(result)
    elif args.command == "execute":
        result = execute_rebalance(
            args.chain, args.token_id, args.profile, args.slippage,
            args.wallet, args.rpc_url,
        )
        print(f"Rebalance plan for position #{args.token_id}: decrease -> collect -> mint ({args.profile})")
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dump_json(result)
    elif args.command == "batch":
        result = batch_rebalance(
            args.chain, args.wallet, args.profile, args.slippage, args.rpc_url,
        )
        print(f"Batch rebalance: {len(result['succeeded'])} succeeded, {len(result['failed'])} failed / {result['totalAttempted']} attempted")
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dump_json(result)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
