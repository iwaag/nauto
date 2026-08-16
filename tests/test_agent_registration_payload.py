from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "jobs" / "agent_registration_payload.py"
SPEC = importlib.util.spec_from_file_location("agent_registration_payload", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"failed to load {MODULE_PATH}")
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)

AgentRegistrationIngestError = module.AgentRegistrationIngestError
PAYLOAD_SCHEMA = module.PAYLOAD_SCHEMA
decode_payload = module.decode_payload


def payload(**overrides):
    document = {
        "schema_version": PAYLOAD_SCHEMA,
        "observed_at": "2026-08-17T12:00:00+00:00",
        "collector": "nctl agents observe",
        "agents": [{"slug": "agforge", "zulip_present": True, "zulip_channels": ["ops"]}],
    }
    document.update(overrides)
    return json.dumps(document)


class DecodePayloadTests(unittest.TestCase):
    def test_accepts_a_well_formed_payload(self):
        observed_at, collector, rows = decode_payload(payload())
        self.assertEqual(observed_at.isoformat(), "2026-08-17T12:00:00+00:00")
        self.assertEqual(collector, "nctl agents observe")
        self.assertEqual(rows[0]["slug"], "agforge")

    def test_rejects_a_foreign_or_missing_schema_version(self):
        for value in ("other.schema.v1", None):
            with self.subTest(value=value), self.assertRaises(AgentRegistrationIngestError):
                decode_payload(payload(schema_version=value))

    def test_rejects_a_naive_timestamp(self):
        with self.assertRaises(AgentRegistrationIngestError) as ctx:
            decode_payload(payload(observed_at="2026-08-17T12:00:00"))
        self.assertIn("UTC offset", str(ctx.exception))

    def test_rejects_an_empty_or_non_list_agents_value(self):
        for value in ([], {}, None):
            with self.subTest(value=value), self.assertRaises(AgentRegistrationIngestError):
                decode_payload(payload(agents=value))

    def test_rejects_unknown_fields_rather_than_dropping_them(self):
        with self.assertRaises(AgentRegistrationIngestError) as ctx:
            decode_payload(payload(agents=[{"slug": "agforge", "zulip_email": "x@y"}]))
        self.assertIn("zulip_email", str(ctx.exception))

    def test_rejects_a_row_without_a_slug_and_a_repeated_slug(self):
        with self.assertRaises(AgentRegistrationIngestError):
            decode_payload(payload(agents=[{"zulip_present": True}]))
        with self.assertRaises(AgentRegistrationIngestError) as ctx:
            decode_payload(payload(agents=[{"slug": "a"}, {"slug": "a"}]))
        self.assertIn("repeats slug", str(ctx.exception))

    def test_rejects_a_malformed_channel_list(self):
        with self.assertRaises(AgentRegistrationIngestError):
            decode_payload(payload(agents=[{"slug": "a", "zulip_channels": "ops"}]))

    def test_rejects_text_that_is_not_json(self):
        with self.assertRaises(AgentRegistrationIngestError):
            decode_payload("not json")


if __name__ == "__main__":
    unittest.main()
