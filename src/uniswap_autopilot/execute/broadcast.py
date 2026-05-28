#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


from uniswap_autopilot.audit import (
    EVENT_BROADCAST,
    EVENT_CONFIRM,
    EVENT_ERROR,
    EVENT_PREFLIGHT,
    log_event,
)
from uniswap_autopilot.common.common import dump_json, load_local_env
from uniswap_autopilot.execute._internal.constants import (
    APPROVE_SELECTOR,
    CHAIN_BY_ID,
    DEFAULT_PRIVATE_KEY_ENV,
    GLOBAL_RPC_ENV_CANDIDATES,
    HEX_DATA_RE,
    HOT_WALLET_ACCOUNT_ENV_NAME,
    HOT_WALLET_KEYSTORE_ENV_NAME,
    HOT_WALLET_PASSWORD_FILE_ENV_NAME,
    HOT_WALLET_PRIVATE_KEY_ENV_NAME,
)
from uniswap_autopilot.execute._internal.preflight import build_preflight_report
from uniswap_autopilot.execute._internal.rpc import (
    estimate_transaction_gas,
    execute_cast_receipt,
    parse_cast_int_output,
    query_erc20_allowance,
    query_erc20_balance,
    query_gas_price,
    query_native_balance,
    receipt_succeeded,
    resolve_rpc_url,
    rpc_env_candidates,
    run_cast_text,
)
from uniswap_autopilot.execute._internal.signer import (
    add_signer_arguments,
    auto_select_signer_args,
    build_signer_namespace,
    detect_hot_wallet_backend,
    ensure_signer_backend,
    has_direct_signer,
    sign_typed_data_with_backend,
)
from uniswap_autopilot.execute._internal.submit import (
    broadcast_with_backend,
    build_broadcast_package,
    extract_transaction_hash,
)
from uniswap_autopilot.execute._internal.tx import (
    base_units_to_human,
    build_confirmation_phrase,
    build_execute_preview,
    decode_erc20_approve_call,
    load_approval_transaction,
    load_json,
    load_swap_transaction,
    lookup_token_decimals,
    lookup_token_symbol,
    normalize_transaction,
    optional_address,
    parse_intish,
    summarize_transaction,
    validate_hex_data,
)

