"""sidefix1 problem_fixplan.md Section 6.1: covers the `ingest_report()` mode-boundary
orchestration decision -- whether Proxmox handling is reached, and how -- for both an
existing observer Device and a not-yet-persisted one, in both preview and apply.

Originated as the Step 0 red test (`ingest_report()`'s early return at `dry_run=true`
omitted `result["proxmox"]` entirely -- the Step 9 blocker recorded in `p2/problem.md`
Section 4.1); Step 2 removed that early return, so the preview case below is now green.

Loads the real `jobs/ingest_nodeutils_inventory.py` module by file path (the nauto
pattern for Django-free unit tests, per `test_nodeutils_ingest_batch.py`), stubbing
only the Nautobot/Django symbols the module imports at module level.
`ingest_proxmox()` itself is replaced with a sentinel
so this test exercises only the orchestration decision in `ingest_report()`, not the
real ORM persistence core, which has its own coverage in
`test_proxmox_cluster_vm_upsert.py` / `test_proxmox_interface_ip_upsert.py`.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from typing import Any
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


class ExistingDeviceProxmoxSectionTest(unittest.TestCase):
    """sidefix1 problem_fixplan.md Section 6.1 items 1-2 (existing Device, preview + apply)."""

    def _make_job(self, dry_run: bool, *, device=None):
        job = ingest_nodeutils_inventory.IngestNodeutilsInventory.__new__(
            ingest_nodeutils_inventory.IngestNodeutilsInventory
        )
        job.logger = MagicMock()
        job.dry_run = dry_run
        job.ingest_proxmox = MagicMock(return_value={"sentinel": "proxmox-ran"})
        job.match_device = MagicMock(return_value=device if device is not None else MagicMock(name="existing-device", pk=1))
        job.resolve_policy_objects = MagicMock(return_value={})
        job.build_device_payload = MagicMock(return_value={})
        job.diff_device = MagicMock(return_value=[])
        job.create_device = MagicMock()
        job.update_device = MagicMock()
        return job

    def _report(self, *, with_proxmox: bool = True):
        report: dict[str, Any] = {"identity": {"hostname": "aghub"}, "facts": {}}
        if with_proxmox:
            report["facts"]["proxmox"] = {"schema_version": "nodeutils.proxmox.v1"}
        return report

    def test_preview_reaches_proxmox_ingest_for_an_existing_device(self) -> None:
        """Formerly the sidefix1 Step 0 red test: `ingest_report()` used to return before
        reading `facts.proxmox` when `dry_run=true`, so `ingest_proxmox()` was never called
        and `result["proxmox"]` was absent -- the Step 9 blocker. Step 2 removed that early
        return, so this now passes."""
        job = self._make_job(dry_run=True)

        result = job.ingest_report(self._report(), policy={}, source="test")

        job.ingest_proxmox.assert_called_once()
        job.create_device.assert_not_called()
        job.update_device.assert_not_called()  # diff_device() returns [] -> unchanged, no-op
        self.assertIn("proxmox", result)
        self.assertEqual(result["proxmox"], {"sentinel": "proxmox-ran"})

    def test_apply_reaches_proxmox_ingest_for_an_existing_device(self) -> None:
        """Same core calls, same order, as preview (fixplan Section 6.1 item 2)."""
        job = self._make_job(dry_run=False)

        result = job.ingest_report(self._report(), policy={}, source="test")

        job.ingest_proxmox.assert_called_once()
        self.assertIn("proxmox", result)

    def test_device_only_report_is_unchanged_and_has_no_proxmox_section(self) -> None:
        """fixplan Section 6.1 item 3: Device-only input keeps its existing summary shape."""
        job = self._make_job(dry_run=True)

        result = job.ingest_report(self._report(with_proxmox=False), policy={}, source="test")

        job.ingest_proxmox.assert_not_called()
        self.assertNotIn("proxmox", result)
        self.assertEqual(result["outcome"], "unchanged")


class NewDeviceProxmoxPreconditionTest(unittest.TestCase):
    """sidefix1 problem_fixplan.md Section 6.1 item 4 / Section 4.4 (new Device, preview)."""

    def _make_job(self):
        job = ingest_nodeutils_inventory.IngestNodeutilsInventory.__new__(
            ingest_nodeutils_inventory.IngestNodeutilsInventory
        )
        job.logger = MagicMock()
        job.dry_run = True
        created = MagicMock(name="new-device", pk=None)
        created.name = "aghub"
        job.ingest_proxmox = MagicMock(return_value={"sentinel": "proxmox-ran"})
        job.match_device = MagicMock(return_value=None)
        job.resolve_policy_objects = MagicMock(return_value={})
        job.build_device_payload = MagicMock(return_value={"name": "aghub"})
        job.diff_device = MagicMock(return_value=[])
        job.create_device = MagicMock(return_value=created)
        job.update_device = MagicMock()
        return job

    def test_new_device_reports_a_truthful_precondition_not_a_proxmox_scope(self) -> None:
        job = self._make_job()
        report = {
            "identity": {"hostname": "aghub"},
            "facts": {"proxmox": {"schema_version": "nodeutils.proxmox.v1"}},
        }

        result = job.ingest_report(report, policy={}, source="test")

        job.create_device.assert_called_once()
        job.ingest_proxmox.assert_not_called()
        self.assertEqual(result["outcome"], "created")
        proxmox = result["proxmox"]
        self.assertIsNone(proxmox["scope_key"])
        self.assertIsNone(proxmox["cluster_id"])
        self.assertEqual(proxmox["observation_state"], "partial")
        self.assertEqual(
            proxmox["guest_errors"],
            [
                {
                    "scope_kind": "platform",
                    "scope_id": "cluster",
                    "section": "cluster_identity",
                    "code": "observer_device_not_persisted",
                }
            ],
        )
        for kind_counts in proxmox["object_counts"].values():
            self.assertEqual(set(kind_counts.values()), {0})


if __name__ == "__main__":
    unittest.main()
