#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


from uniswap_autopilot.common.common import dump_json, load_local_env, normalize_chain, resolve_token
from uniswap_autopilot.search.search import _ds_lookup


MAJOR_CATEGORIES = {"wrapped-native", "wrapped-btc", "wrapped-eth"}
MAJOR_SYMBOLS = {"ETH", "WETH", "BTC", "WBTC", "CBETH", "CBBTC"}


def _token_is_stable(token: dict[str, Any]) -> bool:
    return bool(token.get("isStable")) or token.get("category") == "stablecoin"


def _token_is_major(token: dict[str, Any]) -> bool:
    return (
        token.get("category") in MAJOR_CATEGORIES
        or token.get("symbol", "").upper() in MAJOR_SYMBOLS
    )


def _get_liquidity(chain_key: str, token_address: str) -> float:
    if token_address == "NATIVE":
        return float("inf")
    ds = _ds_lookup(chain_key, token_address)
    if ds:
        return float(ds.get("liquidityUsd") or 0)
    return 0.0


def suggest_slippage(
    chain_name: str,
    token_in_name: str,
    token_out_name: str,
    amount_usd: float | None = None,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    tok_in = resolve_token(chain, token_in_name)
    tok_out = resolve_token(chain, token_out_name)

    in_stable = _token_is_stable(tok_in)
    out_stable = _token_is_stable(tok_out)
    in_major = _token_is_major(tok_in)
    out_major = _token_is_major(tok_out)

    liq_in = _get_liquidity(chain.key, tok_in["address"])
    liq_out = _get_liquidity(chain.key, tok_out["address"])
    pool_liq = min(liq_in, liq_out) if liq_in and liq_out else max(liq_in, liq_out)

    if in_stable and out_stable:
        category = "stable_stable"
        base = 0.1
    elif (in_stable or out_stable) and (in_major or out_major):
        category = "stable_major"
        base = 0.5
    elif in_stable or out_stable:
        if pool_liq >= 1_000_000:
            category = "stable_midcap"
            base = 1.0
        elif pool_liq >= 100_000:
            category = "stable_smallcap"
            base = 2.0
        else:
            category = "stable_microcap"
            base = 3.0
    else:
        if pool_liq >= 1_000_000:
            category = "volatile_midcap"
            base = 1.5
        elif pool_liq >= 100_000:
            category = "volatile_smallcap"
            base = 3.0
        else:
            category = "volatile_microcap"
            base = 5.0

    dynamic_scale = 1.0
    if amount_usd and pool_liq > 0:
        trade_pct = amount_usd / pool_liq
        if trade_pct > 0.02:
            dynamic_scale = trade_pct / 0.02
    elif amount_usd and pool_liq == 0:
        dynamic_scale = 2.0
    final = min(base * dynamic_scale, 10.0)
    final = round(final, 2)

    reasoning = f"{category}: base={base}%"
    if dynamic_scale > 1.0:
        reasoning += f", dynamic_scale={dynamic_scale:.2f}x (trade=${amount_usd:.0f} vs pool_liq=${pool_liq:,.0f})"
    if pool_liq == 0:
        reasoning += ", pool_liq=0 (no DexScreener data, using base)"

    return {
        "recommendedSlippage": final,
        "category": category,
        "baseSlippage": base,
        "dynamicScale": round(dynamic_scale, 3),
        "poolLiquidityUsd": pool_liq,
        "reasoning": reasoning,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Suggest slippage tolerance based on token pair")
    parser.add_argument("--chain", required=True)
    parser.add_argument("--token-in", required=True)
    parser.add_argument("--token-out", required=True)
    parser.add_argument("--amount-usd", type=float, help="Trade amount in USD for dynamic scaling")
    args = parser.parse_args()

    load_local_env()
    result = suggest_slippage(args.chain, args.token_in, args.token_out, args.amount_usd)
    print(f"Recommended: {result['recommendedSlippage']}% ({result['category']})")
    dump_json(result)


if __name__ == "__main__":
    main()
