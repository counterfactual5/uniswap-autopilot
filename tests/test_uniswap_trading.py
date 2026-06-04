from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from uniswap_autopilot.common import common  # type: ignore
from uniswap_autopilot.execute import broadcast as execute_transaction  # type: ignore
from uniswap_autopilot.execute._internal import preflight as execute_preflight  # type: ignore
from uniswap_autopilot.execute._internal import rpc as execute_rpc  # type: ignore
from uniswap_autopilot.execute._internal import signer as execute_signer  # type: ignore
from uniswap_autopilot.execute._internal import submit as execute_submit  # type: ignore
from uniswap_autopilot.execute._internal import tx as execute_tx  # type: ignore
from uniswap_autopilot.swap import flow as run_trade_flow  # type: ignore
from uniswap_autopilot.swap.links import deep_link as build_swap_link  # type: ignore
from uniswap_autopilot.swap.trading_api import quote as trading_api_quote  # type: ignore
from uniswap_autopilot.swap.trading_api import swap as swap_dry_run  # type: ignore


class UniswapTradingEvalTests(unittest.TestCase):
    def test_build_swap_link_preserves_plain_amount_string(self) -> None:
        response = build_swap_link.build_swap_link_response(
            chain_name="base",
            token_in_name="NATIVE",
            token_out_name="USDC",
            amount_value="1000",
        )
        self.assertEqual(response["amount"], "1000")
        self.assertIn("value=1000", response["deepLink"])
        self.assertNotIn("E%2B", response["deepLink"])

    def test_resolve_wallet_address_falls_back_to_env(self) -> None:
        with mock.patch.dict("os.environ", {"SECURE_WALLET_ADDRESS": "0x1111111111111111111111111111111111111111"}, clear=False):
            self.assertEqual(
                common.resolve_wallet_address(None),
                "0x1111111111111111111111111111111111111111",
            )

    def test_resolve_wallet_address_prefers_requested_wallet_class(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "SECURE_WALLET_ADDRESS": "0x1111111111111111111111111111111111111111",
                "HOT_WALLET_ADDRESS": "0x2222222222222222222222222222222222222222",
            },
            clear=False,
        ):
            self.assertEqual(
                common.resolve_wallet_address(None, preference="secure"),
                "0x1111111111111111111111111111111111111111",
            )
            self.assertEqual(
                common.resolve_wallet_address(None, preference="hot"),
                "0x2222222222222222222222222222222222222222",
            )

    def test_quote_payload_uses_zero_address_for_native(self) -> None:
        response, approval_payload, quote_payload = trading_api_quote.prepare_quote_request_data(
            chain_name="base",
            token_in_name="NATIVE",
            token_out_name="USDC",
            amount_value="1",
            wallet="0x1111111111111111111111111111111111111111",
        )
        self.assertIsNone(approval_payload)
        self.assertEqual(
            response["apiTokenIn"]["address"], "0x0000000000000000000000000000000000000000"
        )
        self.assertEqual(
            quote_payload["tokenIn"], "0x0000000000000000000000000000000000000000"
        )

    def test_quote_payload_uses_wallet_env_when_wallet_arg_missing(self) -> None:
        with mock.patch.dict("os.environ", {"SECURE_WALLET_ADDRESS": "0x1111111111111111111111111111111111111111"}, clear=False):
            response, approval_payload, quote_payload = trading_api_quote.prepare_quote_request_data(
                chain_name="base",
                token_in_name="NATIVE",
                token_out_name="USDC",
                amount_value="1",
                wallet=None,
            )
        self.assertIsNone(approval_payload)
        self.assertEqual(response["wallet"], "0x1111111111111111111111111111111111111111")
        self.assertEqual(quote_payload["swapper"], "0x1111111111111111111111111111111111111111")

    def test_detect_hot_wallet_backend_uses_private_key_env_name(self) -> None:
        with mock.patch.object(execute_signer, "_pure_signer_available", return_value=True), mock.patch.dict(
            "os.environ",
            {
                "HOT_WALLET_ADDRESS": "0x1111111111111111111111111111111111111111",
                "HOT_WALLET_PRIVATE_KEY_ENV": "HOT_TEST_PRIVATE_KEY",
                "HOT_TEST_PRIVATE_KEY": "0x1234",
            },
            clear=False,
        ):
            result = execute_transaction.detect_hot_wallet_backend()
        self.assertTrue(result["available"])
        self.assertEqual(result["mode"], "private-key-env")
        self.assertEqual(result["signerArgs"].private_key_env, "HOT_TEST_PRIVATE_KEY")

    def test_auto_select_signer_args_falls_back_to_hot_wallet(self) -> None:
        with mock.patch.object(
            execute_signer,
            "detect_hot_wallet_backend",
            return_value={
                "available": True,
                "signerArgs": execute_transaction.build_signer_namespace(
                    private_key_env="HOT_TEST_PRIVATE_KEY",
                    wallet_source="hot",
                ),
            },
        ):
            selected = execute_transaction.auto_select_signer_args(None)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.private_key_env, "HOT_TEST_PRIVATE_KEY")

    def test_check_approval_skips_native_input(self) -> None:
        response, approval_payload, _ = trading_api_quote.prepare_quote_request_data(
            chain_name="base",
            token_in_name="NATIVE",
            token_out_name="USDC",
            amount_value="1",
            wallet="0x1111111111111111111111111111111111111111",
            check_approval=True,
        )
        self.assertIsNone(approval_payload)
        self.assertTrue(response["approvalCheck"]["skipped"])

    def test_custom_decimals_override_changes_base_amount(self) -> None:
        response, _, quote_payload = trading_api_quote.prepare_quote_request_data(
            chain_name="base",
            token_in_name="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            token_out_name="WETH",
            amount_value="500",
            wallet="0x1111111111111111111111111111111111111111",
            token_in_decimals=6,
        )
        self.assertEqual(response["baseAmount"], "500000000")
        self.assertEqual(quote_payload["amount"], "500000000")

    def test_build_swap_payload_uses_nested_quote_only(self) -> None:
        raw_quote = {
            "routing": "CLASSIC",
            "permitData": None,
            "quote": {"quoteId": "abc", "input": {"amount": "1"}},
        }
        payload = swap_dry_run.build_swap_payload(
            raw_quote=raw_quote,
            simulate_transaction=True,
            refresh_gas_price=True,
        )
        self.assertEqual(payload["quote"], raw_quote["quote"])
        self.assertNotIn("routing", payload)
        self.assertTrue(payload["simulateTransaction"])
        self.assertTrue(payload["refreshGasPrice"])

    def test_build_swap_payload_classic_includes_permit_data_with_signature(self) -> None:
        raw_quote = {
            "routing": "CLASSIC",
            "permitData": {"domain": {}, "types": {}, "values": {}},
            "quote": {"quoteId": "abc"},
        }
        payload = swap_dry_run.build_swap_payload(raw_quote=raw_quote, signature="0x1234")
        self.assertEqual(payload["signature"], "0x1234")
        self.assertIn("permitData", payload)

    def test_build_swap_payload_uniswapx_omits_permit_data_from_swap_body(self) -> None:
        raw_quote = {
            "routing": "DUTCH_V2",
            "permitData": {"domain": {}, "types": {}, "values": {}},
            "quote": {"quoteId": "abc", "encodedOrder": "0xdeadbeef"},
        }
        payload = swap_dry_run.build_swap_payload(raw_quote=raw_quote, signature="0x1234")
        self.assertEqual(payload["signature"], "0x1234")
        self.assertNotIn("permitData", payload)

    def test_build_swap_payload_requires_signature_when_permitdata_present(self) -> None:
        raw_quote = {"permitData": {"domain": {}}, "quote": {"quoteId": "abc"}}
        with self.assertRaisesRegex(ValueError, "必须提供 --signature"):
            swap_dry_run.build_swap_payload(raw_quote=raw_quote)

    def test_load_quote_payload_requires_raw_quote(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "bad.json"
            path.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must contain rawQuote"):
                swap_dry_run.load_quote_payload(str(path))

    def test_load_signature_value_supports_json_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "signature.json"
            path.write_text(json.dumps({"signature": "0x1234"}), encoding="utf-8")
            signature = swap_dry_run.load_signature_value(signature_file=str(path))
            self.assertEqual(signature, "0x1234")

    def test_build_permit_handoff_marks_classic_route_as_requiring_permitdata(self) -> None:
        raw_quote = {
            "routing": "CLASSIC",
            "permitData": {
                "domain": {
                    "name": "Permit2",
                    "chainId": 8453,
                    "verifyingContract": "0x000000000022D473030F116dDEE9F6B43aC78BA3",
                },
                "types": {
                    "PermitSingle": [
                        {"name": "details", "type": "PermitDetails"},
                        {"name": "spender", "type": "address"},
                        {"name": "sigDeadline", "type": "uint256"},
                    ],
                    "PermitDetails": [
                        {"name": "token", "type": "address"},
                        {"name": "amount", "type": "uint160"},
                        {"name": "expiration", "type": "uint48"},
                        {"name": "nonce", "type": "uint48"},
                    ],
                },
                "values": {
                    "details": {
                        "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                        "amount": "1",
                        "expiration": "2",
                        "nonce": "3",
                    },
                    "spender": "0x6ff5693b99212da76ad316178a184ab56d299b43",
                    "sigDeadline": "4",
                },
            },
            "quote": {"quoteId": "abc"},
        }
        handoff = swap_dry_run.build_permit_handoff(raw_quote=raw_quote, quote_file="/tmp/quote.json")
        self.assertEqual(handoff["routing"], "CLASSIC")
        self.assertTrue(handoff["signatureRule"]["sendPermitDataToSwap"])
        self.assertEqual(handoff["quoteFile"], "/tmp/quote.json")
        self.assertEqual(handoff["typedData"]["primaryType"], "PermitSingle")

    def test_normalize_permit_typed_data_builds_standard_eip712_shape(self) -> None:
        permit_data = {
            "domain": {
                "name": "Permit2",
                "chainId": 8453,
                "verifyingContract": "0x000000000022D473030F116dDEE9F6B43aC78BA3",
            },
            "types": {
                "PermitSingle": [
                    {"name": "details", "type": "PermitDetails"},
                    {"name": "spender", "type": "address"},
                    {"name": "sigDeadline", "type": "uint256"},
                ],
                "PermitDetails": [
                    {"name": "token", "type": "address"},
                    {"name": "amount", "type": "uint160"},
                    {"name": "expiration", "type": "uint48"},
                    {"name": "nonce", "type": "uint48"},
                ],
            },
            "values": {
                "details": {
                    "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                    "amount": "1",
                    "expiration": "2",
                    "nonce": "3",
                },
                "spender": "0x6ff5693b99212da76ad316178a184ab56d299b43",
                "sigDeadline": "4",
            },
        }
        typed_data = swap_dry_run.normalize_permit_typed_data(permit_data)
        self.assertEqual(typed_data["primaryType"], "PermitSingle")
        self.assertEqual(typed_data["domain"]["name"], "Permit2")
        self.assertEqual(typed_data["message"], permit_data["values"])
        self.assertIn("EIP712Domain", typed_data["types"])
        self.assertEqual(
            typed_data["types"]["EIP712Domain"],
            [
                {"name": "name", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
        )

    def test_load_swap_transaction_extracts_cast_send_fields(self) -> None:
        swap_blob = {
            "requestPayload": {
                "quote": {
                    "input": {
                        "token": "0x0000000000000000000000000000000000000000",
                    }
                }
            },
            "swapResponse": {
                "requestId": "req-1",
                "gasFee": "123",
                "swap": {
                    "to": "0x6fF5693b99212Da76ad316178A184AB56D299b43",
                    "from": "0x1111111111111111111111111111111111111111",
                    "data": "0xdeadbeef",
                    "value": "0x01",
                    "chainId": 8453,
                    "gasLimit": "97000",
                    "gasPrice": "6000000",
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "swap.json"
            path.write_text(json.dumps(swap_blob), encoding="utf-8")
            tx = execute_transaction.load_swap_transaction(str(path))
        self.assertEqual(tx["kind"], "swap")
        self.assertEqual(tx["chainId"], 8453)
        self.assertEqual(tx["chainKey"], "base")
        self.assertTrue(tx["nativeInput"])
        self.assertEqual(tx["value"], "1")
        self.assertEqual(tx["gasLimit"], "97000")

    def test_load_approval_transaction_extracts_cast_send_fields(self) -> None:
        quote_blob = {
            "approvalCheck": {
                "requestId": "req-2",
                "approval": {
                    "to": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                    "from": "0x1111111111111111111111111111111111111111",
                    "data": "0x095ea7b3",
                    "value": "0x00",
                    "chainId": 8453,
                },
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "quote.json"
            path.write_text(json.dumps(quote_blob), encoding="utf-8")
            tx = execute_transaction.load_approval_transaction(str(path))
        self.assertEqual(tx["kind"], "approval")
        self.assertEqual(tx["chainId"], 8453)
        self.assertEqual(tx["value"], "0")
        self.assertFalse(tx["nativeInput"] if "nativeInput" in tx else False)

    def test_resolve_rpc_url_prefers_chain_specific_env(self) -> None:
        with mock.patch.dict("os.environ", {"BASE_RPC_URL": "https://base.example", "ETH_RPC_URL": "https://eth.example"}, clear=False):
            rpc_url, candidates = execute_transaction.resolve_rpc_url(None, 8453)
        self.assertEqual(rpc_url, "https://base.example")
        self.assertIn("BASE_RPC_URL", candidates)


        raw = "115792089237316195423570985008687907853269984665640564039457584007913129639935 [1.157e77]"
        parsed = execute_transaction.parse_cast_int_output(raw, "cast call allowance")
        self.assertEqual(parsed, int(raw.split(" [", 1)[0]))


        tx = {
            "kind": "approval",
            "chainId": 8453,
            "chainKey": "base",
            "to": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "from": "0x1111111111111111111111111111111111111111",
            "data": "0x095ea7b3",
            "value": "0",
            "gasLimit": None,
            "gasPrice": None,
            "sourceKind": "quote-file",
            "sourceFile": "/tmp/quote.json",
            "requestId": "req-1",
            "nativeInput": False,
        }
        with mock.patch.dict("os.environ", {}, clear=True):
            preview = execute_transaction.build_execute_preview(tx)
        self.assertIsNone(preview["summary"]["rpcUrlResolved"])
        self.assertIn("<unresolved-rpc-url>", preview["commandPreview"])

    def test_build_broadcast_package_requires_exact_confirmation(self) -> None:
        tx = {
            "kind": "approval",
            "chainId": 8453,
            "to": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "from": "0x1111111111111111111111111111111111111111",
            "data": "0x095ea7b3",
            "value": "0",
            "gasLimit": None,
            "gasPrice": None,
        }
        args = type(
            "Args",
            (),
            {
                "private_key_env": "EXECUTOR_PRIVATE_KEY",
                "keystore": None,
                "account": None,
                "interactive": False,
                "password_file": None,
            },
        )()
        with mock.patch.dict("os.environ", {"BASE_RPC_URL": "https://base.example", "EXECUTOR_PRIVATE_KEY": "0x1234"}, clear=False):
            with self.assertRaisesRegex(ValueError, "--confirm must exactly equal"):
                execute_transaction.build_broadcast_package(
                    tx=tx,
                    explicit_rpc_url=None,
                    confirm="wrong",
                signer_args_source=args,
            )

    def test_receipt_succeeded_accepts_hex_and_int_status(self) -> None:
        self.assertTrue(execute_transaction.receipt_succeeded({"status": "0x1"}))
        self.assertTrue(execute_transaction.receipt_succeeded({"status": 1}))
        self.assertFalse(execute_transaction.receipt_succeeded({"status": "0x0"}))

    def test_execute_tx_module_loads_swap_and_approval_transactions_directly(self) -> None:
        swap_blob = {
            "requestPayload": {
                "quote": {
                    "input": {
                        "token": "0x0000000000000000000000000000000000000000",
                        "amount": "1",
                    },
                    "output": {
                        "token": "0x4200000000000000000000000000000000000006",
                        "amount": "2",
                    },
                }
            },
            "swapResponse": {
                "requestId": "req-swap",
                "gasFee": "3",
                "swap": {
                    "to": "0x6fF5693b99212Da76ad316178A184AB56D299b43",
                    "from": "0x1111111111111111111111111111111111111111",
                    "data": "0xdeadbeef",
                    "value": "0x01",
                    "chainId": 8453,
                },
            },
        }
        approval_blob = {
            "approvalCheck": {
                "requestId": "req-approval",
                "approval": {
                    "to": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                    "from": "0x1111111111111111111111111111111111111111",
                    "data": (
                        "0x095ea7b3"
                        "000000000000000000000000000000000022d473030f116ddee9f6b43ac78ba3"
                        "00000000000000000000000000000000000000000000000000000000000f4240"
                    ),
                    "value": "0x00",
                    "chainId": 8453,
                },
            },
            "requestPayloads": {
                "checkApproval": {"amount": "1000000"},
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            swap_path = Path(tmpdir) / "swap.json"
            approval_path = Path(tmpdir) / "approval.json"
            swap_path.write_text(json.dumps(swap_blob), encoding="utf-8")
            approval_path.write_text(json.dumps(approval_blob), encoding="utf-8")
            swap_tx = execute_tx.load_swap_transaction(str(swap_path))
            approval_tx = execute_tx.load_approval_transaction(str(approval_path))

        self.assertTrue(swap_tx["nativeInput"])
        self.assertEqual(swap_tx["inputTokenSymbol"], "NATIVE")
        self.assertEqual(swap_tx["outputTokenSymbol"], "WETH")
        self.assertEqual(approval_tx["approvalSpender"], "0x000000000022d473030f116ddee9f6b43ac78ba3")
        self.assertEqual(approval_tx["requiredAllowance"], "1000000")

    def test_execute_rpc_module_resolves_rpc_and_parses_cast_int_directly(self) -> None:
        with mock.patch.dict("os.environ", {"BASE_RPC_URL": "https://base.example", "ETH_RPC_URL": "https://eth.example"}, clear=True):
            rpc_url, candidates = execute_rpc.resolve_rpc_url(None, 8453)
        self.assertEqual(rpc_url, "https://base.example")
        self.assertEqual(candidates[0], "BASE_RPC_URL")
        self.assertEqual(
            execute_rpc.parse_cast_int_output('"0x10"', "cast call"),
            16,
        )

    def test_execute_signer_module_validates_backend_directly(self) -> None:
        direct_args = execute_signer.build_signer_namespace(private_key_env="EXECUTOR_PRIVATE_KEY")
        with mock.patch.object(execute_signer, "_pure_signer_available", return_value=True):
            self.assertEqual(execute_signer.ensure_signer_backend(direct_args), "pure-python")

    def test_execute_submit_module_builds_package_and_extracts_hash_directly(self) -> None:
        tx = {
            "kind": "approval",
            "chainId": 8453,
            "to": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "from": "0x1111111111111111111111111111111111111111",
            "data": "0x095ea7b3",
            "value": "0",
            "gasLimit": "21000",
            "gasPrice": None,
        }
        args = execute_signer.build_signer_namespace(private_key_env="EXECUTOR_PRIVATE_KEY")
        with mock.patch.dict("os.environ", {"BASE_RPC_URL": "https://base.example", "EXECUTOR_PRIVATE_KEY": "0x1234"}, clear=False):
            package = execute_submit.build_broadcast_package(
                tx=tx,
                explicit_rpc_url=None,
                confirm="BROADCAST APPROVAL 8453 0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                signer_args_source=args,
            )
        self.assertEqual(package["signerBackend"], "pure-python")
        self.assertEqual(
            execute_submit.extract_transaction_hash({"hash": "0xabc"}),
            "0xabc",
        )

    def test_execute_preflight_module_checks_allowance_directly(self) -> None:
        tx = {
            "kind": "approval",
            "chainId": 8453,
            "chainKey": "base",
            "to": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "from": "0x1111111111111111111111111111111111111111",
            "data": "0x095ea7b3",
            "value": "0",
            "gasLimit": "21000",
            "gasPrice": "10",
            "sourceKind": "quote-file",
            "sourceFile": "/tmp/quote.json",
            "nativeInput": False,
            "approvalToken": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "approvalSpender": "0x000000000022D473030F116dDEE9F6B43aC78BA3",
            "requiredAllowance": "1000000",
        }
        with mock.patch.object(execute_preflight, "query_native_balance", return_value=500000), \
             mock.patch.object(execute_preflight, "query_erc20_allowance", return_value=1000000):
            report = execute_preflight.build_preflight_report(tx, explicit_rpc_url="https://base.example")
        self.assertTrue(report["ok"])
        self.assertTrue(report["allowance"]["alreadySufficient"])

    def test_load_auto_trade_policy_normalizes_rule_and_defaults(self) -> None:
        raw_policy = {
            "enabled": True,
            "allowedChains": ["base"],
            "allowedPairs": [
                {
                    "chain": "base",
                    "tokenIn": "USDC",
                    "tokenOut": "WETH",
                    "maxAmount": "100",
                    "maxSlippage": 0.5,
                }
            ],
            "requirePreflightOk": True,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "policy.json"
            path.write_text(json.dumps(raw_policy), encoding="utf-8")
            policy = run_trade_flow.load_auto_trade_policy(str(path))
        self.assertTrue(policy["enabled"])
        self.assertEqual(policy["allowedChains"], ["base"])
        self.assertEqual(
            policy["allowedPairs"][0]["tokenIn"],
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        )
        self.assertEqual(
            policy["allowedPairs"][0]["tokenOut"],
            "0x4200000000000000000000000000000000000006",
        )
        self.assertFalse(policy["allowedPairs"][0]["allowAutoApproval"])
        self.assertFalse(policy["allowedPairs"][0]["allowAutoBroadcastSwap"])
        self.assertFalse(policy["allowedPairs"][0]["allowAutoSignPermit"])

    def test_evaluate_auto_trade_policy_rejects_amount_and_slippage(self) -> None:
        policy = {
            "enabled": True,
            "allowedChains": ["base"],
            "allowedPairs": [
                {
                    "chain": "base",
                    "tokenIn": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                    "tokenOut": "0x4200000000000000000000000000000000000006",
                    "tokenInLabel": "USDC",
                    "tokenOutLabel": "WETH",
                    "maxAmount": "100",
                    "maxSlippage": 0.5,
                    "allowAutoApproval": False,
                    "allowAutoBroadcastSwap": False,
                }
            ],
            "requirePreflightOk": True,
            "sourceFile": "/tmp/policy.json",
        }
        amount_check = run_trade_flow.evaluate_auto_trade_policy(
            policy=policy,
            chain_name="base",
            token_in_name="USDC",
            token_out_name="WETH",
            amount_value="101",
            slippage=0.5,
        )
        slippage_check = run_trade_flow.evaluate_auto_trade_policy(
            policy=policy,
            chain_name="base",
            token_in_name="USDC",
            token_out_name="WETH",
            amount_value="1",
            slippage=0.8,
        )
        self.assertFalse(amount_check["allowed"])
        self.assertIn("exceeds maxAmount", amount_check["issues"][0])
        self.assertFalse(slippage_check["allowed"])
        self.assertIn("exceeds maxSlippage", slippage_check["issues"][0])

    def test_decode_erc20_approve_call_extracts_spender_and_amount(self) -> None:
        decoded = execute_transaction.decode_erc20_approve_call(
            "0x095ea7b3"
            "000000000000000000000000000000000022d473030f116ddee9f6b43ac78ba3"
            "00000000000000000000000000000000000000000000000000000000000f4240"
        )
        self.assertEqual(decoded["spender"], "0x000000000022d473030f116ddee9f6b43ac78ba3")
        self.assertEqual(decoded["amount"], "1000000")

    def test_build_preflight_report_for_approval_checks_native_and_allowance(self) -> None:
        tx = {
            "kind": "approval",
            "chainId": 8453,
            "chainKey": "base",
            "to": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "from": "0x1111111111111111111111111111111111111111",
            "data": "0x095ea7b3",
            "value": "0",
            "gasLimit": "21000",
            "gasPrice": "10",
            "sourceKind": "quote-file",
            "sourceFile": "/tmp/quote.json",
            "requestId": "req-1",
            "nativeInput": False,
            "approvalToken": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "approvalSpender": "0x000000000022D473030F116dDEE9F6B43aC78BA3",
            "requiredAllowance": "1000000",
        }
        with mock.patch.object(execute_preflight, "query_native_balance", return_value=500000), \
             mock.patch.object(execute_preflight, "query_erc20_allowance", return_value=1000000):
            report = execute_transaction.build_preflight_report(
                tx,
                explicit_rpc_url="https://base.example",
            )
        self.assertTrue(report["ok"])
        self.assertEqual(report["gasCost"], "210000")
        self.assertEqual(report["nativeBalance"], "500000")
        self.assertTrue(report["allowance"]["sufficient"])
        self.assertTrue(report["allowance"]["alreadySufficient"])

    def test_build_preflight_report_for_swap_detects_missing_balance_and_allowance(self) -> None:
        tx = {
            "kind": "swap",
            "chainId": 8453,
            "chainKey": "base",
            "to": "0x6fF5693b99212Da76ad316178A184AB56D299b43",
            "from": "0x1111111111111111111111111111111111111111",
            "data": "0xdeadbeef",
            "value": "0",
            "gasLimit": "100000",
            "gasPrice": "10",
            "sourceKind": "swap-file",
            "sourceFile": "/tmp/swap.json",
            "requestId": "req-1",
            "nativeInput": False,
            "inputToken": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "inputAmount": "1000000",
            "permitVerifier": "0x000000000022D473030F116dDEE9F6B43aC78BA3",
        }
        with mock.patch.object(execute_preflight, "query_native_balance", return_value=10000000), \
             mock.patch.object(execute_preflight, "query_erc20_balance", return_value=500000), \
             mock.patch.object(execute_preflight, "query_erc20_allowance", return_value=0):
            report = execute_transaction.build_preflight_report(
                tx,
                explicit_rpc_url="https://base.example",
            )
        self.assertFalse(report["ok"])
        self.assertFalse(report["inputBalance"]["sufficient"])
        self.assertFalse(report["allowance"]["sufficient"])
        self.assertEqual(len(report["issues"]), 2)

    def test_build_preflight_report_soft_fails_when_rpc_query_errors(self) -> None:
        tx = {
            "kind": "approval",
            "chainId": 8453,
            "chainKey": "base",
            "to": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "from": "0x1111111111111111111111111111111111111111",
            "data": "0x095ea7b3",
            "value": "0",
            "gasLimit": "21000",
            "gasPrice": "10",
            "sourceKind": "quote-file",
            "sourceFile": "/tmp/quote.json",
            "requestId": "req-1",
            "nativeInput": False,
        }
        with mock.patch.object(execute_preflight, "query_native_balance", side_effect=RuntimeError("rpc down")):
            report = execute_transaction.build_preflight_report(
                tx,
                explicit_rpc_url="https://base.example",
            )
        self.assertIsNone(report["ok"])
        self.assertFalse(report["checked"])
        self.assertIn("rpc down", report["reason"])

    def test_run_trade_flow_exports_permit_and_approval_preview_when_signature_missing(self) -> None:
        wallet = "0x1111111111111111111111111111111111111111"
        quote_response = {
            "action": "trading_api_quote",
            "chain": {"key": "base", "chainId": 8453},
            "tokenIn": {"symbol": "USDC", "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6},
            "tokenOut": {"symbol": "WETH", "address": "0x4200000000000000000000000000000000000006", "decimals": 18},
            "wallet": wallet,
            "humanAmount": "1",
            "baseAmount": "1000000",
        }
        approval_payload = {
            "walletAddress": wallet,
            "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "amount": "1000000",
            "chainId": 8453,
        }
        quote_payload = {
            "swapper": wallet,
            "tokenIn": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "tokenOut": "0x4200000000000000000000000000000000000006",
            "tokenInChainId": 8453,
            "tokenOutChainId": 8453,
            "amount": "1000000",
            "type": "EXACT_INPUT",
            "slippageTolerance": 0.5,
            "routingPreference": "BEST_PRICE",
        }
        approval_response = {
            "requestId": "req-approve",
            "approval": {
                "to": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "from": wallet,
                "data": "0x095ea7b3",
                "value": "0x00",
                "chainId": 8453,
            },
        }
        raw_quote = {
            "routing": "CLASSIC",
            "permitData": {
                "domain": {
                    "name": "Permit2",
                    "chainId": 8453,
                    "verifyingContract": "0x000000000022D473030F116dDEE9F6B43aC78BA3",
                },
                "types": {
                    "PermitSingle": [
                        {"name": "details", "type": "PermitDetails"},
                        {"name": "spender", "type": "address"},
                        {"name": "sigDeadline", "type": "uint256"},
                    ],
                    "PermitDetails": [
                        {"name": "token", "type": "address"},
                        {"name": "amount", "type": "uint160"},
                        {"name": "expiration", "type": "uint48"},
                        {"name": "nonce", "type": "uint48"},
                    ],
                },
                "values": {
                    "details": {
                        "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                        "amount": "1",
                        "expiration": "2",
                        "nonce": "3",
                    },
                    "spender": "0x6ff5693b99212da76ad316178a184ab56d299b43",
                    "sigDeadline": "4",
                },
            },
            "quote": {
                "quoteId": "abc",
                "input": {"token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "amount": "1000000"},
                "output": {"token": "0x4200000000000000000000000000000000000006", "amount": "1", "recipient": wallet},
                "gasFee": "1",
                "gasUseEstimate": "2",
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            policy_path = Path(tmpdir) / "policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "enabled": True,
                        "allowedChains": ["base"],
                        "allowedPairs": [
                            {
                                "chain": "base",
                                "tokenIn": "USDC",
                                "tokenOut": "WETH",
                                "maxAmount": "100",
                                "maxSlippage": 0.5,
                            }
                        ],
                        "requirePreflightOk": True,
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(trading_api_quote, "load_local_env"),
                mock.patch.object(trading_api_quote, "require_api_key", return_value="test-key"),
                mock.patch.object(trading_api_quote, "prepare_quote_request_data", return_value=(quote_response, approval_payload, quote_payload)),
                mock.patch.object(trading_api_quote, "post_json", side_effect=[approval_response, raw_quote]),
            ):
                response = run_trade_flow.run_trade_flow(
                    chain="base",
                    token_in="USDC",
                    token_out="WETH",
                    amount="1",
                    wallet=wallet,
                    output_dir=tmpdir,
                    policy_file=str(policy_path),
                )
            self.assertTrue(Path(tmpdir, "quote.json").exists())
            self.assertTrue(Path(tmpdir, "permit.json").exists())
            self.assertTrue(Path(tmpdir, "typed-data.json").exists())
        self.assertTrue(response["approval"]["required"])
        self.assertTrue(response["permit"]["required"])
        self.assertTrue(response["policyCheck"]["allowed"])
        self.assertIn("broadcast-approval", response["nextActions"])
        self.assertIn("sign-permit", response["nextActions"])
        self.assertNotIn("swap", response)

    def test_run_trade_flow_paper_trade_records_quote_only_journal_when_permit_signature_missing(self) -> None:
        wallet = "0x1111111111111111111111111111111111111111"
        quote_response = {
            "action": "trading_api_quote",
            "chain": {"key": "base", "chainId": 8453},
            "tokenIn": {"symbol": "USDC", "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6},
            "tokenOut": {"symbol": "WETH", "address": "0x4200000000000000000000000000000000000006", "decimals": 18},
            "wallet": wallet,
            "humanAmount": "1",
            "baseAmount": "1000000",
        }
        approval_payload = {
            "walletAddress": wallet,
            "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "amount": "1000000",
            "chainId": 8453,
        }
        quote_payload = {
            "swapper": wallet,
            "tokenIn": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "tokenOut": "0x4200000000000000000000000000000000000006",
            "tokenInChainId": 8453,
            "tokenOutChainId": 8453,
            "amount": "1000000",
            "type": "EXACT_INPUT",
            "slippageTolerance": 0.5,
            "routingPreference": "BEST_PRICE",
        }
        approval_response = {
            "requestId": "req-approve",
            "approval": {
                "to": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "from": wallet,
                "data": "0x095ea7b3",
                "value": "0x00",
                "chainId": 8453,
            },
        }
        raw_quote = {
            "routing": "CLASSIC",
            "permitData": {
                "domain": {
                    "name": "Permit2",
                    "chainId": 8453,
                    "verifyingContract": "0x000000000022D473030F116dDEE9F6B43aC78BA3",
                },
                "types": {
                    "PermitSingle": [
                        {"name": "details", "type": "PermitDetails"},
                        {"name": "spender", "type": "address"},
                        {"name": "sigDeadline", "type": "uint256"},
                    ],
                    "PermitDetails": [
                        {"name": "token", "type": "address"},
                        {"name": "amount", "type": "uint160"},
                        {"name": "expiration", "type": "uint48"},
                        {"name": "nonce", "type": "uint48"},
                    ],
                },
                "values": {
                    "details": {
                        "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                        "amount": "1",
                        "expiration": "2",
                        "nonce": "3",
                    },
                    "spender": "0x6ff5693b99212da76ad316178a184ab56d299b43",
                    "sigDeadline": "4",
                },
            },
            "quote": {
                "quoteId": "abc",
                "input": {"token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "amount": "1000000"},
                "output": {"token": "0x4200000000000000000000000000000000000006", "amount": "1", "recipient": wallet},
                "gasFee": "1",
                "gasFeeUSD": "0.01",
                "gasUseEstimate": "2",
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                mock.patch.object(run_trade_flow, "load_local_env"),
                mock.patch.object(trading_api_quote, "require_api_key", return_value="test-key"),
                mock.patch.object(trading_api_quote, "prepare_quote_request_data", return_value=(quote_response, approval_payload, quote_payload)),
                mock.patch.object(trading_api_quote, "post_json", side_effect=[approval_response, raw_quote]),
                mock.patch.object(execute_transaction, "build_preflight_report", return_value={"checked": True, "ok": True, "issues": []}),
                mock.patch.object(swap_dry_run, "post_json") as swap_post_json,
            ):
                response = run_trade_flow.run_trade_flow(
                    chain="base",
                    token_in="USDC",
                    token_out="WETH",
                    amount="1",
                    wallet=wallet,
                    output_dir=tmpdir,
                    paper_trade=True,
                )

            swap_post_json.assert_not_called()
            journal_path = Path(tmpdir) / "paper-trade-journal.jsonl"
            self.assertTrue(journal_path.exists())
            journal_lines = journal_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(journal_lines), 1)
            journal_entry = json.loads(journal_lines[0])

            run_output_dir = Path(response["paperTrade"]["runOutputDir"])
            self.assertTrue(run_output_dir.is_dir())
            self.assertTrue((run_output_dir / "quote.json").exists())
            self.assertTrue((run_output_dir / "permit.json").exists())
            self.assertTrue((run_output_dir / "typed-data.json").exists())
            self.assertEqual(response["paperTrade"]["status"], "recorded")
            self.assertEqual(response["paperTrade"]["swapPreviewSource"], "quote-only")
            self.assertTrue(response["permit"]["paperSignatureBypassed"])
            self.assertTrue(response["swap"]["quoteOnly"])
            self.assertEqual(journal_entry["status"], "recorded")
            self.assertEqual(journal_entry["swap"]["paperOnlyReason"], "permit signature is unavailable, paper-trade records quote-only preview")
            self.assertEqual(journal_entry["runOutputDir"], str(run_output_dir))

    def test_run_trade_flow_paper_trade_records_swap_preview_and_journal_for_native_trade(self) -> None:
        wallet = "0x1111111111111111111111111111111111111111"
        quote_response = {
            "action": "trading_api_quote",
            "chain": {"key": "base", "chainId": 8453},
            "tokenIn": {"symbol": "ETH", "address": "NATIVE", "decimals": 18},
            "tokenOut": {"symbol": "USDC", "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6},
            "wallet": wallet,
            "humanAmount": "0.01",
            "baseAmount": "10000000000000000",
        }
        quote_payload = {
            "swapper": wallet,
            "tokenIn": "0x0000000000000000000000000000000000000000",
            "tokenOut": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "tokenInChainId": 8453,
            "tokenOutChainId": 8453,
            "amount": "10000000000000000",
            "type": "EXACT_INPUT",
            "slippageTolerance": 0.5,
            "routingPreference": "BEST_PRICE",
        }
        raw_quote = {
            "routing": "CLASSIC",
            "quote": {
                "quoteId": "native-quote",
                "input": {"token": "0x0000000000000000000000000000000000000000", "amount": "10000000000000000"},
                "output": {"token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "amount": "30000000", "recipient": wallet},
                "gasFee": "12345",
                "gasUseEstimate": "54321",
            },
        }
        swap_response = {
            "requestId": "req-swap",
            "gasFee": "12345",
            "swap": {
                "to": "0x6fF5693b99212Da76ad316178A184AB56D299b43",
                "from": wallet,
                "data": "0xdeadbeef",
                "value": "0x2386f26fc10000",
                "chainId": 8453,
                "gasLimit": "97000",
                "gasPrice": "9000000",
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                mock.patch.object(run_trade_flow, "load_local_env"),
                mock.patch.object(trading_api_quote, "require_api_key", return_value="test-key"),
                mock.patch.object(trading_api_quote, "prepare_quote_request_data", return_value=(quote_response, None, quote_payload)),
                mock.patch.object(trading_api_quote, "post_json", return_value=raw_quote),
                mock.patch.object(swap_dry_run, "post_json", return_value=swap_response),
                mock.patch.object(execute_transaction, "build_preflight_report", return_value={"checked": True, "ok": True, "issues": []}),
            ):
                response = run_trade_flow.run_trade_flow(
                    chain="base",
                    token_in="NATIVE",
                    token_out="USDC",
                    amount="0.01",
                    wallet=wallet,
                    output_dir=tmpdir,
                    paper_trade=True,
                )

            journal_path = Path(tmpdir) / "paper-trade-journal.jsonl"
            journal_lines = journal_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(journal_lines), 1)
            journal_entry = json.loads(journal_lines[0])

            run_output_dir = Path(response["paperTrade"]["runOutputDir"])
            self.assertTrue((run_output_dir / "quote.json").exists())
            self.assertTrue((run_output_dir / "swap.json").exists())
            self.assertEqual(response["paperTrade"]["swapPreviewSource"], "swap-response")
            self.assertFalse(response["swap"].get("quoteOnly", False))
            self.assertEqual(response["swap"]["summary"]["to"], "0x6fF5693b99212Da76ad316178A184AB56D299b43")
            self.assertEqual(journal_entry["swap"]["summary"]["to"], "0x6fF5693b99212Da76ad316178A184AB56D299b43")
            self.assertEqual(journal_entry["status"], "recorded")

    def test_run_trade_flow_with_signature_reuses_saved_quote_and_builds_swap_preview_and_fallback(self) -> None:
        wallet = "0x1111111111111111111111111111111111111111"
        saved_quote_response = {
            "action": "trading_api_quote",
            "chain": {"key": "base", "chainId": 8453},
            "tokenIn": {"symbol": "ETH", "address": "NATIVE", "decimals": 18},
            "tokenOut": {"symbol": "USDC", "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6},
            "wallet": wallet,
            "humanAmount": "0.01",
            "baseAmount": "10000000000000000",
            "quoteSummary": {"routing": "CLASSIC", "outputAmount": "30000000"},
        }
        raw_quote = {
            "routing": "CLASSIC",
            "permitData": {
                "domain": {
                    "name": "Permit2",
                    "chainId": 8453,
                    "verifyingContract": "0x000000000022D473030F116dDEE9F6B43aC78BA3",
                },
                "types": {
                    "PermitSingle": [
                        {"name": "details", "type": "PermitDetails"},
                        {"name": "spender", "type": "address"},
                        {"name": "sigDeadline", "type": "uint256"},
                    ],
                    "PermitDetails": [
                        {"name": "token", "type": "address"},
                        {"name": "amount", "type": "uint160"},
                        {"name": "expiration", "type": "uint48"},
                        {"name": "nonce", "type": "uint48"},
                    ],
                },
                "values": {
                    "details": {
                        "token": "0x0000000000000000000000000000000000000000",
                        "amount": "1",
                        "expiration": "2",
                        "nonce": "3",
                    },
                    "spender": "0x6ff5693b99212da76ad316178a184ab56d299b43",
                    "sigDeadline": "4",
                },
            },
            "quote": {
                "quoteId": "abc",
                "input": {"token": "0x0000000000000000000000000000000000000000", "amount": "10000000000000000"},
                "output": {"token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "amount": "30000000", "recipient": wallet},
                "gasFee": "1",
                "gasUseEstimate": "2",
            },
        }
        saved_quote_response["rawQuote"] = raw_quote
        swap_response = {
            "requestId": "req-swap",
            "gasFee": "1",
            "swap": {
                "to": "0x6fF5693b99212Da76ad316178A184AB56D299b43",
                "from": wallet,
                "data": "0xdeadbeef",
                "value": "0x2386f26fc10000",
                "chainId": 8453,
                "gasLimit": "163916",
                "gasPrice": "9000000",
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            signature_path = Path(tmpdir) / "signature.txt"
            signature_path.write_text("0x1234", encoding="utf-8")
            quote_path = Path(tmpdir) / "quote.json"
            quote_path.write_text(json.dumps(saved_quote_response), encoding="utf-8")
            with (
                mock.patch.object(trading_api_quote, "load_local_env"),
                mock.patch.object(trading_api_quote, "require_api_key", return_value="test-key"),
                mock.patch.object(trading_api_quote, "prepare_quote_request_data") as prepare_quote_request_data,
                mock.patch.object(trading_api_quote, "post_json") as trading_api_post_json,
                mock.patch.object(
                    swap_dry_run,
                    "post_json",
                    side_effect=[
                        RuntimeError("Trading API HTTP 404: execution reverted: TRANSFER_FROM_FAILED"),
                        swap_response,
                    ],
                ),
            ):
                response = run_trade_flow.run_trade_flow(
                    chain="base",
                    token_in="NATIVE",
                    token_out="USDC",
                    amount="0.01",
                    wallet=wallet,
                    output_dir=tmpdir,
                    signature_file=str(signature_path),
                )
            self.assertTrue(Path(tmpdir, "swap.json").exists())
        prepare_quote_request_data.assert_not_called()
        trading_api_post_json.assert_not_called()
        self.assertTrue(response["quote"]["reusedQuote"])
        self.assertTrue(response["permit"]["required"])
        self.assertTrue(response["permit"]["signatureProvided"])
        self.assertFalse(response["swap"]["simulationUsed"])
        self.assertIn("TRANSFER_FROM_FAILED", response["swap"]["simulationFallbackReason"])
        self.assertIn("broadcast-swap", response["nextActions"])

    def test_run_trade_flow_infers_approval_ready_when_allowance_is_already_sufficient(self) -> None:
        wallet = "0x1111111111111111111111111111111111111111"
        quote_response = {
            "action": "trading_api_quote",
            "chain": {"key": "base", "chainId": 8453},
            "tokenIn": {"symbol": "USDC", "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6},
            "tokenOut": {"symbol": "WETH", "address": "0x4200000000000000000000000000000000000006", "decimals": 18},
            "wallet": wallet,
            "humanAmount": "1",
            "baseAmount": "1000000",
        }
        approval_payload = {
            "walletAddress": wallet,
            "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "amount": "1000000",
            "chainId": 8453,
        }
        quote_payload = {
            "swapper": wallet,
            "tokenIn": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "tokenOut": "0x4200000000000000000000000000000000000006",
            "tokenInChainId": 8453,
            "tokenOutChainId": 8453,
            "amount": "1000000",
            "type": "EXACT_INPUT",
            "slippageTolerance": 0.5,
            "routingPreference": "BEST_PRICE",
        }
        approval_response = {
            "requestId": "req-approve",
            "approval": {
                "to": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "from": wallet,
                "data": "0x095ea7b3",
                "value": "0x00",
                "chainId": 8453,
            },
        }
        raw_quote = {
            "routing": "CLASSIC",
            "quote": {
                "quoteId": "abc",
                "input": {"token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "amount": "1000000"},
                "output": {"token": "0x4200000000000000000000000000000000000006", "amount": "1", "recipient": wallet},
                "gasFee": "1",
                "gasUseEstimate": "2",
            },
        }
        swap_response = {
            "requestId": "req-swap",
            "gasFee": "1",
            "swap": {
                "to": "0x6fF5693b99212Da76ad316178A184AB56D299b43",
                "from": wallet,
                "data": "0xdeadbeef",
                "value": "0x00",
                "chainId": 8453,
                "gasLimit": "97000",
                "gasPrice": "9000000",
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                mock.patch.object(trading_api_quote, "load_local_env"),
                mock.patch.object(trading_api_quote, "require_api_key", return_value="test-key"),
                mock.patch.object(trading_api_quote, "prepare_quote_request_data", return_value=(quote_response, approval_payload, quote_payload)),
                mock.patch.object(trading_api_quote, "post_json", side_effect=[approval_response, raw_quote]),
                mock.patch.object(
                    execute_transaction,
                    "build_preflight_report",
                    side_effect=[
                        {"checked": True, "ok": True, "issues": [], "allowance": {"alreadySufficient": True}},
                        {"checked": True, "ok": True, "issues": []},
                    ],
                ),
                mock.patch.object(swap_dry_run, "post_json", return_value=swap_response),
            ):
                response = run_trade_flow.run_trade_flow(
                    chain="base",
                    token_in="USDC",
                    token_out="WETH",
                    amount="1",
                    wallet=wallet,
                    output_dir=tmpdir,
                )

        self.assertTrue(response["approval"]["required"])
        self.assertTrue(response["approval"]["assumeApprovalReady"])
        self.assertTrue(response["approval"]["assumeApprovalReadyInferred"])
        self.assertIn("approval-already-sufficient", response["nextActions"])
        self.assertNotIn("broadcast-approval", response["nextActions"])
        self.assertNotIn("ensure-approval-mined", response["nextActions"])

    def test_run_trade_flow_swap_failure_reports_insufficient_input_balance(self) -> None:
        wallet = "0x1111111111111111111111111111111111111111"
        raw_quote = {
            "routing": "CLASSIC",
            "quote": {
                "quoteId": "abc",
                "input": {"token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "amount": "1000000"},
                "output": {"token": "0x4200000000000000000000000000000000000006", "amount": "1", "recipient": wallet},
                "gasFee": "1000",
                "gasUseEstimate": "2",
            },
        }
        saved_quote_response = {
            "action": "trading_api_quote",
            "chain": {"key": "base", "chainId": 8453},
            "tokenIn": {"symbol": "USDC", "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6},
            "tokenOut": {"symbol": "WETH", "address": "0x4200000000000000000000000000000000000006", "decimals": 18},
            "wallet": wallet,
            "humanAmount": "1",
            "baseAmount": "1000000",
            "rawQuote": raw_quote,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            quote_path = Path(tmpdir) / "quote.json"
            quote_path.write_text(json.dumps(saved_quote_response), encoding="utf-8")
            with (
                mock.patch.object(trading_api_quote, "load_local_env"),
                mock.patch.object(trading_api_quote, "require_api_key", return_value="test-key"),
                mock.patch.object(
                    swap_dry_run,
                    "post_json",
                    side_effect=RuntimeError("Trading API HTTP 404: FAILED_TO_ESTIMATE_GAS: execution reverted"),
                ),
                mock.patch.object(execute_transaction, "resolve_rpc_url", return_value=("https://base.example", ["BASE_RPC_URL"])),
                mock.patch.object(execute_transaction, "query_native_balance", return_value=10_000_000_000_000),
                mock.patch.object(execute_transaction, "query_erc20_balance", return_value=0),
            ):
                with self.assertRaisesRegex(RuntimeError, "insufficient_input_balance"):
                    run_trade_flow.run_trade_flow(
                        chain="base",
                        token_in="USDC",
                        token_out="WETH",
                        amount="1",
                        wallet=wallet,
                        output_dir=tmpdir,
                        quote_file=str(quote_path),
                    )

    def test_run_trade_flow_auto_signs_permit_when_policy_allows(self) -> None:
        wallet = "0x1111111111111111111111111111111111111111"
        quote_response = {
            "action": "trading_api_quote",
            "chain": {"key": "base", "chainId": 8453},
            "tokenIn": {"symbol": "USDC", "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6},
            "tokenOut": {"symbol": "WETH", "address": "0x4200000000000000000000000000000000000006", "decimals": 18},
            "wallet": wallet,
            "humanAmount": "1",
            "baseAmount": "1000000",
        }
        approval_payload = {
            "walletAddress": wallet,
            "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "amount": "1000000",
            "chainId": 8453,
        }
        quote_payload = {
            "swapper": wallet,
            "tokenIn": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "tokenOut": "0x4200000000000000000000000000000000000006",
            "tokenInChainId": 8453,
            "tokenOutChainId": 8453,
            "amount": "1000000",
            "type": "EXACT_INPUT",
            "slippageTolerance": 0.5,
            "routingPreference": "BEST_PRICE",
        }
        approval_response = {
            "requestId": "req-approve",
            "approval": {
                "to": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "from": wallet,
                "data": "0x095ea7b3",
                "value": "0x00",
                "chainId": 8453,
            },
        }
        raw_quote = {
            "routing": "CLASSIC",
            "permitData": {
                "domain": {
                    "name": "Permit2",
                    "chainId": 8453,
                    "verifyingContract": "0x000000000022D473030F116dDEE9F6B43aC78BA3",
                },
                "types": {
                    "PermitSingle": [
                        {"name": "details", "type": "PermitDetails"},
                        {"name": "spender", "type": "address"},
                        {"name": "sigDeadline", "type": "uint256"},
                    ],
                    "PermitDetails": [
                        {"name": "token", "type": "address"},
                        {"name": "amount", "type": "uint160"},
                        {"name": "expiration", "type": "uint48"},
                        {"name": "nonce", "type": "uint48"},
                    ],
                },
                "values": {
                    "details": {
                        "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                        "amount": "1",
                        "expiration": "2",
                        "nonce": "3",
                    },
                    "spender": "0x6ff5693b99212da76ad316178a184ab56d299b43",
                    "sigDeadline": "4",
                },
            },
            "quote": {
                "quoteId": "abc",
                "input": {"token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "amount": "1000000"},
                "output": {"token": "0x4200000000000000000000000000000000000006", "amount": "1", "recipient": wallet},
                "gasFee": "1",
                "gasUseEstimate": "2",
            },
        }
        swap_response = {
            "requestId": "req-swap",
            "gasFee": "1",
            "swap": {
                "to": "0x6fF5693b99212Da76ad316178A184AB56D299b43",
                "from": wallet,
                "data": "0xdeadbeef",
                "value": "0x00",
                "chainId": 8453,
                "gasLimit": "97000",
                "gasPrice": "9000000",
            },
        }
        signer_args = type(
            "Args",
            (),
            {
                "private_key_env": "EXECUTOR_PRIVATE_KEY",
                "keystore": None,
                "account": None,
                "interactive": False,
                "password_file": None,
            },
        )()

        with tempfile.TemporaryDirectory() as tmpdir:
            policy_path = Path(tmpdir) / "policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "enabled": True,
                        "allowedChains": ["base"],
                        "allowedPairs": [
                            {
                                "chain": "base",
                                "tokenIn": "USDC",
                                "tokenOut": "WETH",
                                "maxAmount": "100",
                                "maxSlippage": 0.5,
                                "allowAutoSignPermit": True,
                            }
                        ],
                        "requirePreflightOk": True,
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.dict("os.environ", {"EXECUTOR_PRIVATE_KEY": "0x1234"}, clear=False),
                mock.patch.object(trading_api_quote, "load_local_env"),
                mock.patch.object(trading_api_quote, "require_api_key", return_value="test-key"),
                mock.patch.object(trading_api_quote, "prepare_quote_request_data", return_value=(quote_response, approval_payload, quote_payload)),
                mock.patch.object(trading_api_quote, "post_json", side_effect=[approval_response, raw_quote]),
                mock.patch.object(execute_transaction, "build_preflight_report", return_value={"checked": True, "ok": True, "issues": []}),
                mock.patch.object(execute_transaction, "sign_typed_data_with_backend", return_value={"signature": "0x1234", "signCommandPreview": "pure_signer", "signerBackend": "pure-python"}) as sign_typed_data_with_backend,
                mock.patch.object(swap_dry_run, "post_json", return_value=swap_response),
            ):
                response = run_trade_flow.run_trade_flow(
                    chain="base",
                    token_in="USDC",
                    token_out="WETH",
                    amount="1",
                    wallet=wallet,
                    output_dir=tmpdir,
                    policy_file=str(policy_path),
                    auto_sign_permit=True,
                    signer_args_source=signer_args,
                )
            self.assertTrue(Path(tmpdir, "signature.txt").exists())
        sign_typed_data_with_backend.assert_called_once()
        self.assertTrue(response["permit"]["autoSigned"])
        self.assertTrue(response["permit"]["signatureProvided"])
        self.assertEqual(response["files"]["signature"], str(Path(tmpdir) / "signature.txt"))
        self.assertIn("swap", response)

    def test_run_trade_flow_auto_execute_requires_policy_file(self) -> None:
        with mock.patch.object(trading_api_quote, "load_local_env"):
            with self.assertRaisesRegex(ValueError, "--auto-execute requires --policy-file"):
                run_trade_flow.run_trade_flow(
                    chain="base",
                    token_in="NATIVE",
                    token_out="USDC",
                    amount="0.01",
                    wallet="0x1111111111111111111111111111111111111111",
                    output_dir="/tmp/uniswap-auto-execute",
                    auto_execute=True,
                )

    def test_run_trade_flow_paper_trade_rejects_auto_execute(self) -> None:
        with self.assertRaisesRegex(ValueError, "--paper-trade cannot be combined with --auto-execute"):
            run_trade_flow.run_trade_flow(
                chain="base",
                token_in="NATIVE",
                token_out="USDC",
                amount="0.01",
                wallet="0x1111111111111111111111111111111111111111",
                output_dir="/tmp/uniswap-paper-trade",
                auto_execute=True,
                paper_trade=True,
            )

    def test_run_trade_flow_auto_execute_requires_signer_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            policy_path = Path(tmpdir) / "policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "enabled": True,
                        "allowedChains": ["base"],
                        "allowedPairs": [
                            {
                                "chain": "base",
                                "tokenIn": "NATIVE",
                                "tokenOut": "USDC",
                                "maxAmount": "0.1",
                                "maxSlippage": 0.5,
                                "allowAutoBroadcastSwap": True,
                            }
                        ],
                        "requirePreflightOk": True,
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(trading_api_quote, "load_local_env"), mock.patch.object(
                execute_transaction,
                "auto_select_signer_args",
                return_value=None,
            ):
                with self.assertRaisesRegex(ValueError, "--auto-execute requires signer args"):
                    run_trade_flow.run_trade_flow(
                        chain="base",
                        token_in="NATIVE",
                        token_out="USDC",
                        amount="0.01",
                        wallet="0x1111111111111111111111111111111111111111",
                        output_dir=tmpdir,
                        policy_file=str(policy_path),
                        auto_execute=True,
                    )

    def test_run_trade_flow_auto_execute_broadcasts_approval_and_swap_when_policy_allows(self) -> None:
        wallet = "0x1111111111111111111111111111111111111111"
        quote_response = {
            "action": "trading_api_quote",
            "chain": {"key": "base", "chainId": 8453},
            "tokenIn": {"symbol": "USDC", "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6},
            "tokenOut": {"symbol": "WETH", "address": "0x4200000000000000000000000000000000000006", "decimals": 18},
            "wallet": wallet,
            "humanAmount": "1",
            "baseAmount": "1000000",
        }
        approval_payload = {
            "walletAddress": wallet,
            "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "amount": "1000000",
            "chainId": 8453,
        }
        quote_payload = {
            "swapper": wallet,
            "tokenIn": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "tokenOut": "0x4200000000000000000000000000000000000006",
            "tokenInChainId": 8453,
            "tokenOutChainId": 8453,
            "amount": "1000000",
            "type": "EXACT_INPUT",
            "slippageTolerance": 0.5,
            "routingPreference": "BEST_PRICE",
        }
        approval_response = {
            "requestId": "req-approve",
            "approval": {
                "to": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "from": wallet,
                "data": "0x095ea7b3",
                "value": "0x00",
                "chainId": 8453,
            },
        }
        raw_quote = {
            "routing": "CLASSIC",
            "permitData": {
                "domain": {
                    "name": "Permit2",
                    "chainId": 8453,
                    "verifyingContract": "0x000000000022D473030F116dDEE9F6B43aC78BA3",
                },
                "types": {
                    "PermitSingle": [
                        {"name": "details", "type": "PermitDetails"},
                        {"name": "spender", "type": "address"},
                        {"name": "sigDeadline", "type": "uint256"},
                    ],
                    "PermitDetails": [
                        {"name": "token", "type": "address"},
                        {"name": "amount", "type": "uint160"},
                        {"name": "expiration", "type": "uint48"},
                        {"name": "nonce", "type": "uint48"},
                    ],
                },
                "values": {
                    "details": {
                        "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                        "amount": "1",
                        "expiration": "2",
                        "nonce": "3",
                    },
                    "spender": "0x6ff5693b99212da76ad316178a184ab56d299b43",
                    "sigDeadline": "4",
                },
            },
            "quote": {
                "quoteId": "abc",
                "input": {"token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "amount": "1000000"},
                "output": {"token": "0x4200000000000000000000000000000000000006", "amount": "1", "recipient": wallet},
                "gasFee": "1",
                "gasUseEstimate": "2",
            },
        }
        swap_response = {
            "requestId": "req-swap",
            "gasFee": "1",
            "swap": {
                "to": "0x6fF5693b99212Da76ad316178A184AB56D299b43",
                "from": wallet,
                "data": "0xdeadbeef",
                "value": "0x00",
                "chainId": 8453,
                "gasLimit": "97000",
                "gasPrice": "9000000",
            },
        }
        signer_args = type(
            "Args",
            (),
            {
                "private_key_env": "EXECUTOR_PRIVATE_KEY",
                "keystore": None,
                "account": None,
                "interactive": False,
                "password_file": None,
            },
        )()

        with tempfile.TemporaryDirectory() as tmpdir:
            policy_path = Path(tmpdir) / "policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "enabled": True,
                        "allowedChains": ["base"],
                        "allowedPairs": [
                            {
                                "chain": "base",
                                "tokenIn": "USDC",
                                "tokenOut": "WETH",
                                "maxAmount": "100",
                                "maxSlippage": 0.5,
                                "allowAutoSignPermit": True,
                                "allowAutoApproval": True,
                                "allowAutoBroadcastSwap": True,
                            }
                        ],
                        "requirePreflightOk": True,
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.dict("os.environ", {"BASE_RPC_URL": "https://base.example", "EXECUTOR_PRIVATE_KEY": "0x1234"}, clear=False),
                mock.patch.object(trading_api_quote, "load_local_env"),
                mock.patch.object(trading_api_quote, "require_api_key", return_value="test-key"),
                mock.patch.object(trading_api_quote, "prepare_quote_request_data", return_value=(quote_response, approval_payload, quote_payload)),
                mock.patch.object(trading_api_quote, "post_json", side_effect=[approval_response, raw_quote]),
                mock.patch.object(execute_transaction, "build_preflight_report", return_value={"checked": True, "ok": True, "issues": []}),
                mock.patch.object(execute_transaction, "sign_typed_data_with_backend", return_value={"signature": "0x1234", "signCommandPreview": "pure_signer", "signerBackend": "pure-python"}) as sign_typed_data_with_backend,
                mock.patch.object(swap_dry_run, "post_json", return_value=swap_response),
                mock.patch.object(
                    run_trade_flow,
                    "maybe_broadcast",
                    side_effect=[
                        {"transactionHash": "0xapprove", "receipt": {"status": "0x1"}, "signerBackend": "pure-python"},
                        {"transactionHash": "0xswap", "receipt": {"status": "0x1"}, "signerBackend": "pure-python"},
                    ],
                ) as maybe_broadcast,
            ):
                response = run_trade_flow.run_trade_flow(
                    chain="base",
                    token_in="USDC",
                    token_out="WETH",
                    amount="1",
                    wallet=wallet,
                    output_dir=tmpdir,
                    policy_file=str(policy_path),
                    signer_args_source=signer_args,
                    auto_execute=True,
                )
            self.assertTrue(Path(tmpdir, "signature.txt").exists())
        sign_typed_data_with_backend.assert_called_once()
        self.assertEqual(maybe_broadcast.call_count, 2)
        self.assertTrue(response["automation"]["autoExecuteRequested"])
        self.assertTrue(response["permit"]["autoSigned"])
        self.assertTrue(response["approval"]["autoBroadcast"])
        self.assertTrue(response["swap"]["autoBroadcast"])
        self.assertEqual(response["approval"]["transactionHash"], "0xapprove")
        self.assertEqual(response["swap"]["transactionHash"], "0xswap")
        self.assertEqual(response["nextActions"], [])

    def test_run_trade_flow_auto_execute_rejects_before_broadcast_when_swap_policy_is_missing(self) -> None:
        wallet = "0x1111111111111111111111111111111111111111"
        quote_response = {
            "action": "trading_api_quote",
            "chain": {"key": "base", "chainId": 8453},
            "tokenIn": {"symbol": "USDC", "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6},
            "tokenOut": {"symbol": "WETH", "address": "0x4200000000000000000000000000000000000006", "decimals": 18},
            "wallet": wallet,
            "humanAmount": "1",
            "baseAmount": "1000000",
        }
        approval_payload = {
            "walletAddress": wallet,
            "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "amount": "1000000",
            "chainId": 8453,
        }
        quote_payload = {
            "swapper": wallet,
            "tokenIn": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "tokenOut": "0x4200000000000000000000000000000000000006",
            "tokenInChainId": 8453,
            "tokenOutChainId": 8453,
            "amount": "1000000",
            "type": "EXACT_INPUT",
            "slippageTolerance": 0.5,
            "routingPreference": "BEST_PRICE",
        }
        approval_response = {
            "requestId": "req-approve",
            "approval": {
                "to": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "from": wallet,
                "data": "0x095ea7b3",
                "value": "0x00",
                "chainId": 8453,
            },
        }
        raw_quote = {
            "routing": "CLASSIC",
            "permitData": {
                "domain": {
                    "name": "Permit2",
                    "chainId": 8453,
                    "verifyingContract": "0x000000000022D473030F116dDEE9F6B43aC78BA3",
                },
                "types": {
                    "PermitSingle": [
                        {"name": "details", "type": "PermitDetails"},
                        {"name": "spender", "type": "address"},
                        {"name": "sigDeadline", "type": "uint256"},
                    ],
                    "PermitDetails": [
                        {"name": "token", "type": "address"},
                        {"name": "amount", "type": "uint160"},
                        {"name": "expiration", "type": "uint48"},
                        {"name": "nonce", "type": "uint48"},
                    ],
                },
                "values": {
                    "details": {
                        "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                        "amount": "1",
                        "expiration": "2",
                        "nonce": "3",
                    },
                    "spender": "0x6ff5693b99212da76ad316178a184ab56d299b43",
                    "sigDeadline": "4",
                },
            },
            "quote": {
                "quoteId": "abc",
                "input": {"token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "amount": "1000000"},
                "output": {"token": "0x4200000000000000000000000000000000000006", "amount": "1", "recipient": wallet},
                "gasFee": "1",
                "gasUseEstimate": "2",
            },
        }
        signer_args = type(
            "Args",
            (),
            {
                "private_key_env": "EXECUTOR_PRIVATE_KEY",
                "keystore": None,
                "account": None,
                "interactive": False,
                "password_file": None,
            },
        )()

        with tempfile.TemporaryDirectory() as tmpdir:
            policy_path = Path(tmpdir) / "policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "enabled": True,
                        "allowedChains": ["base"],
                        "allowedPairs": [
                            {
                                "chain": "base",
                                "tokenIn": "USDC",
                                "tokenOut": "WETH",
                                "maxAmount": "100",
                                "maxSlippage": 0.5,
                                "allowAutoSignPermit": True,
                                "allowAutoApproval": True,
                                "allowAutoBroadcastSwap": False,
                            }
                        ],
                        "requirePreflightOk": True,
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.dict("os.environ", {"EXECUTOR_PRIVATE_KEY": "0x1234"}, clear=False),
                mock.patch.object(trading_api_quote, "load_local_env"),
                mock.patch.object(trading_api_quote, "require_api_key", return_value="test-key"),
                mock.patch.object(trading_api_quote, "prepare_quote_request_data", return_value=(quote_response, approval_payload, quote_payload)),
                mock.patch.object(trading_api_quote, "post_json", side_effect=[approval_response, raw_quote]),
                mock.patch.object(execute_transaction, "sign_typed_data_with_backend") as sign_typed_data_with_backend,
                mock.patch.object(run_trade_flow, "maybe_broadcast") as maybe_broadcast,
            ):
                with self.assertRaisesRegex(ValueError, "allowAutoBroadcastSwap"):
                    run_trade_flow.run_trade_flow(
                        chain="base",
                        token_in="USDC",
                        token_out="WETH",
                        amount="1",
                        wallet=wallet,
                        output_dir=tmpdir,
                        policy_file=str(policy_path),
                        signer_args_source=signer_args,
                        auto_execute=True,
                    )
        sign_typed_data_with_backend.assert_not_called()
        maybe_broadcast.assert_not_called()

    def test_run_trade_flow_requires_existing_quote_when_signature_is_provided(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            signature_path = Path(tmpdir) / "signature.txt"
            signature_path.write_text("0x1234", encoding="utf-8")
            with (
                mock.patch.object(trading_api_quote, "load_local_env"),
                mock.patch.object(trading_api_quote, "require_api_key", return_value="test-key"),
            ):
                with self.assertRaisesRegex(ValueError, "requires an existing quote file"):
                    run_trade_flow.run_trade_flow(
                        chain="base",
                        token_in="NATIVE",
                        token_out="USDC",
                        amount="0.01",
                        wallet="0x1111111111111111111111111111111111111111",
                        output_dir=tmpdir,
                        signature_file=str(signature_path),
                    )

    def test_run_trade_flow_broadcasts_approval_when_confirm_matches(self) -> None:
        wallet = "0x1111111111111111111111111111111111111111"
        quote_response = {
            "action": "trading_api_quote",
            "chain": {"key": "base", "chainId": 8453},
            "tokenIn": {"symbol": "USDC", "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6},
            "tokenOut": {"symbol": "WETH", "address": "0x4200000000000000000000000000000000000006", "decimals": 18},
            "wallet": wallet,
            "humanAmount": "1",
            "baseAmount": "1000000",
        }
        approval_payload = {
            "walletAddress": wallet,
            "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "amount": "1000000",
            "chainId": 8453,
        }
        quote_payload = {
            "swapper": wallet,
            "tokenIn": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "tokenOut": "0x4200000000000000000000000000000000000006",
            "tokenInChainId": 8453,
            "tokenOutChainId": 8453,
            "amount": "1000000",
            "type": "EXACT_INPUT",
            "slippageTolerance": 0.5,
            "routingPreference": "BEST_PRICE",
        }
        approval_response = {
            "requestId": "req-approve",
            "approval": {
                "to": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "from": wallet,
                "data": "0x095ea7b3",
                "value": "0x00",
                "chainId": 8453,
            },
        }
        raw_quote = {
            "routing": "CLASSIC",
            "permitData": {
                "domain": {
                    "name": "Permit2",
                    "chainId": 8453,
                    "verifyingContract": "0x000000000022D473030F116dDEE9F6B43aC78BA3",
                },
                "types": {
                    "PermitSingle": [
                        {"name": "details", "type": "PermitDetails"},
                        {"name": "spender", "type": "address"},
                        {"name": "sigDeadline", "type": "uint256"},
                    ],
                    "PermitDetails": [
                        {"name": "token", "type": "address"},
                        {"name": "amount", "type": "uint160"},
                        {"name": "expiration", "type": "uint48"},
                        {"name": "nonce", "type": "uint48"},
                    ],
                },
                "values": {
                    "details": {
                        "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                        "amount": "1",
                        "expiration": "2",
                        "nonce": "3",
                    },
                    "spender": "0x6ff5693b99212da76ad316178a184ab56d299b43",
                    "sigDeadline": "4",
                },
            },
            "quote": {
                "quoteId": "abc",
                "input": {"token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "amount": "1000000"},
                "output": {"token": "0x4200000000000000000000000000000000000006", "amount": "1", "recipient": wallet},
                "gasFee": "1",
                "gasUseEstimate": "2",
            },
        }
        signer_args = type(
            "Args",
            (),
            {
                "private_key_env": "EXECUTOR_PRIVATE_KEY",
                "keystore": None,
                "account": None,
                "interactive": False,
                "password_file": None,
            },
        )()

        with tempfile.TemporaryDirectory() as tmpdir:
            policy_path = Path(tmpdir) / "policy.json"
            policy_path.write_text(
                json.dumps(
                    {
                        "enabled": True,
                        "allowedChains": ["base"],
                        "allowedPairs": [
                            {
                                "chain": "base",
                                "tokenIn": "USDC",
                                "tokenOut": "WETH",
                                "maxAmount": "100",
                                "maxSlippage": 0.5,
                                "allowAutoApproval": False,
                            }
                        ],
                        "requirePreflightOk": True,
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.dict("os.environ", {"BASE_RPC_URL": "https://base.example", "EXECUTOR_PRIVATE_KEY": "0x1234"}, clear=False),
                mock.patch.object(trading_api_quote, "load_local_env"),
                mock.patch.object(trading_api_quote, "require_api_key", return_value="test-key"),
                mock.patch.object(trading_api_quote, "prepare_quote_request_data", return_value=(quote_response, approval_payload, quote_payload)),
                mock.patch.object(trading_api_quote, "post_json", side_effect=[approval_response, raw_quote]),
                mock.patch.object(execute_transaction, "build_preflight_report", return_value={"checked": True, "ok": True, "issues": []}),
                mock.patch.object(run_trade_flow, "maybe_broadcast", return_value={"transactionHash": "0xabc", "receipt": {"status": "0x1"}}) as maybe_broadcast,
            ):
                with self.assertRaisesRegex(ValueError, "policy does not allow approval broadcast"):
                    run_trade_flow.run_trade_flow(
                        chain="base",
                        token_in="USDC",
                        token_out="WETH",
                        amount="1",
                        wallet=wallet,
                        output_dir=tmpdir,
                        broadcast_approval=True,
                        approval_confirm="BROADCAST APPROVAL 8453 0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                        signer_args_source=signer_args,
                        policy_file=str(policy_path),
                    )
        maybe_broadcast.assert_not_called()

    def test_run_trade_flow_blocks_swap_broadcast_without_approval_gate(self) -> None:
        wallet = "0x1111111111111111111111111111111111111111"
        saved_quote_response = {
            "action": "trading_api_quote",
            "chain": {"key": "base", "chainId": 8453},
            "tokenIn": {"symbol": "USDC", "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6},
            "tokenOut": {"symbol": "WETH", "address": "0x4200000000000000000000000000000000000006", "decimals": 18},
            "wallet": wallet,
            "humanAmount": "1",
            "baseAmount": "1000000",
            "approvalCheck": {
                "requestId": "req-approve",
                "approval": {
                    "to": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                    "from": wallet,
                    "data": "0x095ea7b3",
                    "value": "0x00",
                    "chainId": 8453,
                },
            },
            "quoteSummary": {"routing": "CLASSIC"},
            "rawQuote": {
                "routing": "CLASSIC",
                "permitData": {
                    "domain": {
                        "name": "Permit2",
                        "chainId": 8453,
                        "verifyingContract": "0x000000000022D473030F116dDEE9F6B43aC78BA3",
                    },
                    "types": {
                        "PermitSingle": [
                            {"name": "details", "type": "PermitDetails"},
                            {"name": "spender", "type": "address"},
                            {"name": "sigDeadline", "type": "uint256"},
                        ],
                        "PermitDetails": [
                            {"name": "token", "type": "address"},
                            {"name": "amount", "type": "uint160"},
                            {"name": "expiration", "type": "uint48"},
                            {"name": "nonce", "type": "uint48"},
                        ],
                    },
                    "values": {
                        "details": {
                            "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                            "amount": "1",
                            "expiration": "2",
                            "nonce": "3",
                        },
                        "spender": "0x6ff5693b99212da76ad316178a184ab56d299b43",
                        "sigDeadline": "4",
                    },
                },
                "quote": {
                    "quoteId": "abc",
                    "input": {"token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "amount": "1000000"},
                    "output": {"token": "0x4200000000000000000000000000000000000006", "amount": "1", "recipient": wallet},
                    "gasFee": "1",
                    "gasUseEstimate": "2",
                },
            },
        }
        signer_args = type(
            "Args",
            (),
            {
                "private_key_env": "EXECUTOR_PRIVATE_KEY",
                "keystore": None,
                "account": None,
                "interactive": False,
                "password_file": None,
            },
        )()
        swap_response = {
            "requestId": "req-swap",
            "gasFee": "1",
            "swap": {
                "to": "0x6fF5693b99212Da76ad316178A184AB56D299b43",
                "from": wallet,
                "data": "0xdeadbeef",
                "value": "0x00",
                "chainId": 8453,
                "gasLimit": "97000",
                "gasPrice": "9000000",
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            quote_path = Path(tmpdir) / "quote.json"
            quote_path.write_text(json.dumps(saved_quote_response), encoding="utf-8")
            signature_path = Path(tmpdir) / "signature.txt"
            signature_path.write_text("0x1234", encoding="utf-8")
            with (
                mock.patch.dict("os.environ", {"BASE_RPC_URL": "https://base.example", "EXECUTOR_PRIVATE_KEY": "0x1234"}, clear=False),
                mock.patch.object(trading_api_quote, "load_local_env"),
                mock.patch.object(trading_api_quote, "require_api_key", return_value="test-key"),
                mock.patch.object(swap_dry_run, "post_json", return_value=swap_response),
            ):
                with self.assertRaisesRegex(ValueError, "swap broadcast requires approval to be broadcast"):
                    run_trade_flow.run_trade_flow(
                        chain="base",
                        token_in="USDC",
                        token_out="WETH",
                        amount="1",
                        wallet=wallet,
                        output_dir=tmpdir,
                        signature_file=str(signature_path),
                        quote_file=str(quote_path),
                        broadcast_swap=True,
                        swap_confirm="BROADCAST SWAP 8453 0x6ff5693b99212da76ad316178a184ab56d299b43",
                        signer_args_source=signer_args,
                    )

    def test_run_trade_flow_raises_when_broadcast_receipt_is_failed(self) -> None:
        wallet = "0x1111111111111111111111111111111111111111"
        quote_response = {
            "action": "trading_api_quote",
            "chain": {"key": "base", "chainId": 8453},
            "tokenIn": {"symbol": "USDC", "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6},
            "tokenOut": {"symbol": "WETH", "address": "0x4200000000000000000000000000000000000006", "decimals": 18},
            "wallet": wallet,
            "humanAmount": "1",
            "baseAmount": "1000000",
        }
        approval_payload = {
            "walletAddress": wallet,
            "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "amount": "1000000",
            "chainId": 8453,
        }
        quote_payload = {
            "swapper": wallet,
            "tokenIn": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "tokenOut": "0x4200000000000000000000000000000000000006",
            "tokenInChainId": 8453,
            "tokenOutChainId": 8453,
            "amount": "1000000",
            "type": "EXACT_INPUT",
            "slippageTolerance": 0.5,
            "routingPreference": "BEST_PRICE",
        }
        approval_response = {
            "requestId": "req-approve",
            "approval": {
                "to": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "from": wallet,
                "data": "0x095ea7b3",
                "value": "0x00",
                "chainId": 8453,
            },
        }
        raw_quote = {
            "routing": "CLASSIC",
            "permitData": {
                "domain": {
                    "name": "Permit2",
                    "chainId": 8453,
                    "verifyingContract": "0x000000000022D473030F116dDEE9F6B43aC78BA3",
                },
                "types": {
                    "PermitSingle": [
                        {"name": "details", "type": "PermitDetails"},
                        {"name": "spender", "type": "address"},
                        {"name": "sigDeadline", "type": "uint256"},
                    ],
                    "PermitDetails": [
                        {"name": "token", "type": "address"},
                        {"name": "amount", "type": "uint160"},
                        {"name": "expiration", "type": "uint48"},
                        {"name": "nonce", "type": "uint48"},
                    ],
                },
                "values": {
                    "details": {
                        "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                        "amount": "1",
                        "expiration": "2",
                        "nonce": "3",
                    },
                    "spender": "0x6ff5693b99212da76ad316178a184ab56d299b43",
                    "sigDeadline": "4",
                },
            },
            "quote": {
                "quoteId": "abc",
                "input": {"token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "amount": "1000000"},
                "output": {"token": "0x4200000000000000000000000000000000000006", "amount": "1", "recipient": wallet},
                "gasFee": "1",
                "gasUseEstimate": "2",
            },
        }
        signer_args = type(
            "Args",
            (),
            {
                "private_key_env": "EXECUTOR_PRIVATE_KEY",
                "keystore": None,
                "account": None,
                "interactive": False,
                "password_file": None,
            },
        )()
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                mock.patch.dict("os.environ", {"BASE_RPC_URL": "https://base.example", "EXECUTOR_PRIVATE_KEY": "0x1234"}, clear=False),
                mock.patch.object(trading_api_quote, "load_local_env"),
                mock.patch.object(trading_api_quote, "require_api_key", return_value="test-key"),
                mock.patch.object(trading_api_quote, "prepare_quote_request_data", return_value=(quote_response, approval_payload, quote_payload)),
                mock.patch.object(trading_api_quote, "post_json", side_effect=[approval_response, raw_quote]),
                mock.patch.object(execute_transaction, "build_preflight_report", return_value={"checked": True, "ok": True, "issues": []}),
                mock.patch.object(run_trade_flow, "maybe_broadcast", side_effect=RuntimeError("broadcast receipt status is not successful: 0x0")),
            ):
                with self.assertRaisesRegex(RuntimeError, "receipt status is not successful"):
                    run_trade_flow.run_trade_flow(
                        chain="base",
                        token_in="USDC",
                        token_out="WETH",
                        amount="1",
                        wallet=wallet,
                        output_dir=tmpdir,
                        broadcast_approval=True,
                        approval_confirm="BROADCAST APPROVAL 8453 0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                        signer_args_source=signer_args,
                    )


if __name__ == "__main__":
    unittest.main()
