from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

from uniswap_autopilot.common import common  # type: ignore
from uniswap_autopilot.lp.v3 import tick as tick_utils  # type: ignore


class LPContractAddressTests(unittest.TestCase):
    def test_all_supported_chains_have_position_manager(self) -> None:
        for chain_name in common.LP_CONTRACTS:
            addr = common.get_position_manager_address(chain_name)
            self.assertTrue(addr.startswith("0x"), f"{chain_name} PM address invalid")
            self.assertEqual(len(addr), 42)

    def test_all_supported_chains_have_factory(self) -> None:
        for chain_name in common.LP_CONTRACTS:
            addr = common.get_v3_factory_address(chain_name)
            self.assertTrue(addr.startswith("0x"))

    def test_unsupported_chain_raises(self) -> None:
        with self.assertRaises(ValueError):
            common.get_position_manager_address("solana")

    def test_lp_contracts_does_not_include_unichain(self) -> None:
        self.assertNotIn("unichain", common.LP_CONTRACTS)


class FeeTierTests(unittest.TestCase):
    def test_validate_fee_tier_accepts_valid(self) -> None:
        for fee in (100, 500, 3000, 10000):
            self.assertEqual(common.validate_fee_tier(fee), fee)

    def test_validate_fee_tier_rejects_invalid(self) -> None:
        with self.assertRaises(ValueError):
            common.validate_fee_tier(1234)
        with self.assertRaises(ValueError):
            common.validate_fee_tier(0)


class SortTokenAddressesTests(unittest.TestCase):
    def test_sorts_lower_first(self) -> None:
        t0, t1 = common.sort_token_addresses(
            "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
            "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        )
        self.assertEqual(t0, "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48")
        self.assertEqual(t1, "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")

    def test_preserves_order_when_first_is_lower(self) -> None:
        low = "0x0000000000000000000000000000000000000001"
        high = "0x0000000000000000000000000000000000000002"
        t0, t1 = common.sort_token_addresses(low, high)
        self.assertEqual(t0, low)
        self.assertEqual(t1, high)

    def test_rejects_same_address(self) -> None:
        addr = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
        with self.assertRaises(ValueError):
            common.sort_token_addresses(addr, addr)


class TickUtilsTests(unittest.TestCase):
    def test_fee_tier_to_tick_spacing(self) -> None:
        self.assertEqual(tick_utils.fee_tier_to_tick_spacing(100), 1)
        self.assertEqual(tick_utils.fee_tier_to_tick_spacing(500), 10)
        self.assertEqual(tick_utils.fee_tier_to_tick_spacing(3000), 60)
        self.assertEqual(tick_utils.fee_tier_to_tick_spacing(10000), 200)

    def test_fee_tier_rejects_unknown(self) -> None:
        with self.assertRaises(ValueError):
            tick_utils.fee_tier_to_tick_spacing(999)

    def test_nearest_usable_tick_rounds_to_spacing(self) -> None:
        self.assertEqual(tick_utils.nearest_usable_tick(100, 60), 120)
        self.assertEqual(tick_utils.nearest_usable_tick(50, 60), 60)
        self.assertEqual(tick_utils.nearest_usable_tick(0, 60), 0)
        self.assertEqual(tick_utils.nearest_usable_tick(-100, 60), -120)

    def test_nearest_usable_tick_clamps_to_range(self) -> None:
        result = tick_utils.nearest_usable_tick(-900000, 60)
        self.assertEqual(result, tick_utils.MIN_TICK)

    def test_price_to_tick_and_back(self) -> None:
        price = 2000.0
        tick = tick_utils.price_to_tick(price, 18, 18)
        recovered = tick_utils.tick_to_price(tick, 18, 18)
        self.assertAlmostEqual(recovered, price, places=-1)

    def test_price_to_tick_adjusts_for_decimals(self) -> None:
        tick6_18 = tick_utils.price_to_tick(1.0, 6, 18)
        tick18_18 = tick_utils.price_to_tick(1.0, 18, 18)
        self.assertNotEqual(tick6_18, tick18_18)

    def test_price_to_tick_rejects_zero(self) -> None:
        with self.assertRaises(ValueError):
            tick_utils.price_to_tick(0, 18, 18)

    def test_tick_to_sqrt_price_x96_positive_tick(self) -> None:
        result = tick_utils.tick_to_sqrt_price_x96(0)
        # Allow tiny floating-point error (within 1e-6 relative)
        self.assertAlmostEqual(result, 2**96, delta=100)

    def test_suggest_ticks_full_range(self) -> None:
        lower, upper = tick_utils.suggest_ticks_for_range(0, 60)
        self.assertEqual(lower, tick_utils.nearest_usable_tick(tick_utils.MIN_TICK, 60))
        self.assertEqual(upper, tick_utils.nearest_usable_tick(tick_utils.MAX_TICK, 60))

    def test_suggest_ticks_with_price_bounds(self) -> None:
        lower, upper = tick_utils.suggest_ticks_for_range(
            0, 60, price_lower=1000.0, price_upper=3000.0, decimals0=18, decimals1=18,
        )
        self.assertLessEqual(lower, upper)
        self.assertEqual(lower % 60, 0)
        self.assertEqual(upper % 60, 0)


class BuildLPTxCalldataTests(unittest.TestCase):
    def test_mint_calldata_is_hex(self) -> None:
        from uniswap_autopilot.lp.v3.build_tx import _encode_mint_calldata
        result = _encode_mint_calldata(
            "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
            3000, -887220, 887220,
            "1000000000", "500000000000000000",
            "990000000", "495000000000000000",
            "0x1234567890abcdef1234567890abcdef12345678",
            "9999999999",
        )
        self.assertTrue(result.startswith("0x"))
        self.assertIn("88316456", result)  # mint selector

    def test_collect_calldata_is_hex(self) -> None:
        from uniswap_autopilot.lp.v3.build_tx import _encode_collect_calldata
        max_uint128 = str(2**128 - 1)
        result = _encode_collect_calldata(
            "123456",
            "0x1234567890abcdef1234567890abcdef12345678",
            max_uint128, max_uint128,
        )
        self.assertTrue(result.startswith("0x"))
        self.assertIn("fc6f7865", result)  # collect selector


class SlippageCalcTests(unittest.TestCase):
    def test_apply_slippage(self) -> None:
        from uniswap_autopilot.lp.v3.build_tx import _apply_slippage
        result = _apply_slippage("1000000", 0.5)
        self.assertEqual(result, "995000")

    def test_apply_slippage_zero(self) -> None:
        from uniswap_autopilot.lp.v3.build_tx import _apply_slippage
        result = _apply_slippage("1000000", 0)
        self.assertEqual(result, "1000000")


if __name__ == "__main__":
    unittest.main()
