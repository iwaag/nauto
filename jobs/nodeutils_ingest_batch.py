"""Pure helpers for nodeutils batch ingest input parsing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml


class IngestError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReportInput:
    source: str
    text: str


def load_report_batch(report_batch: str) -> list[ReportInput]:
    if not isinstance(report_batch, str) or not report_batch.strip():
        raise IngestError("provide report_batch with a non-empty reports list")
    try:
        loaded = yaml.safe_load(report_batch)
    except yaml.YAMLError as exc:
        raise IngestError(f"failed to parse report_batch: {exc}") from exc
    if not isinstance(loaded, dict):
        raise IngestError("report_batch root must be a mapping")
    reports = loaded.get("reports")
    if not isinstance(reports, list) or not reports:
        raise IngestError("report_batch.reports must be a non-empty list")

    inputs = []
    for index, item in enumerate(reports):
        if not isinstance(item, dict):
            raise IngestError(f"report_batch.reports[{index}] must be a mapping")
        source = item.get("source")
        text = item.get("text")
        if not isinstance(source, str) or not source.strip():
            raise IngestError(f"report_batch.reports[{index}].source must be a non-empty string")
        if not isinstance(text, str) or not text.strip():
            raise IngestError(f"report_batch.reports[{index}].text must be a non-empty string")
        inputs.append(ReportInput(source.strip(), text))
    return inputs


def parse_report_content(item: ReportInput, max_report_bytes: int) -> dict[str, Any]:
    size = len(item.text.encode("utf-8"))
    if size > max_report_bytes:
        raise IngestError(f"report is too large: {size} bytes > {max_report_bytes} bytes")
    try:
        loaded = yaml.safe_load(item.text)
    except yaml.YAMLError as exc:
        raise IngestError(f"failed to parse report: {exc}") from exc
    if not isinstance(loaded, dict):
        raise IngestError("report root must be a mapping")
    return loaded
