from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "jobs" / "proxmox_ingest.py"
_SPEC = importlib.util.spec_from_file_location("proxmox_ingest", _MODULE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"failed to load {_MODULE_PATH}")
proxmox_ingest = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = proxmox_ingest
_SPEC.loader.exec_module(proxmox_ingest)


RECEIVED_AT = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)


def _base_facts(**overrides):
    facts = {
        "schema_version": "nodeutils.proxmox.v1",
        "enabled": True,
        "detected": True,
        "mode": "auto",
        "inventory_source": "nodeutils-proxmox",
        "observed_at": "2026-07-24T12:00:00+00:00",
        "collection": {"state": "complete"},
        "cluster": {
            "name": "aghub-proxmox",
            "name_source": "standalone_node_fallback",
            "identity_value": "aghub",
            "node_count": 1,
            "observed_node_names": ["aghub"],
        },
        "qemu_vms": [],
        "lxc_containers": [],
        "storage_content": [],
    }
    facts.update(overrides)
    return facts


def _lxc_guest(**overrides):
    guest = {
        "guest_type": "lxc",
        "vmid": 108,
        "node": "aghub",
        "name": "agdnsmasq",
        "proxmox_status": "running",
        "status": "Active",
        "vcpus": 1,
        "memory_mb": 512,
        "disk_gb": 7.78,
        "observation": {"state": "complete"},
        "interfaces": {
            "config_interfaces": [],
            "agent_interfaces": [],
            "joined_interfaces": [],
            "unmatched": [],
        },
        "rootfs": {"storage": "local-lvm", "volume": "vm-108-disk-0", "size_gb": 8.0},
    }
    guest.update(overrides)
    return guest


class SchemaVersionTests(unittest.TestCase):
    def test_missing_schema_version_is_rejected(self) -> None:
        facts = _base_facts()
        del facts["schema_version"]
        result = proxmox_ingest.validate_proxmox_facts(facts, received_at=RECEIVED_AT)
        self.assertFalse(result.valid)
        self.assertEqual(result.errors[0]["code"], "unsupported_schema_version")

    def test_unknown_schema_version_is_rejected(self) -> None:
        facts = _base_facts(schema_version="nodeutils.proxmox.v2")
        result = proxmox_ingest.validate_proxmox_facts(facts, received_at=RECEIVED_AT)
        self.assertFalse(result.valid)
        self.assertEqual(result.errors[0]["code"], "unsupported_schema_version")

    def test_unknown_top_level_key_is_rejected(self) -> None:
        facts = _base_facts(unexpected_extra_key="raw dump")
        result = proxmox_ingest.validate_proxmox_facts(facts, received_at=RECEIVED_AT)
        self.assertFalse(result.valid)
        self.assertEqual(result.errors[0]["code"], "unknown_top_level_key")


class TimestampTests(unittest.TestCase):
    def test_naive_timestamp_is_rejected(self) -> None:
        facts = _base_facts(observed_at="2026-07-24T12:00:00")
        result = proxmox_ingest.validate_proxmox_facts(facts, received_at=RECEIVED_AT)
        self.assertFalse(result.valid)
        self.assertEqual(result.errors[0]["code"], "invalid_or_naive_timestamp")

    def test_future_within_skew_is_retained_not_rejected(self) -> None:
        facts = _base_facts(observed_at="2026-07-24T12:04:00+00:00")  # +240s < 300s allowance
        result = proxmox_ingest.validate_proxmox_facts(facts, received_at=RECEIVED_AT)
        self.assertTrue(result.valid)
        self.assertEqual(result.observed_at, RECEIVED_AT + timedelta(seconds=240))

    def test_future_beyond_skew_is_rejected(self) -> None:
        facts = _base_facts(observed_at="2026-07-24T12:10:00+00:00")  # +600s > 300s allowance
        result = proxmox_ingest.validate_proxmox_facts(facts, received_at=RECEIVED_AT)
        self.assertFalse(result.valid)
        self.assertEqual(result.errors[0]["code"], "future_skew_exceeded")

    def test_z_suffix_is_accepted_utc(self) -> None:
        facts = _base_facts(observed_at="2026-07-24T12:00:00Z")
        result = proxmox_ingest.validate_proxmox_facts(facts, received_at=RECEIVED_AT)
        self.assertTrue(result.valid)


class ClusterIdentityValidationTests(unittest.TestCase):
    def test_missing_name_source_is_rejected(self) -> None:
        facts = _base_facts()
        del facts["cluster"]["name_source"]
        result = proxmox_ingest.validate_proxmox_facts(facts, received_at=RECEIVED_AT)
        self.assertFalse(result.valid)
        self.assertEqual(result.errors[0]["code"], "invalid_cluster_identity")

    def test_unknown_name_source_is_rejected(self) -> None:
        facts = _base_facts()
        facts["cluster"]["name_source"] = "guessed"
        result = proxmox_ingest.validate_proxmox_facts(facts, received_at=RECEIVED_AT)
        self.assertFalse(result.valid)

    def test_unknown_cluster_key_is_rejected(self) -> None:
        facts = _base_facts()
        facts["cluster"]["raw_status"] = [{"type": "node"}]
        result = proxmox_ingest.validate_proxmox_facts(facts, received_at=RECEIVED_AT)
        self.assertFalse(result.valid)
        self.assertEqual(result.errors[0]["code"], "invalid_cluster_identity")


