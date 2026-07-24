"""sidefix1 Step 0 item 3: proves the `ingest_report()` early return at
`dry_run=true` omits `result["proxmox"]` entirely, which is the actual Step 9
blocker recorded in `p2/problem.md` and analyzed in
`p2/sidefix1/problem_fixplan.md` Section 4.1.

Loads the real `jobs/ingest_nodeutils_inventory.py` module by file path (the
nauto pattern for Django-free unit tests, per `test_nodeutils_ingest_batch.py`
and `test_generate_desired_services.py`), stubbing only the Nautobot/Django
symbols the module imports at module level. `ingest_proxmox()` itself is
replaced with a sentinel so this test exercises only the orchestration
decision in `ingest_report()` -- whether it reaches the Proxmox branch at all
-- not the real ORM persistence core, which has its own coverage in
`test_proxmox_cluster_vm_upsert.py` / `test_proxmox_interface_ip_upsert.py`.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

MODULE_PATH = Path(__file__).resolve().parents[1] / "jobs" / "ingest_nodeutils_inventory.py"


def _install_stub(name: str, **attrs) -> types.ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _install_django_stubs() -> None:
    _install_stub("nautobot")

    class _FakeVar:
        def __init__(self, *args, **kwargs):
            pass

    class _FakeJob:
        pass

    _install_stub("nautobot.apps")
    _install_stub(
        "nautobot.apps.jobs",
        BooleanVar=_FakeVar,
        IntegerVar=_FakeVar,
        StringVar=_FakeVar,
        Job=_FakeJob,
    )

    _install_stub("django")
    _install_stub("django.apps", apps=MagicMock())

    class _FakeFieldDoesNotExist(Exception):
        pass

    _install_stub("django.core")
    _install_stub("django.core.exceptions", FieldDoesNotExist=_FakeFieldDoesNotExist)

    class _FakeAtomicContext:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    class _FakeTransaction:
        @staticmethod
        def atomic(*args, **kwargs):
            return _FakeAtomicContext()

        @staticmethod
        def set_rollback(*args, **kwargs):
            pass

    _install_stub("django.db", transaction=_FakeTransaction())


def _load_module():
    _install_django_stubs()

    # `ingest_nodeutils_inventory.py` uses `from . import proxmox_upsert`, so it must be
    # loaded as a submodule of a real "jobs" package (not a standalone top-level module)
    # for the relative import to resolve. Register a lightweight package stand-in whose
    # search path is the real jobs/ directory, rather than executing jobs/__init__.py
    # (which imports sibling Jobs this test does not need and should not depend on).
    if "jobs" not in sys.modules:
        jobs_package = types.ModuleType("jobs")
        jobs_package.__path__ = [str(MODULE_PATH.parent)]
        sys.modules["jobs"] = jobs_package

    spec = importlib.util.spec_from_file_location("jobs.ingest_nodeutils_inventory", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ingest_nodeutils_inventory = _load_module()


class DryRunProxmoxSectionTest(unittest.TestCase):
    """sidefix1 problem_fixplan.md Section 6.1 item 1 (existing Device, preview)."""

    def _make_job(self, dry_run: bool):
        job = ingest_nodeutils_inventory.IngestNodeutilsInventory.__new__(
            ingest_nodeutils_inventory.IngestNodeutilsInventory
        )
        job.logger = MagicMock()
        job.dry_run = dry_run
        job.ingest_proxmox = MagicMock(return_value={"sentinel": "proxmox-ran"})
        job.match_device = MagicMock(return_value=MagicMock(name="existing-device", pk=1))
        job.resolve_policy_objects = MagicMock(return_value={})
        job.build_device_payload = MagicMock(return_value={})
        job.diff_device = MagicMock(return_value=[])
        job.create_device = MagicMock()
        job.update_device = MagicMock()
        return job

    def _report(self):
        return {
            "identity": {"hostname": "aghub"},
            "facts": {"proxmox": {"schema_version": "nodeutils.proxmox.v1"}},
        }

    def test_preview_reaches_proxmox_ingest_for_an_existing_device(self) -> None:
        """This is the sidefix1 Step 0 red test: it currently fails because
        `ingest_report()` returns before reading `facts.proxmox` when
        `dry_run=true`, so `ingest_proxmox()` is never called and
        `result["proxmox"]` is absent -- exactly the Step 9 blocker."""
        job = self._make_job(dry_run=True)

        result = job.ingest_report(self._report(), policy={}, source="test")

        job.ingest_proxmox.assert_called_once()
        self.assertIn("proxmox", result)
        self.assertEqual(result["proxmox"], {"sentinel": "proxmox-ran"})

    def test_apply_reaches_proxmox_ingest_for_an_existing_device(self) -> None:
        """Control case: apply mode already reaches ingest_proxmox() today."""
        job = self._make_job(dry_run=False)

        result = job.ingest_report(self._report(), policy={}, source="test")

        job.ingest_proxmox.assert_called_once()
        self.assertIn("proxmox", result)


if __name__ == "__main__":
    unittest.main()
