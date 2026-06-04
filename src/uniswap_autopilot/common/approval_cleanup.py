#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


from uniswap_autopilot.common.common import (
    dump_json,
    get_permit2_address,
    get_position_manager_address,
    get_universal_router_address,
    get_v2_router02_address,
    load_local_env,
    normalize_chain,
    resolve_token,
    resolve_wallet_address,
)
from uniswap_autopilot.execute._internal.rpc import (
    build_calldata, encode_address, encode_uint, query_erc20_allowance, resolve_rpc_url,
)

UINT256_MAX = "115792089237316195423570985008687907853269984665640564039457584007913129639935"


def _get_spenders(chain_name: str) -> list[dict[str, str]]:
    spenders: list[dict[str, str]] = []
    try:
        spenders.append({"label": "positionManager", "address": get_position_manager_address(chain_name)})
    except ValueError:
        pass
    try:
        spenders.append({"label": "router02", "address": get_v2_router02_address(chain_name)})
    except ValueError:
        pass
    spenders.append({"label": "permit2", "address": get_permit2_address()})
    ur = get_universal_router_address(chain_name)
    if ur:
        spenders.append({"label": "universalRouter", "address": ur})
    return spenders


def _get_token_list(chain_name: str) -> list[dict[str, Any]]:
    chain = normalize_chain(chain_name)
    tokens: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sym, tok in chain.tokens.items():
        addr = tok.address
        if addr == "NATIVE" or addr.lower() in seen:
            continue
        seen.add(addr.lower())
        tokens.append({"symbol": sym, "address": addr, "decimals": tok.decimals})
    return tokens


def scan_approvals(
    chain_name: str,
    wallet: str,
    rpc_url: str | None = None,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    rpc, _ = resolve_rpc_url(rpc_url, chain.chain_id)
    if not rpc:
        raise RuntimeError(f"RPC URL not configured for {chain_name}")

    spenders = _get_spenders(chain_name)
    tokens = _get_token_list(chain_name)

    approvals: list[dict[str, Any]] = []
    for tok in tokens:
        for sp in spenders:
            try:
                allowance = query_erc20_allowance(tok["address"], wallet, sp["address"], rpc)
            except Exception:
                continue
            if allowance == 0:
                continue
            approvals.append({
                "token": tok["symbol"],
                "tokenAddress": tok["address"],
                "spenderLabel": sp["label"],
                "spenderAddress": sp["address"],
                "allowance": str(allowance),
                "isMaxApproval": str(allowance) == UINT256_MAX,
            })

    return {
        "action": "approval_scan",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "wallet": wallet,
        "spendersChecked": len(spenders),
        "tokensChecked": len(tokens),
        "nonZeroApprovals": len(approvals),
        "approvals": approvals,
    }


def _build_revoke_tx(
    token_address: str,
    spender_address: str,
    wallet: str,
    chain_id: int,
) -> dict[str, Any]:
    calldata = build_calldata(
        "approve(address,uint256)",
        encode_address(spender_address),
        encode_uint(0),
    )
    return {
        "kind": "approval_revoke",
        "to": token_address,
        "data": calldata,
        "value": "0",
        "chainId": chain_id,
        "from": wallet,
    }


def revoke_approval(
    chain_name: str,
    wallet: str,
    token: str,
    spender: str,
    rpc_url: str | None = None,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    resolved_wallet = resolve_wallet_address(wallet) or wallet

    tok = resolve_token(chain, token)
    if tok["address"] == "NATIVE":
        raise ValueError("cannot revoke approval for native token")

    spenders = _get_spenders(chain_name)
    target = None
    for sp in spenders:
        if sp["label"].lower() == spender.lower() or sp["address"].lower() == spender.lower():
            target = sp
            break
    if not target:
        available = [sp["label"] for sp in spenders]
        raise ValueError(f"spender '{spender}' not found. Available: {available}")

    tx = _build_revoke_tx(tok["address"], target["address"], resolved_wallet, chain.chain_id)

    return {
        "action": "approval_revoke",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "wallet": resolved_wallet,
        "token": tok["symbol"],
        "tokenAddress": tok["address"],
        "spenderLabel": target["label"],
        "spenderAddress": target["address"],
        "transaction": tx,
    }


def revoke_all(
    chain_name: str,
    wallet: str,
    rpc_url: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    resolved_wallet = resolve_wallet_address(wallet) or wallet

    scan = scan_approvals(chain_name, resolved_wallet, rpc_url)

    revoke_txs: list[dict[str, Any]] = []
    for approval in scan["approvals"]:
        tx = _build_revoke_tx(
            approval["tokenAddress"],
            approval["spenderAddress"],
            resolved_wallet,
            chain.chain_id,
        )
        revoke_txs.append({
            "token": approval["token"],
            "tokenAddress": approval["tokenAddress"],
            "spenderLabel": approval["spenderLabel"],
            "spenderAddress": approval["spenderAddress"],
            "previousAllowance": approval["allowance"],
            "transaction": tx,
        })

    return {
        "action": "approval_revoke_all",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "wallet": resolved_wallet,
        "dryRun": dry_run,
        "totalApprovals": len(scan["approvals"]),
        "revokes": revoke_txs,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan and revoke ERC-20 approvals for Uniswap contracts")
    sub = parser.add_subparsers(dest="command")

    s = sub.add_parser("scan", help="Scan all non-zero ERC-20 approvals for known spenders")
    s.add_argument("--chain", required=True)
    s.add_argument("--wallet", required=True)
    s.add_argument("--rpc-url")
    s.add_argument("--output")

    r = sub.add_parser("revoke", help="Revoke a specific token/spender approval")
    r.add_argument("--chain", required=True)
    r.add_argument("--wallet", required=True)
    r.add_argument("--token", required=True, help="Token symbol or address")
    r.add_argument("--spender", required=True, help="Spender label (positionManager, router02, permit2, universalRouter) or address")
    r.add_argument("--rpc-url")
    r.add_argument("--output")

    ra = sub.add_parser("revoke-all", help="Revoke all non-zero approvals (dry-run by default)")
    ra.add_argument("--chain", required=True)
    ra.add_argument("--wallet", required=True)
    ra.add_argument("--rpc-url")
    ra.add_argument("--execute", action="store_true", help="Actually broadcast revoke transactions")
    ra.add_argument("--output")

    args = parser.parse_args()
    load_local_env()

    if args.command == "scan":
        result = scan_approvals(args.chain, args.wallet, args.rpc_url)
        n = result["nonZeroApprovals"]
        print(f"Found {n} non-zero approvals across {result['tokensChecked']} tokens and {result['spendersChecked']} spenders")
        for a in result["approvals"]:
            max_flag = " [MAX]" if a["isMaxApproval"] else ""
            print(f"  {a['token']:8s} -> {a['spenderLabel']:20s} ({a['spenderAddress'][:10]}...){max_flag}  allowance={a['allowance'][:20]}")
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dump_json(result)
    elif args.command == "revoke":
        result = revoke_approval(args.chain, args.wallet, args.token, args.spender, args.rpc_url)
        print(f"Revoke tx: {result['token']} -> {result['spenderLabel']}")
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dump_json(result)
    elif args.command == "revoke-all":
        result = revoke_all(args.chain, args.wallet, args.rpc_url, dry_run=not args.execute)
        mode = "DRY RUN" if result["dryRun"] else "EXECUTE"
        print(f"Revoke all ({mode}): {result['totalApprovals']} approvals to revoke")
        for r in result["revokes"]:
            print(f"  {r['token']:8s} -> {r['spenderLabel']:20s}  (was {r['previousAllowance'][:20]})")
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dump_json(result)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
