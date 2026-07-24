from __future__ import annotations

import contextlib
import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _load(module_name: str, filename: str):
    path = Path(__file__).resolve().parents[1] / "jobs" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


proxmox_ingest = _load("proxmox_ingest", "proxmox_ingest.py")
proxmox_upsert = _load("proxmox_upsert", "proxmox_upsert.py")

validate_proxmox_facts = proxmox_ingest.validate_proxmox_facts

RECEIVED_AT = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)
T0 = "2026-07-24T12:00:00+00:00"
T1 = "2026-07-24T12:02:00+00:00"  # newer, within future-skew allowance
T_OLDER = "2026-07-24T11:00:00+00:00"  # older than T0


# --------------------------------------------------------------------------------------
# Minimal fake Nautobot-shaped ORM: models expose plain attributes plus a mutable
# custom_field_data dict; managers expose .filter(**kwargs) over an in-memory list, mirroring
# the duck-typed interface proxmox_upsert.py requires from real Django/Nautobot querysets.
# --------------------------------------------------------------------------------------


class FakeQuerySet(list):
    def first(self):
        return self[0] if self else None


class FakeManager:
    def __init__(self, store: list):
        self._store = store

    def filter(self, **kwargs):
        def matches(obj):
            for key, value in kwargs.items():
                if getattr(obj, key, None) != value:
                    return False
            return True

        return FakeQuerySet(o for o in self._store if matches(o))


class FakeModel:
    _next_id = 1

    def __init__(self, **kwargs):
        self.pk = None
        self.custom_field_data: dict = {}
        for key, value in kwargs.items():
            setattr(self, key, value)


class FakeClusterType:
    def __init__(self, name):
        self.name = name


class FakeStatus:
    def __init__(self, name):
        self.name = name


class FakeRole:
    def __init__(self, name):
        self.name = name


def make_env():
    cluster_store: list = []
    vm_store: list = []
    saved_ids = {"next": 1}

    def save_fn(obj):
        if obj.pk is None:
            obj.pk = saved_ids["next"]
            saved_ids["next"] += 1
            store = cluster_store if isinstance(obj, FakeModel) and getattr(obj, "_kind", None) == "cluster" else vm_store
            store.append(obj)

    cluster_type = FakeClusterType("Proxmox VE")
    status_active = FakeStatus("Active")
    status_offline = FakeStatus("Offline")
    status_unknown = FakeStatus("Unknown")
    role_vm = FakeRole("virtual-machine")
    role_lxc = FakeRole("lxc-container")
    statuses = {"Active": status_active, "Offline": status_offline, "Unknown": status_unknown}
    roles = {"virtual-machine": role_vm, "lxc-container": role_lxc}

    def make_cluster():
        obj = FakeModel(cluster_type=cluster_type)
        obj._kind = "cluster"
        return obj

    def make_vm(cluster):
        obj = FakeModel(cluster=cluster)
        obj._kind = "vm"
        return obj

    return {
        "cluster_store": cluster_store,
        "vm_store": vm_store,
        "cluster_manager": FakeManager(cluster_store),
        "vm_manager": FakeManager(vm_store),
        "cluster_type": cluster_type,
        "make_cluster": make_cluster,
        "make_vm": make_vm,
        "status_lookup": statuses.get,
        "role_lookup": roles.get,
        "save_fn": save_fn,
    }


def _base_facts(**overrides):
    facts = {
        "schema_version": "nodeutils.proxmox.v1",
        "enabled": True,
        "detected": True,
        "mode": "auto",
        "inventory_source": "nodeutils-proxmox",
        "observed_at": T0,
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
        "vcpus": 1,
        "memory_mb": 512,
        "disk_gb": 7.78,
        "observation": {"state": "complete"},
        "interfaces": {"config_interfaces": [], "agent_interfaces": [], "joined_interfaces": [], "unmatched": []},
        "rootfs": {"storage": "local-lvm", "volume": "vm-108-disk-0", "size_gb": 8.0},
    }
    guest.update(overrides)
    return guest


