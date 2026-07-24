from __future__ import annotations

import contextlib
import importlib.util
import sys
import unittest
from datetime import datetime, timezone
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


proxmox_ingest = _load("proxmox_ingest_s2", "proxmox_ingest.py")
proxmox_upsert = _load("proxmox_upsert_s2", "proxmox_upsert.py")
proxmox_interfaces = proxmox_upsert._load_proxmox_interfaces()

validate_proxmox_facts = proxmox_ingest.validate_proxmox_facts

RECEIVED_AT = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
T0 = "2026-07-25T12:00:00+00:00"


class FakeQuerySet(list):
    def first(self):
        return self[0] if self else None


class FakeModel:
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


class FakeAssignment:
    def __init__(self, *, ip_address, vm_interface=None):
        self.ip_address = ip_address
        self.vm_interface = vm_interface


def make_env(*, seed_ips=(), parent_prefixes=()):
    """Fake ORM that enforces the *real* Nautobot constraint: uniqueness is
    (Namespace, host), independent of mask length. ``parent_prefixes`` is the
    set of address strings (no mask) for which a containing Prefix exists in
    the one Namespace modeled here."""

    cluster_store: list = []
    vm_store: list = []
    iface_store: list = []
    ip_store: list = list(seed_ips)
    assignment_store: list = []
    saved_ids = {"next": 1}

    def save_fn(obj):
        if obj.pk is None:
            obj.pk = saved_ids["next"]
            saved_ids["next"] += 1
            kind = getattr(obj, "_kind", None)
            store = {"cluster": cluster_store, "vm": vm_store, "iface": iface_store, "ip": ip_store}.get(kind)
            if store is not None and obj not in store:
                store.append(obj)

    cluster_type = FakeClusterType("Proxmox VE")
    status_active = FakeStatus("Active")
    role_vm = FakeRole("virtual-machine")
    role_lxc = FakeRole("lxc-container")
    statuses = {"Active": status_active, "Offline": FakeStatus("Offline"), "Unknown": FakeStatus("Unknown")}
    roles = {"virtual-machine": role_vm, "lxc-container": role_lxc}

    def make_cluster():
        obj = FakeModel(cluster_type=cluster_type)
        obj._kind = "cluster"
        return obj

    def make_vm(cluster):
        obj = FakeModel(cluster=cluster)
        obj._kind = "vm"
        return obj

    def make_interface():
        obj = FakeModel()
        obj._kind = "iface"
        return obj

    def resolve_host(address):
        # Fixed contract (sidefix2/plan.md Section 3.2): resolve by host alone within the one
        # modeled Namespace, mirroring Nautobot's real (Namespace, host) uniqueness.
        matches = [ip for ip in ip_store if ip.address_str == address]
        if not matches:
            return proxmox_interfaces.IpLookupResult(status="not_found")
        if len(matches) > 1:
            return proxmox_interfaces.IpLookupResult(status="ambiguous")
        return proxmox_interfaces.IpLookupResult(status="found", ip=matches[0])

    def find_parent_prefix(address):
        return "fake-parent-prefix" if address in parent_prefixes else None

    def create_ip(address, prefix, parent_prefix=None):
        ip = FakeModel(address_str=address, prefix=prefix, parent_prefix=parent_prefix)
        ip._kind = "ip"
        save_fn(ip)
        return ip

    def find_ip_by_id(ip_id):
        if not ip_id:
            return None
        for ip in ip_store:
            if str(ip.pk) == str(ip_id):
                return ip
        return None

    def ip_related_elsewhere(ip_obj, interface):
        return any(a.ip_address is ip_obj and a.vm_interface is not None and a.vm_interface is not interface for a in assignment_store)

    def attach_ip(interface, ip_obj):
        if any(a.ip_address is ip_obj and a.vm_interface is interface for a in assignment_store):
            return
        assignment_store.append(FakeAssignment(ip_address=ip_obj, vm_interface=interface))

    def detach_ip(interface, ip_obj):
        assignment_store[:] = [a for a in assignment_store if not (a.ip_address is ip_obj and a.vm_interface is interface)]

    return {
        "cluster_store": cluster_store, "vm_store": vm_store, "iface_store": iface_store,
        "ip_store": ip_store, "assignment_store": assignment_store,
        "cluster_manager": None, "vm_manager": None, "vminterface_manager": None,
        "cluster_type": cluster_type, "make_cluster": make_cluster, "make_vm": make_vm,
        "make_interface": make_interface, "resolve_host": resolve_host, "find_parent_prefix": find_parent_prefix,
        "create_ip": create_ip, "find_ip_by_id": find_ip_by_id,
        "ip_related_elsewhere": ip_related_elsewhere, "attach_ip": attach_ip, "detach_ip": detach_ip,
        "status_lookup": statuses.get, "role_lookup": roles.get, "save_fn": save_fn,
    }


