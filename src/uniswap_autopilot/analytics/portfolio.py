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
    normalize_chain,
    resolve_token,
    resolve_wallet_address,
)
from uniswap_autopilot.analytics.position import analyze_positions_by_owner, fetch_token_prices
from uniswap_autopilot.execute._internal.rpc import query_erc20_balance, resolve_rpc_url

V3_CHAINS = ["ethereum", "base", "arbitrum", "optimism", "polygon", "celo", "linea", "world_chain", "soneium"]

WALLET_TOKENS = {
    "ethereum": ["WETH", "USDC", "USDT", "DAI", "WBTC", "UNI", "LINK"],
    "base": ["WETH", "USDC", "USDbC", "DAI", "cbETH"],
    "arbitrum": ["WETH", "USDC", "USDT", "ARB", "GMX"],
    "optimism": ["WETH", "USDC", "USDT", "OP"],
    "polygon": ["WETH", "USDC", "USDT", "WMATIC"],
    "celo": ["CELO", "cUSD", "cEUR"],
    "linea": ["WETH", "USDC", "USDT"],
    "world_chain": ["WETH", "USDC", "WLD"],
    "soneium": ["WETH", "USDC"],
}


def _query_token_balances(
    chain_name: str,
    wallet: str,
    rpc_url: str,
    token_symbols: list[str],
) -> list[dict[str, Any]]:
    chain = normalize_chain(chain_name)
    balances: list[dict[str, Any]] = []
    for sym in token_symbols:
        try:
            tok = resolve_token(chain, sym, rpc_url)
            addr = tok["address"]
            if addr == "NATIVE":
                continue
            raw = query_erc20_balance(wallet, addr, rpc_url)
            decimals = tok["decimals"]
            human = raw / (10 ** decimals) if raw > 0 else 0.0
            if human == 0.0:
                continue
            balances.append({
                "symbol": sym,
                "address": addr,
                "rawBalance": str(raw),
                "humanBalance": round(human, 8),
                "decimals": decimals,
            })
        except Exception:
            continue
    return balances