def _qemu_guest(**overrides):
    guest = {
        "guest_type": "qemu",
        "vmid": 102,
        "node": "aghub",
        "name": "aghaos",
        "proxmox_status": "running",
        "vcpus": 2,
        "memory_mb": 4096,
        "disk_gb": 32.0,
        "observation": {"state": "complete"},
        "interfaces": {"config_interfaces": [], "agent_interfaces": [], "joined_interfaces": [], "unmatched": []},
    }
    guest.update(overrides)
    return guest


def run_ingest(facts, env, *, observer_device_id="device-uuid-1", guest_atomic=contextlib.nullcontext):
    validation = validate_proxmox_facts(facts, received_at=RECEIVED_AT)
    assert validation.valid, validation.errors
    return proxmox_upsert.ingest_proxmox_platform(
        validation=validation,
        cluster_manager=env["cluster_manager"],
        vm_manager=env["vm_manager"],
        cluster_type=env["cluster_type"],
        make_cluster=env["make_cluster"],
        make_vm=env["make_vm"],
        status_lookup=env["status_lookup"],
        role_lookup=env["role_lookup"],
        observer_device_id=observer_device_id,
        save_fn=env["save_fn"],
        guest_atomic=guest_atomic,
    )


class ScopeKeyDerivationTests(unittest.TestCase):
    def test_provider_name_scope_key(self) -> None:
        key = proxmox_upsert.derive_cluster_scope_key(
            "proxmox_cluster_name", identity_value="prod-cluster", observer_device_id="dev-1"
        )
        self.assertEqual(key, "cluster-name:prod-cluster")

    def test_standalone_fallback_scope_key(self) -> None:
        key = proxmox_upsert.derive_cluster_scope_key(
            "standalone_node_fallback", identity_value="aghub", observer_device_id="dev-uuid-1"
        )
        self.assertEqual(key, "standalone-device:dev-uuid-1")

    def test_unknown_name_source_rejected(self) -> None:
        with self.assertRaises(proxmox_upsert.ProxmoxUpsertError):
            proxmox_upsert.derive_cluster_scope_key("mystery", identity_value="x", observer_device_id="d")


class ClusterCreateUpdateNoopTests(unittest.TestCase):
    def test_zero_candidates_creates_cluster(self) -> None:
        env = make_env()
        result = run_ingest(_base_facts(), env)
        self.assertEqual(result["object_counts"]["cluster"]["created"], 1)
        self.assertEqual(len(env["cluster_store"]), 1)
        cluster = env["cluster_store"][0]
        self.assertEqual(cluster.name, "aghub-proxmox")
        self.assertEqual(cluster.custom_field_data["proxmox_scope_key"], "standalone-device:device-uuid-1")
        self.assertEqual(cluster.custom_field_data["proxmox_identity_source"], "standalone_node_fallback")
        self.assertEqual(result["observation_state"], "complete")

    def test_provider_name_provenance_creates_cluster(self) -> None:
        env = make_env()
        facts = _base_facts(
            cluster={
                "name": "prod-cluster",
                "name_source": "proxmox_cluster_name",
                "identity_value": "prod-cluster",
                "node_count": 3,
                "observed_node_names": ["a", "b", "c"],
            }
        )
        result = run_ingest(facts, env)
        self.assertEqual(result["scope_key"], "cluster-name:prod-cluster")
        self.assertEqual(env["cluster_store"][0].custom_field_data["proxmox_identity_source"], "proxmox_cluster_name")

    def test_single_node_null_provider_id_uses_fallback(self) -> None:
        env = make_env()
        facts = _base_facts(
            cluster={
                "name": "aghub-proxmox",
                "name_source": "standalone_node_fallback",
                "identity_value": "aghub",
                "node_count": 1,
                "observed_node_names": ["aghub"],
            }
        )
        result = run_ingest(facts, env)
        self.assertEqual(result["identity_source"], "standalone_node_fallback")
        self.assertTrue(result["scope_key"].startswith("standalone-device:"))
        # No proxmox_cluster_id custom field is ever set (not part of the Step 4 mapping).
        self.assertNotIn("proxmox_cluster_id", env["cluster_store"][0].custom_field_data)

    def test_one_candidate_updates_changed_fields_only(self) -> None:
        env = make_env()
        run_ingest(_base_facts(), env)
        before = env["cluster_store"][0]
        before_pk = before.pk

        facts2 = _base_facts(
            observed_at=T1,
            cluster={
                "name": "aghub-proxmox-renamed",
                "name_source": "standalone_node_fallback",
                "identity_value": "aghub",
                "node_count": 1,
                "observed_node_names": ["aghub"],
            },
        )
        result = run_ingest(facts2, env)
        self.assertEqual(result["object_counts"]["cluster"]["updated"], 1)
        self.assertEqual(len(env["cluster_store"]), 1)  # same row, no second Cluster created
        self.assertEqual(env["cluster_store"][0].pk, before_pk)
        self.assertEqual(env["cluster_store"][0].name, "aghub-proxmox-renamed")
        self.assertIn("name", result["changed_fields"]["cluster"])

    def test_identical_repeat_is_noop_and_no_save(self) -> None:
        env = make_env()
        run_ingest(_base_facts(), env)
        cluster = env["cluster_store"][0]
        original_pk = cluster.pk

        # Second identical ingest at the same observed_at with identical values: no-op.
        result = run_ingest(_base_facts(), env)
        self.assertEqual(result["object_counts"]["cluster"]["unchanged"], 1)
        self.assertEqual(env["cluster_store"][0].pk, original_pk)
        self.assertEqual(len(env["cluster_store"]), 1)


