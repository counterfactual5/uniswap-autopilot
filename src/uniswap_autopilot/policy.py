"""Risk-control policy engine — declarative rules, state-machine integration.

A ``policy.yaml`` (or JSON) file defines per-project risk limits.  The engine
loads the file, validates a trade *context* dict against the rules, and returns
a ``CheckResult`` indicating whether the trade should proceed, be warned about,
or be rejected outright.

Designed to sit between the ``PREFLIGHT`` and ``SIGNED`` states in the trade
execution state machine:

    PREFLIGHT  →  policy.check()  →  pass   →  transition(SIGNED)
                                  →  reject →  transition(FAILED)

Policy file resolution (first match wins):

    1. Explicit *path* argument to ``load_policy()``
    2. ``POLICY_FILE`` environment variable
    3. ``~/.stageforge/policy.yaml``
    4. ``~/.stageforge/policy.json``

Minimal policy file example (YAML):

    global:
      max_amount: 1000         # USD equivalent
      allowed_chains:
        - ethereum
        - polygon
        - base
      blacklist_addresses: []
      whitelist_addresses: []  # empty = allow all

    evm-wallet-scanner:
      max_amount: 500

Rules cascade: project-specific overrides merge on top of ``global``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

try:
    import yaml as _yaml  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    _yaml = None  # YAML optional; JSON always works

_DEFAULT_PROJECT = __name__.split(".")[0].replace("_", "-")


# ── Data structures ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Violation:
    """A single rule violation or warning."""

    rule: str
    message: str
    severity: str = "reject"  # "reject" | "warn"


@dataclass
class CheckResult:
    """Outcome of ``check()``."""

    allowed: bool = True
    violations: list[Violation] = field(default_factory=list)
    warnings: list[Violation] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "violations": [{"rule": v.rule, "message": v.message} for v in self.violations],
            "warnings": [{"rule": w.rule, "message": w.message} for w in self.warnings],
        }


@dataclass
class Policy:
    """Resolved rules for a single project (global + project overlay)."""

    max_amount: Decimal | None = None
    allowed_chains: list[str] | None = None
    blacklist_addresses: list[str] = field(default_factory=list)
    whitelist_addresses: list[str] = field(default_factory=list)
    max_slippage_bps: int | None = None
    max_gas_price_gwei: Decimal | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# ── Loading ──────────────────────────────────────────────────────────────────


def _find_policy_file(explicit: str | None = None) -> Path | None:
    """Resolve the policy file path (first match wins)."""
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
        return None

    env = (os.environ.get("POLICY_FILE") or "").strip()
    if env:
        p = Path(env)
        if p.is_file():
            return p
        return None

    base = Path.home() / ".stageforge"
    for name in ("policy.yaml", "policy.yml", "policy.json"):
        p = base / name
        if p.is_file():
            return p
    return None


def _parse_file(path: Path) -> dict[str, Any]:
    """Read a YAML or JSON policy file and return a raw dict."""
    text = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        if _yaml is None:
            raise ImportError(
                "PyYAML is required to load .yaml policy files. "
                "Install it with: pip install pyyaml"
            )
        return _yaml.safe_load(text) or {}  # type: ignore[no-any-return]
    return json.loads(text)


def _decimal_or_none(val: Any) -> Decimal | None:
    if val is None:
        return None
    try:
        return Decimal(str(val))
    except (InvalidOperation, ValueError):
        return None


def _build_policy(raw: dict[str, Any]) -> Policy:
    """Build a ``Policy`` from a raw rule dict (global or project section)."""
    known_keys = {
        "max_amount", "allowed_chains", "blacklist_addresses",
        "whitelist_addresses", "max_slippage_bps", "max_gas_price_gwei",
    }
    extra = {k: v for k, v in raw.items() if k not in known_keys}

    bl = raw.get("blacklist_addresses") or []
    wl = raw.get("whitelist_addresses") or []

    return Policy(
        max_amount=_decimal_or_none(raw.get("max_amount")),
        allowed_chains=raw.get("allowed_chains"),
        blacklist_addresses=[a.lower() for a in bl],
        whitelist_addresses=[a.lower() for a in wl],
        max_slippage_bps=raw.get("max_slippage_bps"),
        max_gas_price_gwei=_decimal_or_none(raw.get("max_gas_price_gwei")),
        extra=extra,
    )


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Shallow merge *overlay* on top of *base* (overlay wins for non-None)."""
    merged = dict(base)
    for k, v in overlay.items():
        if v is not None:
            merged[k] = v
    return merged


def load_policy(
    path: str | None = None,
    *,
    project: str = _DEFAULT_PROJECT,
) -> Policy:
    """Load and resolve the policy for *project*.

    Returns a ``Policy`` with ``global`` rules as base and project-specific
    overrides applied on top.  If no policy file is found, returns a
    permissive default (no limits).
    """
    fp = _find_policy_file(path)
    if fp is None:
        return Policy()

    raw = _parse_file(fp)
    global_rules: dict[str, Any] = raw.get("global", {})
    project_rules: dict[str, Any] = raw.get(project, {})

    merged = _merge(global_rules, project_rules)
    return _build_policy(merged)


