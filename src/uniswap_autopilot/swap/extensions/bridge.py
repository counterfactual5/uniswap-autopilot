#!/usr/bin/env python3
from __future__ import annotations

import argparse
import time
from typing import Any


from uniswap_autopilot.common.common import (
    dump_json,
    load_local_env,
    normalize_chain,
    override_decimals,
    parse_amount,
    post_json,
    require_api_key,
    resolve_api_token,
    resolve_token,
    resolve_wallet_address,
    decimal_to_base_units,
    native_currency_address,
)


def build_cross_chain_quote_payload(
    wallet: str | None,
    src_chain_id: int,
    dst_chain_id: int,
    api_token_in: dict[str, Any],
    api_token_out: dict[str, Any],
    base_amount: str,
    swap_type: str,
    slippage: float,
) -> dict[str, Any]:
    return {
        "swapper": wallet or native_currency_address(),
        "tokenIn": api_token_in["address"],
        "tokenOut": api_token_out["address"],
        "tokenInChainId": src_chain_id,
        "tokenOutChainId": dst_chain_id,
        "amount": base_amount,
        "type": swap_type,
        "slippageTolerance": slippage,
    }


def prepare_cross_chain_quote(
    src_chain_name: str,
    dst_chain_name: str,
    token_in_name: str,
    token_out_name: str,
    amount_value: str,
    wallet: str | None = None,
    token_in_decimals: int | None = None,
    token_out_decimals: int | None = None,
    swap_type: str = "EXACT_INPUT",
    slippage: float = 0.5,
) -> dict[str, Any]:
    src_chain = normalize_chain(src_chain_name)
    dst_chain = normalize_chain(dst_chain_name)
    amount = parse_amount(amount_value)
    token_in = override_decimals(resolve_token(src_chain, token_in_name), token_in_decimals)
    token_out = override_decimals(resolve_token(dst_chain, token_out_name), token_out_decimals)
    api_token_in = resolve_api_token(src_chain, token_in)
    api_token_out = resolve_api_token(dst_chain, token_out)
    validated_wallet = resolve_wallet_address(wallet, "wallet")
    base_amount = decimal_to_base_units(amount, api_token_in["decimals"])

    payload = build_cross_chain_quote_payload(
        wallet=validated_wallet,
        src_chain_id=src_chain.chain_id,
        dst_chain_id=dst_chain.chain_id,
        api_token_in=api_token_in,
        api_token_out=api_token_out,
        base_amount=base_amount,
        swap_type=swap_type,
        slippage=slippage,
    )

    response: dict[str, Any] = {
        "action": "cross_chain_quote",
        "srcChain": {"key": src_chain.key, "chainId": src_chain.chain_id},
        "dstChain": {"key": dst_chain.key, "chainId": dst_chain.chain_id},
        "tokenIn": token_in,
        "tokenOut": token_out,
        "humanAmount": format(amount, "f"),
        "baseAmount": base_amount,
        "requestPayload": payload,
    }
    return response


def fetch_cross_chain_quote(response: dict[str, Any]) -> dict[str, Any]:
    api_key = require_api_key()
    raw = post_json("quote", response["requestPayload"], api_key)
    response["rawQuote"] = raw
    return response


def verify_bridge_arrival(
    dst_chain_name: str,
    token_address: str,
    wallet: str,
    expected_amount_base: str,
    timeout: int = 600,
    poll_interval: int = 30,
    rpc_url: str | None = None,
) -> dict[str, Any]:
    from uniswap_autopilot.common.common import PUBLIC_RPC_URLS
    from uniswap_autopilot.execute._internal.rpc import query_erc20_balance, query_native_balance, resolve_rpc_url

    dst_chain = normalize_chain(dst_chain_name)
    rpc = rpc_url or PUBLIC_RPC_URLS.get(dst_chain.key) or resolve_rpc_url(None, dst_chain.chain_id)[0]
    if not rpc:
        raise RuntimeError(f"RPC URL not configured for {dst_chain_name}")

    expected = int(expected_amount_base)
    start = time.time()
    polls = 0
    balance = 0

    while time.time() - start < timeout:
        polls += 1
        try:
            if token_address in ("NATIVE", native_currency_address()):
                balance = query_native_balance(wallet, rpc)
            else:
                balance = query_erc20_balance(wallet, token_address, rpc)
            if balance >= expected:
                return {
                    "arrived": True,
                    "balance": str(balance),
                    "expected": str(expected),
                    "elapsedSeconds": int(time.time() - start),
                    "polls": polls,
                }
        except Exception:
            pass
        time.sleep(poll_interval)

    return {
        "arrived": False,
        "balance": str(balance),
        "expected": str(expected),
        "elapsedSeconds": int(time.time() - start),
        "polls": polls,
        "timeout": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Cross-chain swap via Uniswap Trading API")
    sub = parser.add_subparsers(dest="command")

    q = sub.add_parser("quote", help="Get cross-chain quote")
    q.add_argument("--src-chain", required=True, help="Source chain")
    q.add_argument("--dst-chain", required=True, help="Destination chain")
    q.add_argument("--token-in", required=True)
    q.add_argument("--token-out", required=True)
    q.add_argument("--amount", required=True)
    q.add_argument("--wallet")
    q.add_argument("--slippage", type=float, default=0.5)
    q.add_argument("--request-only", action="store_true", help="Only print payload")

    v = sub.add_parser("verify", help="Verify bridge arrival")
    v.add_argument("--dst-chain", required=True)
    v.add_argument("--token-address", required=True)
    v.add_argument("--wallet", required=True)
    v.add_argument("--expected-amount", required=True, help="Expected amount in base units")
    v.add_argument("--timeout", type=int, default=600)
    v.add_argument("--poll-interval", type=int, default=30)

    args = parser.parse_args()
    load_local_env()

    if args.command == "quote":
        result = prepare_cross_chain_quote(
            args.src_chain, args.dst_chain, args.token_in, args.token_out,
            args.amount, args.wallet, slippage=args.slippage,
        )
        if not args.request_only:
            result = fetch_cross_chain_quote(result)
        dump_json(result)
    elif args.command == "verify":
        result = verify_bridge_arrival(
            args.dst_chain, args.token_address, args.wallet,
            args.expected_amount, args.timeout, args.poll_interval,
        )
        status = "ARRIVED" if result["arrived"] else "TIMEOUT"
        print(f"Bridge status: {status} ({result['polls']} polls, {result['elapsedSeconds']}s)")
        dump_json(result)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