class ClusterConflictTests(unittest.TestCase):
    def test_same_name_disjoint_scope_is_conflict(self) -> None:
        env = make_env()
        run_ingest(_base_facts(), env, observer_device_id="device-uuid-1")
        # A different observer device reporting the exact same cluster display name.
        result = run_ingest(_base_facts(), env, observer_device_id="device-uuid-DIFFERENT")
        self.assertEqual(result["observation_state"], "partial")
        codes = [e["code"] for e in result["guest_errors"]]
        self.assertIn("same_name_disjoint_scope_conflict", codes)
        # No second Cluster was created.
        self.assertEqual(len(env["cluster_store"]), 1)

    def test_duplicate_scope_key_is_conflict(self) -> None:
        env = make_env()
        # Seed two Clusters that already share one scope key (simulating pre-existing corruption).
        c1 = env["make_cluster"]()
        c1.name = "aghub-proxmox"
        c1.custom_field_data["proxmox_scope_key"] = "standalone-device:device-uuid-1"
        env["save_fn"](c1)
        c2 = env["make_cluster"]()
        c2.name = "aghub-proxmox-2"
        c2.custom_field_data["proxmox_scope_key"] = "standalone-device:device-uuid-1"
        env["save_fn"](c2)

        result = run_ingest(_base_facts(), env, observer_device_id="device-uuid-1")
        self.assertEqual(result["observation_state"], "partial")
        codes = [e["code"] for e in result["guest_errors"]]
        self.assertIn("duplicate_scope_key", codes)
        self.assertEqual(result["object_counts"]["cluster"]["created"], 0)
        self.assertEqual(result["object_counts"]["cluster"]["updated"], 0)