def _enrich_balances_with_usd(
    chain_name: str,
    balances: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    chain = normalize_chain(chain_name)
    if not balances:
        return balances
    addrs = [b["address"] for b in balances]
    price_map: dict[str, float | None] = {}
    for i in range(0, len(addrs), 2):
        batch = addrs[i:i + 2]
        if len(batch) == 2:
            p0, p1 = fetch_token_prices(chain.key, batch[0], batch[1])
            price_map[batch[0]] = p0
            price_map[batch[1]] = p1
        else:
            p0, _ = fetch_token_prices(chain.key, batch[0], batch[0])
            price_map[batch[0]] = p0
    for b in balances:
        price = price_map.get(b["address"])
        b["priceUsd"] = price
        if price is not None:
            b["balanceUsd"] = round(b["humanBalance"] * price, 2)
        else:
            b["balanceUsd"] = None
    return balances


def portfolio_overview(
    wallet: str,
    chains: list[str] | None = None,
    rpc_url: str | None = None,
    include_balances: bool = True,
) -> dict[str, Any]:
    wallet = resolve_wallet_address(wallet) or wallet
    target_chains = chains or V3_CHAINS

    all_positions: list[dict[str, Any]] = []
    chain_summaries: list[dict[str, Any]] = []
    chain_errors: list[dict[str, str]] = []
    total_value_usd = 0.0
    total_fees_usd = 0.0
    total_positions = 0
    total_in_range = 0
    total_out_of_range = 0

    for chain_name in target_chains:
        chain = normalize_chain(chain_name)
        rpc, _ = resolve_rpc_url(rpc_url, chain.chain_id)
        if not rpc:
            chain_errors.append({"chain": chain_name, "error": "RPC URL not configured"})
            continue

        # V3 positions
        try:
            analysis = analyze_positions_by_owner(chain_name, wallet, rpc)
            positions = analysis.get("positions", [])
            chain_value = analysis.get("totalValueUsd", 0.0)
            chain_fees = analysis.get("totalFeesUsd", 0.0)

            for pos in positions:
                pos["chain"] = chain_name
                all_positions.append(pos)
                total_positions += 1
                if pos.get("inRange"):
                    total_in_range += 1
                else:
                    total_out_of_range += 1

            total_value_usd += chain_value
            total_fees_usd += chain_fees
        except Exception as exc:
            chain_errors.append({"chain": chain_name, "error": str(exc)})
            positions = []
            chain_value = 0.0
            chain_fees = 0.0

        # Token balances
        balances: list[dict[str, Any]] = []
        balance_total_usd = 0.0
        if include_balances:
            try:
                token_syms = WALLET_TOKENS.get(chain_name, ["WETH", "USDC"])
                balances = _query_token_balances(chain_name, wallet, rpc, token_syms)
                balances = _enrich_balances_with_usd(chain_name, balances)
                balance_total_usd = sum(b.get("balanceUsd") or 0 for b in balances)
            except Exception:
                pass

        chain_summaries.append({
            "chain": chain_name,
            "chainId": chain.chain_id,
            "positionCount": len(positions),
            "positionValueUsd": round(chain_value, 2),
            "uncollectedFeesUsd": round(chain_fees, 2),
            "tokenBalances": balances,
            "tokenBalanceUsd": round(balance_total_usd, 2),
        })

    return {
        "action": "portfolio_overview",
        "wallet": wallet,
        "chains": chain_summaries,
        "summary": {
            "totalChains": len(chain_summaries),
            "totalPositions": total_positions,
            "inRange": total_in_range,
            "outOfRange": total_out_of_range,
            "totalPositionValueUsd": round(total_value_usd, 2),
            "totalUncollectedFeesUsd": round(total_fees_usd, 2),
            "totalTokenBalanceUsd": round(sum(cs.get("tokenBalanceUsd", 0) for cs in chain_summaries), 2),
            "grandTotalUsd": round(
                total_value_usd
                + sum(cs.get("tokenBalanceUsd", 0) for cs in chain_summaries),
                2,
            ),
        },
        "positions": all_positions,
        "errors": chain_errors if chain_errors else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Cross-chain portfolio overview for a Uniswap wallet")
    parser.add_argument("--wallet", required=True)
    parser.add_argument("--chains", help="Comma-separated chain names (default: all V3 chains)")
    parser.add_argument("--rpc-url")
    parser.add_argument("--no-balances", action="store_true", help="Skip ERC-20 balance queries")
    parser.add_argument("--output")
    args = parser.parse_args()

    load_local_env()

    chains = None
    if args.chains:
        chains = [c.strip() for c in args.chains.split(",") if c.strip()]

    result = portfolio_overview(
        wallet=args.wallet,
        chains=chains,
        rpc_url=args.rpc_url,
        include_balances=not args.no_balances,
    )

    s = result["summary"]
    print(f"Portfolio for {args.wallet}")
    print(f"  Chains: {s['totalChains']}  Positions: {s['totalPositions']} ({s['inRange']} in range, {s['outOfRange']} out)")
    print(f"  Position Value: ${s['totalPositionValueUsd']:.2f}  Uncollected Fees: ${s['totalUncollectedFeesUsd']:.2f}")
    print(f"  Token Balances: ${s['totalTokenBalanceUsd']:.2f}  Grand Total: ${s['grandTotalUsd']:.2f}")

    for cs in result["chains"]:
        pos_val = cs["positionValueUsd"]
        bal_val = cs["tokenBalanceUsd"]
        n_pos = cs["positionCount"]
        n_bal = len(cs.get("tokenBalances", []))
        print(f"  {cs['chain']:12s}: {n_pos} positions (${pos_val:.2f}), {n_bal} tokens (${bal_val:.2f})")

    if result.get("errors"):
        for err in result["errors"]:
            print(f"  {err['chain']}: {err['error']}", file=sys.stderr)

    if args.output:
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    dump_json(result)


if __name__ == "__main__":
    main()
