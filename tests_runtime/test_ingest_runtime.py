"""Tier A real-ORM nauto ingest proof, run only by the documented Nautobot runtime gate."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.test import TestCase

from jobs.ingest_nodeutils_inventory import IngestNodeutilsInventory
from jobs.seed_home_cluster import SeedHomeCluster
from nautobot.dcim.models import Device
from nautobot.virtualization.models import Cluster, VirtualMachine


class NautoIngestRuntimeTests(TestCase):
    """Framework-owned persistence only; pure validation matrices remain in nauto/tests/."""

    @classmethod
    def setUpTestData(cls) -> None:
        seed = SeedHomeCluster()
        seed.logger = logging.getLogger("p3.nauto.seed")
        seed.run("seed/home_cluster.yaml", dry_run=False, update_existing=True)

    def _report(self, *, collected_at: str | None = None) -> dict:
        return {
            "schema_version": "nodeutils.inventory.v2",
            "collector": {"name": "p3-runtime", "version": "1"},
            "identity": {"hostname": "p3-runtime-node", "fqdn": "p3-runtime-node.example.test", "serial_number": "P3-RUNTIME-1"},
            "collected_at": collected_at or datetime.now(timezone.utc).isoformat(),
            "facts": {
                "system": "Linux",
                "os_name": "Ubuntu",
                "os_version": "24.04",
                "architecture": "arm64",
                "hardware": {"manufacturer": "Generic"},
                "cpu": {"model": "synthetic", "logical_cores": 2},
                "memory": {"total_gb": 4},
                "disk": {"root_total_gb": 20},
                "network": {"primary_interface": {"name": "eth0"}, "primary_mac_address": "02:00:00:00:00:31"},
                "services": {"observed_services": {}},
            },
            "self_reported": {"purpose": "p3-runtime"},
        }

    def _run(self, report: dict, *, dry_run: bool = False) -> dict:
        job = IngestNodeutilsInventory()
        job.logger = logging.getLogger("p3.nauto.ingest")
        files: list[tuple[str, str]] = []
        job.create_file = lambda name, content: files.append((name, content))
        job.run(
            report_batch=json.dumps({"reports": [{"source": "p3-runtime", "text": json.dumps(report)}]}),
            policy_file="seed/nodeutils_ingest.yaml",
            dry_run=dry_run,
            max_report_age_hours=72,
            max_report_bytes=1024 * 1024,
        )
        assert len(files) == 1
        return json.loads(files[0][1])

    def _proxmox_facts(self) -> dict:
        return {
            "schema_version": "nodeutils.proxmox.v1",
            "enabled": True,
            "detected": True,
            "mode": "auto",
            "inventory_source": "p3-runtime",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "collection": {"state": "complete"},
            "cluster": {
                "name": "p3-runtime-proxmox",
                "name_source": "standalone_node_fallback",
                "identity_value": "p3-runtime-node",
                "node_count": 1,
                "observed_node_names": ["p3-runtime-node"],
            },
            "qemu_vms": [],
            "lxc_containers": [],
            "storage_content": [],
        }

    def _lxc_guest(self, *, vmid: int, name: str) -> dict:
        return {
            "guest_type": "lxc",
            "vmid": vmid,
            "node": "p3-runtime-node",
            "name": name,
            "proxmox_status": "running",
            "status": "Active",
            "vcpus": 1,
            "memory_mb": 512,
            "disk_gb": 8,
            "observation": {"state": "complete"},
            "interfaces": {
                "config_interfaces": [],
                "agent_interfaces": [],
                "joined_interfaces": [],
                "unmatched": [],
            },
            "rootfs": {"storage": "local-lvm", "volume": f"vm-{vmid}-disk-0", "size_gb": 8},
        }

    def test_valid_stale_and_repeat_reports_use_real_orm_transactionally(self) -> None:
        report = self._report()
        valid = self._run(report)
        self.assertEqual(valid["summary"], {"total": 1, "created": 1, "updated": 0, "unchanged": 0, "skipped": 0})
        device = Device.objects.get(name="p3-runtime-node")
        initial_id, initial_updated = device.pk, device.last_updated

        repeated = self._run(report)
        self.assertEqual(
            repeated["summary"],
            {"total": 1, "created": 0, "updated": 0, "unchanged": 1, "skipped": 0},
            repeated,
        )
        device.refresh_from_db()
        self.assertEqual((device.pk, device.last_updated), (initial_id, initial_updated))

        stale = self._run(self._report(collected_at=(datetime.now(timezone.utc) - timedelta(hours=73)).isoformat()))
        self.assertEqual(stale["summary"], {"total": 1, "created": 0, "updated": 0, "unchanged": 0, "skipped": 1})
        self.assertEqual(Device.objects.filter(name="p3-runtime-node").count(), 1)

    def test_existing_device_proxmox_platform_uses_real_orm_and_invalid_input_writes_nothing(self) -> None:
        self._run(self._report())
        report = self._report()
        report["facts"]["proxmox"] = self._proxmox_facts()
        valid = self._run(report)
        self.assertEqual(valid["results"][0]["proxmox"]["object_counts"]["cluster"]["created"], 1)
        self.assertEqual(Cluster.objects.filter(name="p3-runtime-proxmox").count(), 1)
        cluster = Cluster.objects.get(name="p3-runtime-proxmox")
        initial_updated = cluster.last_updated

        repeated = self._run(report)
        self.assertEqual(repeated["results"][0]["proxmox"]["object_counts"]["cluster"]["unchanged"], 1)
        cluster.refresh_from_db()
        self.assertEqual(cluster.last_updated, initial_updated)

        invalid = self._report()
        invalid["facts"]["proxmox"] = {"schema_version": "nodeutils.proxmox.v999"}
        rejected = self._run(invalid)
        self.assertEqual(rejected["results"][0]["proxmox"]["guest_errors"][0]["code"], "unsupported_schema_version")
        self.assertEqual(Cluster.objects.filter(name="p3-runtime-proxmox").count(), 1)

    def test_malformed_guest_isolated_while_valid_sibling_persists(self) -> None:
        self._run(self._report())
        report = self._report()
        facts = self._proxmox_facts()
        facts["lxc_containers"] = [
            self._lxc_guest(vmid=301, name="p3-valid-guest"),
            self._lxc_guest(vmid=-1, name="p3-invalid-guest"),
        ]
        report["facts"]["proxmox"] = facts

        result = self._run(report)["results"][0]["proxmox"]
        self.assertEqual(result["observation_state"], "partial")
        self.assertEqual(result["object_counts"]["vm"]["created"], 1)
        self.assertIn(
            {"scope_kind": "guest", "scope_id": "lxc:p3-runtime-node:-1", "section": "identity", "code": "invalid_vmid"},
            result["guest_errors"],
        )
        self.assertTrue(VirtualMachine.objects.filter(name="p3-valid-guest").exists())
        self.assertFalse(VirtualMachine.objects.filter(name="p3-invalid-guest").exists())

    def test_real_constraint_failure_rolls_back_only_its_guest_savepoint(self) -> None:
        self._run(self._report())
        report = self._report()
        facts = self._proxmox_facts()
        facts["lxc_containers"] = [
            self._lxc_guest(vmid=401, name="p3-savepoint-valid"),
            self._lxc_guest(vmid=402, name="x" * 300),
        ]
        report["facts"]["proxmox"] = facts

        result = self._run(report)["results"][0]["proxmox"]
        self.assertEqual(result["observation_state"], "partial")
        self.assertEqual(result["object_counts"]["vm"]["created"], 1)
        self.assertIn("guest_upsert_failed", {error["code"] for error in result["guest_errors"]})
        self.assertTrue(VirtualMachine.objects.filter(name="p3-savepoint-valid").exists())
        self.assertFalse(VirtualMachine.objects.filter(name="x" * 300).exists())