class GuestIdentityTests(unittest.TestCase):
    def test_qemu_and_lxc_both_created(self) -> None:
        env = make_env()
        facts = _base_facts(qemu_vms=[_qemu_guest()], lxc_containers=[_lxc_guest()])
        result = run_ingest(facts, env)
        self.assertEqual(result["object_counts"]["vm"]["created"], 2)
        self.assertEqual(len(env["vm_store"]), 2)
        lxc = next(v for v in env["vm_store"] if v.custom_field_data["proxmox_guest_type"] == "lxc")
        self.assertEqual(lxc.custom_field_data["proxmox_vmid"], 108)
        self.assertEqual(lxc.cluster, env["cluster_store"][0])

    def test_capacity_unit_mapping(self) -> None:
        env = make_env()
        facts = _base_facts(qemu_vms=[_qemu_guest()], lxc_containers=[_lxc_guest()])
        run_ingest(facts, env)
        qemu = next(v for v in env["vm_store"] if v.custom_field_data["proxmox_guest_type"] == "qemu")
        lxc = next(v for v in env["vm_store"] if v.custom_field_data["proxmox_guest_type"] == "lxc")
        self.assertEqual(qemu.vcpus, 2)
        self.assertEqual(qemu.memory, 4096)
        self.assertFalse(hasattr(qemu, "disk"))  # QEMU aggregate disk_gb never used as root disk
        self.assertEqual(lxc.disk, 8.0)  # from parsed rootfs.size_gb only, not aggregate disk_gb=7.78
        self.assertEqual(lxc.custom_field_data["proxmox_lxc_rootfs"], {"storage": "local-lvm", "volume": "vm-108-disk-0", "size_gb": 8.0})
        self.assertIsNone(qemu.custom_field_data["proxmox_lxc_rootfs"])

    def test_status_and_role_mapping(self) -> None:
        env = make_env()
        facts = _base_facts(
            qemu_vms=[_qemu_guest(proxmox_status="stopped")],
            lxc_containers=[_lxc_guest(proxmox_status="paused")],
        )
        run_ingest(facts, env)
        qemu = next(v for v in env["vm_store"] if v.custom_field_data["proxmox_guest_type"] == "qemu")
        lxc = next(v for v in env["vm_store"] if v.custom_field_data["proxmox_guest_type"] == "lxc")
        self.assertEqual(qemu.status.name, "Offline")
        self.assertEqual(qemu.role.name, "virtual-machine")
        self.assertEqual(lxc.status.name, "Offline")
        self.assertEqual(lxc.role.name, "lxc-container")

        env2 = make_env()
        facts2 = _base_facts(qemu_vms=[_qemu_guest(proxmox_status="something-else")])
        run_ingest(facts2, env2)
        self.assertEqual(env2["vm_store"][0].status.name, "Unknown")

    def test_duplicate_name_different_vmid_is_conflict_not_implicit_match(self) -> None:
        # Guest matching rule 2: "a same-name-only row is a conflict, not an implicit match."
        # Two genuinely different VMIDs that happen to share a display name must not silently
        # both get created; the second one is rolled back as an ambiguous-ownership conflict.
        env = make_env()
        facts = _base_facts(
            qemu_vms=[_qemu_guest(vmid=201, name="worker"), _qemu_guest(vmid=202, name="worker")]
        )
        result = run_ingest(facts, env)
        self.assertEqual(result["object_counts"]["vm"]["created"], 1)
        self.assertEqual(result["object_counts"]["vm"]["skipped"], 1)
        self.assertEqual(len(env["vm_store"]), 1)
        codes = [e["code"] for e in result["guest_errors"]]
        self.assertIn("same_name_conflict", codes)
        self.assertEqual(result["observation_state"], "partial")

    def test_duplicate_vmid_kind_rolls_back_that_guest(self) -> None:
        env = make_env()
        # Pre-seed two existing VMs within the same cluster sharing (guest_type, vmid) — corruption.
        run_ingest(_base_facts(), env)
        cluster = env["cluster_store"][0]
        for i in range(2):
            v = env["make_vm"](cluster)
            v.name = f"dup{i}"
            v.custom_field_data.update({"proxmox_guest_type": "qemu", "proxmox_vmid": 999})
            env["save_fn"](v)
        self.assertEqual(len(env["vm_store"]), 2)

        facts = _base_facts(observed_at=T1, qemu_vms=[_qemu_guest(vmid=999, name="dup-incoming")])
        result = run_ingest(facts, env)
        self.assertEqual(result["object_counts"]["vm"]["skipped"], 1)
        codes = [e["code"] for e in result["guest_errors"]]
        self.assertIn("duplicate_vmid_kind", codes)
        self.assertEqual(result["observation_state"], "partial")
        # No third VM row was created/updated for the conflicting vmid.
        self.assertEqual(len(env["vm_store"]), 2)

    def test_cross_cluster_conflict_rolls_back_guest(self) -> None:
        env = make_env()
        run_ingest(_base_facts(), env, observer_device_id="device-uuid-1")
        other_cluster_facts = _base_facts(
            cluster={
                "name": "other-proxmox",
                "name_source": "standalone_node_fallback",
                "identity_value": "other",
                "node_count": 1,
                "observed_node_names": ["other"],
            }
        )
        run_ingest(other_cluster_facts, env, observer_device_id="device-uuid-2")
        self.assertEqual(len(env["cluster_store"]), 2)

        # A VM with vmid=555/qemu already exists under cluster #1.
        facts_a = _base_facts(observed_at=T0, qemu_vms=[_qemu_guest(vmid=555, name="fixed")])
        run_ingest(facts_a, env, observer_device_id="device-uuid-1")
        self.assertEqual(len(env["vm_store"]), 1)

        # The same vmid/kind now reported under cluster #2: cross-cluster conflict, rolled back.
        facts_b = _base_facts(
            observed_at=T1,
            cluster={
                "name": "other-proxmox",
                "name_source": "standalone_node_fallback",
                "identity_value": "other",
                "node_count": 1,
                "observed_node_names": ["other"],
            },
            qemu_vms=[_qemu_guest(vmid=555, name="fixed")],
        )
        result = run_ingest(facts_b, env, observer_device_id="device-uuid-2")
        codes = [e["code"] for e in result["guest_errors"]]
        self.assertIn("cross_cluster_conflict", codes)
        self.assertEqual(len(env["vm_store"]), 1)  # still just the original VM under cluster #1


