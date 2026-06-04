#!/usr/bin/env python3
from __future__ import annotations

import argparse


from uniswap_autopilot.common.common import (
    add_common_arguments,
    build_swap_link,
    dump_json,
    normalize_chain,
    override_decimals,
    parse_amount,
    resolve_token,
)


def build_swap_link_response(
    chain_name: str,
    token_in_name: str,
    token_out_name: str,
    amount_value: str,
    field: str = "INPUT",
    token_in_decimals: int | None = None,
    token_out_decimals: int | None = None,
) -> dict[str, object]:
    chain = normalize_chain(chain_name)
    amount = parse_amount(amount_value)
    token_in = override_decimals(resolve_token(chain, token_in_name), token_in_decimals)
    token_out = override_decimals(resolve_token(chain, token_out_name), token_out_decimals)
    human_amount = format(amount, "f")
    link = build_swap_link(chain, token_in, token_out, human_amount, field)
    return {
        "action": "build_swap_link",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "tokenIn": token_in,
        "tokenOut": token_out,
        "amount": human_amount,
        "field": field,
        "deepLink": link,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 Uniswap swap deep link")
    add_common_arguments(parser)
    parser.add_argument(
        "--field",
        choices=["INPUT", "OUTPUT"],
        default="INPUT",
        help="value 对应输入还是输出字段",
    )
    args = parser.parse_args()

    dump_json(
        build_swap_link_response(
            chain_name=args.chain,
            token_in_name=args.token_in,
            token_out_name=args.token_out,
            amount_value=args.amount,
            field=args.field,
            token_in_decimals=args.token_in_decimals,
            token_out_decimals=args.token_out_decimals,
        )
    )


if __name__ == "__main__":
    main()
