"""Trade execution state machine — idempotent, resumable, auditable.

Provides a lightweight checkpoint mechanism for transaction workflows that
share the same ``run_id`` used by the audit system.  Each state transition is
recorded to a JSON state file so that a crashed orchestrator can resume from
the last safe checkpoint without re-signing or re-broadcasting transactions.

States are the same event names used by ``audit.py`` so the audit log and the
state machine speak the same language:

    INIT  →  PREFLIGHT  →  SIGNED  →  BROADCAST  →  CONFIRMED

Any state can transition to FAILED (terminal).  CANCELLED is also terminal.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Constants ───────────────────────────────────────────────────────────────

STATE_INIT = "init"
STATE_PREFLIGHT = "preflight"
STATE_SIGNED = "signed"
STATE_BROADCAST = "broadcast"
STATE_CONFIRMED = "confirmed"
STATE_CANCELLED = "cancelled"
STATE_FAILED = "failed"

TERMINAL_STATES: frozenset[str] = frozenset({STATE_CONFIRMED, STATE_CANCELLED, STATE_FAILED})

# Ordered so the comparison STATE_ORDER.index(a) < STATE_ORDER.index(b) works.
STATE_ORDER: tuple[str, ...] = (
    STATE_INIT,
    STATE_PREFLIGHT,
    STATE_SIGNED,
    STATE_BROADCAST,
    STATE_CONFIRMED,
)

# Valid transition map — any state can additionally go to FAILED.
_VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    STATE_INIT: frozenset({STATE_PREFLIGHT, STATE_CANCELLED}),
    STATE_PREFLIGHT: frozenset({STATE_SIGNED, STATE_CANCELLED}),
    STATE_SIGNED: frozenset({STATE_BROADCAST, STATE_CANCELLED}),
    STATE_BROADCAST: frozenset({STATE_CONFIRMED}),
}


# ── Module-level state ──────────────────────────────────────────────────────

_write_lock = threading.Lock()
_DEFAULT_PROJECT = __name__.split(".")[0].replace("_", "-")


def _state_dir() -> Path:
    """Return the directory used for state machine checkpoint files.

    The directory can be overridden with the ``STAGEFORGE_STATE_DIR``
    environment variable; otherwise ``~/.stageforge/states`` is used.
    """
    env = os.environ.get("STAGEFORGE_STATE_DIR", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".stageforge" / "states"


def _state_path(run_id: str) -> Path:
    """File-system path for a given *run_id*."""
    return _state_dir() / f"{run_id}.json"


def _now_iso() -> str:
    """ISO-8601 UTC timestamp (without microsecond noise)."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_run_id(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    for name in ("STAGEFORGE_RUN_ID", "AUDIT_RUN_ID", "RUN_ID"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


# ── Action mapping (happy-path states → next action) ─────────────────────

_STATE_TO_ACTION: dict[str, str | None] = {
    STATE_INIT: STATE_PREFLIGHT,
    STATE_PREFLIGHT: STATE_SIGNED,
    STATE_SIGNED: STATE_BROADCAST,
    STATE_BROADCAST: STATE_CONFIRMED,
    STATE_CONFIRMED: None,
    STATE_CANCELLED: None,
    STATE_FAILED: None,
}


# ── Public API ───────────────────────────────────────────────────────────────