class FreshnessTests(unittest.TestCase):
    def test_older_observed_at_is_stale_and_not_applied(self) -> None:
        env = make_env()
        run_ingest(_base_facts(observed_at=T1), env)
        cluster_before = env["cluster_store"][0].name

        result = run_ingest(_base_facts(observed_at=T_OLDER, cluster={
            "name": "should-not-apply", "name_source": "standalone_node_fallback",
            "identity_value": "aghub", "node_count": 1, "observed_node_names": ["aghub"],
        }), env)
        self.assertEqual(env["cluster_store"][0].name, cluster_before)
        self.assertEqual(result["object_counts"]["cluster"]["skipped"], 1)

    def test_equal_timestamp_identical_values_is_noop(self) -> None:
        env = make_env()
        run_ingest(_base_facts(), env)
        pk = env["cluster_store"][0].pk
        result = run_ingest(_base_facts(), env)
        self.assertEqual(result["object_counts"]["cluster"]["unchanged"], 1)
        self.assertEqual(env["cluster_store"][0].pk, pk)

    def test_equal_timestamp_conflicting_values_is_rejected(self) -> None:
        env = make_env()
        run_ingest(_base_facts(), env)
        original_name = env["cluster_store"][0].name

        conflicting = _base_facts(cluster={
            "name": "different-name-same-generation",
            "name_source": "standalone_node_fallback",
            "identity_value": "aghub",
            "node_count": 1,
            "observed_node_names": ["aghub"],
        })
        result = run_ingest(conflicting, env)
        self.assertEqual(env["cluster_store"][0].name, original_name)  # not overwritten
        self.assertEqual(result["object_counts"]["cluster"]["skipped"], 1)

    def test_newer_observation_updates(self) -> None:
        env = make_env()
        run_ingest(_base_facts(observed_at=T0), env)
        result = run_ingest(_base_facts(observed_at=T1, cluster={
            "name": "aghub-proxmox-newname", "name_source": "standalone_node_fallback",
            "identity_value": "aghub", "node_count": 1, "observed_node_names": ["aghub"],
        }), env)
        self.assertEqual(result["object_counts"]["cluster"]["updated"], 1)
        self.assertEqual(env["cluster_store"][0].name, "aghub-proxmox-newname")


class MultiGenerationMergeTests(unittest.TestCase):
    def test_partial_generation_updates_observed_guest_without_touching_unobserved_guest(self) -> None:
        env = make_env()
        facts_gen1 = _base_facts(qemu_vms=[_qemu_guest(vmid=101), _qemu_guest(vmid=102, name="second")])
        run_ingest(facts_gen1, env)
        second_before = next(v for v in env["vm_store"] if v.custom_field_data["proxmox_vmid"] == 102)
        second_pk = second_before.pk
        second_observed_at = second_before.custom_field_data["proxmox_observed_at"]

        # Generation 2 only re-observes vmid=101 (e.g. vmid=102's guest list attempt failed upstream
        # and nodeutils/proxmox_ingest simply omitted it from this report).
        facts_gen2 = _base_facts(observed_at=T1, qemu_vms=[_qemu_guest(vmid=101, name="renamed-101")])
        run_ingest(facts_gen2, env)

        updated_101 = next(v for v in env["vm_store"] if v.custom_field_data["proxmox_vmid"] == 101)
        untouched_102 = next(v for v in env["vm_store"] if v.custom_field_data["proxmox_vmid"] == 102)
        self.assertEqual(updated_101.name, "renamed-101")
        self.assertEqual(updated_101.custom_field_data["proxmox_observed_at"], T1)
        # vmid=102 was not part of generation 2 at all, so its stored evidence time is untouched —
        # no parent-time inheritance from the newer report's platform observed_at.
        self.assertEqual(untouched_102.pk, second_pk)
        self.assertEqual(untouched_102.custom_field_data["proxmox_observed_at"], second_observed_at)
        self.assertEqual(untouched_102.name, "second")


