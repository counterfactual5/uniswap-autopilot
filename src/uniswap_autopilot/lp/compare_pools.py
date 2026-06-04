#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


from uniswap_autopilot.common.common import (
    V3_FEE_TIERS,
    dump_json,
    load_local_env,
    normalize_chain,
    resolve_token,
    sort_token_addresses,
)
from uniswap_autopilot.lp.v3.pool import query_pool_full_info
from uniswap_autopilot.search.search import _ds_lookup, _fetch_json

DEFILLAMA_YIELDS_URL = "https://yields.llama.fi/pools"

DEFILLAMA_CHAIN_MAP: dict[str, str] = {
    "avalanche": "avax",
}


def _defillama_chain_name(chain_key: str) -> str:
    mapped = DEFILLAMA_CHAIN_MAP.get(chain_key, chain_key)
    return mapped.capitalize()


def fetch_defillama_yields(chain: str) -> dict[str, dict]:
    chain_key = chain.strip().lower()
    dl_chain = _defillama_chain_name(chain_key)

    try:
        data = _fetch_json(DEFILLAMA_YIELDS_URL, timeout=30)
    except Exception as exc:
        print(f"  [warn] DefiLlama yields fetch failed: {exc}", file=sys.stderr)
        return {}

    pools = data.get("data", [])
    result: dict[str, dict] = {}
    for pool in pools:
        if pool.get("project") != "uniswap-v3":
            continue
        if (pool.get("chain") or "").lower() != dl_chain.lower():
            continue
        pool_addr = (pool.get("pool") or "").lower()
        if not pool_addr:
            continue
        result[pool_addr] = {
            "apy": pool.get("apy"),
            "apyBase": pool.get("apyBase"),
            "tvlUsd": pool.get("tvlUsd"),
            "volumeUsd1d": pool.get("volumeUsd1d"),
        }
    return result


def fetch_dexscreener_pair(
    chain: str, token0_addr: str, token1_addr: str
) -> dict | None:
    try:
        ds = _ds_lookup(chain, token0_addr)
    except Exception:
        ds = None
    if not ds:
        try:
            ds = _ds_lookup(chain, token1_addr)
        except Exception:
            return None
    if not ds:
        return None
    return {
        "volume24h": ds.get("volume24h"),
        "liquidityUsd": ds.get("liquidityUsd"),
        "priceUsd": ds.get("priceUsd"),
    }


def _fmt_usd(val: float | None) -> str:
    if val is None:
        return "-"
    if val >= 1_000_000_000:
        return f"${val / 1_000_000_000:.2f}B"
    if val >= 1_000_000:
        return f"${val / 1_000_000:.2f}M"
    if val >= 1_000:
        return f"${val / 1_000:.1f}K"
    if val > 0:
        return f"${val:.4f}"
    return "-"


def _fmt_pct(val: float | None) -> str:
    if val is None:
        return "-"
    return f"{val:.2f}%"


