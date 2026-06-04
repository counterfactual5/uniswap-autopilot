"""Structured audit logging — single source of truth for cross-project events.

Every transaction-touching project emits the same JSON-line schema, so a single
``cat *.jsonl | jq`` can correlate Uniswap swaps, Hyperliquid orders, and
Polymarket trades by ``run_id``.

Schema (one JSON object per line):

    {
      "ts":         <ISO-8601 UTC, e.g. "2026-05-28T07:55:00.123Z">,
      "ts_unix":    <float seconds since epoch>,
      "event":      <enum: see EVENT_* constants below>,
      "project":    <e.g. "evm-wallet-scanner", "uniswap-autopilot">,
      "run_id":     <str|null — populated from STAGEFORGE_RUN_ID or caller>,
      "chain":      <str|null — e.g. "ethereum", "hyperliquid">,
      "wallet":     <str|null — 0x... or trader id>,
      "tx_hash":    <str|null — populated once broadcast>,
      "error_code": <str|null — short stable code like "rpc_timeout">,
      "details":    <dict — free-form, never None>
    }

Required fields are always present (null when unknown) so downstream consumers
can rely on the schema without defensive ``in`` checks.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Event enum (stable strings) ────────────────────────────────────────────

EVENT_PREFLIGHT = "preflight"
EVENT_QUOTE = "quote"
EVENT_SIGN = "sign"
EVENT_BROADCAST = "broadcast"
EVENT_CONFIRM = "confirm"
EVENT_CANCEL = "cancel"
EVENT_ERROR = "error"

ALLOWED_EVENTS = frozenset(
    {
        EVENT_PREFLIGHT,
        EVENT_QUOTE,
        EVENT_SIGN,
        EVENT_BROADCAST,
        EVENT_CONFIRM,
        EVENT_CANCEL,
        EVENT_ERROR,
    }
)

# Required-keys order is fixed so jq/grep-based downstream tools have
# predictable JSON-line layouts.
REQUIRED_KEYS: tuple[str, ...] = (
    "ts",
    "ts_unix",
    "event",
    "project",
    "run_id",
    "chain",
    "wallet",
    "tx_hash",
    "error_code",
    "details",
)


_write_lock = threading.Lock()
_DEFAULT_PROJECT = __name__.split(".")[0].replace("_", "-")


def _now() -> tuple[str, float]:
    now = datetime.now(tz=timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z", now.timestamp()


def _resolve_run_id(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    for name in ("STAGEFORGE_RUN_ID", "AUDIT_RUN_ID", "RUN_ID"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def build_record(
    *,
    event: str,
    project: str = _DEFAULT_PROJECT,
    run_id: str | None = None,
    chain: str | None = None,
    wallet: str | None = None,
    tx_hash: str | None = None,
    error_code: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical record dict (does not emit it)."""
    if event not in ALLOWED_EVENTS:
        raise ValueError(
            f"unknown audit event {event!r}; allowed: {sorted(ALLOWED_EVENTS)}"
        )
    ts_iso, ts_unix = _now()
    return {
        "ts": ts_iso,
        "ts_unix": ts_unix,
        "event": event,
        "project": project,
        "run_id": _resolve_run_id(run_id),
        "chain": chain,
        "wallet": wallet,
        "tx_hash": tx_hash,
        "error_code": error_code,
        "details": details or {},
    }


def emit(record: dict[str, Any]) -> None:
    """Write a record to the configured sink(s).

    The sink target order is:

    1. The file at ``AUDIT_LOG_PATH`` if set, appended.
    2. stderr — always, so a tail-friendly trail exists.
    """
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))

    with _write_lock:
        path = (os.environ.get("AUDIT_LOG_PATH") or "").strip()
        if path:
            try:
                p = Path(path)
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                # Never let logging failure break the trade path.
                pass

        # stderr fallback — present even when AUDIT_LOG_PATH is set so the
        # operator can ``tail -f`` without finding the file first.
        try:
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass


def log_event(
    *,
    event: str,
    project: str = _DEFAULT_PROJECT,
    run_id: str | None = None,
    chain: str | None = None,
    wallet: str | None = None,
    tx_hash: str | None = None,
    error_code: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Shorthand: build + emit. Returns the emitted record."""
    record = build_record(
        event=event,
        project=project,
        run_id=run_id,
        chain=chain,
        wallet=wallet,
        tx_hash=tx_hash,
        error_code=error_code,
        details=details,
    )
    emit(record)
    return record


__all__ = [
    "ALLOWED_EVENTS",
    "EVENT_BROADCAST",
    "EVENT_CANCEL",
    "EVENT_CONFIRM",
    "EVENT_ERROR",
    "EVENT_PREFLIGHT",
    "EVENT_QUOTE",
    "EVENT_SIGN",
    "REQUIRED_KEYS",
    "build_record",
    "emit",
    "log_event",
]
