"""Ingest observed agag agent registration into nintent's ObservedAgentRegistration rows.

The collector (`nctl agents observe`) holds the Zulip credential and
does the reading; this Job holds none and only writes what it was handed, keyed
by `DesiredAgent.slug`. A payload row naming an unknown agent is skipped with a
reason rather than creating an agent out of an observation — desired state is
never invented from actual state.

Payload validation lives in the Django-free `agent_registration_payload` module
beside this one, mirroring `nodeutils_ingest_batch.py`.
"""

from __future__ import annotations

import json
from typing import Any

from django.apps import apps
from django.db import transaction

from nautobot.apps.jobs import Job, StringVar

from .agent_registration_payload import (
    OBSERVED_FIELDS as _OBSERVED_FIELDS,
    PAYLOAD_SCHEMA,
    decode_payload,
)

SUMMARY_SCHEMA = "agent.registration.ingest.summary.v1"
SUMMARY_FILE = "agent-registration-ingest-summary.json"


class IngestAgentRegistration(Job):
    """Replace each named agent's observed Zulip registration."""

    payload = StringVar(
        default="",
        description=f"JSON document with schema_version {PAYLOAD_SCHEMA}, observed_at, and an agents list.",
    )

    class Meta:
        name = "Ingest Agent Registration"
        description = "Write observed Zulip agent registration collected by nctl."
        has_sensitive_variables = False

    def run(self, payload: str) -> None:
        observed_at, collector, rows = decode_payload(payload)
        DesiredAgent = apps.get_model("nautobot_intent_catalog.DesiredAgent")
        Observed = apps.get_model("nautobot_intent_catalog.ObservedAgentRegistration")

        results: list[dict[str, Any]] = []
        with transaction.atomic():
            for row in rows:
                slug = row["slug"]
                agent = DesiredAgent.objects.filter(slug=slug).first()
                if agent is None:
                    self.logger.warning("Skipping %s: no DesiredAgent with that slug", slug)
                    results.append({"slug": slug, "outcome": "skipped",
                                    "error": "no DesiredAgent with that slug"})
                    continue
                values = {name: row[name] for name in _OBSERVED_FIELDS if name in row}
                observation = Observed.objects.filter(desired_agent=agent).first()
                created = observation is None
                if created:
                    observation = Observed(desired_agent=agent)
                changed = sorted(
                    name for name, value in values.items() if getattr(observation, name, None) != value
                )
                for name, value in values.items():
                    setattr(observation, name, value)
                observation.observed_at = observed_at
                observation.collector = collector
                observation.full_clean()
                observation.save()
                results.append({
                    "slug": slug,
                    "outcome": "created" if created else ("updated" if changed else "unchanged"),
                    "changed_fields": changed,
                    "error": None,
                })

        counts = {name: 0 for name in ("created", "updated", "unchanged", "skipped")}
        for item in results:
            counts[item["outcome"]] += 1
        counts["total"] = len(results)
        self.logger.info(
            "Agent registration: total=%s created=%s updated=%s unchanged=%s skipped=%s",
            counts["total"], counts["created"], counts["updated"],
            counts["unchanged"], counts["skipped"],
        )
        self.create_file(
            SUMMARY_FILE,
            json.dumps(
                {"schema_version": SUMMARY_SCHEMA, "observed_at": observed_at.isoformat(),
                 "results": results, "summary": counts},
                sort_keys=True, indent=2, ensure_ascii=True,
            ) + "\n",
        )
