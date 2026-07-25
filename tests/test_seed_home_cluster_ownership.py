"""interface_contract/p1 Section 7.1: Seed Home Cluster is native-Nautobot-prerequisite-only.

A source/data scan rather than an executed-Job test: `jobs/seed_home_cluster.py` imports
`django.apps`/`django.db` etc. unconditionally (no `try/except ImportError` guard, unlike
nintent's Job modules), so exercising `SeedHomeCluster.run()` itself requires a live Nautobot
test runner (out of scope here, same boundary as the other nauto Job tests). This proves the
ownership contract statically: no nintent import/reference remains in the Job source, and the
checked-in seed document declares no nintent desired root.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

JOBS_DIR = Path(__file__).resolve().parents[1] / "jobs"
SEED_DIR = Path(__file__).resolve().parents[1] / "seed"


class SeedHomeClusterOwnershipTests(unittest.TestCase):
    def test_job_source_has_no_nintent_import_or_reference(self) -> None:
        source = (JOBS_DIR / "seed_home_cluster.py").read_text(encoding="utf-8")

        self.assertNotIn("nautobot_intent_catalog", source)
        self.assertNotIn("IntentSource", source)
        self.assertNotIn("DesiredService", source)
        self.assertNotIn("ensure_intent_sources", source)
        self.assertNotIn("ensure_desired_services", source)

    def test_home_cluster_yaml_has_no_nintent_desired_roots(self) -> None:
        data = yaml.safe_load((SEED_DIR / "home_cluster.yaml").read_text(encoding="utf-8")) or {}

        self.assertNotIn("intent_sources", data)
        self.assertNotIn("desired_services", data)
        # Native Nautobot prerequisite roots remain.
        self.assertIn("locations", data)
        self.assertIn("statuses", data)
        self.assertIn("custom_fields", data)


if __name__ == "__main__":
    unittest.main()
