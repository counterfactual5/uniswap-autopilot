from __future__ import annotations

from pathlib import Path
import time
from typing import Any

from uniswap_autopilot.common.common import normalize_chain
from uniswap_autopilot.execute import broadcast as execute_transaction

NATIVE_CURRENCY_ADDRESS = "0x0000000000000000000000000000000000000000"
STALE_QUOTE_SECONDS = 600


def append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def parse_intish(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        base = 16 if cleaned.lower().startswith("0x") else 10
        return int(cleaned, base)
    return int(value)


def extract_permit_sig_deadline(raw_quote: dict[str, Any]) -> int | None:
    permit_values = (raw_quote.get("permitData") or {}).get("values") or {}
    return parse_intish(permit_values.get("sigDeadline"))


def quote_age_seconds(path: Path) -> int | None:
    if not path.exists():
        return None
    return max(0, int(time.time() - path.stat().st_mtime))


def classify_swap_failure(
    *,
    error_text: str,
    raw_quote: dict[str, Any],
    wallet: str,
    chain_name: str,
    explicit_rpc_url: str | None,
    quote_path: Path,
    reused_saved_quote: bool,
    has_signature: bool,
    approval_required: bool,
    assume_approval_ready: bool,
) -> dict[str, Any]:
    error_lower = error_text.lower()
    category = "swap_request_failed"
    if "transfer_from_failed" in error_lower:
        category = "transfer_from_failed"
    elif "failed_to_estimate_gas" in error_lower or "execution reverted" in error_lower:
        category = "simulation_reverted"

    issues: list[str] = []
    next_actions: list[str] = []
    checks: dict[str, Any] = {}

    now = int(time.time())
    sig_deadline = extract_permit_sig_deadline(raw_quote)
    if sig_deadline is not None:
        checks["permitSigDeadline"] = sig_deadline
        checks["permitSigDeadlineExpired"] = sig_deadline <= now
        if sig_deadline <= now:
            category = "permit_signature_expired"
            append_unique(issues, "permitData.sigDeadline has passed")
            append_unique(next_actions, "refresh-quote")
            append_unique(next_actions, "re-sign-permit")

    if reused_saved_quote:
        age = quote_age_seconds(quote_path)
        checks["quoteAgeSeconds"] = age
        if age is not None and age > STALE_QUOTE_SECONDS:
            append_unique(issues, f"reused quote is stale ({age}s old)")
            append_unique(next_actions, "refresh-quote")
            if has_signature:
                append_unique(next_actions, "re-sign-permit")

    chain = normalize_chain(chain_name)
    rpc_url, rpc_candidates = execute_transaction.resolve_rpc_url(explicit_rpc_url, chain.chain_id)
    checks["rpcUrlResolved"] = rpc_url
    checks["rpcEnvCandidates"] = rpc_candidates
    if not rpc_url:
        append_unique(issues, "RPC URL is not configured, cannot verify balance/allowance diagnostics")
        return {
            "category": category,
            "issues": issues,
            "nextActions": next_actions,
            "checks": checks,
        }

    quote_blob = raw_quote.get("quote") or {}
    quote_input = quote_blob.get("input") or {}
    input_token = str(quote_input.get("token") or "")
    input_amount = parse_intish(quote_input.get("amount"))
    gas_fee = parse_intish(quote_blob.get("gasFee")) or 0
    checks["inputToken"] = input_token
    checks["inputAmount"] = str(input_amount) if input_amount is not None else None
    checks["gasFeeFromQuote"] = str(gas_fee)

    try:
        native_balance = execute_transaction.query_native_balance(wallet, rpc_url)
        checks["nativeBalance"] = str(native_balance)
        if native_balance < gas_fee:
            if category not in {"insufficient_input_balance"}:
                category = "insufficient_native_gas"
            append_unique(issues, f"native balance {native_balance} is below quoted gas fee {gas_fee}")
            append_unique(next_actions, "top-up-native-gas")
    except Exception as exc:  # noqa: BLE001
        append_unique(issues, f"failed to query native balance: {exc}")
        return {
            "category": category,
            "issues": issues,
            "nextActions": next_actions,
            "checks": checks,
        }

    if input_amount is None:
        append_unique(issues, "quote.input.amount is missing")
        return {
            "category": category,
            "issues": issues,
            "nextActions": next_actions,
            "checks": checks,
        }

    if input_token.lower() == NATIVE_CURRENCY_ADDRESS:
        required_native = input_amount + gas_fee
        checks["requiredNativeForInputAndGas"] = str(required_native)
        if native_balance < required_native:
            category = "insufficient_input_balance"
            append_unique(
                issues,
                f"native balance {native_balance} is below required {required_native} (input + gas)",
            )
            append_unique(next_actions, "top-up-input-token")
            append_unique(next_actions, "reduce-amount")
        return {
            "category": category,
            "issues": issues,
            "nextActions": next_actions,
            "checks": checks,
        }

    try:
        token_balance = execute_transaction.query_erc20_balance(wallet, input_token, rpc_url)
        checks["inputTokenBalance"] = str(token_balance)
        if token_balance < input_amount:
            category = "insufficient_input_balance"
            append_unique(issues, f"token balance {token_balance} is below required input {input_amount}")
            append_unique(next_actions, "top-up-input-token")
            append_unique(next_actions, "reduce-amount")
    except Exception as exc:  # noqa: BLE001
        append_unique(issues, f"failed to query token balance: {exc}")
        return {
            "category": category,
            "issues": issues,
            "nextActions": next_actions,
            "checks": checks,
        }

    permit_verifier = ((raw_quote.get("permitData") or {}).get("domain") or {}).get("verifyingContract")
    if permit_verifier:
        try:
            allowance = execute_transaction.query_erc20_allowance(input_token, wallet, permit_verifier, rpc_url)
            checks["permitAllowance"] = {
                "spender": permit_verifier.lower(),
                "current": str(allowance),
                "required": str(input_amount),
                "sufficient": allowance >= input_amount,
            }
            if allowance < input_amount:
                category = "insufficient_allowance"
                append_unique(
                    issues,
                    f"allowance {allowance} is below required input {input_amount} for {permit_verifier.lower()}",
                )
                if approval_required and not assume_approval_ready:
                    append_unique(next_actions, "broadcast-approval")
                else:
                    append_unique(next_actions, "refresh-quote")
                    if has_signature:
                        append_unique(next_actions, "re-sign-permit")
        except Exception as exc:  # noqa: BLE001
            append_unique(issues, f"failed to query permit allowance: {exc}")

    return {
        "category": category,
        "issues": issues,
        "nextActions": next_actions,
        "checks": checks,
    }


def build_swap_failure_message(error_text: str, diagnosis: dict[str, Any]) -> str:
    parts = [f"swap dry-run failed ({diagnosis.get('category')})"]
    issues = diagnosis.get("issues") or []
    if issues:
        parts.append("issues: " + "; ".join(issues))
    actions = diagnosis.get("nextActions") or []
    if actions:
        parts.append("next: " + ", ".join(actions))
    headline = error_text.splitlines()[0].strip()
    if len(headline) > 220:
        headline = headline[:220] + "..."
    parts.append(f"upstream: {headline}")
    return " | ".join(parts)
