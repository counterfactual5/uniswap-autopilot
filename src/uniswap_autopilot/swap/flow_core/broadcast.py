from __future__ import annotations

import argparse
from typing import Any

from uniswap_autopilot.execute import broadcast as execute_transaction


def maybe_broadcast(
    tx: dict[str, Any],
    explicit_rpc_url: str | None,
    confirm: str | None,
    signer_args_source: argparse.Namespace,
    receipt_confirmations: int,
    use_flashbots: bool = False,
) -> dict[str, Any]:
    broadcast = execute_transaction.broadcast_with_backend(
        tx=tx,
        explicit_rpc_url=explicit_rpc_url,
        confirm=confirm,
        signer_args_source=signer_args_source,
        use_flashbots=use_flashbots,
    )
    preflight = execute_transaction.build_preflight_report(
        tx=tx,
        explicit_rpc_url=broadcast["rpcUrl"],
        strict=True,
    )
    if not preflight.get("ok"):
        raise RuntimeError("broadcast preflight failed: " + "; ".join(preflight.get("issues") or []))
    broadcast_result = broadcast["broadcastResult"]
    transaction_hash = execute_transaction.extract_transaction_hash(broadcast_result)
    receipt = execute_transaction.execute_cast_receipt(
        tx_hash=transaction_hash,
        rpc_url=broadcast["rpcUrl"],
        confirmations=receipt_confirmations,
    )
    if not execute_transaction.receipt_succeeded(receipt):
        raise RuntimeError(f"broadcast receipt status is not successful: {receipt.get('status')}")
    response = {
        "commandPreview": broadcast["commandPreview"],
        "preflight": preflight,
        "broadcastResult": broadcast_result,
        "transactionHash": transaction_hash,
        "receipt": receipt,
        "signerBackend": broadcast["signerBackend"],
    }
    if broadcast.get("serviceDecision") is not None:
        response["serviceDecision"] = broadcast["serviceDecision"]
    return response
