"""Transaction signing via eth-account (pure Python).

Private key is read from an environment variable — never stored on disk.
Requires the ``[signer]`` extra::

    pip install uniswap-autopilot[signer]
    export EXECUTOR_PRIVATE_KEY=0x...
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from uniswap_autopilot.common.common import load_local_env, resolve_wallet_address
from uniswap_autopilot.execute._internal.constants import (
    HOT_WALLET_PRIVATE_KEY_ENV_NAME,
)

# ── Pure Python signer (optional) ──────────────────────────────────────────

try:
    from uniswap_autopilot.execute._internal.pure_signer import (
        is_available as _pure_signer_available,
    )
except ImportError:

    def _pure_signer_available() -> bool:
        return False


# ── Namespace / arg helpers ────────────────────────────────────────────────


def has_direct_signer(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "private_key_env", None))


def build_signer_namespace(
    *,
    private_key_env: str | None = None,
    wallet_source: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        private_key_env=private_key_env,
        wallet_source=wallet_source,
    )


# ── Wallet resolution ─────────────────────────────────────────────────────


def resolve_wallet(args: argparse.Namespace | None) -> str | None:
    """Resolve wallet address from args or environment."""
    if args is not None and getattr(args, "private_key_env", None):
        from uniswap_autopilot.execute._internal.pure_signer import get_address

        addr = get_address(args.private_key_env)
        if addr:
            return addr
    return resolve_wallet_address(args)


# ── Backend detection ──────────────────────────────────────────────────────


def detect_hot_wallet_backend() -> dict[str, Any]:
    """Detect if pure-signer (eth-account) is available."""
    load_local_env()
    private_key_env = os.environ.get(HOT_WALLET_PRIVATE_KEY_ENV_NAME, "").strip() or None
    wallet = resolve_wallet_address(None, preference="hot")

    result: dict[str, Any] = {
        "backend": "pure-python",
        "configured": wallet is not None and private_key_env is not None,
        "available": False,
        "wallet": wallet,
        "walletFound": wallet is not None,
        "mode": None,
        "reason": None,
        "signerArgs": None,
    }
    if not wallet:
        result["reason"] = "WALLET_ADDRESS is not configured"
        return result
    if not private_key_env:
        result["reason"] = "Private key env var not configured"
        return result
    if not os.environ.get(private_key_env, "").strip():
        result["reason"] = f"{private_key_env} is not set"
        return result
    if not _pure_signer_available():
        result["reason"] = "eth-account not installed (pip install uniswap-autopilot[signer])"
        return result

    result["mode"] = "private-key-env"
    result["available"] = True
    result["signerArgs"] = build_signer_namespace(
        private_key_env=private_key_env,
        wallet_source="hot",
    )
    return result


def auto_select_signer_args(args: argparse.Namespace | None = None) -> argparse.Namespace | None:
    """Auto-select signer args from environment."""
    load_local_env()
    if args is not None:
        if has_direct_signer(args):
            return args

    # Try hot wallet backend
    hot = detect_hot_wallet_backend()
    if hot["available"]:
        return hot["signerArgs"]

    # Try secure wallet
    secure_wallet = resolve_wallet_address(None, preference="secure")
    if secure_wallet:
        pk_env = os.environ.get(HOT_WALLET_PRIVATE_KEY_ENV_NAME, "").strip() or None
        if pk_env and os.environ.get(pk_env, "").strip():
            return build_signer_namespace(
                private_key_env=pk_env,
                wallet_source="secure",
            )

    return None


def ensure_signer_backend(args: argparse.Namespace | None, action_name: str = "broadcast") -> str:
    """Ensure a signer is available. Returns 'pure-python'."""
    if args is not None and has_direct_signer(args):
        if _pure_signer_available():
            return "pure-python"
        raise RuntimeError(
            f"Cannot {action_name}: eth-account is not installed. Install with: pip install uniswap-autopilot[signer]"
        )

    # Try auto-select
    selected = auto_select_signer_args(args)
    if selected is not None:
        if _pure_signer_available():
            return "pure-python"
        raise RuntimeError(
            f"Cannot {action_name}: eth-account is not installed. Install with: pip install uniswap-autopilot[signer]"
        )

    raise ValueError(
        f"Cannot {action_name}: no signer configured. "
        "Set EXECUTOR_PRIVATE_KEY env var and install: pip install uniswap-autopilot[signer]"
    )


# ── Argument parsing ──────────────────────────────────────────────────────


def add_signer_arguments(parser: argparse.ArgumentParser) -> None:
    """Add signer-related arguments to an argparse parser."""
    parser.add_argument(
        "--private-key-env",
        default=HOT_WALLET_PRIVATE_KEY_ENV_NAME,
        help="Environment variable holding the hex private key (default: %(default)s)",
    )


# ── Signing API ────────────────────────────────────────────────────────────


def sign_typed_data_with_backend(
    typed_data_file: str,
    typed_data: dict[str, Any],
    tx: dict[str, Any],
    signer_args_source: argparse.Namespace,
) -> dict[str, Any]:
    """Sign EIP-712 typed data using pure Python signer."""
    pk_env = getattr(signer_args_source, "private_key_env", None)
    if not pk_env:
        raise RuntimeError("private_key_env not set on signer args")

    from uniswap_autopilot.execute._internal.pure_signer import sign_typed_data

    signature = sign_typed_data(typed_data, private_key_env=pk_env)
    return {
        "signature": signature,
        "signCommandPreview": f"pure_signer.sign_typed_data(env={pk_env})",
        "signerBackend": "pure-python",
    }


def sign_and_broadcast(
    tx: dict[str, Any],
    rpc_url: str,
    signer_args_source: argparse.Namespace,
) -> dict[str, Any]:
    """Sign a transaction and broadcast via eth_sendRawTransaction.

    Preflight gates (before signing):
      1. Wallet must have enough native balance for gas.
      2. Transaction must pass eth_estimateGas (dry-run simulation).
    """
    pk_env = getattr(signer_args_source, "private_key_env", None)
    if not pk_env:
        raise RuntimeError("private_key_env not set on signer args")

    from uniswap_autopilot.audit import EVENT_ERROR, log_event
    from uniswap_autopilot.execute._internal.pure_signer import (
        get_address,
        sign_transaction,
    )
    from uniswap_autopilot.execute._internal.rpc import (
        _json_rpc,
        estimate_transaction_gas,
        query_gas_price,
        query_native_balance,
    )

    # Resolve wallet address
    wallet = tx.get("from") or get_address(private_key_env=pk_env)
    if not wallet:
        raise RuntimeError("Cannot determine wallet address for preflight checks")

    run_id = os.environ.get("STAGEFORGE_RUN_ID") or os.environ.get("RUN_ID")
    chain_id = tx.get("chainId")

    # Gate 1: native balance must cover gas
    balance = query_native_balance(wallet, rpc_url)
    if balance == 0:
        log_event(
            event=EVENT_ERROR,
            chain=str(chain_id) if chain_id else None,
            wallet=wallet,
            run_id=run_id,
            error_code="no_gas",
            details={"native_balance": 0},
        )
        raise RuntimeError(f"Wallet {wallet} has zero native balance — cannot pay for gas.")

    # Gate 2: estimateGas simulation
    try:
        estimated = estimate_transaction_gas(tx, rpc_url)
    except Exception as exc:
        reason = str(getattr(exc, "args", [str(exc)])[0])
        log_event(
            event=EVENT_ERROR,
            chain=str(chain_id) if chain_id else None,
            wallet=wallet,
            run_id=run_id,
            error_code="simulation_failed",
            details={"to": tx.get("to"), "value": str(tx.get("value", 0)), "revert": reason},
        )
        raise RuntimeError(f"Gas estimation failed (tx would revert): {reason}") from exc

    # Optional: warn if balance < estimated * gas_price
    try:
        gas_price = query_gas_price(rpc_url)
        if balance < estimated * gas_price:
            log_event(
                event=EVENT_ERROR,
                chain=str(chain_id) if chain_id else None,
                wallet=wallet,
                run_id=run_id,
                error_code="insufficient_gas",
                details={
                    "native_balance": balance,
                    "estimated_gas": estimated,
                    "gas_price": gas_price,
                    "required": estimated * gas_price,
                },
            )
            raise RuntimeError(
                f"Wallet {wallet} balance ({balance}) insufficient for gas (estimate: {estimated} * price: {gas_price})"
            )
    except RuntimeError:
        raise
    except Exception:
        pass  # gas-price check is best-effort

    signed_hex = sign_transaction(tx, private_key_env=pk_env, chain_id=chain_id)
    tx_hash = _json_rpc("eth_sendRawTransaction", [signed_hex], rpc_url)

    # Wait briefly and get receipt
    import time

    time.sleep(1)
    receipt = _json_rpc("eth_getTransactionReceipt", [tx_hash], rpc_url)

    if receipt is None:
        broadcast_result = {"transactionHash": tx_hash, "status": "pending"}
    else:
        broadcast_result = receipt

    return {
        "commandPreview": f"pure_signer.sign_transaction + eth_sendRawTransaction (env={pk_env})",
        "rpcUrl": rpc_url,
        "broadcastResult": broadcast_result,
        "signerBackend": "pure-python",
    }
