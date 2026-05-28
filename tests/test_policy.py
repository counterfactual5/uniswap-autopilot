"""Tests for uniswap-autopilot risk-control policy (shared + project-specific)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from decimal import Decimal

from uniswap_autopilot.policy import (
    Policy,
    check,
    check_uniswap,
    load_policy,
)


class TestSharedChecks(unittest.TestCase):
    """Shared policy rules work in uniswap context."""

    def test_max_amount_reject(self) -> None:
        pol = Policy(max_amount=Decimal("500"))
        result = check(pol, {"amount": "1000"})
        self.assertFalse(result.allowed)

    def test_allowed_chains(self) -> None:
        pol = Policy(allowed_chains=["ethereum", "base", "polygon"])
        result = check(pol, {"chain": "base"})
        self.assertTrue(result.allowed)

    def test_slippage_reject(self) -> None:
        pol = Policy(max_slippage_bps=50)
        result = check(pol, {"slippage_bps": 100})
        self.assertFalse(result.allowed)
        self.assertEqual(result.violations[0].rule, "max_slippage_bps")


class TestMinOutputAmount(unittest.TestCase):
    """Uniswap-specific: min_output_amount."""

    def test_above_min(self) -> None:
        pol = Policy(extra={"min_output_amount": "1.0"})
        result = check_uniswap(pol, {"output_amount": "2.5"})
        self.assertTrue(result.allowed)

    def test_below_min(self) -> None:
        pol = Policy(extra={"min_output_amount": "1.0"})
        result = check_uniswap(pol, {"output_amount": "0.5"})
        self.assertFalse(result.allowed)
        self.assertEqual(result.violations[0].rule, "min_output_amount")

    def test_no_limit(self) -> None:
        pol = Policy()
        result = check_uniswap(pol, {"output_amount": "0.001"})
        self.assertTrue(result.allowed)


class TestLoadPolicyProject(unittest.TestCase):
    """load_policy defaults to uniswap-autopilot."""

    def test_project_overlay(self) -> None:
        data = {
            "global": {"max_amount": 1000, "max_slippage_bps": 100},
            "uniswap-autopilot": {
                "max_amount": 500,
                "max_slippage_bps": 50,
                "min_output_amount": "0.1",
            },
        }
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump(data, f)
            path = f.name
        try:
            pol = load_policy(path)
            self.assertEqual(pol.max_amount, Decimal("500"))
            self.assertEqual(pol.max_slippage_bps, 50)
            self.assertEqual(pol.extra.get("min_output_amount"), "0.1")
        finally:
            os.unlink(path)


class TestCombinedViolation(unittest.TestCase):
    """Multiple violations from shared + project-specific rules."""

    def test_amount_and_output(self) -> None:
        pol = Policy(max_amount=Decimal("100"), extra={"min_output_amount": "5"})
        result = check_uniswap(pol, {"amount": "500", "output_amount": "1"})
        self.assertFalse(result.allowed)
        rules = {v.rule for v in result.violations}
        self.assertIn("max_amount", rules)
        self.assertIn("min_output_amount", rules)


class TestPolicyGateE2E(unittest.TestCase):
    """End-to-end: a real policy file loaded via POLICY_FILE drives check_uniswap
    exactly as broadcast.py does, and rejects an over-limit swap."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        policy_data = {
            "uniswap-autopilot": {
                "max_amount": 100,
                "allowed_chains": ["ethereum", "base"],
            }
        }
        self._policy_path = os.path.join(self._tmpdir.name, "policy.json")
        with open(self._policy_path, "w", encoding="utf-8") as fh:
            json.dump(policy_data, fh)
        os.environ["POLICY_FILE"] = self._policy_path

    def tearDown(self) -> None:
        self._tmpdir.cleanup()
        os.environ.pop("POLICY_FILE", None)

    def test_loaded_policy_rejects_over_limit(self) -> None:
        pol = load_policy()  # resolves via POLICY_FILE, project=uniswap-autopilot
        # The same context shape broadcast.py builds.
        ctx = {"chain": "ethereum", "sender": "0xabc", "receiver": "0xdef", "amount": "500"}
        result = check_uniswap(pol, ctx)
        self.assertFalse(result.allowed)
        self.assertEqual(result.violations[0].rule, "max_amount")

    def test_loaded_policy_rejects_disallowed_chain(self) -> None:
        pol = load_policy()
        ctx = {"chain": "arbitrum", "sender": "0xabc", "amount": "10"}
        result = check_uniswap(pol, ctx)
        self.assertFalse(result.allowed)
        self.assertEqual(result.violations[0].rule, "allowed_chains")

    def test_loaded_policy_allows_within_limits(self) -> None:
        pol = load_policy()
        ctx = {"chain": "base", "sender": "0xabc", "amount": "10"}
        result = check_uniswap(pol, ctx)
        self.assertTrue(result.allowed)


if __name__ == "__main__":
    unittest.main()