def _safe_float(val: Any, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def compare_pools_for_pair(
    chain_name: str, token_a: str, token_b: str, rpc_url: str | None
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    token_a_info = resolve_token(chain, token_a, rpc_url)
    token_b_info = resolve_token(chain, token_b, rpc_url)

    def _addr_or_wrapped(info: dict[str, Any]) -> str:
        addr = info["address"]
        if addr != "NATIVE":
            return addr
        wrapped = chain.tokens.get(chain.wrapped_native_symbol.upper())
        if wrapped:
            return wrapped.address
        wtoken = resolve_token(chain, chain.wrapped_native_symbol, rpc_url)
        return wtoken["address"]

    addr_a = _addr_or_wrapped(token_a_info)
    addr_b = _addr_or_wrapped(token_b_info)
    token0_addr, token1_addr = sort_token_addresses(addr_a, addr_b)

    pool_results: list[dict[str, Any]] = []
    fee_tiers_sorted = sorted(V3_FEE_TIERS)

    for fee_tier in fee_tiers_sorted:
        try:
            info = query_pool_full_info(
                chain_name, token_a, token_b, fee_tier, rpc_url
            )
        except Exception as exc:
            pool_results.append({
                "feeTier": fee_tier,
                "feePct": f"{fee_tier / 10000:.2f}%",
                "exists": False,
                "error": str(exc),
            })
            continue

        if not info.get("exists"):
            pool_results.append({
                "feeTier": fee_tier,
                "feePct": f"{fee_tier / 10000:.2f}%",
                "poolAddress": None,
                "exists": False,
            })
            continue

        pool_addr = info.get("poolAddress", "").lower()
        pool_results.append({
            "feeTier": fee_tier,
            "feePct": f"{fee_tier / 10000:.2f}%",
            "poolAddress": info.get("poolAddress"),
            "exists": True,
            "currentTick": info.get("currentTick"),
            "currentPrice": info.get("currentPrice"),
            "liquidity": info.get("liquidity"),
            "_addr_lower": pool_addr,
        })

    # Fetch DefiLlama yields for the chain
    dl_yields = fetch_defillama_yields(chain_name)

    # Fetch DexScreener data for the pair
    ds_data = fetch_dexscreener_pair(chain.key, token0_addr, token1_addr)

    # Enrich pool results with yield and volume data
    for pr in pool_results:
        if not pr.get("exists"):
            pr["tvlUsd"] = None
            pr["volume24h"] = None
            pr["apy"] = None
            pr["apyBase"] = None
            continue

        addr_lower = pr.get("_addr_lower", "")
        dl = dl_yields.get(addr_lower, {})
        pr["tvlUsd"] = dl.get("tvlUsd")
        pr["volumeUsd1d"] = dl.get("volumeUsd1d")
        pr["apy"] = dl.get("apy")
        pr["apyBase"] = dl.get("apyBase")

        if ds_data:
            pr["dexVolume24h"] = ds_data.get("volume24h")
            pr["dexLiquidityUsd"] = ds_data.get("liquidityUsd")
            pr["dexPriceUsd"] = ds_data.get("priceUsd")
        else:
            pr["dexVolume24h"] = None
            pr["dexLiquidityUsd"] = None
            pr["dexPriceUsd"] = None

        # Clean internal key
        pr.pop("_addr_lower", None)

    # Determine recommendation
    eligible = [pr for pr in pool_results if pr.get("exists")]
    recommendation = None
    if eligible:
        high_tvl = [p for p in eligible if (_safe_float(p.get("tvlUsd")) or _safe_float(p.get("dexLiquidityUsd"))) > 100_000]
        if high_tvl:
            best = max(high_tvl, key=lambda p: _safe_float(p.get("apy")))
            reason = "highest APY"
        else:
            best = max(eligible, key=lambda p: _safe_float(p.get("tvlUsd")) or _safe_float(p.get("dexLiquidityUsd")))
            reason = "highest TVL (no pool above $100K TVL)"
        recommendation = {
            "feeTier": best["feeTier"],
            "feePct": best["feePct"],
            "poolAddress": best["poolAddress"],
            "reason": reason,
        }

    return {
        "action": "compare_pools",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "tokenA": token_a_info,
        "tokenB": token_b_info,
        "token0": token0_addr,
        "token1": token1_addr,
        "pools": pool_results,
        "recommendation": recommendation,
    }


def _print_comparison_table(result: dict[str, Any]) -> None:
    chain = result["chain"]["key"]
    ta = result["tokenA"]["symbol"]
    tb = result["tokenB"]["symbol"]
    print(f"\nPool Comparison: {ta}/{tb} on {chain}")
    print()
    header = (
        f"{'Fee Tier':<10} "
        f"{'Pool Address':<22} "
        f"{'TVL':>14} "
        f"{'Volume 24h':>14} "
        f"{'APY':>10} "
        f"{'Liquidity':>16}"
    )
    print(header)
    print("-" * len(header) + "-" * 10)

    for pool in result["pools"]:
        fee_pct = pool["feePct"]
        if not pool.get("exists"):
            print(
                f"{fee_pct:<10} "
                f"{'not found':<22} "
                f"{'-':>14} "
                f"{'-':>14} "
                f"{'-':>10} "
                f"{'-':>16}"
            )
            continue

        addr = pool.get("poolAddress", "")
        addr_short = addr[:8] + "..." + addr[-4:] if len(addr) > 14 else addr
        tvl = _safe_float(pool.get("tvlUsd")) or _safe_float(pool.get("dexLiquidityUsd"))
        vol = _safe_float(pool.get("volumeUsd1d")) or _safe_float(pool.get("dexVolume24h"))
        apy = pool.get("apy")
        liq = pool.get("liquidity", "-")

        tvl_str = _fmt_usd(tvl) if tvl > 0 else "-"
        vol_str = _fmt_usd(vol) if vol > 0 else "-"
        apy_str = _fmt_pct(apy) if apy is not None else "-"
        liq_str = str(liq) if liq != "-" else "-"

        print(
            f"{fee_pct:<10} "
            f"{addr_short:<22} "
            f"{tvl_str:>14} "
            f"{vol_str:>14} "
            f"{apy_str:>10} "
            f"{liq_str:>16}"
        )

    print("-" * len(header) + "-" * 10)

    rec = result.get("recommendation")
    if rec:
        print(f"\nRecommended: {rec['feePct']} ({rec['reason']})")
        print(f"  Pool: {rec['poolAddress']}")
    else:
        print("\nNo recommendation (no pools found)")

    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare Uniswap v3 pools across fee tiers for a token pair"
    )
    parser.add_argument(
        "--chain", required=True, help="Chain name, e.g. base / ethereum / arbitrum"
    )
    parser.add_argument(
        "--token-a", required=True, help="Token symbol or address"
    )
    parser.add_argument(
        "--token-b", required=True, help="Token symbol or address"
    )
    parser.add_argument("--rpc-url", help="RPC URL (reads from env if not provided)")
    parser.add_argument("--output", help="Output JSON file path")

    args = parser.parse_args()

    try:
        load_local_env()
        result = compare_pools_for_pair(
            args.chain, args.token_a, args.token_b, args.rpc_url
        )
        _print_comparison_table(result)
        if args.output:
            Path(args.output).write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        dump_json(result)
    except Exception as exc:
        print(
            json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2),
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
