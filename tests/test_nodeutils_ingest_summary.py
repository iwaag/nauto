from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "jobs" / "nodeutils_ingest_summary.py"
SPEC = importlib.util.spec_from_file_location("nodeutils_ingest_summary", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"failed to load {MODULE_PATH}")
summary_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = summary_module
SPEC.loader.exec_module(summary_module)

build_ingest_summary = summary_module.build_ingest_summary


class BuildIngestSummaryTest(unittest.TestCase):
    def test_counts_each_outcome_and_preserves_rows(self) -> None:
        rows = [
            {"source": "new", "outcome": "created"},
            {"source": "changed", "outcome": "updated"},
            {"source": "same", "outcome": "unchanged"},
            {"source": "bad", "outcome": "skipped", "error": "invalid"},
        ]

        payload = build_ingest_summary(rows, dry_run=False)

        self.assertEqual(payload["schema_version"], "nodeutils.ingest.summary.v1")
        self.assertEqual(
            payload["summary"],
            {"total": 4, "created": 1, "updated": 1, "unchanged": 1, "skipped": 1},
        )
        self.assertEqual(payload["results"], rows)
        self.assertFalse(payload["dry_run"])

    def test_dry_run_is_explicit(self) -> None:
        payload = build_ingest_summary([{"source": "x", "outcome": "created"}], dry_run=True)
        self.assertTrue(payload["dry_run"])

    def test_rejects_unknown_outcome(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown ingest outcome"):
            build_ingest_summary([{"source": "x", "outcome": "mystery"}], dry_run=False)


if __name__ == "__main__":
    unittest.main()
