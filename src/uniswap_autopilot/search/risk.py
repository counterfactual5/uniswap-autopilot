#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from typing import Any


from uniswap_autopilot.common.common import dump_json, normalize_chain, resolve_token, load_local_env
from uniswap_autopilot.search.search import _ds_lookup

# Price feed integration


def _compute_risk(
    liquidity_usd: float,
    volume_24h: float,
    market_cap: float,
    price_change_24h: float,
) -> tuple[int, list[str]]:
    score = 0
    warnings: list[str] = []

    # Liquidity
    if liquidity_usd < 10_000:
        score += 30
        warnings.append(f"Very low liquidity: ${liquidity_usd:,.0f}")
    elif liquidity_usd < 100_000:
        score += 15
        warnings.append(f"Low liquidity: ${liquidity_usd:,.0f}")
    elif liquidity_usd < 1_000_000:
        score += 5
        warnings.append(f"Moderate liquidity: ${liquidity_usd:,.0f}")

    # Volume
    if volume_24h < 1_000:
        score += 20
        warnings.append(f"Very low 24h volume: ${volume_24h:,.0f}")
    elif volume_24h < 10_000:
        score += 10
        warnings.append(f"Low 24h volume: ${volume_24h:,.0f}")

    # Market cap
    if market_cap < 50_000:
        score += 15
        warnings.append(f"Very low market cap: ${market_cap:,.0f}")
    elif market_cap < 500_000:
        score += 8
        warnings.append(f"Low market cap: ${market_cap:,.0f}")

    # Price change
    abs_change = abs(price_change_24h)
    if abs_change > 50:
        score += 15
        warnings.append(f"Extreme price change: {price_change_24h:+.1f}%")
    elif abs_change > 30:
        score += 8
        warnings.append(f"Large price change: {price_change_24h:+.1f}%")

    return score, warnings


def _risk_level(score: int) -> str:
    if score <= 25:
        return "LOW"
    if score <= 50:
        return "MEDIUM"
    if score <= 75:
        return "HIGH"
    return "EXTREME"


def assess_risk(chain_name: str, token_name: str) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    token = resolve_token(chain, token_name)

    address = token["address"]
    if address == "NATIVE":
        return {
            "action": "risk_assessment",
            "chain": {"key": chain.key, "chainId": chain.chain_id},
            "token": token,
            "riskScore": 0,
            "riskLevel": "LOW",
            "warnings": ["Native token - risk scoring does not apply"],
            "marketData": None,
        }

    # Fetch DexScreener data
    ds_data = _ds_lookup(chain.key, address)
    liquidity_usd = 0.0
    volume_24h = 0.0
    market_cap = 0.0
    price_change_24h = 0.0
    price_usd: float | None = None

    if ds_data:
        liquidity_usd = float(ds_data.get("liquidityUsd") or 0)
        volume_24h = float(ds_data.get("volume24h") or 0)
        market_cap = float(ds_data.get("marketCap") or 0)
        price_changes = ds_data.get("priceChange") or {}
        price_change_24h = float(price_changes.get("h24") or 0)
        price_usd = ds_data.get("priceUsd")
        if price_usd is not None:
            try:
                price_usd = float(price_usd)
            except (ValueError, TypeError):
                price_usd = None

    # Fetch price from price-feed (multi-source fallback)
    if price_usd is None:
        from price_feed import get_price
        result = get_price(chain.key, address, tier="normal")
        if result:
            price_usd = result["price"]

    score, warnings = _compute_risk(liquidity_usd, volume_24h, market_cap, price_change_24h)
    level = _risk_level(score)

    market_data: dict[str, Any] = {
        "liquidityUsd": liquidity_usd,
        "volume24h": volume_24h,
        "marketCap": market_cap,
        "priceChange24h": price_change_24h,
    }
    if price_usd is not None:
        market_data["priceUsd"] = price_usd

    return {
        "action": "risk_assessment",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "token": token,
        "riskScore": score,
        "riskLevel": level,
        "warnings": warnings,
        "marketData": market_data,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Token risk assessment")
    parser.add_argument("--chain", required=True, help="Chain name, e.g. base / ethereum")
    parser.add_argument("--token", required=True, help="Token symbol, address, or NATIVE")

    args = parser.parse_args()

    try:
        load_local_env()
        result = assess_risk(args.chain, args.token)

        print(f"Risk Score: {result['riskScore']}/100")
        print(f"Risk Level: {result['riskLevel']}")
        if result["warnings"]:
            print("Warnings:")
            for w in result["warnings"]:
                print(f"  - {w}")

        dump_json(result)

    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
