from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from uniswap_autopilot.swap.trading_api import quote as trading_api_quote


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def build_paper_trade_entry_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid4().hex[:8]}"


def build_paper_trade_paths(output_dir: str, journal_file: str | None) -> tuple[str, Path, Path]:
    entry_id = build_paper_trade_entry_id()
    output_root = Path(output_dir)
    run_output_dir = output_root / "runs" / entry_id
    journal_path = Path(journal_file) if journal_file else output_root / "paper-trade-journal.jsonl"
    return entry_id, run_output_dir, journal_path


def build_paper_trade_info(
    *,
    entry_id: str,
    journal_path: Path,
    run_output_dir: Path,
    status: str,
    response: dict[str, Any] | None,
    error: str | None = None,
) -> dict[str, Any]:
    swap = (response or {}).get("swap") or {}
    info: dict[str, Any] = {
        "enabled": True,
        "status": status,
        "entryId": entry_id,
        "journalFile": str(journal_path),
        "runOutputDir": str(run_output_dir),
        "recordedAt": datetime.now(timezone.utc).isoformat(),
        "swapPreviewSource": "quote-only" if swap.get("quoteOnly") else "swap-response",
    }
    if error is not None:
        info["error"] = error
    return info


def build_paper_trade_journal_entry(
    *,
    entry_id: str,
    journal_status: str,
    run_output_dir: Path,
    request_context: dict[str, Any],
    response: dict[str, Any] | None,
    error: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "entryId": entry_id,
        "recordedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "paper-trade",
        "status": journal_status,
        "runOutputDir": str(run_output_dir),
        "requestedTrade": request_context,
    }
    payload = response or {}
    for field in (
        "action",
        "inputs",
        "files",
        "quote",
        "automation",
        "policyCheck",
        "approval",
        "permit",
        "swap",
        "swapFailureDiagnosis",
        "nextActions",
    ):
        if field in payload:
            entry[field] = payload[field]
    if error is not None:
        entry["error"] = error
    return entry


def build_quote_only_paper_swap(raw_quote: dict[str, Any], reason: str) -> dict[str, Any]:
    quote = raw_quote.get("quote") or {}
    return {
        "paperOnly": True,
        "quoteOnly": True,
        "paperOnlyReason": reason,
        "simulationRequested": False,
        "simulationUsed": False,
        "simulationFallbackReason": None,
        "broadcastRequested": False,
        "autoBroadcast": False,
        "quoteSummary": trading_api_quote.summarize_quote(raw_quote),
        "quoteGasFee": quote.get("gasFee"),
        "quoteGasFeeUSD": quote.get("gasFeeUSD"),
        "quoteGasUseEstimate": quote.get("gasUseEstimate"),
        "preflight": {
            "checked": False,
            "ok": None,
            "reason": reason,
        },
    }