class TransactionTests(unittest.TestCase):
    def test_invalid_report_produces_zero_writes(self) -> None:
        env = make_env()
        bad_facts = _base_facts()
        del bad_facts["schema_version"]
        validation = validate_proxmox_facts(bad_facts, received_at=RECEIVED_AT)
        self.assertFalse(validation.valid)
        # ingest_proxmox_platform is never called for an invalid report in the real Job path
        # (see ingest_nodeutils_inventory.ingest_proxmox); simulate that boundary here.
        self.assertEqual(env["cluster_store"], [])
        self.assertEqual(env["vm_store"], [])

    def test_one_bad_guest_isolated_others_committed(self) -> None:
        env = make_env()
        facts = _base_facts(
            qemu_vms=[_qemu_guest(vmid=301, name="good-qemu")],
            lxc_containers=[
                _lxc_guest(vmid=108, name="good-lxc"),
                _lxc_guest(vmid=None, name="bad-lxc"),  # invalid vmid: isolated by proxmox_ingest itself
            ],
        )
        validation = validate_proxmox_facts(facts, received_at=RECEIVED_AT)
        self.assertTrue(validation.valid)
        self.assertEqual(validation.state, "partial")
        result = proxmox_upsert.ingest_proxmox_platform(
            validation=validation,
            cluster_manager=env["cluster_manager"],
            vm_manager=env["vm_manager"],
            cluster_type=env["cluster_type"],
            make_cluster=env["make_cluster"],
            make_vm=env["make_vm"],
            status_lookup=env["status_lookup"],
            role_lookup=env["role_lookup"],
            observer_device_id="device-uuid-1",
            save_fn=env["save_fn"],
        )
        self.assertEqual(result["object_counts"]["vm"]["created"], 2)  # good qemu + good lxc
        self.assertEqual(result["observation_state"], "partial")
        self.assertEqual(len(env["vm_store"]), 2)
        names = sorted(v.name for v in env["vm_store"])
        self.assertEqual(names, ["good-lxc", "good-qemu"])

    def test_guest_savepoint_exception_rolls_back_only_that_guest(self) -> None:
        env = make_env()

        @contextlib.contextmanager
        def failing_atomic_for_second_call():
            failing_atomic_for_second_call.calls += 1
            if failing_atomic_for_second_call.calls == 2:
                raise RuntimeError("simulated savepoint failure")
            yield

        failing_atomic_for_second_call.calls = 0

        facts = _base_facts(qemu_vms=[_qemu_guest(vmid=1), _qemu_guest(vmid=2, name="second"), _qemu_guest(vmid=3, name="third")])
        validation = validate_proxmox_facts(facts, received_at=RECEIVED_AT)
        result = proxmox_upsert.ingest_proxmox_platform(
            validation=validation,
            cluster_manager=env["cluster_manager"],
            vm_manager=env["vm_manager"],
            cluster_type=env["cluster_type"],
            make_cluster=env["make_cluster"],
            make_vm=env["make_vm"],
            status_lookup=env["status_lookup"],
            role_lookup=env["role_lookup"],
            observer_device_id="device-uuid-1",
            save_fn=env["save_fn"],
            guest_atomic=failing_atomic_for_second_call,
        )
        self.assertEqual(len(env["vm_store"]), 2)  # guest #2 rolled back, #1 and #3 committed
        vmids = sorted(v.custom_field_data["proxmox_vmid"] for v in env["vm_store"])
        self.assertEqual(vmids, [1, 3])
        self.assertEqual(result["observation_state"], "partial")

    def test_batch_continues_after_one_report_fails(self) -> None:
        # Two independent reports processed in sequence: the first is invalid (zero writes),
        # the second is valid and fully applied — proving the pure upsert path itself carries no
        # cross-report state that would need "unbreaking" (the real Job's per-report try/except
        # plus outer transaction.atomic() is what prevents a broken Django transaction).
        env = make_env()
        bad = _base_facts()
        del bad["schema_version"]
        bad_validation = validate_proxmox_facts(bad, received_at=RECEIVED_AT)
        self.assertFalse(bad_validation.valid)

        good_validation = validate_proxmox_facts(_base_facts(qemu_vms=[_qemu_guest()]), received_at=RECEIVED_AT)
        result = proxmox_upsert.ingest_proxmox_platform(
            validation=good_validation,
            cluster_manager=env["cluster_manager"],
            vm_manager=env["vm_manager"],
            cluster_type=env["cluster_type"],
            make_cluster=env["make_cluster"],
            make_vm=env["make_vm"],
            status_lookup=env["status_lookup"],
            role_lookup=env["role_lookup"],
            observer_device_id="device-uuid-1",
            save_fn=env["save_fn"],
        )
        self.assertEqual(result["object_counts"]["cluster"]["created"], 1)
        self.assertEqual(result["object_counts"]["vm"]["created"], 1)


