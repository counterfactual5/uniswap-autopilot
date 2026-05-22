#!/usr/bin/env python3
"""Detect signing backend availability."""
from __future__ import annotations

import argparse

from uniswap_autopilot.common.common import dump_json, load_local_env
from uniswap_autopilot.execute._internal.signer import detect_hot_wallet_backend


def sanitize_backend_status(status: dict[str, object]) -> dict[str, object]:
    sanitized = dict(status)
    signer_args = sanitized.pop("signerArgs", None)
    if signer_args is None:
        return sanitized

    mode = None
    wallet_source = None
    if getattr(signer_args, "private_key_env", None):
        mode = "private-key-env"
    wallet_source = getattr(signer_args, "wallet_source", None)

    sanitized["signerConfig"] = {
        "mode": mode,
        "walletSource": wallet_source,
    }
    return sanitized


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect signing backend availability")
    args = parser.parse_args()

    load_local_env()
    hot = sanitize_backend_status(detect_hot_wallet_backend())

    selected_backend = None
    selected_wallet = None
    if hot.get("available"):
        selected_backend = "pure-python"
        selected_wallet = hot.get("wallet")

    dump_json({
        "action": "detect_execution_context",
        "signer": hot,
        "selectedBackend": selected_backend,
        "selectedWallet": selected_wallet,
    })


if __name__ == "__main__":
    main()
