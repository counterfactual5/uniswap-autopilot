"""Transaction broadcast — pure Python signing + eth_sendRawTransaction.

Requires ``pip install uniswap-autopilot[signer]`` for the ``eth-account`` library.
"""

from __future__ import annotations

import argparse
from typing import Any

from uniswap_autopilot.execute._internal.rpc import resolve_rpc_url
from uniswap_autopilot.execute._internal.signer import (
    ensure_signer_backend,
    sign_and_broadcast,
)
from uniswap_autopilot.execute._internal.tx import build_confirmation_phrase


def build_broadcast_package(
    tx: dict[str, Any],
    explicit_rpc_url: str | None,
    confirm: str | None,
    signer_args_source: argparse.Namespace,
) -> dict[str, Any]:
    """Validate confirmation phrase and resolve RPC URL."""
    expected_confirmation = build_confirmation_phrase(tx)
    if confirm != expected_confirmation:
        raise ValueError(f"--confirm must exactly equal: {expected_confirmation}")

    rpc_url, rpc_candidates = resolve_rpc_url(explicit_rpc_url, tx["chainId"])
    if not rpc_url:
        raise RuntimeError(
            f"RPC URL is not configured; set one of {', '.join(rpc_candidates)} or pass --rpc-url"
        )
    return {
        "expectedConfirmation": expected_confirmation,
        "rpcUrl": rpc_url,
        "signerBackend": "pure-python",
    }


def broadcast_with_backend(
    tx: dict[str, Any],
    explicit_rpc_url: str | None,
    confirm: str | None,
    signer_args_source: argparse.Namespace,
    **kwargs: Any,
) -> dict[str, Any]:
    """Sign and broadcast a transaction.

    Returns dict with broadcastResult containing the transaction receipt.
    """
    ensure_signer_backend(signer_args_source, action_name="broadcast")
    package = build_broadcast_package(
        tx=tx,
        explicit_rpc_url=explicit_rpc_url,
        confirm=confirm,
        signer_args_source=signer_args_source,
    )
    return sign_and_broadcast(
        tx=tx,
        rpc_url=package["rpcUrl"],
        signer_args_source=signer_args_source,
    )


def extract_transaction_hash(broadcast_result: dict[str, Any]) -> str:
    """Extract transaction hash from broadcast result."""
    for field in ("transactionHash", "hash"):
        value = broadcast_result.get(field)
        if isinstance(value, str) and value.startswith("0x"):
            return value
    raise RuntimeError("broadcast result does not contain transactionHash")
