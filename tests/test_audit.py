"""Schema-stability tests for the uniswap-autopilot audit emitter.

See ``hyperliquid-autopilot/tests/test_audit.py`` and the parallel files in
``polymarket-autopilot`` for the rationale — every trading repo must keep the
same field set so a single cross-project audit consolidator can read them all
without per-project schema branches.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from uniswap_autopilot import audit


REQUIRED_KEYS = {
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
}


class AuditSchemaTests(unittest.TestCase):
    def test_required_keys_present(self):
        record = audit.build_record(event=audit.EVENT_BROADCAST)
        self.assertEqual(set(record.keys()), REQUIRED_KEYS)

    def test_project_tag_matches_repo(self):
        record = audit.build_record(event=audit.EVENT_QUOTE)
        self.assertEqual(record["project"], "uniswap-autopilot")

    def test_unknown_event_rejected(self):
        with self.assertRaises(ValueError):
            audit.build_record(event="zzz")

    def test_run_id_pulled_from_stageforge_env(self):
        with mock.patch.dict(os.environ, {"STAGEFORGE_RUN_ID": "run-13"}, clear=True):
            record = audit.build_record(event=audit.EVENT_SIGN)
        self.assertEqual(record["run_id"], "run-13")

    def test_emit_to_file_one_record_per_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "audit.jsonl")
            with mock.patch.dict(os.environ, {"AUDIT_LOG_PATH": path}, clear=True):
                audit.log_event(event=audit.EVENT_BROADCAST, chain="ethereum", wallet="0xabc")
                audit.log_event(event=audit.EVENT_CONFIRM, chain="ethereum", wallet="0xabc", tx_hash="0xdef")
            with open(path, encoding="utf-8") as fh:
                lines = [json.loads(line) for line in fh if line.strip()]
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[0]["event"], "broadcast")
            self.assertEqual(lines[1]["tx_hash"], "0xdef")


if __name__ == "__main__":
    unittest.main()
