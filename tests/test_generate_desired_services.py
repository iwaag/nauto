"""Phase 4 Step 4.6 item 2 (p4/plan.md Decision 9): proves the checked-in
`seed/service_repositories.yaml` parses through `GenerateDesiredServices`'
*actual* reader (`_load_repository_specs`), not a reimplementation of it --
this is the nauto-side half of the ownership boundary the plan draws between
`service_repositories` (nauto candidate generation only) and `intent_sources`
plus desired objects (nintent's strict ledger import, which explicitly
rejects this file's top-level key -- see nintent's own
`test_loader_rejects_unknown_top_level_key`-class test).
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "jobs" / "generate_desired_services.py"
SEED_PATH = Path(__file__).resolve().parents[1] / "seed" / "service_repositories.yaml"


def _load_module():
    """Load generate_desired_services.py with a minimal nautobot.apps.jobs stub.

    The real module is only ever imported inside a running Nautobot process; this
    mirrors nauto's existing pattern (test_nodeutils_ingest_batch.py) of loading a
    Job module directly by file path for a Django-free unit test, extended with the
    stub this particular file needs since it (unlike the nodeutils jobs) imports
    nautobot.apps.jobs at module level.
    """

    if "nautobot" not in sys.modules:
        nautobot_module = types.ModuleType("nautobot")
        nautobot_apps_module = types.ModuleType("nautobot.apps")
        nautobot_apps_jobs_module = types.ModuleType("nautobot.apps.jobs")

        class _FakeVar:
            def __init__(self, *args, **kwargs):
                pass

        class _FakeJob:
            pass

        nautobot_apps_jobs_module.BooleanVar = _FakeVar
        nautobot_apps_jobs_module.IntegerVar = _FakeVar
        nautobot_apps_jobs_module.StringVar = _FakeVar
        nautobot_apps_jobs_module.Job = _FakeJob

        sys.modules["nautobot"] = nautobot_module
        sys.modules["nautobot.apps"] = nautobot_apps_module
        sys.modules["nautobot.apps.jobs"] = nautobot_apps_jobs_module

    spec = importlib.util.spec_from_file_location("generate_desired_services", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


generate_desired_services = _load_module()


class ServiceRepositoriesSeedFixtureTest(unittest.TestCase):
    def test_checked_in_seed_parses_through_the_real_loader(self) -> None:
        specs = generate_desired_services._load_repository_specs(SEED_PATH)

        self.assertEqual(len(specs), 1)
        [spec] = specs
        self.assertEqual(spec.url, "https://github.com/iwaag/agservice-storage")
        self.assertTrue(spec.enabled)
        self.assertIsNone(spec.ref)
        self.assertEqual(spec.catalog_paths, list(generate_desired_services.DEFAULT_CATALOG_PATHS))
        self.assertEqual(spec.basic_file_paths, list(generate_desired_services.DEFAULT_BASIC_FILE_PATHS))

    def test_loader_rejects_a_non_list_service_repositories(self) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as handle:
            handle.write("service_repositories: not-a-list\n")
            temp_path = Path(handle.name)
        try:
            with self.assertRaises(ValueError):
                generate_desired_services._load_repository_specs(temp_path)
        finally:
            temp_path.unlink()
