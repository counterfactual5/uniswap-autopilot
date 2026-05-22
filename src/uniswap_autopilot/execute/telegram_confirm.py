#!/usr/bin/env python3
"""
Telegram confirmation helper for Uniswap trade execution.

Sends trade details to Telegram with inline Approve/Reject buttons,
then polls for callback response.  Pure stdlib — no curl dependency.

Usage:
    from uniswap_autopilot.execute.telegram_confirm import request_trade_confirmation
    if not request_trade_confirmation(trade_details):
        sys.exit(0)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

# ── config ──────────────────────────────────────────────────────────────────


def _read_openclaw_config() -> tuple[str, str]:
    """Return (bot_token, chat_id) from openclaw.json."""
    cfg_path = Path.home() / ".openclaw" / "openclaw.json"
    with open(cfg_path) as f:
        cfg = json.load(f)
    tg = cfg.get("channels", {}).get("telegram", {})
    bot_token = tg.get("botToken", "")
    if not bot_token:
        raise RuntimeError("Telegram botToken not found in openclaw.json")
    allow = tg.get("allowFrom", [])
    chat_id = str(allow[-1]) if allow else os.environ.get("TELEGRAM_CHAT_ID", "")
    return bot_token, chat_id


BOT_TOKEN, CHAT_ID = "", ""


def _ensure_config():
    """Lazily load config on first use to avoid import-time failure."""
    global BOT_TOKEN, CHAT_ID
    if BOT_TOKEN:
        return
    BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
    if BOT_TOKEN and CHAT_ID:
        return
    try:
        BOT_TOKEN, CHAT_ID = _read_openclaw_config()
    except (FileNotFoundError, RuntimeError):
        pass


def _state_file() -> Path:
    """Cross-platform state file path."""
    return Path(tempfile.gettempdir()) / "uniswap_trade_confirmation_state.json"


# ── helpers ─────────────────────────────────────────────────────────────────


def _tg_api(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Call Telegram Bot API via urllib (no curl dependency)."""
    _ensure_config()
    if not BOT_TOKEN:
        raise RuntimeError(
            "Telegram bot token not configured. "
            "Set TELEGRAM_BOT_TOKEN env or configure openclaw.json"
        )
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except (HTTPError, URLError) as e:
        return {"ok": False, "error": str(e)}


def _poll_updates(offset: int | None = None, timeout: int = 2) -> list[dict]:
    """Short-poll getUpdates for callback_query only."""
    payload: dict[str, Any] = {
        "allowed_updates": ["callback_query"],
        "timeout": timeout,
    }
    if offset is not None:
        payload["offset"] = offset
    resp = _tg_api("getUpdates", payload)
    return resp.get("result", [])


# ── main API ────────────────────────────────────────────────────────────────


def request_trade_confirmation(
    trade_details: dict[str, Any],
    timeout_seconds: int = 300,
) -> bool:
    """
    Send trade confirmation to Telegram and wait for user response.

    Returns True if approved, False if rejected or timeout.
    """
    confirmation_id = f"uni_{int(time.time()) % 100000:05d}"

    lines = [
        "🔍 *Uniswap Trade Confirmation*",
        "",
        f"📍 Chain: `{trade_details.get('chain', '?')}`",
        f"🔄 {trade_details.get('tokenIn', '?')} → {trade_details.get('tokenOut', '?')}",
        f"💰 {trade_details.get('amountIn', '?')} → {trade_details.get('amountOut', '?')}",
        "",
        f"⏳ Timeout: {timeout_seconds // 60} min",
        f"🆔 `{confirmation_id}`",
    ]

    payload = {
        "chat_id": CHAT_ID,
        "text": "\n".join(lines),
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"uap_{confirmation_id}"},
                {"text": "❌ Reject", "callback_data": f"urj_{confirmation_id}"},
            ]]
        },
    }

    resp = _tg_api("sendMessage", payload)
    if not resp.get("ok"):
        print(f"⚠️ Telegram sendMessage failed: {resp}")
        return False

    msg_id = resp["result"]["message_id"]
    print(f"📤 Confirmation sent (msg_id={msg_id}, id={confirmation_id})")

    state = {
        "confirmation_id": confirmation_id,
        "status": "pending",
        "created_at": time.time(),
    }
    _state_file().write_text(json.dumps(state))

    deadline = time.time() + timeout_seconds
    last_offset: int | None = None

    while time.time() < deadline:
        try:
            updates = _poll_updates(
                offset=last_offset,
                timeout=min(5, int(deadline - time.time())),
            )
        except Exception as e:
            print(f"⚠️ Poll error: {e}, retrying...")
            time.sleep(2)
            continue

        for upd in updates:
            last_offset = upd["update_id"] + 1
            cb = upd.get("callback_query")
            if not cb:
                continue
            data = cb.get("data", "")
            if data in (f"uap_{confirmation_id}", f"urj_{confirmation_id}"):
                is_approved = data.startswith("uap_")
                decision = "✅ Approved" if is_approved else "❌ Rejected"
                state["status"] = "approved" if is_approved else "rejected"
                state["resolved_at"] = time.time()
                _state_file().write_text(json.dumps(state))
                _tg_api("editMessageReplyMarkup", {
                    "chat_id": CHAT_ID,
                    "message_id": msg_id,
                })
                _tg_api("editMessageText", {
                    "chat_id": CHAT_ID,
                    "message_id": msg_id,
                    "text": f"{'\\n'.join(lines)}\n\n🔒 *Decision: {decision}*",
                    "parse_mode": "Markdown",
                })
                _tg_api("answerCallbackQuery", {
                    "callback_query_id": cb["id"],
                })
                print(f"{decision} by user")
                return is_approved

        time.sleep(0.5)

    print("⏰ Confirmation timeout")
    state["status"] = "timeout"
    _state_file().write_text(json.dumps(state))
    return False


# ── CLI test ────────────────────────────────────────────────────────────────


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Test Telegram trade confirmation")
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()

    if args.test:
        ok = request_trade_confirmation({
            "chain": "Base",
            "tokenIn": "ETH",
            "tokenOut": "USDC",
            "amountIn": "0.1 ETH",
            "amountOut": "~200 USDC",
        }, timeout_seconds=120)
        print(f"\nResult: {'APPROVED' if ok else 'REJECTED/TIMEOUT'}")
    else:
        print("Use --test to send a test confirmation")


if __name__ == "__main__":
    main()
