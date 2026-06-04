"""EVM RPC utilities — pure JSON-RPC over urllib, no external CLI dependencies.

All chain interactions (balance, allowance, gas, receipts) use standard
JSON-RPC calls.  Falls back to ``cast`` (Foundry) only when explicitly
requested via the ``CAST_EXECUTABLE`` environment variable.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from typing import Any
from urllib.request import Request, urlopen

from uniswap_autopilot.execute._internal.constants import CHAIN_BY_ID, GLOBAL_RPC_ENV_CANDIDATES


# ── JSON-RPC helpers ────────────────────────────────────────────────────────

def _json_rpc(method: str, params: list[Any], rpc_url: str, timeout: int = 30) -> Any:
    """Send a JSON-RPC request and return the ``result`` field."""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }).encode("utf-8")
    req = Request(rpc_url, data=payload, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read())
    if body.get("error"):
        raise RuntimeError(f"RPC error: {body['error']}")
    return body.get("result")


def _decode_int(hex_or_int: Any) -> int:
    """Decode a hex string or int from RPC response."""
    if isinstance(hex_or_int, int):
        return hex_or_int
    if isinstance(hex_or_int, str):
        return int(hex_or_int, 0)
    raise ValueError(f"Cannot decode integer from {type(hex_or_int)}: {hex_or_int}")


# ── ERC-20 ABI selectors ───────────────────────────────────────────────────

_ERC20_BALANCE_OF = "0x70a08231"  # balanceOf(address)
_ERC20_ALLOWANCE = "0xdd62ed3e"   # allowance(address,address)


def _encode_address(addr: str) -> str:
    """Left-pad address to 32 bytes."""
    return addr.lower().replace("0x", "").rjust(64, "0")


def _encode_uint256(val: int) -> str:
    """Encode uint256 to 32-byte hex."""
    return hex(val)[2:].rjust(64, "0")


# ── cast CLI fallback ──────────────────────────────────────────────────────

def _has_cast() -> bool:
    """Check if ``cast`` (Foundry) is available."""
    return bool(os.environ.get("CAST_EXECUTABLE", "")) or _which("cast")


def _which(name: str) -> bool:
    """Check if a command exists on PATH."""
    try:
        subprocess.run(["which", name], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def run_cast_text(command: list[str]) -> str:
    """Run a ``cast`` command and return stdout.  Requires Foundry."""
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    if completed.returncode != 0:
        raise RuntimeError(stderr or stdout or f"{shlex.join(command)} failed with exit code {completed.returncode}")
    if not stdout:
        raise RuntimeError(f"{shlex.join(command)} returned empty stdout")
    return stdout


def parse_cast_int_output(stdout: str, field_name: str) -> int:
    value = stdout.strip().splitlines()[-1].strip()
    if value.startswith('"') and value.endswith('"'):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
    if isinstance(value, str) and " [" in value:
        value = value.split(" [", 1)[0].strip()
    try:
        return int(value, 0)
    except ValueError as exc:
        raise RuntimeError(f"{field_name} returned non-integer output: {stdout}") from exc


# ── Public API (cast-free by default) ─────────────────────────────────────

def query_native_balance(address: str, rpc_url: str) -> int:
    """Query native token balance (wei) via ``eth_getBalance``."""
    result = _json_rpc("eth_getBalance", [address, "latest"], rpc_url)
    return _decode_int(result)


def query_erc20_balance(owner: str, token: str, rpc_url: str) -> int:
    """Query ERC-20 balance via ``eth_call``."""
    data = _ERC20_BALANCE_OF + _encode_address(owner)
    result = _json_rpc("eth_call", [{"to": token, "data": "0x" + data}, "latest"], rpc_url)
    return _decode_int(result)


def query_erc20_allowance(token: str, owner: str, spender: str, rpc_url: str) -> int:
    """Query ERC-20 allowance via ``eth_call``."""
    data = _ERC20_ALLOWANCE + _encode_address(owner) + _encode_address(spender)
    result = _json_rpc("eth_call", [{"to": token, "data": "0x" + data}, "latest"], rpc_url)
    return _decode_int(result)


def estimate_transaction_gas(tx: dict[str, Any], rpc_url: str) -> int:
    """Estimate gas for a transaction via ``eth_estimateGas``."""
    params: dict[str, str] = {
        "to": tx["to"],
        "data": tx["data"],
        "value": hex(int(tx["value"])),
    }
    if tx.get("from"):
        params["from"] = tx["from"]
    result = _json_rpc("eth_estimateGas", [params], rpc_url)
    return _decode_int(result)


def query_gas_price(rpc_url: str) -> int:
    """Query current gas price via ``eth_gasPrice``."""
    result = _json_rpc("eth_gasPrice", [], rpc_url)
    return _decode_int(result)


def execute_cast_receipt(
    tx_hash: str,
    rpc_url: str,
    confirmations: int = 1,
    *,
    poll_interval: float = 2.0,
    max_wait_seconds: float = 300.0,
) -> dict[str, Any]:
    """Wait for a transaction receipt and ``confirmations`` additional blocks.

    Polls ``eth_getTransactionReceipt`` until the tx is mined, then waits
    until ``current_block >= receipt.blockNumber + (confirmations - 1)``.
    Previously this function fetched the receipt once and ignored the
    ``confirmations`` argument entirely, which silently lied to callers
    that thought they were waiting for finality.

    Raises ``TimeoutError`` if the receipt is not seen within
    ``max_wait_seconds``.
    """
    import time as _time

    if confirmations < 1:
        confirmations = 1

    deadline = _time.monotonic() + max_wait_seconds
    receipt: dict[str, Any] | None = None
    while _time.monotonic() < deadline:
        result = _json_rpc("eth_getTransactionReceipt", [tx_hash], rpc_url)
        if result is not None:
            receipt = result
            break
        _time.sleep(poll_interval)

    if receipt is None:
        raise TimeoutError(f"Receipt for tx {tx_hash} not seen within {max_wait_seconds}s")

    if confirmations == 1:
        return receipt

    receipt_block_raw = receipt.get("blockNumber")
    if receipt_block_raw is None:
        return receipt
    receipt_block = _decode_int(receipt_block_raw)

    while _time.monotonic() < deadline:
        head = _decode_int(_json_rpc("eth_blockNumber", [], rpc_url))
        if head - receipt_block + 1 >= confirmations:
            return receipt
        _time.sleep(poll_interval)

    raise TimeoutError(
        f"Tx {tx_hash} mined at block {receipt_block} but only saw "
        f"{head - receipt_block + 1} confirmation(s) within {max_wait_seconds}s "
        f"(requested {confirmations})"
    )


def receipt_succeeded(receipt: dict[str, Any]) -> bool:
    """Check if a receipt indicates success."""
    status = receipt.get("status")
    if isinstance(status, str):
        return status.lower() in {"0x1", "1"}
    if isinstance(status, int):
        return status == 1
    return False


# ── RPC URL resolution ────────────────────────────────────────────────────

def rpc_env_candidates(chain_id: int) -> list[str]:
    chain = CHAIN_BY_ID.get(chain_id)
    candidates: list[str] = []
    if chain:
        key = chain.key.upper()
        candidates.extend([
            f"{key}_RPC_URL",
            f"RPC_URL_{key}",
            f"{key}_MAINNET_RPC_URL",
        ])
    candidates.extend(GLOBAL_RPC_ENV_CANDIDATES)
    deduped: list[str] = []
    for candidate in candidates:
        if candidate not in deduped:
            deduped.append(candidate)
    return deduped


def resolve_rpc_url(explicit_rpc_url: str | None, chain_id: int) -> tuple[str | None, list[str]]:
    """Resolve RPC URL from explicit value or environment variables."""
    if explicit_rpc_url:
        return explicit_rpc_url, []
    candidates = rpc_env_candidates(chain_id)
    for env_name in candidates:
        value = os.environ.get(env_name)
        if value:
            return value, candidates
    return None, candidates


# ── High-level eth_call wrapper ──────────────────────────────────────────

def eth_call(to: str, data: str, rpc_url: str, block: str = "latest") -> str:
    """Make an ``eth_call`` and return the raw hex result string.

    Parameters
    ----------
    to : str
        Contract address (0x-prefixed).
    data : str
        ABI-encoded call data (4-byte selector + encoded args, 0x-prefixed).
    rpc_url : str
        JSON-RPC endpoint.
    block : str
        Block tag (default ``"latest"``).

    Returns
    -------
    str
        Raw hex return value from the contract (``0x`` prefixed).
    """
    result = _json_rpc("eth_call", [{"to": to, "data": data}, block], rpc_url)
    if result is None:
        raise RuntimeError(f"eth_call to {to} returned null (contract may have reverted)")
    return result


# ── Minimal ABI encoding (pure Python, no deps) ──────────────────────────

# keccak256 would be ideal but requires hashlib which supports it on Python 3.6+
# For the selectors we need, we hardcode them (they're standard ERC function signatures).

# Common function selectors (keccak256 first 4 bytes)
_SELECTORS: dict[str, str] = {
    "decimals()": "0x313ce567",
    "symbol()": "0x95d89b41",
    "name()": "0x06fdde03",
    "totalSupply()": "0x18160ddd",
    "balanceOf(address)": "0x70a08231",
    "allowance(address,address)": "0xdd62ed3e",
    "approve(address,uint256)": "0x095ea7b3",
    "transfer(address,uint256)": "0xa9059cbb",
    "transferFrom(address,address,uint256)": "0x23b872dd",
    # WETH
    "withdraw(uint256)": "0x2e1a7d4d",
    "deposit()": "0xd0e30db0",
    # Uniswap V2
    "getPair(address,address)": "0xe6a43905",
    "factory()": "0xc45a0155",
    "token0()": "0x0dfe1681",
    "token1()": "0xd21220a7",
    "getReserves()": "0x0902f1ac",
    "mint(address)": "0x6c904f02",
    "burn(address)": "0xf429f9e7",
    "swap(uint256,uint256,address,bytes)": "0x022c0d9f",
    "skim(address)": "0xbc25cf77",
    "sync()": "0xfff6cae9",
    # Uniswap V3 NonfungiblePositionManager
    "positions(uint256)": "0x99fbab88",
    "tokenOfOwnerByIndex(address,uint256)": "0x2f745c59",
    # Uniswap V3 Pool
    "getPool(address,address,uint24)": "0x1698ee82",
    "slot0()": "0x3850c7bd",
    "liquidity()": "0x455a4812",
    "feeGrowthGlobal0X128()": "0xf3058399",
    "feeGrowthGlobal1X128()": "0x463e4d99",
    "tickSpacing()": "0xd0c93a7c",
    # Uniswap V3 actions
    "mint((address,address,uint24,int24,int24,uint256,uint256,uint256,uint256,address,uint256))": "0x88316456",
    "increaseLiquidity((uint256,uint256,uint256,uint256,uint256,uint256))": "0x219f5d17",
    "decreaseLiquidity((uint256,uint128,uint256,uint256,uint256))": "0x0c7e2c76",
    "collect((uint256,address,uint128,uint128))": "0xfc6f7865",
    "burn(uint256)": "0x42966c68",
    # Multicall
    "multicall(bytes32,bytes[])": "0xac9650d8",
    "multicall(uint256,bytes[])": "0x5ae401dc",
    # Uniswap V4
    "getPoolId(address,address,address,bytes32)": None,  # needs dynamic encoding
    "getSlot0(bytes32)": None,
    "getPosition(bytes32,address,int24,int24)": None,
    "getLiquidity(bytes32)": None,
    "currency0()": "0x3fc8cef3",
    "currency1()": "0x8ee59feb",
    "fee()": "0xddca3f43",
    "hooks()": "0xb7b04879",
    "parameters()": "0x6c2e9c33",
    "poolManager()": "0x3a98ef39",
    "nextTokenId()": "0x800f9a5b",
    "modifyLiquidities(bytes,uint256)": "0x0b22dd98",
    "settle()": "0x4b309806",
    "take(address,address,uint256)": "0xa5d7c38c",
    "clear(address,uint256,bool)": "0x524b9e89",
    "sweep(address,address,uint256)": "0x49df728c",
    "settleFor(address)": "0x2c398a6c",
    "closeToken(uint256,address,address,bytes32)": None,
    # V2 Router
    "addLiquidity(address,address,uint256,uint256,uint256,uint256,address,uint256)": "0xe8e33700",
    "removeLiquidity(address,address,uint256,uint256,uint256,address,uint256)": "0x21959967",
    # V4 Position Manager
    "getPositionLiquidity(uint256)": "0x2e3b7a7e",
    "getPoolAndPositionInfo(uint256)": "0x3af6b530",
}


def encode_selector(signature: str) -> str:
    """Return the 4-byte function selector for a given Solidity signature.

    For standard signatures the selector is looked up from a built-in table.
    For unknown signatures, a :pyexc:`ValueError` is raised.
    """
    sel = _SELECTORS.get(signature)
    if sel is None:
        raise ValueError(f"Unknown function selector for '{signature}'. Add it to _SELECTORS or use a keccak library.")
    return sel


def encode_address(addr: str) -> str:
    """Left-pad address to 32 bytes (no 0x prefix)."""
    return addr.lower().replace("0x", "").rjust(64, "0")


def encode_uint(val: int) -> str:
    """Encode uint256 to 32-byte hex (no 0x prefix)."""
    return hex(val)[2:].rjust(64, "0")


def encode_int(val: int) -> str:
    """Encode int256 to 32-byte hex (no 0x prefix)."""
    if val >= 0:
        return hex(val)[2:].rjust(64, "0")
    # Two's complement for negative
    return hex(val + (1 << 256))[2:].rjust(64, "0")


def encode_bytes32(b: str) -> str:
    """Encode a bytes32 hex value (no 0x prefix)."""
    return b.replace("0x", "").rjust(64, "0")


def decode_uint(hex_val: str) -> int:
    """Decode a uint256 from hex."""
    return int(hex_val, 16) if isinstance(hex_val, str) else int(hex_val)


def decode_int256(hex_val: str) -> int:
    """Decode an int256 from hex (handles negative)."""
    val = int(hex_val, 16) if isinstance(hex_val, str) else int(hex_val)
    if val >= (1 << 255):
        val -= (1 << 256)
    return val


def decode_address(hex_val: str) -> str:
    """Decode an address from a 32-byte hex return value."""
    clean = hex_val.replace("0x", "")[-40:]
    return "0x" + clean


def decode_string(hex_val: str) -> str:
    """Decode a dynamic string from ABI-encoded hex return."""
    clean = hex_val.replace("0x", "")
    if len(clean) < 128:
        return ""
    # First 32 bytes = offset (should be 0x20 for basic string)
    # Next 32 bytes = length
    length = int(clean[64:128], 16)
    # Then the string bytes
    hex_str = clean[128:128 + length * 2]
    return bytes.fromhex(hex_str).decode("utf-8", errors="replace")


def read_erc20_decimals(token_address: str, rpc_url: str) -> int:
    """Read ERC-20 decimals() from chain."""
    data = _SELECTORS["decimals()"] 
    raw = eth_call(token_address, data, rpc_url)
    return decode_uint(raw)


def read_erc20_symbol(token_address: str, rpc_url: str) -> str:
    """Read ERC-20 symbol() from chain."""
    data = _SELECTORS["symbol()"]
    raw = eth_call(token_address, data, rpc_url)
    try:
        return decode_string(raw)
    except Exception:
        return raw.replace("0x", "")[:8]  # fallback for short symbol


def build_calldata(selector_or_sig: str, *args: str) -> str:
    """Build calldata from a function selector/signature and hex-encoded arguments.

    Parameters
    ----------
    selector_or_sig : str
        Either a 0x-prefixed 4-byte selector, or a Solidity signature like
        ``"approve(address,uint256)"``.
    *args : str
        ABI-encoded argument values as hex strings (32 bytes each, no 0x prefix).

    Returns
    -------
    str
        Full calldata (0x-prefixed).
    """
    if selector_or_sig.startswith("0x") and len(selector_or_sig) == 10:
        selector = selector_or_sig
    else:
        selector = encode_selector(selector_or_sig)
    return "0x" + selector.replace("0x", "") + "".join(a.replace("0x", "") for a in args)


def eth_fee_history(block_count: int, newest_block: str, rpc_url: str, reward_percentiles: list[int] | None = None) -> dict[str, Any]:
    """Call eth_feeHistory."""
    params = [hex(block_count), newest_block]
    if reward_percentiles is not None:
        params.append(reward_percentiles)
    return _json_rpc("eth_feeHistory", params, rpc_url)