class GuestValidationTests(unittest.TestCase):
    def test_valid_lxc_guest_is_accepted(self) -> None:
        facts = _base_facts(lxc_containers=[_lxc_guest()])
        result = proxmox_ingest.validate_proxmox_facts(facts, received_at=RECEIVED_AT)
        self.assertTrue(result.valid)
        self.assertEqual(result.state, "complete")
        self.assertEqual(len(result.lxc_containers), 1)

    def test_qemu_guest_with_rootfs_key_is_rejected(self) -> None:
        guest = _lxc_guest(guest_type="qemu")
        facts = _base_facts(qemu_vms=[guest])
        result = proxmox_ingest.validate_proxmox_facts(facts, received_at=RECEIVED_AT)
        self.assertTrue(result.valid)  # platform still valid; this guest is isolated
        self.assertEqual(result.state, "partial")
        self.assertEqual(result.qemu_vms, [])
        self.assertEqual(result.errors[0]["code"], "unknown_key")  # rootfs is not an allowed QEMU key

    def test_invalid_vmid_is_isolated_not_platform_fatal(self) -> None:
        good = _lxc_guest()
        bad = _lxc_guest(vmid=-1, name="broken")
        facts = _base_facts(lxc_containers=[good, bad])
        result = proxmox_ingest.validate_proxmox_facts(facts, received_at=RECEIVED_AT)
        self.assertTrue(result.valid)
        self.assertEqual(result.state, "partial")
        self.assertEqual(len(result.lxc_containers), 1)
        self.assertEqual(result.lxc_containers[0]["name"], "agdnsmasq")
        self.assertEqual(result.errors[0]["code"], "invalid_vmid")

    def test_malformed_rootfs_isolates_guest(self) -> None:
        bad = _lxc_guest(rootfs={"storage": "local-lvm", "unexpected": True})
        facts = _base_facts(lxc_containers=[bad])
        result = proxmox_ingest.validate_proxmox_facts(facts, received_at=RECEIVED_AT)
        self.assertTrue(result.valid)
        self.assertEqual(result.state, "partial")
        self.assertEqual(result.lxc_containers, [])
        self.assertEqual(result.errors[0]["code"], "malformed_rootfs")


class StorageContentValidationTests(unittest.TestCase):
    def _scope(self, **overrides):
        scope = {
            "node": "aghub",
            "storage": "local",
            "content_type": "vztmpl",
            "state": "complete",
            "last_attempted_at": "2026-07-24T12:00:00+00:00",
            "evidence_observed_at": "2026-07-24T12:00:00+00:00",
            "omitted_error_count": 0,
            "errors": [],
            "items": [{"volid": "local:vztmpl/debian-13-standard.tar.zst", "content": "vztmpl", "format": "tzst", "size_bytes": 123}],
        }
        scope.update(overrides)
        return scope

    def test_valid_storage_scope_is_accepted(self) -> None:
        facts = _base_facts(storage_content=[self._scope()])
        result = proxmox_ingest.validate_proxmox_facts(facts, received_at=RECEIVED_AT)
        self.assertTrue(result.valid)
        self.assertEqual(result.state, "complete")
        self.assertEqual(result.storage_content[0]["items"][0]["volid"], "local:vztmpl/debian-13-standard.tar.zst")

    def test_missing_volid_isolates_scope(self) -> None:
        facts = _base_facts(storage_content=[self._scope(items=[{"content": "vztmpl"}])])
        result = proxmox_ingest.validate_proxmox_facts(facts, received_at=RECEIVED_AT)
        self.assertTrue(result.valid)
        self.assertEqual(result.state, "partial")
        self.assertEqual(result.storage_content, [])
        self.assertEqual(result.errors[0]["code"], "malformed_storage_item")

    def test_non_vztmpl_content_type_is_rejected(self) -> None:
        facts = _base_facts(storage_content=[self._scope(content_type="iso")])
        result = proxmox_ingest.validate_proxmox_facts(facts, received_at=RECEIVED_AT)
        self.assertEqual(result.state, "partial")
        self.assertEqual(result.storage_content, [])


class OneBadGuestIsolationTests(unittest.TestCase):
    def test_one_bad_guest_does_not_affect_sibling_guests_or_storage(self) -> None:
        facts = _base_facts(
            qemu_vms=[
                {
                    "guest_type": "qemu",
                    "vmid": 102,
                    "node": "aghub",
                    "name": "aghaos",
                    "proxmox_status": "running",
                    "status": "Active",
                    "vcpus": 2,
                    "memory_mb": 8192,
                    "disk_gb": 32.0,
                    "observation": {"state": "complete"},
                    "interfaces": {
                        "config_interfaces": [],
                        "agent_interfaces": [],
                        "joined_interfaces": [],
                        "unmatched": [],
                    },
                }
            ],
            lxc_containers=[_lxc_guest(), _lxc_guest(vmid=None, name="broken")],
            storage_content=[self.__class__._make_valid_scope()],
        )
        result = proxmox_ingest.validate_proxmox_facts(facts, received_at=RECEIVED_AT)
        self.assertTrue(result.valid)
        self.assertEqual(result.state, "partial")
        self.assertEqual(len(result.qemu_vms), 1)
        self.assertEqual(len(result.lxc_containers), 1)
        self.assertEqual(len(result.storage_content), 1)

    @staticmethod
    def _make_valid_scope():
        return {
            "node": "aghub",
            "storage": "local",
            "content_type": "vztmpl",
            "state": "complete",
            "last_attempted_at": "2026-07-24T12:00:00+00:00",
            "evidence_observed_at": "2026-07-24T12:00:00+00:00",
            "omitted_error_count": 0,
            "errors": [],
            "items": [{"volid": "local:vztmpl/debian-13-standard.tar.zst"}],
        }


if __name__ == "__main__":
    unittest.main()
