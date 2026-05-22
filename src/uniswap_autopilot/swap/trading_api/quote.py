#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


from uniswap_autopilot.common.common import (
    add_common_arguments,
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


def summarize_quote(raw_quote: dict[str, Any]) -> dict[str, Any]:
    routing = raw_quote.get("routing")
    quote = raw_quote.get("quote") or {}
    summary: dict[str, Any] = {"routing": routing}
    price_impact = quote.get("priceImpact")
    if price_impact is not None:
        summary["priceImpact"] = price_impact
        try:
            pi = float(price_impact)
            if pi >= 10:
                summary["priceImpactWarning"] = "EXTREME"
            elif pi >= 5:
                summary["priceImpactWarning"] = "HIGH"
            elif pi >= 1:
                summary["priceImpactWarning"] = "MODERATE"
        except (ValueError, TypeError):
            pass
    if routing == "CLASSIC":
        summary["outputAmount"] = ((quote.get("output") or {}).get("amount"))
        summary["gasFee"] = quote.get("gasFee")
        summary["gasFeeUSD"] = quote.get("gasFeeUSD")
        summary["gasUseEstimate"] = quote.get("gasUseEstimate")
        return summary

    order_info = quote.get("orderInfo") or {}
    outputs = order_info.get("outputs") or []
    best_output = outputs[0].get("startAmount") if outputs else None
    summary["bestOutputAmount"] = best_output
    summary["deadline"] = order_info.get("deadline")
    summary["chainId"] = order_info.get("chainId")
    return summary


def build_approval_payload(
    wallet: str,
    chain_id: int,
    api_token_in: dict[str, Any],
    base_amount: str,
) -> dict[str, Any]:
    return {
        "walletAddress": wallet,
        "token": api_token_in["address"],
        "amount": base_amount,
        "chainId": chain_id,
    }


def build_quote_payload(
    wallet: str | None,
    chain_id: int,
    api_token_in: dict[str, Any],
    api_token_out: dict[str, Any],
    base_amount: str,
    swap_type: str,
    slippage: float,
    routing_preference: str,
    dst_chain_id: int | None = None,
) -> dict[str, Any]:
    return {
        "swapper": wallet or "0x0000000000000000000000000000000000000001",
        "tokenIn": api_token_in["address"],
        "tokenOut": api_token_out["address"],
        "tokenInChainId": chain_id,
        "tokenOutChainId": dst_chain_id or chain_id,
        "amount": base_amount,
        "type": swap_type,
        "slippageTolerance": slippage,
        "routingPreference": routing_preference,
    }


def prepare_quote_request_data(
    chain_name: str,
    token_in_name: str,
    token_out_name: str,
    amount_value: str,
    wallet: str | None = None,
    token_in_decimals: int | None = None,
    token_out_decimals: int | None = None,
    swap_type: str = "EXACT_INPUT",
    slippage: float = 0.5,
    routing_preference: str = "BEST_PRICE",
    check_approval: bool = False,
    auto_slippage: bool = False,
    dst_chain_name: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
    chain = normalize_chain(chain_name)
    amount = parse_amount(amount_value)
    token_in = override_decimals(resolve_token(chain, token_in_name), token_in_decimals)

    if dst_chain_name:
        dst_chain = normalize_chain(dst_chain_name)
        token_out = override_decimals(resolve_token(dst_chain, token_out_name), token_out_decimals)
    else:
        dst_chain = chain
        token_out = override_decimals(resolve_token(chain, token_out_name), token_out_decimals)

    api_token_in = resolve_api_token(chain, token_in)
    api_token_out = resolve_api_token(dst_chain, token_out)
    validated_wallet = resolve_wallet_address(wallet, "wallet")

    if check_approval and not validated_wallet:
        raise ValueError("--check-approval requires --wallet or a wallet address env var")
    if slippage < 0 or slippage > 100:
        raise ValueError("--slippage must be between 0 and 100")

    effective_slippage = slippage
    slippage_source = "manual"
    if auto_slippage:
        from uniswap_autopilot.swap.extensions.slippage import suggest_slippage
        suggestion = suggest_slippage(chain_name, token_in_name, token_out_name, float(amount))
        effective_slippage = suggestion["recommendedSlippage"]
        slippage_source = f"auto({suggestion['category']})"

    base_amount = decimal_to_base_units(amount, api_token_in["decimals"])
    response: dict[str, Any] = {
        "action": "trading_api_quote",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "tokenIn": token_in,
        "tokenOut": token_out,
        "apiTokenIn": api_token_in,
        "apiTokenOut": api_token_out,
        "wallet": validated_wallet,
        "humanAmount": format(amount, "f"),
        "baseAmount": base_amount,
        "slippage": effective_slippage,
        "slippageSource": slippage_source,
    }
    if dst_chain_name:
        response["dstChain"] = {"key": dst_chain.key, "chainId": dst_chain.chain_id}
        response["crossChain"] = True

    approval_payload: dict[str, Any] | None = None
    if check_approval:
        if token_in["address"] == "NATIVE":
            response["approvalCheck"] = {
                "skipped": True,
                "reason": "token_in is native, approval is not required",
            }
        else:
            approval_payload = build_approval_payload(
                wallet=validated_wallet,
                chain_id=chain.chain_id,
                api_token_in=api_token_in,
                base_amount=base_amount,
            )

    quote_payload = build_quote_payload(
        wallet=validated_wallet,
        chain_id=chain.chain_id,
        api_token_in=api_token_in,
        api_token_out=api_token_out,
        base_amount=base_amount,
        swap_type=swap_type,
        slippage=effective_slippage,
        routing_preference=routing_preference,
        dst_chain_id=dst_chain.chain_id if dst_chain_name else None,
    )
    return response, approval_payload, quote_payload


def main() -> None:
    parser = argparse.ArgumentParser(description="调用 Uniswap Trading API 做 quote / approval dry-run")
    add_common_arguments(parser)
    parser.add_argument(
        "--wallet",
        help="用户钱包地址。check-approval 必填；若未提供，则优先回退到 SECURE_WALLET_ADDRESS / HOT_WALLET_ADDRESS",
    )
    parser.add_argument(
        "--swap-type",
        choices=["EXACT_INPUT", "EXACT_OUTPUT"],
        default="EXACT_INPUT",
        help="Trading API quote type",
    )
    parser.add_argument(
        "--slippage",
        type=float,
        default=0.5,
        help="滑点百分比，默认 0.5",
    )
    parser.add_argument(
        "--routing-preference",
        default="BEST_PRICE",
        help="如 BEST_PRICE / FASTEST / CLASSIC",
    )
    parser.add_argument(
        "--auto-slippage",
        action="store_true",
        help="Automatically suggest slippage based on token pair and liquidity",
    )
    parser.add_argument(
        "--dst-chain",
        help="Destination chain for cross-chain swap (bridge)",
    )
    parser.add_argument(
        "--check-approval",
        action="store_true",
        help="先调用 /check_approval",
    )
    parser.add_argument(
        "--request-only",
        action="store_true",
        help="只打印将发送的请求 payload，不实际调用 Trading API",
    )
    parser.add_argument("--output", help="把完整 JSON 输出写入文件")
    args = parser.parse_args()

    try:
        load_local_env()
        response, approval_payload, quote_payload = prepare_quote_request_data(
            chain_name=args.chain,
            token_in_name=args.token_in,
            token_out_name=args.token_out,
            amount_value=args.amount,
            wallet=args.wallet,
            token_in_decimals=args.token_in_decimals,
            token_out_decimals=args.token_out_decimals,
            swap_type=args.swap_type,
            slippage=args.slippage,
            routing_preference=args.routing_preference,
            check_approval=args.check_approval,
            auto_slippage=args.auto_slippage,
            dst_chain_name=args.dst_chain,
        )
        response["requestOnly"] = args.request_only
        response["requestPayloads"] = {
            "checkApproval": approval_payload,
            "quote": quote_payload,
        }

        if not args.request_only:
            api_key = require_api_key()
            if approval_payload is not None:
                response["approvalCheck"] = post_json("check_approval", approval_payload, api_key)
            raw_quote = post_json("quote", quote_payload, api_key)
            response["quoteSummary"] = summarize_quote(raw_quote)
            pi_warning = (response["quoteSummary"] or {}).get("priceImpactWarning")
            if pi_warning:
                pi_val = response["quoteSummary"].get("priceImpact", "?")
                print(f"WARNING: price impact {pi_val}% ({pi_warning})", file=sys.stderr)
            response["rawQuote"] = raw_quote

        if args.output:
            Path(args.output).write_text(
                json.dumps(response, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        dump_json(response)
    except Exception as exc:  # noqa: BLE001 - CLI should return readable errors
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