class FakeManager:
    def __init__(self, store: list):
        self._store = store

    def filter(self, **kwargs):
        def matches(obj):
            return all(getattr(obj, k, None) == v for k, v in kwargs.items())
        return FakeQuerySet(o for o in self._store if matches(o))


def _base_facts(**overrides):
    facts = {
        "schema_version": "nodeutils.proxmox.v1",
        "enabled": True, "detected": True, "mode": "auto",
        "inventory_source": "nodeutils-proxmox", "observed_at": T0,
        "collection": {"state": "complete"},
        "cluster": {
            "name": "aghub-proxmox", "name_source": "standalone_node_fallback",
            "identity_value": "aghub", "node_count": 1, "observed_node_names": ["aghub"],
        },
        "qemu_vms": [], "lxc_containers": [], "storage_content": [],
    }
    facts.update(overrides)
    return facts


def _complete_observation(section_name, evidence_time=T0):
    return {"state": "complete", "sections": {section_name: {"state": "complete", "evidence_observed_at": evidence_time}}}


def _lxc_guest(*, interfaces, **overrides):
    guest = {
        "guest_type": "lxc", "vmid": 108, "node": "aghub", "name": "agdnsmasq",
        "proxmox_status": "running", "vcpus": 1, "memory_mb": 512, "disk_gb": 7.78,
        "observation": _complete_observation("config"), "interfaces": interfaces,
        "rootfs": {"storage": "local-lvm", "volume": "vm-108-disk-0", "size_gb": 8.0},
    }
    guest.update(overrides)
    return guest


def _qemu_guest(*, interfaces, **overrides):
    guest = {
        "guest_type": "qemu", "vmid": 102, "node": "aghub", "name": "aghaos",
        "proxmox_status": "running", "vcpus": 2, "memory_mb": 4096, "disk_gb": 32.0,
        "observation": _complete_observation("agent_interfaces"), "interfaces": interfaces,
    }
    guest.update(overrides)
    return guest


def joined_lxc(config_slot="net0", mac="aa:bb:cc:dd:ee:02", bridge="vmbr0", guest_if="eth0", ip="192.168.0.2/24"):
    return {"config_slot": config_slot, "mac_address": mac, "bridge": bridge, "guest_interface_name": guest_if, "ip": ip}


def joined_qemu(config_slot="net0", mac="aa:bb:cc:dd:ee:01", bridge="vmbr0", guest_if="eth0", addrs=None):
    return {
        "config_slot": config_slot, "mac_address": mac, "bridge": bridge, "guest_interface_name": guest_if,
        "ip_addresses": addrs if addrs is not None else [{"address": "2400:2410:1f84:800::1", "type": "ipv6", "prefix": 128}],
    }


def run_ingest(facts, env):
    validation = validate_proxmox_facts(facts, received_at=RECEIVED_AT)
    assert validation.valid, validation.errors
    return proxmox_upsert.ingest_proxmox_platform(
        validation=validation,
        cluster_manager=FakeManager(env["cluster_store"]),
        vm_manager=FakeManager(env["vm_store"]),
        cluster_type=env["cluster_type"], make_cluster=env["make_cluster"], make_vm=env["make_vm"],
        status_lookup=env["status_lookup"], role_lookup=env["role_lookup"],
        observer_device_id="device-uuid-1", save_fn=env["save_fn"], guest_atomic=contextlib.nullcontext,
        vminterface_manager=FakeManager(env["iface_store"]), make_interface=env["make_interface"],
        resolve_host=env["resolve_host"], find_parent_prefix=env["find_parent_prefix"],
        create_ip=env["create_ip"], find_ip_by_id=env["find_ip_by_id"],
        ip_related_elsewhere=env["ip_related_elsewhere"],
        attach_ip=env["attach_ip"], detach_ip=env["detach_ip"],
    )


