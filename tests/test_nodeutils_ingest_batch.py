from __future__ import annotations

import unittest
import importlib.util
import sys
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "jobs" / "nodeutils_ingest_batch.py"
SPEC = importlib.util.spec_from_file_location("nodeutils_ingest_batch", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"failed to load {MODULE_PATH}")
nodeutils_ingest_batch = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = nodeutils_ingest_batch
SPEC.loader.exec_module(nodeutils_ingest_batch)

IngestError = nodeutils_ingest_batch.IngestError
ReportInput = nodeutils_ingest_batch.ReportInput
load_report_batch = nodeutils_ingest_batch.load_report_batch
parse_report_content = nodeutils_ingest_batch.parse_report_content


class LoadReportBatchTest(unittest.TestCase):
    def test_accepts_multiple_reports(self) -> None:
        inputs = load_report_batch(
            """
            reports:
              - source: agpc
                text: '{"schema_version": "nodeutils.inventory.v1"}'
              - source: " agstudio "
                text: '{"schema_version": "nodeutils.inventory.v1", "identity": {}}'
            """
        )

        self.assertEqual(
            inputs,
            [
                ReportInput("agpc", '{"schema_version": "nodeutils.inventory.v1"}'),
                ReportInput("agstudio", '{"schema_version": "nodeutils.inventory.v1", "identity": {}}'),
            ],
        )

    def test_rejects_missing_reports(self) -> None:
        with self.assertRaisesRegex(IngestError, "reports must be a non-empty list"):
            load_report_batch("not_reports: []")

    def test_rejects_empty_reports(self) -> None:
        with self.assertRaisesRegex(IngestError, "reports must be a non-empty list"):
            load_report_batch("reports: []")

    def test_rejects_missing_source(self) -> None:
        with self.assertRaisesRegex(IngestError, r"reports\[0\].source"):
            load_report_batch(
                """
                reports:
                  - text: '{}'
                """
            )

    def test_rejects_missing_text(self) -> None:
        with self.assertRaisesRegex(IngestError, r"reports\[0\].text"):
            load_report_batch(
                """
                reports:
                  - source: agpc
                """
            )

    def test_rejects_non_string_text(self) -> None:
        with self.assertRaisesRegex(IngestError, r"reports\[0\].text"):
            load_report_batch(
                """
                reports:
                  - source: agpc
                    text:
                      schema_version: nodeutils.inventory.v1
                """
            )


class ParseReportTextTest(unittest.TestCase):
    def test_parses_json_report_content(self) -> None:
        report = parse_report_content(ReportInput("agpc", '{"schema_version": "nodeutils.inventory.v1"}'), 1024)

        self.assertEqual(report, {"schema_version": "nodeutils.inventory.v1"})

    def test_rejects_oversized_report_content(self) -> None:
        with self.assertRaisesRegex(IngestError, "report is too large"):
            parse_report_content(ReportInput("agpc", '{"too_large": true}'), 4)

    def test_rejects_non_mapping_report_content(self) -> None:
        with self.assertRaisesRegex(IngestError, "report root must be a mapping"):
            parse_report_content(ReportInput("agpc", "[]"), 1024)


if __name__ == "__main__":
    unittest.main()