class SanitizeCreatedIdsTests(unittest.TestCase):
    """sidefix1 problem_fixplan.md Section 5.4/Step 3: a preview-created Cluster's real-but-
    rollback-only pk must not be reported as an apply-stable id; a pre-existing Cluster's id
    must still be reported even when the caller asks for sanitization."""

    def test_newly_created_cluster_id_is_none_when_sanitized(self) -> None:
        env = make_env()
        facts = _base_facts()
        validation = validate_proxmox_facts(facts, received_at=RECEIVED_AT)
        result = proxmox_upsert.ingest_proxmox_platform(
            validation=validation,
            cluster_manager=env["cluster_manager"],
            vm_manager=env["vm_manager"],
            cluster_type=env["cluster_type"],
            make_cluster=env["make_cluster"],
            make_vm=env["make_vm"],
            status_lookup=env["status_lookup"],
            role_lookup=env["role_lookup"],
            observer_device_id="device-uuid-1",
            save_fn=env["save_fn"],
            sanitize_created_ids=True,
        )
        self.assertEqual(result["object_counts"]["cluster"]["created"], 1)
        # save_fn still ran and allocated a real pk on the in-memory object...
        self.assertIsNotNone(env["cluster_store"][0].pk)
        # ...but the reported id is sanitized because this call's own match found nothing.
        self.assertIsNone(result["cluster_id"])

    def test_newly_created_cluster_id_is_present_when_not_sanitized(self) -> None:
        env = make_env()
        result = run_ingest(_base_facts(), env)
        self.assertEqual(result["object_counts"]["cluster"]["created"], 1)
        self.assertIsNotNone(result["cluster_id"])

    def test_preexisting_cluster_id_is_retained_even_when_sanitized(self) -> None:
        env = make_env()
        # First call (not sanitized) creates the real Cluster row, as apply would.
        run_ingest(_base_facts(), env)
        real_id = env["cluster_store"][0].pk

        # Second call, same before image, asks for sanitization -- but the Cluster already
        # existed before *this* call, so its id is not preview-temporary and must be kept.
        facts = _base_facts()
        validation = validate_proxmox_facts(facts, received_at=RECEIVED_AT)
        result = proxmox_upsert.ingest_proxmox_platform(
            validation=validation,
            cluster_manager=env["cluster_manager"],
            vm_manager=env["vm_manager"],
            cluster_type=env["cluster_type"],
            make_cluster=env["make_cluster"],
            make_vm=env["make_vm"],
            status_lookup=env["status_lookup"],
            role_lookup=env["role_lookup"],
            observer_device_id="device-uuid-1",
            save_fn=env["save_fn"],
            sanitize_created_ids=True,
        )
        self.assertEqual(result["object_counts"]["cluster"]["unchanged"], 1)
        self.assertEqual(result["cluster_id"], str(real_id))


if __name__ == "__main__":
    unittest.main()