def load_state(run_id: str) -> dict[str, Any] | None:
    """Read an existing state checkpoint, or ``None`` if not found."""
    path = _state_path(run_id)
    try:
        with path.open() as fh:
            return json.load(fh)  # type: ignore[no-any-return]
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def init_state(
    run_id: str,
    *,
    project: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create (or return) the initial state record.

    If a state file already exists for *run_id* it is returned unchanged
    (idempotent).  Callers that want to start over must delete the file
    first.
    """
    existing = load_state(run_id)
    if existing is not None:
        return existing

    now = _now_iso()
    state: dict[str, Any] = {
        "run_id": run_id,
        "project": project or _DEFAULT_PROJECT,
        "current_state": STATE_INIT,
        "created_at": now,
        "updated_at": now,
        "transition_log": [],
        "payload": payload or {},
    }
    _atomic_write(_state_path(run_id), state)
    return state


def transition(
    run_id: str,
    target_state: str,
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Advance the state machine to *target_state*.

    Rules:

    * If *target_state* is FAILED the transition is always accepted
      (any state can fail).
    * If the current state is already >= *target_state* in the happy-path
      order, the call is a no-op (idempotent).
    * If the current state is a terminal state, raises ``RuntimeError``.
    * If the transition is not allowed, raises ``ValueError``.

    Returns the *updated* state dict.
    """
    state = init_state(run_id)  # ensure state file exists
    current = state["current_state"]

    # Terminal state — no-op if already in target, error otherwise.
    if current in TERMINAL_STATES:
        if current == target_state:
            return state
        raise RuntimeError(
            f"State machine for {run_id} is in terminal state {current!r} — cannot transition to {target_state!r}."
        )

    # Idempotent — already at or beyond this state.
    if (
        target_state != STATE_FAILED
        and current in STATE_ORDER
        and target_state in STATE_ORDER
        and STATE_ORDER.index(current) >= STATE_ORDER.index(target_state)
    ):
        return state

    # Validate transition.
    if target_state == STATE_FAILED:
        pass  # always allowed
    else:
        allowed = _VALID_TRANSITIONS.get(current, frozenset())
        if target_state not in allowed:
            raise ValueError(
                f"Illegal transition for {run_id}: {current!r} → {target_state!r}. Allowed: {sorted(allowed)}"
            )

    # Perform transition.
    now = _now_iso()
    transition_entry: dict[str, Any] = {
        "from": current,
        "to": target_state,
        "at": now,
    }
    state["current_state"] = target_state
    state["updated_at"] = now
    state["transition_log"].append(transition_entry)
    if payload is not None:
        state["payload"].update(payload)

    _atomic_write(_state_path(run_id), state)
    return state


def status(run_id: str) -> dict[str, Any]:
    """Return a high-level status summary for *run_id*."""
    state = load_state(run_id)
    if state is None:
        return {"run_id": run_id, "found": False, "current_state": None}
    return {
        "run_id": run_id,
        "found": True,
        "project": state.get("project"),
        "current_state": state.get("current_state"),
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
        "transition_count": len(state.get("transition_log", [])),
    }


def next_action(run_id: str) -> str | None:
    """Return the next allowed happy-path action for *run_id*, or None.

    Callers should check this before executing any side effect (sign, broadcast,
    confirm).  If the returned action does not match what the caller plans to do
    the caller must skip that step — it has already been completed in a prior
    run (anti-replay).
    """
    state = load_state(run_id)
    if state is None:
        return STATE_PREFLIGHT  # fresh run — start at preflight
    return _STATE_TO_ACTION.get(state.get("current_state", STATE_INIT))


def resume(run_id: str) -> str | None:
    """Return the action that an interrupted run should resume from, or None.

    ``resume()`` is a convenience wrapper around ``next_action()`` with clearer
    semantics for orchestrators.

    Returns ``None`` when the run is already complete (CONFIRMED / CANCELLED /
    FAILED) or the state file is missing.
    """
    return next_action(run_id)


def list_runs() -> list[dict[str, Any]]:
    """Return status summaries for all persisted runs."""
    sd = _state_dir()
    results: list[dict[str, Any]] = []
    if not sd.is_dir():
        return results
    for p in sorted(sd.glob("*.json")):
        run_id = p.stem
        s = load_state(run_id)
        if s is not None:
            results.append(
                {
                    "run_id": run_id,
                    "project": s.get("project"),
                    "current_state": s.get("current_state"),
                    "updated_at": s.get("updated_at"),
                }
            )
    return results


# ── Internal helpers ─────────────────────────────────────────────────────────


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """Write *data* to *path* atomically (write-tmp + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


__all__ = [
    "STATE_BROADCAST",
    "STATE_CANCELLED",
    "STATE_CONFIRMED",
    "STATE_FAILED",
    "STATE_INIT",
    "STATE_PREFLIGHT",
    "STATE_SIGNED",
    "TERMINAL_STATES",
    "init_state",
    "list_runs",
    "load_state",
    "next_action",
    "resume",
    "status",
    "transition",
]
