"""Pure schema builder for the nodeutils ingest Job's structured result."""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = "nodeutils.ingest.summary.v1"
OUTCOMES = ("created", "updated", "unchanged", "skipped")


def build_ingest_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {outcome: 0 for outcome in OUTCOMES}
    normalized = []
    for row in results:
        outcome = str(row.get("outcome") or "")
        if outcome not in counts:
            raise ValueError(f"unknown ingest outcome: {outcome!r}")
        counts[outcome] += 1
        normalized.append(dict(row))
    return {
        "schema_version": SCHEMA_VERSION,
        "summary": {"total": len(normalized), **counts},
        "results": normalized,
    }
