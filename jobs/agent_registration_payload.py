"""Pure validation of the observed-agent-registration payload (agent_intent p1 step 3).

Django-free on purpose, like `nodeutils_ingest_batch.py`: the Job is a thin
adapter around this, and this is what the repository's ordinary test suite can
exercise without Nautobot.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

PAYLOAD_SCHEMA = "nctl.agent-registration.v1"

OBSERVED_FIELDS = (
    "zulip_present",
    "zulip_user_id",
    "zulip_is_active",
    "zulip_channels",
)


class AgentRegistrationIngestError(ValueError):
    """A deterministic payload validation failure."""


def decode_payload(raw: str) -> tuple[datetime, str, list[dict[str, Any]]]:
    """Validate the envelope and return (observed_at, collector, rows).

    Rejects rather than repairs: an unknown field, a naive timestamp, or an
    empty agents list is a failed ingest, because a partially understood
    observation written as actual state is worse than no observation.
    """
    try:
        document = json.loads(raw or "")
    except json.JSONDecodeError as exc:
        raise AgentRegistrationIngestError(f"payload is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise AgentRegistrationIngestError("payload must be an object")
    if document.get("schema_version") != PAYLOAD_SCHEMA:
        raise AgentRegistrationIngestError(
            f"payload schema_version must be {PAYLOAD_SCHEMA}, got {document.get('schema_version')!r}"
        )
    try:
        observed_at = datetime.fromisoformat(str(document.get("observed_at") or ""))
    except ValueError as exc:
        raise AgentRegistrationIngestError(
            f"payload observed_at is not an ISO 8601 timestamp: {exc}"
        ) from exc
    if observed_at.utcoffset() is None:
        raise AgentRegistrationIngestError("payload observed_at must carry a UTC offset")

    agents = document.get("agents")
    if not isinstance(agents, list) or not agents:
        raise AgentRegistrationIngestError("payload agents must be a non-empty list")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(agents):
        if not isinstance(row, dict) or not str(row.get("slug") or "").strip():
            raise AgentRegistrationIngestError(f"agents[{index}] must be an object with a slug")
        unknown = sorted(set(row) - {"slug", *OBSERVED_FIELDS})
        if unknown:
            raise AgentRegistrationIngestError(
                f"agents[{index}] has unknown fields: {', '.join(unknown)}"
            )
        channels = row.get("zulip_channels", [])
        if not isinstance(channels, list) or any(not isinstance(item, str) for item in channels):
            raise AgentRegistrationIngestError(
                f"agents[{index}].zulip_channels must be a list of channel names"
            )
        if row["slug"] in seen:
            raise AgentRegistrationIngestError(f"agents[{index}] repeats slug {row['slug']!r}")
        seen.add(row["slug"])
        rows.append(row)
    return observed_at, str(document.get("collector") or ""), rows
