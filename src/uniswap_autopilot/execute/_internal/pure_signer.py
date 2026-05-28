"""Pure Python transaction signing and broadcasting via eth-account.

This module provides the same interface as cast-based signing but uses
the ``eth-account`` library instead of the Foundry CLI.  Activated when
the ``[signer]`` extra is installed::

    pip install uniswap-autopilot[signer]

Private key is read from the environment variable specified by
``--private-key-env`` (default: ``EXECUTOR_PRIVATE_KEY``).
"""

from __future__ import annotations

import json
import os
from typing import Any

# eth-account is an optional dependency — import failures are handled gracefully.
try:
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    from eth_account.types import SignableMessage

    _HAS_ETH_ACCOUNT = True
except ImportError:
    _HAS_ETH_ACCOUNT = False


class SignerUnavailableError(RuntimeError):
    """Raised when eth-account is not installed."""


def is_available() -> bool:
    """Check if the pure-Python signer is available."""
    return _HAS_ETH_ACCOUNT


def _require_available():
    if not _HAS_ETH_ACCOUNT:
        raise SignerUnavailableError(
            "eth-account is not installed. "
            "Install it with: pip install uniswap-autopilot[signer]"
        )


def _get_private_key(env_name: str) -> str:
    """Read a hex private key from an environment variable."""
    key = os.environ.get(env_name, "").strip()
    if not key:
        raise RuntimeError(f"Private key not found in env var {env_name}")
    if not key.startswith("0x"):
        key = "0x" + key
    return key


def sign_typed_data(
    typed_data: dict[str, Any],
    private_key_env: str = "EXECUTOR_PRIVATE_KEY",
) -> str:
    """Sign EIP-712 typed data and return the signature hex string.

    Parameters
    ----------
    typed_data : dict
        The EIP-712 typed data object (domain, types, message).
    private_key_env : str
        Environment variable name holding the hex private key.

    Returns
    -------
    str
        Signature as a 0x-prefixed hex string.
    """
    _require_available()
    pk = _get_private_key(private_key_env)

    # Build the signable message from typed data components
    domain = typed_data.get("domain", {})
    types = typed_data.get("types", {})
    primary_type = typed_data.get("primaryType", "")
    message = typed_data.get("message", {})

    # Remove EIP712Domain from types if present (it's implicit)
    types_clean = {k: v for k, v in types.items() if k != "EIP712Domain"}

    signable = encode_typed_data(
        full_message={
            "types": types,
            "domain": domain,
            "primaryType": primary_type,
            "message": message,
        }
    )
    signed = Account.sign_message(signable, pk)
    return signed.signature.hex()


def sign_transaction(
    tx: dict[str, Any],
    private_key_env: str = "EXECUTOR_PRIVATE_KEY",
    chain_id: int | None = None,
) -> str:
    """Sign a legacy EVM transaction and return the signed raw tx hex.

    Parameters
    ----------
    tx : dict
        Transaction dict with keys: to, data, value, gas (or gasLimit), etc.
    private_key_env : str
        Environment variable name holding the hex private key.
    chain_id : int, optional
        Chain ID for EIP-155 replay protection.

    Returns
    -------
    str
        Signed raw transaction as 0x-prefixed hex string.
    """
    _require_available()
    pk = _get_private_key(private_key_env)

    # ``nonce`` MUST be supplied by the caller — silently defaulting to 0
    # historically allowed the wrong nonce to be signed when the upstream
    # pipeline forgot to attach one, leading to replacement-tx failures or,
    # worse, replaying an old nonce against the network. Fail fast instead.
    if "nonce" not in tx or tx["nonce"] is None:
        raise ValueError(
            "sign_transaction: 'nonce' is required. "
            "Fetch it from the RPC (e.g. eth_getTransactionCount(addr, 'pending')) "
            "and pass it explicitly — defaulting to 0 is unsafe."
        )
    nonce_raw = tx["nonce"]
    if isinstance(nonce_raw, int):
        nonce_int = nonce_raw
    else:
        nonce_int = int(str(nonce_raw), 0)
    if nonce_int < 0:
        raise ValueError(f"sign_transaction: nonce must be >= 0, got {nonce_int}")

    gas_limit = tx.get("gasLimit") or tx.get("gas") or tx.get("gas_limit", 210000)
    gas_price = tx.get("gasPrice") or tx.get("gas_price")
    max_fee = tx.get("maxFeePerGas")
    priority_fee = tx.get("maxPriorityFeePerGas")

    typed_tx: dict[str, Any] = {
        "nonce": nonce_int,
        "to": tx["to"],
        "data": tx["data"],
        "value": int(tx["value"], 0) if isinstance(tx["value"], str) else int(tx["value"]),
        "gas": int(gas_limit) if not isinstance(gas_limit, int) else gas_limit,
    }

    if chain_id is not None:
        typed_tx["chainId"] = chain_id

    # EIP-1559 if max_fee/priority_fee provided, else legacy
    if max_fee and priority_fee:
        typed_tx["maxFeePerGas"] = int(max_fee, 0) if isinstance(max_fee, str) else int(max_fee)
        typed_tx["maxPriorityFeePerGas"] = int(priority_fee, 0) if isinstance(priority_fee, str) else int(priority_fee)
    elif gas_price:
        typed_tx["gasPrice"] = int(gas_price, 0) if isinstance(gas_price, str) else int(gas_price)
    else:
        # Default to legacy with reasonable gas price
        typed_tx["gasPrice"] = 0

    signed = Account.sign_transaction(typed_tx, pk)
    return signed.raw_transaction.hex()


def get_address(private_key_env: str = "EXECUTOR_PRIVATE_KEY") -> str | None:
    """Derive the wallet address from the private key env var.

    Returns None if the env var is not set.
    """
    pk = os.environ.get(private_key_env, "").strip()
    if not pk:
        return None
    if not pk.startswith("0x"):
        pk = "0x" + pk
    try:
        return Account.from_key(pk).address
    except Exception:
        return None
