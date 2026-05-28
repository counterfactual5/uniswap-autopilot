from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from uniswap_autopilot.execute import telegram_confirm


class TelegramConfirmTests(unittest.TestCase):
    def test_state_file_is_namespaced_by_confirmation_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch("tempfile.gettempdir", return_value=tmpdir):
                p1 = telegram_confirm._state_file("uni_12345")
                p2 = telegram_confirm._state_file("uni_67890")
            self.assertNotEqual(p1, p2)
            self.assertIn("uni_12345", str(p1))
            self.assertIn("uni_67890", str(p2))

    def test_ignores_callback_from_non_allowed_user(self) -> None:
        sent_messages: list[dict] = []
        state_snapshots: list[dict] = []
        callback_acks: list[dict] = []

        def fake_tg_api(method: str, payload: dict):
            if method == "sendMessage":
                sent_messages.append(payload)
                return {"ok": True, "result": {"message_id": 42}}
            if method == "getUpdates":
                return {
                    "ok": True,
                    "result": [
                        {
                            "update_id": 1,
                            "callback_query": {
                                "id": "cb-1",
                                "from": {"id": 999},
                                "data": "uap_uni_00000",
                            },
                        }
                    ],
                }
            if method == "answerCallbackQuery":
                callback_acks.append(payload)
                return {"ok": True}
            return {"ok": True}

        def fake_write_text(self: Path, content: str, *args, **kwargs):
            state_snapshots.append(json.loads(content))
            return len(content)

        clock = {"t": 100000.0}

        def fake_time() -> float:
            clock["t"] += 1.0
            return clock["t"]

        with (
            mock.patch.object(telegram_confirm, "_ensure_config"),
            mock.patch.object(telegram_confirm, "BOT_TOKEN", "x"),
            mock.patch.object(telegram_confirm, "CHAT_ID", "123"),
            mock.patch.object(telegram_confirm, "ALLOWED_USER_IDS", ["111"]),
            mock.patch.object(telegram_confirm, "_tg_api", side_effect=fake_tg_api),
            mock.patch("time.time", side_effect=fake_time),
            mock.patch("time.sleep"),
            mock.patch.object(Path, "write_text", fake_write_text),
        ):
            ok = telegram_confirm.request_trade_confirmation(
                {"chain": "Base", "tokenIn": "ETH", "tokenOut": "USDC", "amountIn": "1", "amountOut": "2"},
                timeout_seconds=5,
            )

        self.assertFalse(ok)
        self.assertTrue(sent_messages)
        self.assertTrue(callback_acks)  # unauthorized callback got explicit ack
        # state should never become "approved" from unauthorized actor
        self.assertIn("timeout", {s.get("status") for s in state_snapshots})


if __name__ == "__main__":
    unittest.main()
