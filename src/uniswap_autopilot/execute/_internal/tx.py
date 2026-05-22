from __future__ import annotations

import json
import shlex
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from uniswap_autopilot.common.common import validate_address
from uniswap_autopilot.execute._internal.constants import APPROVE_SELECTOR, CHAIN_BY_ID, HEX_DATA_RE
from uniswap_autopilot.execute._internal.rpc import resolve_rpc_url

def load_json(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))

def parse_intish(value: Any, field_name: str) -> int | None:
    if value in (None, "", "null"):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must not be boolean")
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"{field_name} must be non-negative")
        return value
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            parsed = int(cleaned, 0)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an int or hex string") from exc
        if parsed < 0:
            raise ValueError(f"{field_name} must be non-negative")
        return parsed
    raise ValueError(f"{field_name} must be an int or string")

def validate_hex_data(value: Any, field_name: str = "data") -> str:
    if not isinstance(value, str) or not HEX_DATA_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a 0x-prefixed hex string")
    if value == "0x":
        raise ValueError(f"{field_name} must not be empty")
    return value

def optional_address(value: Any) -> str | None:
    if value in (None, "", "null"):
        return None
    try:
        return validate_address(value, "address")
    except ValueError:
        return None

def lookup_token_symbol(chain_id: int, address: str | None) -> str | None:
    if not address:
        return None
    chain = CHAIN_BY_ID.get(chain_id)
    if not chain:
        return None
    normalized = address.lower()
    for symbol, token in chain.tokens.items():
        if token.address.lower() == normalized:
            return symbol
    return None

def lookup_token_decimals(
    chain_id: int,
    token_symbol: str | None = None,
    token_address: str | None = None,
) -> int:
    chain = CHAIN_BY_ID.get(chain_id)
    if not chain:
        return 18
    if token_symbol == "NATIVE":
        return 18
    if token_symbol:
        token = chain.tokens.get(str(token_symbol).upper())
        if token:
            return token.decimals
    if token_address:
        normalized = token_address.lower()
        for token in chain.tokens.values():
            if token.address.lower() == normalized:
                return token.decimals
    return 18

