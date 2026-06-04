#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from urllib.parse import urlencode


from uniswap_autopilot.common.common import dump_json, normalize_chain, resolve_token, load_local_env


def _swap_url(chain_url_param: str, addr_in: str, addr_out: str) -> str:
    params = {
        "chain": chain_url_param,
        "inputCurrency": addr_in,
        "outputCurrency": addr_out,
    }
    return f"https://app.uniswap.org/swap?{urlencode(params)}"


def _lp_url(chain_url_param: str, symbol_a: str, symbol_b: str, fee: int | None = None) -> str:
    base = f"https://app.uniswap.org/add/{symbol_a}/{symbol_b}?chain={chain_url_param}"
    if fee is not None:
        base += f"&fee={fee}"
    return base


def build_swap_link(
    chain_name: str,
    token_in_name: str,
    token_out_name: str,
) -> dict[str, object]:
    chain = normalize_chain(chain_name)
    token_in = resolve_token(chain, token_in_name)
    token_out = resolve_token(chain, token_out_name)

    # For swap links, native tokens use "NATIVE" as inputCurrency (Uniswap convention)
    addr_in = "NATIVE" if token_in["address"] == "NATIVE" else token_in["address"]
    addr_out = "NATIVE" if token_out["address"] == "NATIVE" else token_out["address"]

    url = _swap_url(chain.url_param, addr_in, addr_out)

    print(f"Swap URL: {url}")

    return {
        "action": "deep_link_swap",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "tokenIn": token_in,
        "tokenOut": token_out,
        "url": url,
    }


def build_lp_link(
    chain_name: str,
    token_a_name: str,
    token_b_name: str,
    fee_tier: int | None = None,
) -> dict[str, object]:
    chain = normalize_chain(chain_name)
    token_a = resolve_token(chain, token_a_name)
    token_b = resolve_token(chain, token_b_name)

    # For LP links, use token symbols in the URL path
    symbol_a = token_a["symbol"]
    symbol_b = token_b["symbol"]

    url = _lp_url(chain.url_param, symbol_a, symbol_b, fee_tier)

    print(f"LP URL: {url}")

    return {
        "action": "deep_link_lp",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "tokenA": token_a,
        "tokenB": token_b,
        "feeTier": fee_tier,
        "url": url,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate app.uniswap.org deep links")
    sub = parser.add_subparsers(dest="command")

    # swap
    sw = sub.add_parser("swap", help="Generate a swap deep link")
    sw.add_argument("--chain", required=True, help="Chain name, e.g. base / ethereum")
    sw.add_argument("--token-in", required=True, help="Input token symbol, address, or NATIVE")
    sw.add_argument("--token-out", required=True, help="Output token symbol, address, or NATIVE")

    # lp
    lp = sub.add_parser("lp", help="Generate an LP deep link")
    lp.add_argument("--chain", required=True, help="Chain name, e.g. base / ethereum")
    lp.add_argument("--token-a", required=True, help="Token A symbol or address")
    lp.add_argument("--token-b", required=True, help="Token B symbol or address")
    lp.add_argument("--fee-tier", type=int, default=None, help="V3 fee tier (e.g. 3000)")

    args = parser.parse_args()

    try:
        load_local_env()

        if args.command == "swap":
            result = build_swap_link(
                chain_name=args.chain,
                token_in_name=args.token_in,
                token_out_name=args.token_out,
            )
            dump_json(result)

        elif args.command == "lp":
            result = build_lp_link(
                chain_name=args.chain,
                token_a_name=args.token_a,
                token_b_name=args.token_b,
                fee_tier=args.fee_tier,
            )
            dump_json(result)

        else:
            parser.print_help()
            sys.exit(1)

    except Exception as exc:
        import json
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
