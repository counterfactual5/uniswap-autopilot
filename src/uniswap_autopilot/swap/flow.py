#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any


from uniswap_autopilot.common.common import add_common_arguments, decimal_to_base_units, dump_json, load_local_env, normalize_chain, parse_amount, resolve_token, resolve_wallet_address
from uniswap_autopilot.execute import broadcast as execute_transaction
from uniswap_autopilot.swap.flow_core.artifacts import write_json
from uniswap_autopilot.swap.flow_core.broadcast import maybe_broadcast
from uniswap_autopilot.swap.flow_core.diagnostics import (
    build_swap_failure_message,
    classify_swap_failure,
    extract_permit_sig_deadline,
    parse_intish,
)
from uniswap_autopilot.swap.flow_core.paper import (
    append_jsonl,
    build_paper_trade_entry_id,
    build_paper_trade_info,
    build_paper_trade_journal_entry,
    build_paper_trade_paths,
    build_quote_only_paper_swap,
)
from uniswap_autopilot.swap.flow_core.policy import (
    evaluate_auto_trade_policy,
    load_auto_trade_policy,
    normalize_policy_token,
    policy_rule_allows,
)
from uniswap_autopilot.swap.trading_api import quote as trading_api_quote
from uniswap_autopilot.swap.trading_api import swap as swap_dry_run

SIMULATION_FALLBACK_MARKERS = ("TRANSFER_FROM_FAILED",)