def base_units_to_human(amount: Any, decimals: int) -> str:
    raw = parse_intish(amount, "amount")
    if raw is None:
        return "0"
    scaled = Decimal(raw) / (Decimal(10) ** decimals)
    text = format(scaled, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"

def decode_erc20_approve_call(data: str) -> dict[str, str] | None:
    validated = validate_hex_data(data, "data")
    if not validated.startswith(APPROVE_SELECTOR):
        return None
    payload = validated[10:]
    if len(payload) < 128:
        return None
    spender_word = payload[:64]
    amount_word = payload[64:128]
    spender = optional_address(f"0x{spender_word[-40:]}")
    if not spender:
        return None
    return {
        "spender": spender,
        "amount": str(int(amount_word, 16)),
    }

def normalize_transaction(
    tx: dict[str, Any],
    tx_kind: str,
    source_file: str,
    source_kind: str,
    native_input: bool = False,
) -> dict[str, Any]:
    if not isinstance(tx, dict):
        raise ValueError("transaction payload must be an object")

    chain_id = parse_intish(tx.get("chainId"), "chainId")
    if chain_id is None:
        raise ValueError("chainId is required")

    gas_limit = parse_intish(tx.get("gasLimit"), "gasLimit")
    gas_price = parse_intish(tx.get("gasPrice"), "gasPrice")
    value_int = parse_intish(tx.get("value"), "value")
    normalized = {
        "kind": tx_kind,
        "sourceKind": source_kind,
        "sourceFile": source_file,
        "chainId": chain_id,
        "chainKey": (CHAIN_BY_ID.get(chain_id).key if chain_id in CHAIN_BY_ID else None),
        "to": validate_address(tx.get("to"), "to"),
        "from": validate_address(tx.get("from"), "from") if tx.get("from") else None,
        "data": validate_hex_data(tx.get("data"), "data"),
        "value": str(value_int or 0),
        "gasLimit": (str(gas_limit) if gas_limit is not None else None),
        "gasPrice": (str(gas_price) if gas_price is not None else None),
        "nativeInput": native_input,
    }
    if native_input and value_int == 0:
        raise ValueError("native-input swap requires non-zero tx.value")
    return normalized

def load_swap_transaction(path: str) -> dict[str, Any]:
    blob = load_json(path)
    swap_response = blob.get("swapResponse") or {}
    swap = swap_response.get("swap")
    if not swap:
        raise ValueError("swap file must contain swapResponse.swap")
    request_payload = blob.get("requestPayload") or {}
    quote = request_payload.get("quote") or {}
    input_token = (quote.get("input") or {}).get("token")
    native_input = str(input_token or "").lower() == "0x0000000000000000000000000000000000000000"
    normalized = normalize_transaction(
        tx=swap,
        tx_kind="swap",
        source_file=path,
        source_kind="swap-file",
        native_input=native_input,
    )
    normalized["requestId"] = swap_response.get("requestId")
    normalized["gasFee"] = swap_response.get("gasFee")
    normalized["apiSignature"] = swap_response.get("signature")
    quote_input = quote.get("input") or {}
    quote_output = quote.get("output") or {}
    normalized["inputToken"] = optional_address(quote_input.get("token"))
    normalized["inputTokenSymbol"] = (
        "NATIVE"
        if native_input
        else lookup_token_symbol(chain_id=normalized["chainId"], address=normalized["inputToken"])
    )
    normalized["inputAmount"] = (
        str(parse_intish(quote_input.get("amount"), "quote.input.amount"))
        if quote_input.get("amount") not in (None, "", "null")
        else None
    )
    normalized["outputToken"] = optional_address(quote_output.get("token"))
    normalized["outputTokenSymbol"] = lookup_token_symbol(
        chain_id=normalized["chainId"],
        address=normalized["outputToken"],
    )
    normalized["outputAmount"] = (
        str(parse_intish(quote_output.get("amount"), "quote.output.amount"))
        if quote_output.get("amount") not in (None, "", "null")
        else None
    )
    permit_data = request_payload.get("permitData") or {}
    permit_domain = permit_data.get("domain") or {}
    permit_values = permit_data.get("values") or {}
    normalized["permitVerifier"] = optional_address(permit_domain.get("verifyingContract"))
    normalized["permitSpender"] = optional_address(permit_values.get("spender"))
    return normalized

def load_approval_transaction(path: str) -> dict[str, Any]:
    blob = load_json(path)
    approval_check = blob.get("approvalCheck") or {}
    approval = approval_check.get("approval")
    if not approval:
        raise ValueError("quote file must contain approvalCheck.approval")
    normalized = normalize_transaction(
        tx=approval,
        tx_kind="approval",
        source_file=path,
        source_kind="quote-file",
    )
    normalized["requestId"] = approval_check.get("requestId")
    request_payload = ((blob.get("requestPayloads") or {}).get("checkApproval")) or {}
    decoded_approval = decode_erc20_approve_call(normalized["data"])
    normalized["approvalToken"] = normalized["to"]
    normalized["approvalSpender"] = (
        decoded_approval.get("spender") if decoded_approval else None
    )
    normalized["approvalAmount"] = (
        decoded_approval.get("amount") if decoded_approval else None
    )
    normalized["requiredAllowance"] = (
        str(parse_intish(request_payload.get("amount"), "checkApproval.amount"))
        if request_payload.get("amount") not in (None, "", "null")
        else normalized["approvalAmount"]
    )
    raw_quote = blob.get("rawQuote") or {}
    quote = raw_quote.get("quote") or {}
    quote_input = quote.get("input") or {}
    quote_output = quote.get("output") or {}
    quote_input_meta = blob.get("tokenIn") or {}
    quote_output_meta = blob.get("tokenOut") or {}
    normalized["inputToken"] = optional_address(quote_input.get("token")) or normalized["approvalToken"]
    normalized["inputTokenSymbol"] = (
        quote_input_meta.get("symbol")
        or lookup_token_symbol(chain_id=normalized["chainId"], address=normalized["inputToken"])
    )
    normalized["inputAmount"] = (
        str(parse_intish(quote_input.get("amount"), "quote.input.amount"))
        if quote_input.get("amount") not in (None, "", "null")
        else normalized["requiredAllowance"]
    )
    normalized["outputToken"] = optional_address(quote_output.get("token"))
    normalized["outputTokenSymbol"] = (
        quote_output_meta.get("symbol")
        or lookup_token_symbol(chain_id=normalized["chainId"], address=normalized["outputToken"])
    )
    normalized["outputAmount"] = (
        str(parse_intish(quote_output.get("amount"), "quote.output.amount"))
        if quote_output.get("amount") not in (None, "", "null")
        else None
    )
    return normalized

def build_confirmation_phrase(tx: dict[str, Any]) -> str:
    return f"BROADCAST {tx['kind'].upper()} {tx['chainId']} {tx['to'].lower()}"

def summarize_transaction(tx: dict[str, Any], rpc_url: str | None, rpc_candidates: list[str]) -> dict[str, Any]:
    summary = {
        "kind": tx["kind"],
        "chain": {"chainId": tx["chainId"], "key": tx["chainKey"]},
        "from": tx["from"],
        "to": tx["to"],
        "value": tx["value"],
        "gasLimit": tx["gasLimit"],
        "gasPrice": tx["gasPrice"],
        "dataBytes": (len(tx["data"]) - 2) // 2,
        "sourceKind": tx["sourceKind"],
        "sourceFile": tx["sourceFile"],
        "rpcUrlResolved": rpc_url,
        "rpcEnvCandidates": rpc_candidates,
        "requestId": tx.get("requestId"),
        "confirmation": build_confirmation_phrase(tx),
    }
    if tx["kind"] == "swap":
        summary["nativeInput"] = tx["nativeInput"]
        summary["gasFee"] = tx.get("gasFee")
    return summary

def build_execute_preview(tx: dict[str, Any], explicit_rpc_url: str | None = None) -> dict[str, Any]:
    rpc_url, rpc_candidates = resolve_rpc_url(explicit_rpc_url, tx["chainId"])
    summary = summarize_transaction(tx, rpc_url, rpc_candidates)
    preview_rpc_url = rpc_url or "<unresolved-rpc-url>"
    return {
        "summary": summary,
        "commandPreview": f"pure_signer.sign_transaction + eth_sendRawTransaction (rpc={preview_rpc_url})",
    }


# Price feed integration


def _get_price_feed():
    """Lazy import to avoid circular deps."""
    from price_feed import get_price, get_prices_batch, get_eth_price
    return get_price, get_prices_batch, get_eth_price


def estimate_trade_value_usd(summary: dict) -> float:
    """Estimate trade value in USD from transaction summary.

    Strategy (priority order):
    1. Stablecoin side → quote 金额即 USD
    2. ETH pair → quote ETH 数量 × 实时 ETH 价格（price-feed 多源）
    3. Non-ETH pair → price-feed 批量获取双边价格，拿到任一即返回
    4. 全失败 → 强制确认
    """
    get_price, get_prices_batch, get_eth_price = _get_price_feed()

    token_in = (summary.get("tokenIn") or "").upper()
    token_out = (summary.get("tokenOut") or "").upper()
    stablecoins = {"USDC", "USDT", "DAI", "USDC.E", "USDT.E"}
    eth_like = {"ETH", "WETH", "NATIVE"}

    chain = (summary.get("chain") or "base").lower()

    # ── 1. Stablecoin side → quote 金额即 USD ──
    if token_in in stablecoins:
        raw = summary.get("amountIn", "0")
        try:
            return float(str(raw).split()[0])
        except (ValueError, IndexError):
            return float("inf")
    if token_out in stablecoins:
        raw = summary.get("amountOut", "0")
        try:
            return float(str(raw).split()[0])
        except (ValueError, IndexError):
            return float("inf")

    # ── 2. ETH pair → quote ETH 数量 × ETH 价格 ──
    eth_amount = None
    if token_in in eth_like:
        raw = summary.get("amountIn", "0")
        try:
            eth_amount = float(str(raw).split()[0])
        except (ValueError, IndexError):
            pass
    elif token_out in eth_like:
        raw = summary.get("amountOut", "0")
        try:
            eth_amount = float(str(raw).split()[0])
        except (ValueError, IndexError):
            pass

    if eth_amount is not None:
        eth_price = get_eth_price(chain, tier="fast")
        if eth_price is not None:
            return eth_amount * eth_price

    # ── 3. Non-ETH, non-stablecoin pair → price-feed 批量获取 ──
    addr_in = summary.get("inputToken", "")
    addr_out = summary.get("outputToken", "")

    requests = []
    for addr, raw_amt in [(addr_in, summary.get("amountIn", "0")), (addr_out, summary.get("amountOut", "0"))]:
        if addr and addr.startswith("0x") and len(addr) == 42:
            requests.append((chain, addr, raw_amt))

    if requests:
        fetch_list = [(c, a) for c, a, _ in requests]
        results = get_prices_batch(fetch_list, tier="fast")
        for chain_r, addr, raw_amt in requests:
            key = f"{chain_r}:{addr.lower()}"
            if key in results:
                try:
                    amount = float(str(raw_amt).split()[0])
                    return amount * results[key]["price"]
                except (ValueError, IndexError):
                    pass

    # ── 4. 全失败 → 强制确认 ──
    return float("inf")