class ExistingHostDifferentMaskTests(unittest.TestCase):
    """report0.md Section 2 / plan.md 1.1: agdnsmasq case. resolve_host(host) must locate
    the existing /32 row for an observed /24 by host identity alone (sidefix2 Step 1 fix),
    reusing it rather than attempting a duplicate create."""

    def test_existing_32_row_is_reused_for_observed_24_without_raising(self) -> None:
        existing = FakeModel(address_str="192.168.0.2", prefix=32, dns_name="agdnsmasq.home.arpa")
        existing._kind = "ip"
        existing.pk = "existing-ip-1"
        env = make_env(seed_ips=[existing])
        facts = _base_facts(lxc_containers=[
            _lxc_guest(interfaces={
                "config_interfaces": [], "agent_interfaces": [],
                "joined_interfaces": [joined_lxc(ip="192.168.0.2/24")], "unmatched": [],
            })
        ])
        result = run_ingest(facts, env)

        self.assertNotIn(
            {"scope_kind": "guest", "scope_id": "lxc:108", "section": "identity", "code": "guest_upsert_failed"},
            result["guest_errors"],
        )
        self.assertEqual(len(env["ip_store"]), 1, "must not create a second IPAddress for the same host")
        self.assertEqual(existing.dns_name, "agdnsmasq.home.arpa", "native fields of the reused row must stay untouched")


class MissingParentPrefixTests(unittest.TestCase):
    """report0.md Section 3 / plan.md 1.2: aghaos case. find_parent_prefix() is checked
    before create_ip is ever called (sidefix2 Step 1 fix), so a missing parent Prefix
    produces a bounded ip.skipped conflict instead of rolling back the whole guest."""

    def test_missing_parent_prefix_does_not_fail_the_whole_guest(self) -> None:
        env = make_env(parent_prefixes=())  # no Prefix covers the observed IPv6 address
        facts = _base_facts(qemu_vms=[
            _qemu_guest(interfaces={
                "config_interfaces": [], "agent_interfaces": [joined_qemu()],
                "joined_interfaces": [joined_qemu()], "unmatched": [],
            })
        ])
        result = run_ingest(facts, env)

        self.assertEqual(len(env["vm_store"]), 1, "the qemu:102 VM must still be committed")
        codes = {(e["scope_id"], e["code"]) for e in result["guest_errors"]}
        self.assertNotIn(("qemu:102", "guest_upsert_failed"), codes)


class GuestSavepointCountTruthTests(unittest.TestCase):
    """report0.md Section 4 / plan.md Section 3.6: today ``counts["vm"]`` is mutated
    directly inside the guest's try-block before the guest_atomic() body finishes, so an
    exception raised later in the same guest (e.g. during interface sync) leaves the
    vm.created increment in place *in addition to* the vm.skipped increment -- a leaked,
    double count for one guest."""

    def test_failure_after_vm_upsert_does_not_leave_a_created_count(self) -> None:
        env = make_env(parent_prefixes=())

        def raising_resolve_host(address):
            raise RuntimeError("simulated unexpected interface-stage failure")

        env["resolve_host"] = raising_resolve_host
        facts = _base_facts(qemu_vms=[
            _qemu_guest(interfaces={
                "config_interfaces": [], "agent_interfaces": [joined_qemu()],
                "joined_interfaces": [joined_qemu()], "unmatched": [],
            })
        ])
        result = run_ingest(facts, env)

        self.assertEqual(result["object_counts"]["vm"]["created"], 0, "rolled-back guest must not count as created")
        self.assertEqual(result["object_counts"]["vm"]["skipped"], 1)


if __name__ == "__main__":
    unittest.main()
