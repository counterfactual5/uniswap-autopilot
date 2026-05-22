#!/usr/bin/env python3
"""Token cache tests."""
from __future__ import annotations

import json
import unittest

from uniswap_autopilot.common import common  # type: ignore


class TokenCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cache_file = common.TOKEN_CACHE_FILE
        # Clean up before each test
        if self.cache_file.exists():
            self.cache_file.unlink()

    def tearDown(self) -> None:
        if self.cache_file.exists():
            self.cache_file.unlink()

    def test_cache_put_and_get(self) -> None:
        common._cache_put("base", "AERO", {
            "kind": "erc20", "symbol": "AERO",
            "address": "0x940181a94A353A48BA1189514632a47ed9922803",
            "decimals": 18,
        })
        result = common._cache_get("base", "AERO")
        self.assertIsNotNone(result)
        self.assertEqual(result["decimals"], 18)

    def test_cache_get_missing_chain(self) -> None:
        result = common._cache_get("nonexistent_chain", "ETH")
        self.assertIsNone(result)

    def test_cache_get_missing_symbol(self) -> None:
        common._cache_put("base", "USDC", {"address": "0xabc", "decimals": 6})
        result = common._cache_get("base", "MISSING")
        self.assertIsNone(result)

    def test_cache_get_by_address(self) -> None:
        addr = "0x940181a94A353A48BA1189514632a47ed9922803"
        common._cache_put("base", "AERO", {
            "symbol": "AERO", "address": addr, "decimals": 18,
        })
        result = common._cache_get("base", addr)
        self.assertIsNotNone(result)
        self.assertEqual(result["symbol"], "AERO")

    def test_cache_overwrite(self) -> None:
        common._cache_put("base", "TEST", {"address": "0xold", "decimals": 18})
        common._cache_put("base", "TEST", {"address": "0xnew", "decimals": 6})
        result = common._cache_get("base", "TEST")
        self.assertEqual(result["address"], "0xnew")

    def test_cache_persistence(self) -> None:
        common._cache_put("base", "FOO", {"address": "0x123", "decimals": 18})
        # Re-read from disk
        self.assertTrue(self.cache_file.exists())
        data = json.loads(self.cache_file.read_text())
        self.assertIn("base", data)
        self.assertIn("FOO", data["base"])

    def test_resolve_token_hits_cache(self) -> None:
        chain = common.normalize_chain("base")
        addr = "0x1234567890abcdef1234567890abcdef12345678"
        common._cache_put("base", "CACHETEST", {
            "kind": "erc20", "symbol": "CACHETEST",
            "address": addr, "decimals": 9,
            "category": None, "isStable": False, "priceHint": None,
        })
        result = common.resolve_token(chain, "CACHETEST")
        self.assertEqual(result["address"], addr)
        self.assertEqual(result["decimals"], 9)

    def test_resolve_token_address_cached(self) -> None:
        chain = common.normalize_chain("base")
        addr = "0x1234567890abcdef1234567890abcdef12345678"
        common._cache_put("base", "CACHETEST", {
            "kind": "erc20", "symbol": "CACHETEST",
            "address": addr, "decimals": 9,
        })
        # Passing address should hit cache instead of RPC call
        result = common.resolve_token(chain, addr, rpc_url=None)
        self.assertEqual(result["symbol"], "CACHETEST")

    def test_cache_token_from_search(self) -> None:
        common.cache_token_from_search(
            chain_key="arbitrum", symbol="GMX",
            address="0xfc5A1A6EB076a2C7aD06eD22C90d7E710E35ad0a",
            decimals=18,
        )
        result = common._cache_get("arbitrum", "GMX")
        self.assertIsNotNone(result)
        self.assertEqual(result["address"], "0xfc5A1A6EB076a2C7aD06eD22C90d7E710E35ad0a")

    def test_auto_search_returns_none_when_no_results(self) -> None:
        # With a truly bogus query that no DEX has, _auto_search should return None
        import unittest.mock as mock
        with mock.patch("uniswap_autopilot.common.common._auto_search_token", return_value=None):
            chain = common.normalize_chain("base")
            with self.assertRaises(ValueError):
                common.resolve_token(chain, "ZZZZNOTATOKEN12345")


if __name__ == "__main__":
    unittest.main()
