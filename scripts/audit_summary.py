#!/usr/bin/env python3
"""Summarize a StageForge audit JSON-lines log.

Reads one JSON object per line (the schema emitted by ``audit.py``) and prints
a compact report:

* total records + parse errors
* count per ``event``
* count per ``error_code`` (e.g. policy_rejected, rpc_timeout)
* policy warnings seen (from preflight events with ``details.stage == "policy"``)
* per ``run_id`` final event + whether it reached broadcast/confirm

Usage::

    python3 audit_summary.py audit.jsonl
    cat audit.jsonl | python3 audit_summary.py -
    python3 audit_summary.py audit.jsonl --json   # machine-readable output

Zero dependencies — pure standard library.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from typing import Any


def _read_lines(path: str) -> list[str]:
    if path == "-":
        return sys.stdin.read().splitlines()
    with open(path, encoding="utf-8") as fh:
        return fh.read().splitlines()


def summarize(lines: list[str]) -> dict[str, Any]:
    events: Counter[str] = Counter()
    error_codes: Counter[str] = Counter()
    projects: Counter[str] = Counter()
    policy_warnings: Counter[str] = Counter()
    parse_errors = 0
    total = 0

    # run_id → last event seen + set of events
    run_last: dict[str, str] = {}
    run_events: dict[str, set[str]] = defaultdict(set)

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        total += 1
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            parse_errors += 1
            continue

        event = str(rec.get("event", "?"))
        events[event] += 1

        if rec.get("error_code"):
            error_codes[str(rec["error_code"])] += 1

        if rec.get("project"):
            projects[str(rec["project"])] += 1

        details = rec.get("details") or {}
        if event == "preflight" and details.get("stage") == "policy":
            for w in details.get("warnings", []) or []:
                policy_warnings[str(w.get("rule", "?"))] += 1

        run_id = rec.get("run_id")
        if run_id:
            run_last[run_id] = event
            run_events[run_id].add(event)

    runs_reached_broadcast = sum(
        1 for ev in run_events.values() if "broadcast" in ev
    )
    runs_reached_confirm = sum(
        1 for ev in run_events.values() if "confirm" in ev
    )
    runs_rejected = sum(
        1 for rid, last in run_last.items()
        if "error" in run_events[rid]
    )

    return {
        "total_records": total,
        "parse_errors": parse_errors,
        "events": dict(events.most_common()),
        "error_codes": dict(error_codes.most_common()),
        "projects": dict(projects.most_common()),
        "policy_warnings": dict(policy_warnings.most_common()),
        "runs": {
            "unique": len(run_events),
            "reached_broadcast": runs_reached_broadcast,
            "reached_confirm": runs_reached_confirm,
            "with_error_event": runs_rejected,
        },
    }


def _print_human(summary: dict[str, Any]) -> None:
    print(f"records: {summary['total_records']}  (parse errors: {summary['parse_errors']})")

    def _section(title: str, mapping: dict[str, Any]) -> None:
        print(f"\n{title}:")
        if not mapping:
            print("  (none)")
            return
        width = max(len(str(k)) for k in mapping)
        for k, v in mapping.items():
            print(f"  {str(k).ljust(width)}  {v}")

    _section("events", summary["events"])
    _section("error_codes", summary["error_codes"])
    _section("projects", summary["projects"])
    _section("policy_warnings", summary["policy_warnings"])

    runs = summary["runs"]
    print("\nruns:")
    print(f"  unique             {runs['unique']}")
    print(f"  reached_broadcast  {runs['reached_broadcast']}")
    print(f"  reached_confirm    {runs['reached_confirm']}")
    print(f"  with_error_event   {runs['with_error_event']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize a StageForge audit JSON-lines log.")
    parser.add_argument("path", help="Path to audit .jsonl file, or '-' for stdin")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args(argv)

    try:
        lines = _read_lines(args.path)
    except OSError as exc:
        print(f"error: cannot read {args.path!r}: {exc}", file=sys.stderr)
        return 1

    summary = summarize(lines)

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        _print_human(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