def run_trade_flow(
    chain: str,
    token_in: str,
    token_out: str,
    amount: str,
    wallet: str | None,
    output_dir: str,
    token_in_decimals: int | None = None,
    token_out_decimals: int | None = None,
    swap_type: str = "EXACT_INPUT",
    slippage: float = 0.5,
    auto_slippage: bool = False,
    auto_unwrap: bool = False,
    dst_chain: str | None = None,
    routing_preference: str = "BEST_PRICE",
    signature: str | None = None,
    signature_file: str | None = None,
    quote_file: str | None = None,
    rpc_url: str | None = None,
    check_approval: bool = True,
    broadcast_approval: bool = False,
    approval_confirm: str | None = None,
    broadcast_swap: bool = False,
    swap_confirm: str | None = None,
    assume_approval_ready: bool = False,
    signer_args_source: argparse.Namespace | None = None,
    receipt_confirmations: int = 1,
    policy_file: str | None = None,
    auto_sign_permit: bool = False,
    auto_execute: bool = False,
    paper_trade: bool = False,
    journal_file: str | None = None,
    use_flashbots: bool = False,
) -> dict[str, Any]:
    load_local_env()
    effective_slippage = slippage
    slippage_source = "manual"
    if auto_slippage:
        from uniswap_autopilot.swap.extensions.slippage import suggest_slippage
        suggestion = suggest_slippage(chain, token_in, token_out, float(parse_amount(amount)))
        effective_slippage = suggestion["recommendedSlippage"]
        slippage_source = f"auto({suggestion['category']})"
    if paper_trade and broadcast_approval:
        raise ValueError("--paper-trade cannot be combined with --broadcast-approval")
    if paper_trade and broadcast_swap:
        raise ValueError("--paper-trade cannot be combined with --broadcast-swap")
    if paper_trade and auto_sign_permit:
        raise ValueError("--paper-trade cannot be combined with --auto-sign-permit")
    if paper_trade and auto_execute:
        raise ValueError("--paper-trade cannot be combined with --auto-execute")
    effective_flashbots = use_flashbots
    if use_flashbots and normalize_chain(chain).key != "ethereum":
        effective_flashbots = False
        print(f"warning: --flashbots only works on Ethereum mainnet; ignoring for {chain}", file=sys.stderr)
    effective_signer_args = execute_transaction.auto_select_signer_args(signer_args_source)
    wallet_preference = "any"
    if effective_signer_args is not None:
        if execute_transaction.has_direct_signer(effective_signer_args):
            wallet_preference = "secure"
    resolved_wallet = resolve_wallet_address(wallet, "wallet", preference=wallet_preference)
    if not resolved_wallet:
        raise ValueError("--wallet is required unless a wallet address env var is configured")

    paper_trade_entry_id: str | None = None
    journal_path: Path | None = None
    if paper_trade:
        paper_trade_entry_id, output_root, journal_path = build_paper_trade_paths(output_dir, journal_file)
    else:
        output_root = Path(output_dir)

    request_context = {
        "chain": chain,
        "tokenIn": token_in,
        "tokenOut": token_out,
        "amount": amount,
        "wallet": resolved_wallet,
        "swapType": swap_type,
        "slippage": effective_slippage,
        "slippageSource": slippage_source,
        "routingPreference": routing_preference,
        "outputDir": str(output_root),
    }
    if dst_chain:
        request_context["dstChain"] = dst_chain

    response: dict[str, Any] | None = None
    try:
        effective_signature = swap_dry_run.load_signature_value(signature=signature, signature_file=signature_file)
        if quote_file:
            quote_path = Path(quote_file)
        else:
            quote_path = output_root / "quote.json"
        permit_path = output_root / "permit.json"
        typed_data_path = output_root / "typed-data.json"
        signature_path = output_root / "signature.txt"
        swap_path = output_root / "swap.json"
        policy_check: dict[str, Any] | None = None
        if policy_file:
            policy = load_auto_trade_policy(policy_file)
            policy_check = evaluate_auto_trade_policy(
                policy=policy,
                chain_name=chain,
                token_in_name=token_in,
                token_out_name=token_out,
                amount_value=amount,
                slippage=slippage,
            )
            if not policy_check["allowed"]:
                raise ValueError("trade does not satisfy auto-trade policy: " + "; ".join(policy_check["issues"]))
        if auto_execute:
            if policy_check is None:
                raise ValueError("--auto-execute requires --policy-file")
            if effective_signer_args is None or not execute_transaction.has_direct_signer(effective_signer_args):
                raise ValueError("--auto-execute requires signer args")
        effective_auto_sign_permit = auto_sign_permit or auto_execute

        reused_saved_quote = effective_signature is not None or quote_file is not None
        if reused_saved_quote:
            if not quote_path.exists():
                raise ValueError(f"quote reuse requires an existing quote file: {quote_path}")
            quote_response = swap_dry_run.load_quote_payload(str(quote_path))
            raw_quote = quote_response["rawQuote"]
            # 验证复用的 quote 金额与传入的 amount 参数是否匹配
            quote_input = raw_quote.get("quote", {}).get("input", {})
            quote_input_amount = quote_input.get("amount")
            if quote_input_amount:
                # 将传入的 amount 转换成 base units 进行比较
                current_chain = normalize_chain(chain)
                current_token_in = resolve_token(current_chain, token_in)
                current_base_amount = decimal_to_base_units(parse_amount(amount), current_token_in["decimals"])
                if str(current_base_amount) != str(quote_input_amount):
                    raise ValueError(
                        f"quote file amount mismatch: quote at {quote_path} has {quote_input_amount} (wei), but requested amount is {amount} ({current_base_amount} wei)"
                    )
        else:
            api_key = trading_api_quote.require_api_key()
            quote_response, approval_payload, quote_payload = trading_api_quote.prepare_quote_request_data(
                chain_name=chain,
                token_in_name=token_in,
                token_out_name=token_out,
                amount_value=amount,
                wallet=resolved_wallet,
                token_in_decimals=token_in_decimals,
                token_out_decimals=token_out_decimals,
                swap_type=swap_type,
                slippage=effective_slippage,
                routing_preference=routing_preference,
                check_approval=check_approval,
                auto_slippage=auto_slippage,
                dst_chain_name=dst_chain,
            )
            quote_response["requestOnly"] = False
            quote_response["requestPayloads"] = {
                "checkApproval": approval_payload,
                "quote": quote_payload,
            }

            if approval_payload is not None:
                quote_response["approvalCheck"] = trading_api_quote.post_json("check_approval", approval_payload, api_key)

            raw_quote = trading_api_quote.post_json("quote", quote_payload, api_key)
            quote_response["quoteSummary"] = trading_api_quote.summarize_quote(raw_quote)
            quote_response["rawQuote"] = raw_quote
            write_json(quote_path, quote_response)

        approval = (quote_response.get("approvalCheck") or {}).get("approval")
        permit_data = raw_quote.get("permitData")
        effective_assume_approval_ready = assume_approval_ready
        if auto_execute:
            if permit_data and effective_signature is None and not policy_rule_allows(policy_check, "allowAutoSignPermit"):
                raise ValueError("auto-execute requires allowAutoSignPermit for permit-required trades")
            if approval and not effective_assume_approval_ready and not policy_rule_allows(policy_check, "allowAutoApproval"):
                raise ValueError("auto-execute requires allowAutoApproval for approval-required trades")
            if not policy_rule_allows(policy_check, "allowAutoBroadcastSwap"):
                raise ValueError("auto-execute requires allowAutoBroadcastSwap for swap broadcast")

        response = {
            "action": "trade_flow",
            "inputs": {
                "chain": chain,
                "tokenIn": token_in,
                "tokenOut": token_out,
                "amount": amount,
                "wallet": resolved_wallet,
                "swapType": swap_type,
                "slippage": effective_slippage,
                "slippageSource": slippage_source,
                "routingPreference": routing_preference,
            },
            "files": {
                "quote": str(quote_path),
            },
            "quote": {
                "summary": quote_response.get("quoteSummary") or trading_api_quote.summarize_quote(raw_quote),
                "reusedQuote": reused_saved_quote,
            },
            "automation": {
                "policyEnabled": policy_check is not None,
                "autoExecuteRequested": auto_execute,
                "autoSignPermitRequested": auto_sign_permit,
                "autoSignPermitActive": effective_auto_sign_permit,
                "walletPreference": wallet_preference,
                "signerBackendDetected": (
                    "trade-signer"
                    if effective_signer_args is not None and execute_transaction.has_direct_signer(effective_signer_args)
                    else ("direct" if effective_signer_args is not None and execute_transaction.has_direct_signer(effective_signer_args) else None)
                ),
            },
            "nextActions": [],
        }
        if policy_check is not None:
            response["policyCheck"] = policy_check

        approval_broadcasted = False
        auto_broadcasted_approval = False
        approval_assumed_from_preflight = False
        if approval:
            approval_tx = execute_transaction.load_approval_transaction(str(quote_path))
            effective_broadcast_approval = broadcast_approval
            if auto_execute and not effective_broadcast_approval and not effective_assume_approval_ready:
                if policy_rule_allows(policy_check, "allowAutoApproval"):
                    effective_broadcast_approval = True
                    auto_broadcasted_approval = True
                else:
                    raise ValueError("auto-execute requires allowAutoApproval for approval-required trades")
            approval_preflight = execute_transaction.build_preflight_report(
                approval_tx,
                explicit_rpc_url=rpc_url,
            )
            response["approval"] = {
                "required": True,
                "broadcastRequested": effective_broadcast_approval,
                "autoBroadcast": auto_broadcasted_approval,
                "assumeApprovalReady": effective_assume_approval_ready,
                "assumeApprovalReadyInferred": False,
                "preflight": approval_preflight,
                **execute_transaction.build_execute_preview(approval_tx, explicit_rpc_url=rpc_url),
            }
            if not effective_broadcast_approval and not effective_assume_approval_ready:
                allowance = (approval_preflight.get("allowance") or {})
                if allowance.get("alreadySufficient"):
                    effective_assume_approval_ready = True
                    approval_assumed_from_preflight = True
                    response["approval"]["assumeApprovalReady"] = True
                    response["approval"]["assumeApprovalReadyInferred"] = True
            if effective_broadcast_approval:
                if policy_check is not None and not policy_rule_allows(policy_check, "allowAutoApproval"):
                    raise ValueError("auto-trade policy does not allow approval broadcast")
                if effective_signer_args is None:
                    raise ValueError("broadcast_approval requires signer args")
                effective_approval_confirm = approval_confirm
                if effective_approval_confirm is None and auto_broadcasted_approval:
                    effective_approval_confirm = execute_transaction.build_confirmation_phrase(approval_tx)
                approval_broadcast = maybe_broadcast(
                    tx=approval_tx,
                    explicit_rpc_url=rpc_url,
                    confirm=effective_approval_confirm,
                    signer_args_source=effective_signer_args,
                    receipt_confirmations=receipt_confirmations,
                    use_flashbots=effective_flashbots,
                )
                response["approval"].update(approval_broadcast)
                approval_broadcasted = True
            elif not effective_assume_approval_ready:
                response["nextActions"].append("broadcast-approval")
        else:
            response["approval"] = {
                "required": False,
                "broadcastRequested": False,
                "autoBroadcast": False,
            }

        permit_auto_signed = False
        if permit_data and effective_signature is None:
            handoff = swap_dry_run.build_permit_handoff(raw_quote=raw_quote, quote_file=str(quote_path))
            write_json(permit_path, handoff)
            write_json(typed_data_path, handoff["typedData"])
            response["files"]["permit"] = str(permit_path)
            response["files"]["typedData"] = str(typed_data_path)
            response["permit"] = {
                "required": True,
                "routing": handoff["routing"],
                "quoteFile": handoff["quoteFile"],
                "signatureRule": handoff["signatureRule"],
                "instructions": handoff["instructions"],
            }
            if effective_auto_sign_permit:
                if policy_check is not None and not policy_rule_allows(policy_check, "allowAutoSignPermit"):
                    raise ValueError("auto-trade policy does not allow permit auto-sign")
                if effective_signer_args is None or not execute_transaction.has_direct_signer(effective_signer_args):
                    raise ValueError("auto_sign_permit requires signer args")
                quote_blob = raw_quote.get("quote") or {}
                quote_input = quote_blob.get("input") or {}
                quote_output = quote_blob.get("output") or {}
                permit_domain = (raw_quote.get("permitData") or {}).get("domain") or {}
                permit_sign_tx = {
                    "kind": "swap",
                    "chainId": normalize_chain(chain).chain_id,
                    "to": permit_domain.get("verifyingContract") or resolved_wallet,
                    "from": resolved_wallet,
                    "data": "0x01",
                    "value": "0",
                    "inputToken": quote_input.get("token") or "",
                    "inputAmount": quote_input.get("amount") or "0",
                    "outputToken": quote_output.get("token") or "",
                    "outputAmount": quote_output.get("amount") or None,
                    "nativeInput": str(quote_input.get("token") or "").lower() == "0x0000000000000000000000000000000000000000",
                    "sourceKind": "quote-file",
                    "sourceFile": str(quote_path),
                }
                sign_result = execute_transaction.sign_typed_data_with_backend(
                    typed_data_file=str(typed_data_path),
                    typed_data=handoff["typedData"],
                    tx=permit_sign_tx,
                    signer_args_source=effective_signer_args,
                )
                signed = sign_result["signature"]
                signature_path.write_text(signed + "\n", encoding="utf-8")
                effective_signature = signed
                permit_auto_signed = True
                response["files"]["signature"] = str(signature_path)
                response["permit"].update(
                    {
                        "autoSigned": True,
                        "signatureProvided": True,
                        "signatureFile": str(signature_path),
                        "signCommandPreview": sign_result["signCommandPreview"],
                        "signerBackend": sign_result["signerBackend"],
                    }
                )
                if sign_result.get("serviceDecision") is not None:
                    response["permit"]["serviceDecision"] = sign_result["serviceDecision"]
            else:
                if broadcast_swap or auto_execute:
                    raise ValueError("swap broadcast requires permit signature; rerun with --signature or --signature-file")
                response["nextActions"].append("sign-permit")
                if paper_trade:
                    response["permit"]["paperSignatureBypassed"] = True
                else:
                    return response

        response["permit"] = {
            **(response.get("permit") or {}),
            "required": bool(permit_data),
            "signatureProvided": effective_signature is not None,
            "autoSigned": permit_auto_signed,
        }
        permit_sig_deadline = extract_permit_sig_deadline(raw_quote)
        if permit_sig_deadline is not None:
            response["permit"]["sigDeadline"] = str(permit_sig_deadline)
            # Ignore tiny placeholder values often used in tests/mocks.
            if permit_sig_deadline >= 1_000_000_000:
                response["permit"]["sigDeadlineExpired"] = permit_sig_deadline <= int(time.time())
                if response["permit"]["sigDeadlineExpired"]:
                    raise ValueError(
                        "permit signature has expired; refresh quote (do not reuse old quote/signature) and sign again"
                    )
            else:
                response["permit"]["sigDeadlineExpired"] = False

        if permit_data and effective_signature is None and paper_trade:
            response["swap"] = build_quote_only_paper_swap(
                raw_quote=raw_quote,
                reason="permit signature is unavailable, paper-trade records quote-only preview",
            )
        else:
            api_key = trading_api_quote.require_api_key()
            simulation_requested = True
            simulation_used = True
            simulation_fallback_reason: str | None = None
            swap_payload = swap_dry_run.build_swap_payload(
                raw_quote=raw_quote,
                signature=effective_signature,
                simulate_transaction=True,
                refresh_gas_price=True,
            )
            try:
                swap_response = swap_dry_run.post_json("swap", swap_payload, api_key)
            except RuntimeError as exc:
                if not any(marker in str(exc) for marker in SIMULATION_FALLBACK_MARKERS):
                    diagnosis = classify_swap_failure(
                        error_text=str(exc),
                        raw_quote=raw_quote,
                        wallet=resolved_wallet,
                        chain_name=chain,
                        explicit_rpc_url=rpc_url,
                        quote_path=quote_path,
                        reused_saved_quote=reused_saved_quote,
                        has_signature=effective_signature is not None,
                        approval_required=bool(approval),
                        assume_approval_ready=effective_assume_approval_ready,
                    )
                    response["swapFailureDiagnosis"] = diagnosis
                    raise RuntimeError(build_swap_failure_message(str(exc), diagnosis)) from exc
                simulation_used = False
                simulation_fallback_reason = str(exc)
                swap_payload = swap_dry_run.build_swap_payload(
                    raw_quote=raw_quote,
                    signature=effective_signature,
                    simulate_transaction=False,
                    refresh_gas_price=False,
                )
                swap_response = swap_dry_run.post_json("swap", swap_payload, api_key)

            wrapped_swap_response = {
                "action": "swap_dry_run",
                "requestOnly": False,
                "requestPayload": swap_payload,
                "swapResponse": swap_response,
            }
            write_json(swap_path, wrapped_swap_response)
            response["files"]["swap"] = str(swap_path)

            swap_tx = execute_transaction.load_swap_transaction(str(swap_path))
            swap_preflight = execute_transaction.build_preflight_report(
                swap_tx,
                explicit_rpc_url=rpc_url,
            )
            effective_broadcast_swap = broadcast_swap
            auto_broadcasted_swap = False
            if auto_execute and not effective_broadcast_swap:
                if policy_rule_allows(policy_check, "allowAutoBroadcastSwap"):
                    effective_broadcast_swap = True
                    auto_broadcasted_swap = True
                else:
                    raise ValueError("auto-execute requires allowAutoBroadcastSwap for swap broadcast")
            response["swap"] = {
                "simulationRequested": simulation_requested,
                "simulationUsed": simulation_used,
                "simulationFallbackReason": simulation_fallback_reason,
                "broadcastRequested": effective_broadcast_swap,
                "autoBroadcast": auto_broadcasted_swap,
                "preflight": swap_preflight,
                **execute_transaction.build_execute_preview(swap_tx, explicit_rpc_url=rpc_url),
            }
            if effective_broadcast_swap:
                if policy_check is not None and not policy_rule_allows(policy_check, "allowAutoBroadcastSwap"):
                    raise ValueError("auto-trade policy does not allow swap broadcast")
                if approval and not (approval_broadcasted or effective_assume_approval_ready):
                    raise ValueError(
                        "swap broadcast requires approval to be broadcast in this run or --assume-approval-ready"
                    )
                if effective_signer_args is None:
                    raise ValueError("broadcast_swap requires signer args")
                effective_swap_confirm = swap_confirm
                if effective_swap_confirm is None and auto_broadcasted_swap:
                    effective_swap_confirm = execute_transaction.build_confirmation_phrase(swap_tx)
                swap_broadcast = maybe_broadcast(
                    tx=swap_tx,
                    explicit_rpc_url=rpc_url,
                    confirm=effective_swap_confirm,
                    signer_args_source=effective_signer_args,
                    receipt_confirmations=receipt_confirmations,
                    use_flashbots=effective_flashbots,
                )
                response["swap"].update(swap_broadcast)

                # Gas tracking from receipt
                receipt = swap_broadcast.get("receipt") or {}
                gas_used = parse_intish(receipt.get("gasUsed"))
                effective_gas_price = parse_intish(receipt.get("effectiveGasPrice"))
                if gas_used is not None:
                    gas_cost_wei = gas_used * (effective_gas_price or 0)
                    response["swap"]["gasTracking"] = {
                        "gasUsed": gas_used,
                        "effectiveGasPrice": effective_gas_price,
                        "gasCostWei": gas_cost_wei,
                    }

                # Auto WETH unwrap on L2
                if auto_unwrap:
                    from uniswap_autopilot.common.gas import check_weth_unwrap_needed, build_weth_unwrap_tx
                    quote_out = (raw_quote.get("quote") or {}).get("output") or {}
                    if check_weth_unwrap_needed(chain, quote_out, token_out):
                        out_amount = str(quote_out.get("amount") or "0")
                        unwrap_tx = build_weth_unwrap_tx(chain, out_amount, resolved_wallet)
                        response["swap"]["wethUnwrap"] = {
                            "needed": True,
                            "transaction": unwrap_tx,
                        }
                        try:
                            unwrap_broadcast = maybe_broadcast(
                                tx=unwrap_tx.get("transaction") or unwrap_tx,
                                explicit_rpc_url=rpc_url,
                                confirm=None,
                                signer_args_source=effective_signer_args,
                                receipt_confirmations=receipt_confirmations,
                                use_flashbots=effective_flashbots,
                            )
                            response["swap"]["wethUnwrap"]["broadcast"] = unwrap_broadcast
                        except Exception as unwrap_exc:
                            response["swap"]["wethUnwrap"]["error"] = str(unwrap_exc)
                            response["nextActions"].append("broadcast-weth-unwrap")
                elif dst_chain and response["swap"].get("broadcastRequested"):
                    from uniswap_autopilot.swap.extensions.bridge import verify_bridge_arrival
                    response["bridgeVerification"] = {
                        "dstChain": dst_chain,
                        "status": "pending",
                        "instructions": "Use swap.extensions.bridge.verify_bridge_arrival() to poll for arrival",
                    }
            else:
                response["nextActions"].append("broadcast-swap")
        if approval and not approval_broadcasted and not effective_assume_approval_ready:
            response["nextActions"].append("ensure-approval-mined")
        if approval_assumed_from_preflight:
            response["nextActions"].append("approval-already-sufficient")

        if paper_trade:
            paper_info = build_paper_trade_info(
                entry_id=paper_trade_entry_id or build_paper_trade_entry_id(),
                journal_path=journal_path or (output_root / "paper-trade-journal.jsonl"),
                run_output_dir=output_root,
                status="recorded",
                response=response,
            )
            append_jsonl(
                Path(paper_info["journalFile"]),
                build_paper_trade_journal_entry(
                    entry_id=paper_info["entryId"],
                    journal_status="recorded",
                    run_output_dir=output_root,
                    request_context=request_context,
                    response=response,
                ),
            )
            response["paperTrade"] = paper_info
        return response
    except Exception as exc:
        if paper_trade:
            effective_entry_id = paper_trade_entry_id or build_paper_trade_entry_id()
            effective_journal_path = journal_path or (Path(output_dir) / "paper-trade-journal.jsonl")
            effective_response = response or {
                "action": "trade_flow",
                "inputs": request_context,
                "nextActions": [],
            }
            append_jsonl(
                effective_journal_path,
                build_paper_trade_journal_entry(
                    entry_id=effective_entry_id,
                    journal_status="error",
                    run_output_dir=output_root,
                    request_context=request_context,
                    response=effective_response,
                    error=str(exc),
                ),
            )
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="串行编排 Uniswap quote -> permit -> swap -> execute preview")
    add_common_arguments(parser)
    parser.add_argument(
        "--wallet",
        help="用户钱包地址；若未提供，则优先回退到 SECURE_WALLET_ADDRESS / HOT_WALLET_ADDRESS",
    )
    parser.add_argument("--output-dir", default="", help="输出目录")
    parser.add_argument(
        "--swap-type",
        choices=["EXACT_INPUT", "EXACT_OUTPUT"],
        default="EXACT_INPUT",
        help="Trading API quote type",
    )
    parser.add_argument("--slippage", type=float, default=0.5, help="滑点百分比，默认 0.5")
    parser.add_argument("--auto-slippage", action="store_true", help="Automatically suggest slippage based on token pair")
    parser.add_argument("--auto-unwrap", action="store_true", help="Auto-unwrap WETH to native on L2 after swap")
    parser.add_argument("--flashbots", action="store_true", help="Route transaction through Flashbots private mempool (Ethereum only)")
    parser.add_argument("--dst-chain", help="Destination chain for cross-chain swap (bridge)")
    parser.add_argument("--routing-preference", default="BEST_PRICE", help="如 BEST_PRICE / FASTEST / CLASSIC")
    parser.add_argument("--signature", help="直接传入 permitData 签名")
    parser.add_argument("--signature-file", help="permitData 签名文件")
    parser.add_argument("--quote-file", help="复用既有 quote.json；传签名时推荐使用，避免签名失效")
    parser.add_argument("--rpc-url", help="显式指定 execute preview 用的 RPC URL")
    parser.add_argument("--broadcast-approval", action="store_true", help="在 runner 内直接广播 approval")
    parser.add_argument("--approval-confirm", help="approval 广播确认短语，必须精确匹配")
    parser.add_argument("--broadcast-swap", action="store_true", help="在 runner 内直接广播 swap")
    parser.add_argument("--swap-confirm", help="swap 广播确认短语，必须精确匹配")
    parser.add_argument(
        "--assume-approval-ready",
        action="store_true",
        help="当 quote 文件里存在 approval，但你已在链上完成授权时，允许直接广播 swap",
    )
    parser.add_argument("--receipt-confirmations", type=int, default=1, help="广播后查询 receipt 的确认数，默认 1")
    parser.add_argument("--policy-file", help="自动交易策略 JSON；提供后会对链/币对/金额/滑点做强制校验")
    parser.add_argument("--auto-sign-permit", action="store_true", help="当 permitData 存在时自动签名 typed data")
    parser.add_argument(
        "--auto-execute",
        action="store_true",
        help="命中策略时自动签 permit，并自动广播 approval / swap",
    )
    parser.add_argument(
        "--paper-trade",
        action="store_true",
        help="不真实签名/广播；output-dir 会作为根目录并把每次假盘写到 runs/<entryId>/ 下，同时追加 journal",
    )
    parser.add_argument(
        "--journal-file",
        help="paper-trade 模式的 JSONL journal 路径；默认 <output-dir>/paper-trade-journal.jsonl",
    )
    execute_transaction.add_signer_arguments(parser)
    parser.add_argument(
        "--skip-approval-check",
        action="store_true",
        help="跳过 /check_approval。默认当 token_in 为 ERC20 时会检查",
    )
    parser.add_argument("--output", help="把完整 JSON 输出写入文件")
    args = parser.parse_args()

    try:
        response = run_trade_flow(
            chain=args.chain,
            token_in=args.token_in,
            token_out=args.token_out,
            amount=args.amount,
            wallet=args.wallet,
            output_dir=args.output_dir,
            token_in_decimals=args.token_in_decimals,
            token_out_decimals=args.token_out_decimals,
            swap_type=args.swap_type,
            slippage=args.slippage,
            auto_slippage=args.auto_slippage,
            auto_unwrap=args.auto_unwrap,
            dst_chain=args.dst_chain,
            routing_preference=args.routing_preference,
            signature=args.signature,
            signature_file=args.signature_file,
            quote_file=args.quote_file,
            rpc_url=args.rpc_url,
            check_approval=not args.skip_approval_check,
            broadcast_approval=args.broadcast_approval,
            approval_confirm=args.approval_confirm,
            broadcast_swap=args.broadcast_swap,
            swap_confirm=args.swap_confirm,
            assume_approval_ready=args.assume_approval_ready,
            signer_args_source=args,
            receipt_confirmations=args.receipt_confirmations,
            policy_file=args.policy_file,
            auto_sign_permit=args.auto_sign_permit,
            auto_execute=args.auto_execute,
            paper_trade=args.paper_trade,
            journal_file=args.journal_file,
            use_flashbots=args.flashbots,
        )
        if args.output:
            write_json(Path(args.output), response)
        dump_json(response)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
