from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from uniswap_autopilot.common.common import normalize_chain, parse_amount, resolve_token


def normalize_policy_token(chain_name: str, token_name: str) -> str:
    chain = normalize_chain(chain_name)
    token = resolve_token(chain, token_name)
    if token["address"] == "NATIVE":
        return "NATIVE"
    return str(token["address"]).lower()


def load_auto_trade_policy(path: str) -> dict[str, Any]:
    blob = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(blob, dict):
        raise ValueError("auto-trade policy must be a JSON object")

    enabled = blob.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError("auto-trade policy.enabled must be a boolean")

    allowed_chains_raw = blob.get("allowedChains") or []
    if not isinstance(allowed_chains_raw, list) or not all(isinstance(item, str) for item in allowed_chains_raw):
        raise ValueError("auto-trade policy.allowedChains must be a string array")
    allowed_chains = [normalize_chain(item).key for item in allowed_chains_raw]

    allowed_pairs_raw = blob.get("allowedPairs")
    if not isinstance(allowed_pairs_raw, list) or not allowed_pairs_raw:
        raise ValueError("auto-trade policy.allowedPairs must be a non-empty array")

    allowed_pairs: list[dict[str, Any]] = []
    for index, raw_rule in enumerate(allowed_pairs_raw):
        if not isinstance(raw_rule, dict):
            raise ValueError(f"auto-trade policy.allowedPairs[{index}] must be an object")
        chain_name = raw_rule.get("chain")
        token_in = raw_rule.get("tokenIn")
        token_out = raw_rule.get("tokenOut")
        if not all(isinstance(value, str) and value.strip() for value in (chain_name, token_in, token_out)):
            raise ValueError(
                f"auto-trade policy.allowedPairs[{index}] must include non-empty chain/tokenIn/tokenOut"
            )
        normalized_chain = normalize_chain(chain_name).key
        normalized_rule = {
            "chain": normalized_chain,
            "tokenIn": normalize_policy_token(normalized_chain, token_in),
            "tokenOut": normalize_policy_token(normalized_chain, token_out),
            "tokenInLabel": token_in,
            "tokenOutLabel": token_out,
            "allowAutoSignPermit": bool(raw_rule.get("allowAutoSignPermit", False)),
            "allowAutoApproval": bool(raw_rule.get("allowAutoApproval", False)),
            "allowAutoBroadcastSwap": bool(raw_rule.get("allowAutoBroadcastSwap", False)),
        }
        if "maxAmount" in raw_rule:
            if not isinstance(raw_rule["maxAmount"], str):
                raise ValueError(f"auto-trade policy.allowedPairs[{index}].maxAmount must be a string")
            normalized_rule["maxAmount"] = str(parse_amount(raw_rule["maxAmount"]))
        if "maxSlippage" in raw_rule:
            max_slippage = raw_rule["maxSlippage"]
            if not isinstance(max_slippage, (int, float)):
                raise ValueError(f"auto-trade policy.allowedPairs[{index}].maxSlippage must be numeric")
            if max_slippage < 0 or max_slippage > 100:
                raise ValueError(f"auto-trade policy.allowedPairs[{index}].maxSlippage must be between 0 and 100")
            normalized_rule["maxSlippage"] = float(max_slippage)
        allowed_pairs.append(normalized_rule)

    require_preflight_ok = blob.get("requirePreflightOk", True)
    if not isinstance(require_preflight_ok, bool):
        raise ValueError("auto-trade policy.requirePreflightOk must be a boolean")

    return {
        "enabled": enabled,
        "allowedChains": allowed_chains,
        "allowedPairs": allowed_pairs,
        "requirePreflightOk": require_preflight_ok,
        "sourceFile": path,
    }


def evaluate_auto_trade_policy(
    policy: dict[str, Any],
    chain_name: str,
    token_in_name: str,
    token_out_name: str,
    amount_value: str,
    slippage: float,
) -> dict[str, Any]:
    chain_key = normalize_chain(chain_name).key
    requested_token_in = normalize_policy_token(chain_key, token_in_name)
    requested_token_out = normalize_policy_token(chain_key, token_out_name)
    requested_amount = parse_amount(amount_value)

    issues: list[str] = []
    matched_rule: dict[str, Any] | None = None

    if not policy["enabled"]:
        issues.append("policy is disabled")

    if policy["allowedChains"] and chain_key not in policy["allowedChains"]:
        issues.append(f"chain '{chain_key}' is not allowed")

    for rule in policy["allowedPairs"]:
        if (
            rule["chain"] == chain_key
            and rule["tokenIn"] == requested_token_in
            and rule["tokenOut"] == requested_token_out
        ):
            matched_rule = rule
            break
    if matched_rule is None:
        issues.append("token pair is not allowed")
    else:
        if "maxAmount" in matched_rule and requested_amount > parse_amount(matched_rule["maxAmount"]):
            issues.append(
                f"amount {format(requested_amount, 'f')} exceeds maxAmount {matched_rule['maxAmount']}"
            )
        if "maxSlippage" in matched_rule and slippage > float(matched_rule["maxSlippage"]):
            issues.append(
                f"slippage {slippage} exceeds maxSlippage {matched_rule['maxSlippage']}"
            )

    return {
        "policyFile": policy["sourceFile"],
        "allowed": len(issues) == 0,
        "chain": chain_key,
        "tokenIn": requested_token_in,
        "tokenOut": requested_token_out,
        "amount": format(requested_amount, "f"),
        "slippage": slippage,
        "requirePreflightOk": policy["requirePreflightOk"],
        "matchedRule": matched_rule,
        "issues": issues,
    }


def policy_rule_allows(policy_check: dict[str, Any] | None, field_name: str) -> bool:
    return bool((policy_check or {}).get("matchedRule", {}).get(field_name))

