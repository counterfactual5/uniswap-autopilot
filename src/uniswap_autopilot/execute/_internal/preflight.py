from __future__ import annotations

from typing import Any

from uniswap_autopilot.execute._internal.rpc import (
    estimate_transaction_gas,
    query_erc20_allowance,
    query_erc20_balance,
    query_gas_price,
    query_native_balance,
    resolve_rpc_url,
)
from uniswap_autopilot.execute._internal.tx import parse_intish

def build_preflight_report(
    tx: dict[str, Any],
    explicit_rpc_url: str | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    rpc_url, rpc_candidates = resolve_rpc_url(explicit_rpc_url, tx["chainId"])
    if not rpc_url:
        if strict:
            raise RuntimeError(
                f"RPC URL is not configured; set one of {', '.join(rpc_candidates)} or pass --rpc-url"
            )
        return {
            "checked": False,
            "ok": None,
            "reason": "RPC URL is not configured",
            "rpcUrlResolved": None,
            "rpcEnvCandidates": rpc_candidates,
        }

    owner = tx.get("from")
    if not owner:
        if strict:
            raise RuntimeError("preflight requires tx.from")
        return {
            "checked": False,
            "ok": None,
            "reason": "transaction.from is missing",
            "rpcUrlResolved": rpc_url,
            "rpcEnvCandidates": rpc_candidates,
        }
    try:
        native_balance = query_native_balance(owner, rpc_url)
        gas_limit = parse_intish(tx.get("gasLimit"), "gasLimit")
        if gas_limit is None:
            gas_limit = estimate_transaction_gas(tx, rpc_url)
        gas_price = parse_intish(tx.get("gasPrice"), "gasPrice")
        if gas_price is None:
            gas_price = query_gas_price(rpc_url)

        native_value = int(tx["value"])
        gas_cost = gas_limit * gas_price
        total_native_required = native_value + gas_cost
        issues: list[str] = []
        report: dict[str, Any] = {
            "checked": True,
            "ok": True,
            "owner": owner,
            "rpcUrlResolved": rpc_url,
            "rpcEnvCandidates": rpc_candidates,
            "nativeBalance": str(native_balance),
            "gasLimitUsed": str(gas_limit),
            "gasPriceUsed": str(gas_price),
            "gasCost": str(gas_cost),
            "nativeValue": str(native_value),
            "totalNativeRequired": str(total_native_required),
            "nativeSufficient": native_balance >= total_native_required,
            "issues": issues,
        }
        if not report["nativeSufficient"]:
            issues.append(
                f"native balance {native_balance} is below required {total_native_required}"
            )

        if tx["kind"] == "approval":
            token = tx.get("approvalToken")
            spender = tx.get("approvalSpender")
            required_allowance = tx.get("requiredAllowance")
            if token and spender and required_allowance:
                allowance = query_erc20_allowance(token, owner, spender, rpc_url)
                required_int = int(required_allowance)
                report["allowance"] = {
                    "token": token,
                    "spender": spender,
                    "current": str(allowance),
                    "required": str(required_int),
                    "sufficient": allowance >= required_int,
                }
                if allowance >= required_int:
                    report["allowance"]["alreadySufficient"] = True

        if tx["kind"] == "swap":
            input_token = tx.get("inputToken")
            input_amount = tx.get("inputAmount")
            if tx.get("nativeInput"):
                report["inputBalance"] = {
                    "asset": "native",
                    "token": input_token,
                    "current": str(native_balance),
                    "required": str(total_native_required),
                    "sufficient": native_balance >= total_native_required,
                }
            elif input_token and input_amount:
                token_balance = query_erc20_balance(owner, input_token, rpc_url)
                required_int = int(input_amount)
                sufficient = token_balance >= required_int
                report["inputBalance"] = {
                    "asset": "erc20",
                    "token": input_token,
                    "current": str(token_balance),
                    "required": str(required_int),
                    "sufficient": sufficient,
                }
                if not sufficient:
                    issues.append(
                        f"token balance {token_balance} is below required input {required_int}"
                    )

                permit_verifier = tx.get("permitVerifier")
                if permit_verifier:
                    allowance = query_erc20_allowance(input_token, owner, permit_verifier, rpc_url)
                    allowance_sufficient = allowance >= required_int
                    report["allowance"] = {
                        "token": input_token,
                        "spender": permit_verifier,
                        "current": str(allowance),
                        "required": str(required_int),
                        "sufficient": allowance_sufficient,
                        "source": "permitVerifier",
                    }
                    if not allowance_sufficient:
                        issues.append(
                            f"allowance {allowance} is below required input {required_int} for {permit_verifier.lower()}"
                        )

        report["ok"] = len(issues) == 0
        return report
    except Exception as exc:  # noqa: BLE001
        if strict:
            raise
        return {
            "checked": False,
            "ok": None,
            "reason": str(exc),
            "rpcUrlResolved": rpc_url,
            "rpcEnvCandidates": rpc_candidates,
        }
