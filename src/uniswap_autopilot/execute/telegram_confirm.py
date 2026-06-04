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
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# ── config ──────────────────────────────────────────────────────────────────


def _read_config_file(config_path: str | None = None) -> tuple[str, str, list[str]]:
    """Return (bot_token, chat_id, allowed_user_ids) from a JSON config file.

    Config is expected to be a dict with optional nested keys, e.g.
    {"botToken": "...", "chatId": "...", "allowFrom": [...]}  or
    {"channels": {"telegram": {"botToken": "...", "allowFrom": [...]}}}
    """
    path = Path(config_path or os.environ.get("TELEGRAM_CONFIG_PATH", ""))
    if not path.is_file():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path) as f:
        cfg = json.load(f)

    bot_token = cfg.get("botToken", "")
    chat_id = cfg.get("chatId", "")
    allow_from = cfg.get("allowFrom") or []

    if not bot_token:
        tg = cfg.get("channels", {}).get("telegram", {})
        bot_token = tg.get("botToken", "")
        if not chat_id:
            allow = tg.get("allowFrom", [])
            chat_id = str(allow[-1]) if allow else ""
        if not allow_from:
            allow_from = tg.get("allowFrom") or []

    if not bot_token:
        raise RuntimeError("Telegram botToken not found in config file")
    return bot_token, chat_id, [str(x) for x in allow_from]


BOT_TOKEN, CHAT_ID = "", ""
ALLOWED_USER_IDS: list[str] = []


def _ensure_config():
    """Lazily load config on first use to avoid import-time failure."""
    global BOT_TOKEN, CHAT_ID, ALLOWED_USER_IDS
    if BOT_TOKEN:
        return
    BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
    env_allow = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "")
    if env_allow:
        ALLOWED_USER_IDS = [x.strip() for x in env_allow.split(",") if x.strip()]
    if BOT_TOKEN and CHAT_ID and ALLOWED_USER_IDS:
        return
    try:
        token, chat, allow = _read_config_file()
        BOT_TOKEN = BOT_TOKEN or token
        CHAT_ID = CHAT_ID or chat
        if not ALLOWED_USER_IDS:
            ALLOWED_USER_IDS = allow
    except (FileNotFoundError, RuntimeError):
        pass


def _state_file(confirmation_id: str | None = None) -> Path:
    """Cross-platform state file path.

    The previous implementation used a single shared file for ALL
    confirmation requests, which made it impossible to run concurrent trade
    flows safely (one would overwrite another's state). We now namespace the
    file by ``confirmation_id`` so each request owns its own state slot.
    """
    name = "uniswap_trade_confirmation_state"
    if confirmation_id:
        # Strip any characters that would be problematic on disk.
        safe = "".join(c for c in confirmation_id if c.isalnum() or c in ("_", "-"))
        name = f"{name}_{safe}"
    return Path(tempfile.gettempdir()) / f"{name}.json"


# ── helpers ─────────────────────────────────────────────────────────────────


def _tg_api(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Call Telegram Bot API via urllib (no curl dependency)."""
    _ensure_config()
    if not BOT_TOKEN:
        raise RuntimeError(
            "Telegram bot token not configured. "
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars, "
            "or provide a config file via TELEGRAM_CONFIG_PATH"
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
    _ensure_config()
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
    state_path = _state_file(confirmation_id)
    state_path.write_text(json.dumps(state))

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
            from_user = cb.get("from", {}) if isinstance(cb, dict) else {}
            from_id = str(from_user.get("id", "")).strip()
            if ALLOWED_USER_IDS and from_id not in ALLOWED_USER_IDS:
                _tg_api("answerCallbackQuery", {
                    "callback_query_id": cb.get("id", ""),
                    "text": "Not authorized",
                    "show_alert": True,
                })
                continue
            data = cb.get("data", "")
            if data in (f"uap_{confirmation_id}", f"urj_{confirmation_id}"):
                is_approved = data.startswith("uap_")
                decision = "✅ Approved" if is_approved else "❌ Rejected"
                state["status"] = "approved" if is_approved else "rejected"
                state["resolved_at"] = time.time()
                state_path.write_text(json.dumps(state))
                _tg_api("editMessageReplyMarkup", {
                    "chat_id": CHAT_ID,
                    "message_id": msg_id,
                })
                _tg_api("editMessageText", {
                    "chat_id": CHAT_ID,
                    "message_id": msg_id,
                    "text": "\n".join(lines) + "\n\n🔒 *Decision: " + decision + "*",
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
    state_path.write_text(json.dumps(state))
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