def main() -> None:
    parser = argparse.ArgumentParser(description="对 approval 或 swap 交易做安全广播")
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--swap-file", help="swap_dry_run.py 的输出文件")
    source_group.add_argument("--approval-file", help="trading_api_quote.py 的 quote 输出文件;会读取 approvalCheck.approval")
    source_group.add_argument("--lp-file", help="LP 脚本(build_lp_tx.py / run_lp_flow.py)的输出文件")
    parser.add_argument("--rpc-url", help="显式指定 RPC URL;否则尝试从环境变量解析")
    parser.add_argument("--broadcast", action="store_true", help="真的调用 cast send 广播交易")
    parser.add_argument("--confirm", help="必须与脚本给出的 confirmation 完全一致才允许广播")
    parser.add_argument("--telegram-confirm", action="store_true", help="下单前推送 Telegram inline button 等待用户确认")
    parser.add_argument("--receipt-confirmations", type=int, default=1, help="广播后查询 receipt 的确认数,默认 1")
    add_signer_arguments(parser)
    parser.add_argument("--output", help="把完整 JSON 输出写入文件")
    args = parser.parse_args()

    try:
        load_local_env()

        if args.swap_file:
            tx = load_swap_transaction(args.swap_file)
        elif args.approval_file:
            tx = load_approval_transaction(args.approval_file)
        elif args.lp_file:
            blob = json.loads(Path(args.lp_file).read_text(encoding="utf-8"))
            lp_tx = blob.get("transaction") or blob.get("mint", {}).get("transaction") or blob.get("increase", {}).get("transaction") or blob.get("decrease", {}).get("transaction") or blob.get("collect", {}).get("transaction")
            if not lp_tx:
                raise ValueError("LP file must contain 'transaction' or a sub-action with 'transaction'")
            tx = normalize_transaction(lp_tx, tx_kind="lp", source_file=str(args.lp_file), source_kind="lp-file")
        else:
            raise ValueError("one of --swap-file, --approval-file, --lp-file is required")

        preview = build_execute_preview(tx, explicit_rpc_url=args.rpc_url)
        summary = preview["summary"]
        rpc_url = summary["rpcUrlResolved"]
        rpc_candidates = summary["rpcEnvCandidates"]

        response: dict[str, Any] = {
            "action": "execute_transaction",
            "summary": summary,
            "broadcastRequested": args.broadcast,
            "commandPreview": preview["commandPreview"],
        }
        response["preflight"] = build_preflight_report(tx, explicit_rpc_url=args.rpc_url)

        # Telegram confirmation if requested or exceeds threshold
        THRESHOLD_USD = float(os.environ.get("TRADE_CONFIRM_THRESHOLD_USD", "0"))
        
        # Calculate trade value in USD
        from uniswap_autopilot.execute._internal.tx import estimate_trade_value_usd
        estimate_data = {
            **summary,
            "tokenIn": tx.get("inputTokenSymbol", ""),
            "tokenOut": tx.get("outputTokenSymbol", ""),
            "amountIn": tx.get("inputAmount", "0"),
            "amountOut": tx.get("outputAmount", "0"),
            "inputToken": tx.get("inputToken"),
            "outputToken": tx.get("outputToken"),
            "chain": (tx.get("chainKey") or "").lower(),
        }
        trade_value_usd = estimate_trade_value_usd(estimate_data)
        
        if args.telegram_confirm or (THRESHOLD_USD > 0 and trade_value_usd >= THRESHOLD_USD):
            from uniswap_autopilot.execute.telegram_confirm import request_trade_confirmation
            
            trade_details = {
                "chain": summary.get("chain"),
                "tokenIn": summary.get("tokenIn"),
                "tokenOut": summary.get("tokenOut"),
                "amountIn": summary.get("amountIn"),
                "amountOut": summary.get("amountOut"),
                "slippage": summary.get("slippage"),
                "valueUsd": f"${trade_value_usd:.2f}",
            }
            
            if not request_trade_confirmation(trade_details, timeout_seconds=300):
                print("❌ Trade rejected or timeout")
                sys.exit(0)
            
            # Telegram 确认后自动补上确认短语，免去手动 --confirm
            if not args.confirm:
                from uniswap_autopilot.execute._internal.tx import build_confirmation_phrase
                args.confirm = build_confirmation_phrase(tx)
            
            response["telegramConfirmation"] = {
                "status": "approved",
                "valueUsd": f"${trade_value_usd:.2f}",
            }
        else:
            response["telegramConfirmation"] = {
                "status": "skipped",
                "reason": "below threshold",
                "valueUsd": f"${trade_value_usd:.2f}",
            }

        chain_key = (summary.get("chain") or tx.get("chainKey") or "").lower() or None
        wallet_addr = (
            (tx.get("from") if isinstance(tx, dict) else None)
            or summary.get("from")
            or summary.get("sender")
        )

        if args.broadcast:
            response["preflight"] = build_preflight_report(
                tx,
                explicit_rpc_url=args.rpc_url,
                strict=True,
            )
            log_event(
                event=EVENT_PREFLIGHT,
                chain=chain_key,
                wallet=wallet_addr,
                error_code=None if response["preflight"].get("ok") else "preflight_failed",
                details={"ok": response["preflight"].get("ok"), "issues": response["preflight"].get("issues")},
            )
            if response["preflight"].get("ok") is False:
                raise RuntimeError(
                    "broadcast preflight failed: " + "; ".join(response["preflight"].get("issues") or [])
                )
            broadcast = broadcast_with_backend(
                tx=tx,
                explicit_rpc_url=args.rpc_url,
                confirm=args.confirm,
                signer_args_source=args,
            )
            response["commandPreview"] = broadcast["commandPreview"]
            response["broadcastResult"] = broadcast["broadcastResult"]
            response["signerBackend"] = broadcast["signerBackend"]
            if broadcast.get("serviceDecision") is not None:
                response["serviceDecision"] = broadcast["serviceDecision"]
            response["transactionHash"] = extract_transaction_hash(response["broadcastResult"])
            log_event(
                event=EVENT_BROADCAST,
                chain=chain_key,
                wallet=wallet_addr,
                tx_hash=response.get("transactionHash"),
                details={
                    "signerBackend": broadcast["signerBackend"],
                    "tokenIn": tx.get("inputTokenSymbol"),
                    "tokenOut": tx.get("outputTokenSymbol"),
                    "amountIn": str(tx.get("inputAmount", "")),
                    "amountOut": str(tx.get("outputAmount", "")),
                },
            )
            response["receipt"] = execute_cast_receipt(
                tx_hash=response["transactionHash"],
                rpc_url=broadcast["rpcUrl"],
                confirmations=args.receipt_confirmations,
            )
            success = receipt_succeeded(response["receipt"])
            log_event(
                event=EVENT_CONFIRM,
                chain=chain_key,
                wallet=wallet_addr,
                tx_hash=response.get("transactionHash"),
                error_code=None if success else "receipt_failed",
                details={
                    "status": response["receipt"].get("status"),
                    "confirmations": args.receipt_confirmations,
                },
            )
            if not success:
                raise RuntimeError(f"broadcast receipt status is not successful: {response['receipt'].get('status')}")

        if args.output:
            Path(args.output).write_text(
                json.dumps(response, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        dump_json(response)
    except Exception as exc:  # noqa: BLE001
        log_event(
            event=EVENT_ERROR,
            chain=None,
            wallet=None,
            error_code=type(exc).__name__,
            details={"message": str(exc), "action": "execute_transaction"},
        )
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
