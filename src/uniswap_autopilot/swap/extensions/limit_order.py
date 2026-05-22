#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


from uniswap_autopilot.common.common import (
    decimal_to_base_units,
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
)
from uniswap_autopilot.swap.trading_api.quote import build_quote_payload, summarize_quote
from uniswap_autopilot.swap.trading_api.swap import (
    build_swap_payload,
    is_uniswapx_route,
    load_quote_payload,
    load_signature_value,
)


def prepare_limit_order_quote(
    chain_name: str,
    token_in_name: str,
    token_out_name: str,
    amount: str,
    target_price: float | None = None,
    target_output: str | None = None,
    wallet: str | None = None,
    slippage: float = 0.5,
    token_in_decimals: int | None = None,
    token_out_decimals: int | None = None,
    request_only: bool = False,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    token_in = override_decimals(resolve_token(chain, token_in_name), token_in_decimals)
    token_out = override_decimals(resolve_token(chain, token_out_name), token_out_decimals)
    api_token_in = resolve_api_token(chain, token_in)
    api_token_out = resolve_api_token(chain, token_out)
    validated_wallet = resolve_wallet_address(wallet, "wallet")

    input_amount = parse_amount(amount)

    # Compute target output amount in token_out base units
    if target_output is not None:
        output_human = float(parse_amount(target_output))
    elif target_price is not None:
        output_human = float(input_amount) * target_price
    else:
        raise ValueError("either --target-price or --target-output is required")

    output_base = decimal_to_base_units(parse_amount(str(output_human)), api_token_out["decimals"])

    payload = build_quote_payload(
        wallet=validated_wallet,
        chain_id=chain.chain_id,
        api_token_in=api_token_in,
        api_token_out=api_token_out,
        base_amount=output_base,
        swap_type="EXACT_OUTPUT",
        slippage=slippage,
        routing_preference="BEST_PRICE",
    )

    response: dict[str, Any] = {
        "action": "limit_order_quote",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "tokenIn": token_in,
        "tokenOut": token_out,
        "apiTokenIn": api_token_in,
        "apiTokenOut": api_token_out,
        "wallet": validated_wallet,
        "inputHumanAmount": format(input_amount, "f"),
        "targetOutputHumanAmount": format(output_human, "f"),
        "targetOutputBase": output_base,
        "targetPrice": target_price,
        "slippage": slippage,
        "requestPayload": payload,
    }

    if not request_only:
        api_key = require_api_key()
        raw_quote = post_json("quote", payload, api_key)
        response["quoteSummary"] = summarize_quote(raw_quote)
        response["rawQuote"] = raw_quote
        routing = raw_quote.get("routing")
        if routing and not is_uniswapx_route(raw_quote):
            response["warning"] = f"routing is {routing}, not a UniswapX Dutch order; limit order semantics may not apply"

    return response


def submit_limit_order(
    quote_file: str,
    signature: str | None = None,
    signature_file: str | None = None,
    simulate: bool = True,
) -> dict[str, Any]:
    quote_data = load_quote_payload(quote_file)
    raw_quote = quote_data.get("rawQuote") or quote_data

    if not is_uniswapx_route(raw_quote):
        routing = raw_quote.get("routing", "unknown")
        return {
            "action": "limit_order_submit",
            "status": "rejected",
            "reason": f"quote routing is '{routing}', not a UniswapX Dutch order; cannot submit as limit order",
        }

    effective_signature = load_signature_value(signature=signature, signature_file=signature_file)

    api_key = require_api_key()
    swap_payload = build_swap_payload(
        raw_quote=raw_quote,
        signature=effective_signature,
        simulate_transaction=simulate,
        refresh_gas_price=True,
    )
    swap_response = post_json("swap", swap_payload, api_key)

    return {
        "action": "limit_order_submit",
        "status": "submitted",
        "routing": raw_quote.get("routing"),
        "simulated": simulate,
        "swapPayload": swap_payload,
        "swapResponse": swap_response,
    }


def check_order_status(
    chain_name: str,
    tx_hash: str | None = None,
    rpc_url: str | None = None,
) -> dict[str, Any]:
    if not tx_hash:
        raise ValueError("--tx-hash is required")

    chain = normalize_chain(chain_name)
    from uniswap_autopilot.execute._internal.rpc import execute_cast_receipt, resolve_rpc_url

    rpc, _ = resolve_rpc_url(rpc_url, chain.chain_id)
    if not rpc:
        raise RuntimeError(f"RPC URL not configured for {chain_name}")

    receipt = execute_cast_receipt(tx_hash=tx_hash, rpc_url=rpc, confirmations=1)
    status = receipt.get("status")
    succeeded = status == "1" or status == 1

    # Check for Dutch order events in logs
    dutch_order_detected = False
    logs = receipt.get("logs") or []
    for log in logs:
        topics = log.get("topics") or []
        if topics and len(topics[0]) == 66:
            # DutchOrderFilled event from UniswapX Reactor
            if str(topics[0]).startswith("0x5ab3"):
                dutch_order_detected = True
                break

    # Try to get block timestamp for expiry check
    block_number_hex = receipt.get("blockNumber")
    current_time = None
    if block_number_hex:
        try:
            from uniswap_autopilot.execute._internal.rpc import _json_rpc
            block = _json_rpc("eth_getBlockByNumber", [block_number_hex, False], rpc)
            ts_hex = block.get("timestamp") if isinstance(block, dict) else None
            if ts_hex:
                current_time = int(ts_hex, 16)
        except Exception:
            pass

    return {
        "action": "limit_order_status",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "txHash": tx_hash,
        "status": "filled" if succeeded else "failed",
        "dutchOrderDetected": dutch_order_detected,
        "receipt": receipt,
        "blockTimestamp": current_time,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Uniswap limit orders via UniswapX Dutch auction")
    sub = parser.add_subparsers(dest="command")

    q = sub.add_parser("quote", help="Get a limit order quote (EXACT_OUTPUT via UniswapX)")
    q.add_argument("--chain", required=True)
    q.add_argument("--token-in", required=True)
    q.add_argument("--token-out", required=True)
    q.add_argument("--amount", required=True, help="Input amount")
    q.add_argument("--target-price", type=float, help="Target price (token_out per token_in)")
    q.add_argument("--target-output", help="Exact output amount (alternative to --target-price)")
    q.add_argument("--wallet")
    q.add_argument("--slippage", type=float, default=0.5)
    q.add_argument("--token-in-decimals", type=int)
    q.add_argument("--token-out-decimals", type=int)
    q.add_argument("--request-only", action="store_true")
    q.add_argument("--output")

    s = sub.add_parser("submit", help="Submit a signed limit order")
    s.add_argument("--quote-file", required=True)
    s.add_argument("--signature")
    s.add_argument("--signature-file")
    s.add_argument("--no-simulate", action="store_true")
    s.add_argument("--output")

    c = sub.add_parser("status", help="Check order fill status by tx hash")
    c.add_argument("--chain", required=True)
    c.add_argument("--tx-hash", required=True)
    c.add_argument("--rpc-url")
    c.add_argument("--output")

    args = parser.parse_args()
    load_local_env()

    if args.command == "quote":
        result = prepare_limit_order_quote(
            chain_name=args.chain,
            token_in_name=args.token_in,
            token_out_name=args.token_out,
            amount=args.amount,
            target_price=args.target_price,
            target_output=args.target_output,
            wallet=args.wallet,
            slippage=args.slippage,
            token_in_decimals=args.token_in_decimals,
            token_out_decimals=args.token_out_decimals,
            request_only=args.request_only,
        )
        if "warning" in result:
            print(f"WARNING: {result['warning']}", file=sys.stderr)
        print(f"Limit order quote: {args.amount} {args.token_in} -> {result['targetOutputHumanAmount']} {args.token_out}")
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dump_json(result)
    elif args.command == "submit":
        result = submit_limit_order(
            quote_file=args.quote_file,
            signature=args.signature,
            signature_file=args.signature_file,
            simulate=not args.no_simulate,
        )
        print(f"Limit order status: {result.get('status', 'unknown')}")
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dump_json(result)
    elif args.command == "status":
        result = check_order_status(args.chain, args.tx_hash, args.rpc_url)
        print(f"Order status: {result['status']}")
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dump_json(result)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
