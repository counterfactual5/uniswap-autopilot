#!/usr/bin/env python3
"""Tests for lp.v3.compound and lp.v3.auto_rebalance."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

MOCK_WALLET = "0x1234567890abcdef1234567890abcdef12345678"

MOCK_POSITION = {
    "tokenId": 100,
    "nonce": 0,
    "operator": "0x0000000000000000000000000000000000000000",
    "token0": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",  # USDC
    "token1": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",  # WETH
    "fee": 3000,
    "tickLower": -887220,
    "tickUpper": 887220,
    "liquidity": "1000000000000000000",
}

MOCK_ANALYZED = {
    "tokenId": 100,
    "token0": {"symbol": "USDC", "amount": "1000.0", "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"},
    "token1": {"symbol": "WETH", "amount": "0.5", "address": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"},
    "uncollectedFees": {"token0": "10.0", "token1": "0.005", "totalUsd": 25.0},
    "totalValueUsd": 2500.0,
    "inRange": True,
    "feeTier": 3000,
    "tickLower": -887220,
    "tickUpper": 887220,
}

MOCK_ANALYZED_LOW_FEES = {
    **MOCK_ANALYZED,
    "uncollectedFees": {"token0": "0.5", "token1": "0.0001", "totalUsd": 1.0},
}

MOCK_DECREASE = {"calldata": "0xdecrease", "to": "0xPM", "value": "0"}
MOCK_COLLECT = {"calldata": "0xcollect", "to": "0xPM", "value": "0"}
MOCK_MINT = {"calldata": "0xmint", "to": "0xPM", "value": "0"}

MOCK_RANGES = {
    "suggestions": [
        {"profile": "CONSERVATIVE", "tickLower": -100, "tickUpper": 100, "rangeWidthPct": 5.0},
        {"profile": "MODERATE", "tickLower": -200, "tickUpper": 200, "rangeWidthPct": 15.0},
        {"profile": "AGGRESSIVE", "tickLower": -500, "tickUpper": 500, "rangeWidthPct": 40.0},
    ]
}

TOKEN_SIDE_EFFECT = lambda chain, addr: {  # noqa: E731
    "decimals": 6 if "A0b8" in addr else 18,
    "symbol": "USDC" if "A0b8" in addr else "WETH",
    "address": addr,
}


def _compound_patches(extra=None):
    """Common mock patches for compound execute tests."""
    patches = [
        mock.patch("uniswap_autopilot.lp.v3.compound.query_position", return_value=MOCK_POSITION),
        mock.patch("uniswap_autopilot.lp.v3.compound.analyze_position", return_value=MOCK_ANALYZED),
        mock.patch("uniswap_autopilot.lp.v3.compound.resolve_token", side_effect=TOKEN_SIDE_EFFECT),
        mock.patch("uniswap_autopilot.lp.v3.compound.resolve_wallet_address", return_value=MOCK_WALLET),
        mock.patch("uniswap_autopilot.lp.v3.compound.build_decrease_liquidity_transaction", return_value=MOCK_DECREASE),
        mock.patch("uniswap_autopilot.lp.v3.compound.build_collect_transaction", return_value=MOCK_COLLECT),
        mock.patch("uniswap_autopilot.lp.v3.compound.build_mint_transaction", return_value=MOCK_MINT),
    ]
    if extra:
        patches.extend(extra)
    return patches


def _rebalance_patches(extra=None):
    """Common mock patches for rebalance execute tests."""
    patches = [
        mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.query_position", return_value=MOCK_POSITION),
        mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.analyze_position", return_value=MOCK_ANALYZED),
        mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.resolve_token", side_effect=TOKEN_SIDE_EFFECT),
        mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.resolve_wallet_address", return_value=MOCK_WALLET),
        mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.suggest_ranges", return_value=MOCK_RANGES),
        mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.build_decrease_liquidity_transaction", return_value=MOCK_DECREASE),
        mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.build_collect_transaction", return_value=MOCK_COLLECT),
        mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.build_mint_transaction", return_value=MOCK_MINT),
    ]
    if extra:
        patches.extend(extra)
    return patches


def apply_patches(patches, func):
    """Decorator-like: apply a list of mock patches to a test method."""
    import functools
    for p in reversed(patches):
        func = p(func)
    return func


# ─── Compound Scan ────────────────────────────────────────────


class CompoundScanTests(unittest.TestCase):
    @mock.patch("uniswap_autopilot.lp.v3.compound.analyze_position")
    @mock.patch("uniswap_autopilot.lp.v3.compound.query_positions_by_owner")
    def test_scan_finds_candidates_above_threshold(self, mock_query, mock_analyze):
        from uniswap_autopilot.lp.v3 import compound
        mock_query.return_value = [100, 200]
        mock_analyze.side_effect = [MOCK_ANALYZED, MOCK_ANALYZED_LOW_FEES]

        result = compound.scan_compound_candidates("base", MOCK_WALLET, min_fee_usd=10.0)

        self.assertEqual(result["action"], "compound_scan")
        self.assertEqual(result["totalPositions"], 2)
        self.assertEqual(result["candidatesForCompound"], [100])

    @mock.patch("uniswap_autopilot.lp.v3.compound.query_positions_by_owner")
    def test_scan_empty(self, mock_query):
        from uniswap_autopilot.lp.v3 import compound
        mock_query.return_value = []
        result = compound.scan_compound_candidates("base", MOCK_WALLET)
        self.assertEqual(result["totalPositions"], 0)

    @mock.patch("uniswap_autopilot.lp.v3.compound.analyze_position")
    @mock.patch("uniswap_autopilot.lp.v3.compound.query_positions_by_owner")
    def test_scan_handles_error(self, mock_query, mock_analyze):
        from uniswap_autopilot.lp.v3 import compound
        mock_query.return_value = [999]
        mock_analyze.side_effect = Exception("RPC error")
        result = compound.scan_compound_candidates("base", MOCK_WALLET)
        self.assertIn("error", result["positions"][0])


# ─── Compound Execute ─────────────────────────────────────────


class CompoundExecutePatched(unittest.TestCase):
    """Tests that need full mock stack for execute_compound."""

    def _run_compound(self, **kwargs):
        from uniswap_autopilot.lp.v3 import compound
        return compound.execute_compound(
            chain_name="base", token_id=100,
            wallet=MOCK_WALLET, request_only=True,
            **kwargs,
        )

    @mock.patch("uniswap_autopilot.lp.v3.compound.build_mint_transaction", return_value=MOCK_MINT)
    @mock.patch("uniswap_autopilot.lp.v3.compound.build_collect_transaction", return_value=MOCK_COLLECT)
    @mock.patch("uniswap_autopilot.lp.v3.compound.build_decrease_liquidity_transaction", return_value=MOCK_DECREASE)
    @mock.patch("uniswap_autopilot.lp.v3.compound.resolve_token", side_effect=TOKEN_SIDE_EFFECT)
    @mock.patch("uniswap_autopilot.lp.v3.compound.analyze_position", return_value=MOCK_ANALYZED)
    @mock.patch("uniswap_autopilot.lp.v3.compound.query_position", return_value=MOCK_POSITION)
    @mock.patch("uniswap_autopilot.lp.v3.compound.resolve_wallet_address", return_value=MOCK_WALLET)
    def test_returns_three_steps(self, mock_w, mock_qp, mock_az, mock_rt, mock_dec, mock_col, mock_mint):
        result = self._run_compound()
        self.assertEqual(result["action"], "compound")
        self.assertIn("decrease", result["steps"])
        self.assertIn("collect", result["steps"])
        self.assertIn("mint", result["steps"])
        self.assertFalse(result["broadcastReady"])

    @mock.patch("uniswap_autopilot.lp.v3.compound.build_mint_transaction", return_value=MOCK_MINT)
    @mock.patch("uniswap_autopilot.lp.v3.compound.build_collect_transaction", return_value=MOCK_COLLECT)
    @mock.patch("uniswap_autopilot.lp.v3.compound.build_decrease_liquidity_transaction", return_value=MOCK_DECREASE)
    @mock.patch("uniswap_autopilot.lp.v3.compound.resolve_token", side_effect=TOKEN_SIDE_EFFECT)
    @mock.patch("uniswap_autopilot.lp.v3.compound.analyze_position", return_value=MOCK_ANALYZED)
    @mock.patch("uniswap_autopilot.lp.v3.compound.query_position", return_value=MOCK_POSITION)
    @mock.patch("uniswap_autopilot.lp.v3.compound.resolve_wallet_address", return_value=MOCK_WALLET)
    def test_same_range_as_original(self, mock_w, mock_qp, mock_az, mock_rt, mock_dec, mock_col, mock_mint):
        result = self._run_compound()
        self.assertEqual(result["tickLower"], -887220)
        self.assertEqual(result["tickUpper"], 887220)
        mint_call = mock_mint.call_args
        self.assertEqual(mint_call.kwargs["tick_lower"], -887220)


# ─── Compound Batch ────────────────────────────────────────────


class CompoundBatchTests(unittest.TestCase):
    @mock.patch("uniswap_autopilot.lp.v3.compound.execute_compound")
    @mock.patch("uniswap_autopilot.lp.v3.compound.scan_compound_candidates")
    def test_batch_all_succeed(self, mock_scan, mock_exec):
        from uniswap_autopilot.lp.v3 import compound
        mock_scan.return_value = {
            "action": "compound_scan",
            "chain": {"key": "base", "chainId": 8453},
            "wallet": MOCK_WALLET, "minFeeUsd": 10.0,
            "totalPositions": 2, "candidatesForCompound": [100, 200],
            "positions": [],
        }
        mock_exec.side_effect = [
            {"action": "compound", "tokenId": 100, "steps": {}},
            {"action": "compound", "tokenId": 200, "steps": {}},
        ]
        result = compound.batch_compound("base", MOCK_WALLET)
        self.assertEqual(result["succeeded"], [100, 200])
        self.assertEqual(result["failed"], [])

    @mock.patch("uniswap_autopilot.lp.v3.compound.execute_compound")
    @mock.patch("uniswap_autopilot.lp.v3.compound.scan_compound_candidates")
    def test_batch_continues_on_failure(self, mock_scan, mock_exec):
        from uniswap_autopilot.lp.v3 import compound
        mock_scan.return_value = {
            "action": "compound_scan",
            "chain": {"key": "base", "chainId": 8453},
            "wallet": MOCK_WALLET, "minFeeUsd": 10.0,
            "totalPositions": 2, "candidatesForCompound": [100, 200],
            "positions": [],
        }
        mock_exec.side_effect = [
            Exception("RPC timeout"),
            {"action": "compound", "tokenId": 200, "steps": {}},
        ]
        result = compound.batch_compound("base", MOCK_WALLET)
        self.assertEqual(result["succeeded"], [200])
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["failed"][0]["tokenId"], 100)


# ─── Rebalance Scan ────────────────────────────────────────────


class RebalanceScanTests(unittest.TestCase):
    @mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.analyze_positions_by_owner")
    def test_finds_out_of_range(self, mock_analyze):
        from uniswap_autopilot.lp.v3 import auto_rebalance
        mock_analyze.return_value = {
            "positions": [
                {**MOCK_ANALYZED, "tokenId": 100, "inRange": True},
                {**MOCK_ANALYZED, "tokenId": 200, "inRange": False},
            ]
        }
        result = auto_rebalance.scan_rebalance_candidates("base", MOCK_WALLET)
        self.assertEqual(result["candidatesForRebalance"], [200])

    @mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.analyze_positions_by_owner")
    def test_all_in_range(self, mock_analyze):
        from uniswap_autopilot.lp.v3 import auto_rebalance
        mock_analyze.return_value = {"positions": [{**MOCK_ANALYZED, "tokenId": 100, "inRange": True}]}
        result = auto_rebalance.scan_rebalance_candidates("base", MOCK_WALLET)
        self.assertEqual(result["candidatesForRebalance"], [])


# ─── Rebalance Execute ─────────────────────────────────────────


class RebalanceExecuteTests(unittest.TestCase):
    @mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.build_mint_transaction", return_value=MOCK_MINT)
    @mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.build_collect_transaction", return_value=MOCK_COLLECT)
    @mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.build_decrease_liquidity_transaction", return_value=MOCK_DECREASE)
    @mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.resolve_token", side_effect=TOKEN_SIDE_EFFECT)
    @mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.suggest_ranges", return_value=MOCK_RANGES)
    @mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.analyze_position", return_value=MOCK_ANALYZED)
    @mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.query_position", return_value=MOCK_POSITION)
    @mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.resolve_wallet_address", return_value=MOCK_WALLET)
    def test_uses_new_range(self, mock_w, mock_qp, mock_az, mock_rng, mock_rt, mock_dec, mock_col, mock_mint):
        from uniswap_autopilot.lp.v3 import auto_rebalance
        result = auto_rebalance.execute_rebalance("base", 100, profile="MODERATE", wallet=MOCK_WALLET)
        self.assertEqual(result["newRange"]["tickLower"], -200)
        self.assertEqual(result["newRange"]["tickUpper"], 200)
        mint_call = mock_mint.call_args
        self.assertEqual(mint_call.kwargs["tick_lower"], -200)

    @mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.query_position", return_value={**MOCK_POSITION, "liquidity": "0"})
    @mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.resolve_wallet_address", return_value=MOCK_WALLET)
    def test_rejects_zero_liquidity(self, mock_w, mock_qp):
        from uniswap_autopilot.lp.v3 import auto_rebalance
        with self.assertRaises(ValueError) as ctx:
            auto_rebalance.execute_rebalance("base", 100, wallet=MOCK_WALLET)
        self.assertIn("no liquidity", str(ctx.exception))

    @mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.resolve_wallet_address", return_value="")
    def test_rejects_missing_wallet(self, mock_w):
        from uniswap_autopilot.lp.v3 import auto_rebalance
        with self.assertRaises(ValueError) as ctx:
            auto_rebalance.execute_rebalance("base", 100, wallet=None)
        self.assertIn("wallet address required", str(ctx.exception))

    def test_rejects_invalid_profile(self):
        from uniswap_autopilot.lp.v3 import auto_rebalance
        with mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.resolve_wallet_address", return_value=MOCK_WALLET):
            with self.assertRaises(ValueError) as ctx:
                auto_rebalance.execute_rebalance("base", 100, profile="INVALID", wallet=MOCK_WALLET)
            self.assertIn("profile must be", str(ctx.exception))

    @mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.build_mint_transaction", return_value=MOCK_MINT)
    @mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.build_collect_transaction", return_value=MOCK_COLLECT)
    @mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.build_decrease_liquidity_transaction", return_value=MOCK_DECREASE)
    @mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.resolve_token", side_effect=TOKEN_SIDE_EFFECT)
    @mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.suggest_ranges", return_value=MOCK_RANGES)
    @mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.analyze_position", return_value=MOCK_ANALYZED)
    @mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.query_position", return_value=MOCK_POSITION)
    @mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.resolve_wallet_address", return_value=MOCK_WALLET)
    def test_all_three_profiles(self, mock_w, mock_qp, mock_az, mock_rng, mock_rt, mock_dec, mock_col, mock_mint):
        from uniswap_autopilot.lp.v3 import auto_rebalance
        for profile, tl, tu in [("CONSERVATIVE", -100, 100), ("MODERATE", -200, 200), ("AGGRESSIVE", -500, 500)]:
            result = auto_rebalance.execute_rebalance("base", 100, profile=profile, wallet=MOCK_WALLET)
            self.assertEqual(result["newRange"]["tickLower"], tl)
            self.assertEqual(result["newRange"]["tickUpper"], tu)


# ─── Rebalance Batch ────────────────────────────────────────────


class RebalanceBatchTests(unittest.TestCase):
    @mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.execute_rebalance")
    @mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.scan_rebalance_candidates")
    def test_batch_continues_on_failure(self, mock_scan, mock_exec):
        from uniswap_autopilot.lp.v3 import auto_rebalance
        mock_scan.return_value = {
            "action": "rebalance_scan",
            "chain": {"key": "base", "chainId": 8453},
            "wallet": MOCK_WALLET, "totalPositions": 2,
            "outOfRange": [{"tokenId": 100}, {"tokenId": 200}],
            "inRange": [], "candidatesForRebalance": [100, 200],
        }
        mock_exec.side_effect = [
            Exception("no pool found"),
            {"action": "rebalance", "tokenId": 200, "newRange": {"tickLower": -200, "tickUpper": 200}, "steps": {}},
        ]
        result = auto_rebalance.batch_rebalance("base", MOCK_WALLET)
        self.assertEqual(result["succeeded"], [200])
        self.assertEqual(len(result["failed"]), 1)

    @mock.patch("uniswap_autopilot.lp.v3.auto_rebalance.scan_rebalance_candidates")
    def test_batch_empty(self, mock_scan):
        from uniswap_autopilot.lp.v3 import auto_rebalance
        mock_scan.return_value = {
            "action": "rebalance_scan",
            "chain": {"key": "base", "chainId": 8453},
            "wallet": MOCK_WALLET, "totalPositions": 0,
            "outOfRange": [], "inRange": [], "candidatesForRebalance": [],
        }
        result = auto_rebalance.batch_rebalance("base", MOCK_WALLET)
        self.assertEqual(result["totalAttempted"], 0)


# ─── Precision ────────────────────────────────────────────────


class PrecisionTests(unittest.TestCase):
    @mock.patch("uniswap_autopilot.lp.v3.compound.build_mint_transaction", return_value=MOCK_MINT)
    @mock.patch("uniswap_autopilot.lp.v3.compound.build_collect_transaction", return_value=MOCK_COLLECT)
    @mock.patch("uniswap_autopilot.lp.v3.compound.build_decrease_liquidity_transaction", return_value=MOCK_DECREASE)
    @mock.patch("uniswap_autopilot.lp.v3.compound.resolve_token", side_effect=TOKEN_SIDE_EFFECT)
    @mock.patch("uniswap_autopilot.lp.v3.compound.analyze_position")
    @mock.patch("uniswap_autopilot.lp.v3.compound.query_position", return_value=MOCK_POSITION)
    @mock.patch("uniswap_autopilot.lp.v3.compound.resolve_wallet_address", return_value=MOCK_WALLET)
    def test_decimal_not_float(self, mock_w, mock_qp, mock_az, mock_rt, mock_dec, mock_col, mock_mint):
        from uniswap_autopilot.lp.v3 import compound
        # 0.1 + 0.2 in float = 0.30000000000000004; with Decimal = 0.3
        mock_az.return_value = {
            **MOCK_ANALYZED,
            "token0": {"symbol": "USDC", "amount": "0.1", "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"},
            "uncollectedFees": {"token0": "0.2", "token1": "0.1", "totalUsd": 500.0},
        }
        compound.execute_compound("base", 100, wallet=MOCK_WALLET, request_only=True)
        mint_call = mock_mint.call_args
        # 0.1 + 0.2 USDC = 0.3 USDC = 300000 base units (6 decimals)
        self.assertEqual(mint_call.kwargs["amount0"], "300000")


if __name__ == "__main__":
    unittest.main()