# ── Checks ───────────────────────────────────────────────────────────────────


def check(
    policy: Policy,
    context: dict[str, Any],
) -> CheckResult:
    """Validate a trade *context* against *policy* rules.

    Expected *context* keys (all optional — absent keys skip the check):

        amount          float/str/Decimal — trade amount (USD or native units)
        chain           str — chain name, e.g. "ethereum"
        sender          str — sender address
        receiver        str — receiver address
        spender         str — spender / operator address
        slippage_bps    int — slippage in basis points
        gas_price_gwei  float/str — gas price in gwei

    Returns a ``CheckResult`` with ``allowed=False`` if any hard violation is
    found.  Warnings do not block the trade.
    """
    result = CheckResult()

    _check_max_amount(policy, context, result)
    _check_allowed_chains(policy, context, result)
    _check_blacklist(policy, context, result)
    _check_whitelist(policy, context, result)
    _check_max_slippage(policy, context, result)
    _check_max_gas_price(policy, context, result)

    if result.violations:
        result.allowed = False

    return result


# ── Individual rule checks ───────────────────────────────────────────────────


def _check_max_amount(policy: Policy, ctx: dict[str, Any], res: CheckResult) -> None:
    if policy.max_amount is None:
        return
    raw = ctx.get("amount")
    if raw is None:
        return
    try:
        amt = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return
    if amt > policy.max_amount:
        res.violations.append(Violation(
            rule="max_amount",
            message=f"amount {amt} exceeds limit {policy.max_amount}",
        ))


def _check_allowed_chains(policy: Policy, ctx: dict[str, Any], res: CheckResult) -> None:
    if not policy.allowed_chains:
        return
    chain = ctx.get("chain")
    if chain is None:
        return
    if chain.lower() not in [c.lower() for c in policy.allowed_chains]:
        res.violations.append(Violation(
            rule="allowed_chains",
            message=f"chain {chain!r} not in allowed list {policy.allowed_chains}",
        ))


def _check_blacklist(policy: Policy, ctx: dict[str, Any], res: CheckResult) -> None:
    if not policy.blacklist_addresses:
        return
    for key in ("sender", "receiver", "spender"):
        addr = ctx.get(key)
        if addr and addr.lower() in policy.blacklist_addresses:
            res.violations.append(Violation(
                rule="blacklist_addresses",
                message=f"{key} {addr} is blacklisted",
            ))


def _check_whitelist(policy: Policy, ctx: dict[str, Any], res: CheckResult) -> None:
    if not policy.whitelist_addresses:
        return
    for key in ("sender", "receiver", "spender"):
        addr = ctx.get(key)
        if addr and addr.lower() not in policy.whitelist_addresses:
            res.warnings.append(Violation(
                rule="whitelist_addresses",
                message=f"{key} {addr} not in whitelist",
                severity="warn",
            ))


def _check_max_slippage(policy: Policy, ctx: dict[str, Any], res: CheckResult) -> None:
    if policy.max_slippage_bps is None:
        return
    slippage = ctx.get("slippage_bps")
    if slippage is None:
        return
    try:
        val = int(slippage)
    except (TypeError, ValueError):
        return
    if val > policy.max_slippage_bps:
        res.violations.append(Violation(
            rule="max_slippage_bps",
            message=f"slippage {val} bps exceeds limit {policy.max_slippage_bps} bps",
        ))


def _check_max_gas_price(policy: Policy, ctx: dict[str, Any], res: CheckResult) -> None:
    if policy.max_gas_price_gwei is None:
        return
    gp = ctx.get("gas_price_gwei")
    if gp is None:
        return
    try:
        val = Decimal(str(gp))
    except (InvalidOperation, ValueError):
        return
    if val > policy.max_gas_price_gwei:
        res.warnings.append(Violation(
            rule="max_gas_price_gwei",
            message=f"gas price {val} gwei exceeds soft limit {policy.max_gas_price_gwei} gwei",
            severity="warn",
        ))


# ── Uniswap-specific checks ──────────────────────────────────────────────────


def _check_min_output_amount(policy: Policy, ctx: dict[str, Any], res: CheckResult) -> None:
    min_out = policy.extra.get("min_output_amount")
    if min_out is None:
        return
    output = ctx.get("output_amount")
    if output is None:
        return
    try:
        out_dec = Decimal(str(output))
        min_dec = Decimal(str(min_out))
    except (InvalidOperation, ValueError):
        return
    if min_dec <= 0:
        return  # 0 or negative means disabled
    if out_dec < min_dec:
        res.violations.append(Violation(
            rule="min_output_amount",
            message=f"output amount {out_dec} below minimum {min_dec}",
        ))


def check_uniswap(
    policy: Policy,
    context: dict[str, Any],
) -> CheckResult:
    """Run shared + Uniswap-specific checks."""
    result = check(policy, context)
    _check_min_output_amount(policy, context, result)
    if result.violations:
        result.allowed = False
    return result


__all__ = [
    "CheckResult",
    "Policy",
    "Violation",
    "check",
    "check_uniswap",
    "load_policy",
]
