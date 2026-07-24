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


proxmox_ingest = _load("proxmox_ingest", "proxmox_ingest.py")
proxmox_upsert = _load("proxmox_upsert", "proxmox_upsert.py")
proxmox_interfaces = proxmox_upsert._load_proxmox_interfaces()

validate_proxmox_facts = proxmox_ingest.validate_proxmox_facts

RECEIVED_AT = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)
T0 = "2026-07-24T12:00:00+00:00"
T1 = "2026-07-24T12:01:00+00:00"
T2 = "2026-07-24T12:02:00+00:00"  # T0 < T1 < T2, all within the 300s future-skew allowance


# --------------------------------------------------------------------------------------
# Fake Nautobot-shaped ORM, extended from test_proxmox_cluster_vm_upsert.py's pattern with
# VMInterface and IPAddress + a through-model-style assignment list (mirrors the live
# ipam/ip-address-to-interface shape report2.0.md's Step 0 introspection recorded: mutually
# exclusive interface/vm_interface fields on one assignment row).
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
    def __init__(self, *, ip_address, vm_interface=None, device_interface=None):
        self.ip_address = ip_address
        self.vm_interface = vm_interface
        self.interface = device_interface  # device-level (dual-layer, never touched by us)


def make_env():
    cluster_store: list = []
    vm_store: list = []
    iface_store: list = []
    ip_store: list = []
    assignment_store: list = []
    saved_ids = {"next": 1}

    def save_fn(obj):
        if obj.pk is None:
            obj.pk = saved_ids["next"]
            saved_ids["next"] += 1
            kind = getattr(obj, "_kind", None)
            store = {"cluster": cluster_store, "vm": vm_store, "iface": iface_store, "ip": ip_store}.get(kind)
            if store is not None:
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

    def make_interface():
        obj = FakeModel()
        obj._kind = "iface"
        return obj

    def resolve_host(address):
        matches = [ip for ip in ip_store if ip.address_str == address]
        if not matches:
            return proxmox_interfaces.IpLookupResult(status="not_found")
        if len(matches) > 1:
            return proxmox_interfaces.IpLookupResult(status="ambiguous")
        return proxmox_interfaces.IpLookupResult(status="found", ip=matches[0])

    def find_parent_prefix(address):
        return "fake-parent-prefix"  # every address has a covering Prefix in this fixture

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
        for a in assignment_store:
            if a.ip_address is ip_obj and a.vm_interface is not None and a.vm_interface is not interface:
                return True
        return False

    def attach_ip(interface, ip_obj):
        for a in assignment_store:
            if a.ip_address is ip_obj and a.vm_interface is interface:
                return
        assignment_store.append(FakeAssignment(ip_address=ip_obj, vm_interface=interface))

    def detach_ip(interface, ip_obj):
        assignment_store[:] = [
            a for a in assignment_store if not (a.ip_address is ip_obj and a.vm_interface is interface)
        ]

    return {
        "cluster_store": cluster_store,
        "vm_store": vm_store,
        "iface_store": iface_store,
        "ip_store": ip_store,
        "assignment_store": assignment_store,
        "cluster_manager": FakeManager(cluster_store),
        "vm_manager": FakeManager(vm_store),
        "vminterface_manager": FakeManager(iface_store),
        "cluster_type": cluster_type,
        "make_cluster": make_cluster,
        "make_vm": make_vm,
        "make_interface": make_interface,
        "resolve_host": resolve_host,
        "find_parent_prefix": find_parent_prefix,
        "create_ip": create_ip,
        "find_ip_by_id": find_ip_by_id,
        "ip_related_elsewhere": ip_related_elsewhere,
        "attach_ip": attach_ip,
        "detach_ip": detach_ip,
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


def _complete_observation(section_name, evidence_time=T0):
    return {
        "state": "complete",
        "sections": {section_name: {"state": "complete", "evidence_observed_at": evidence_time}},
    }


def _partial_observation(section_name, evidence_time=None):
    return {
        "state": "partial",
        "sections": {section_name: {"state": "partial", "evidence_observed_at": evidence_time}},
    }


def _qemu_guest(*, interfaces=None, observation=None, **overrides):
    guest = {
        "guest_type": "qemu",
        "vmid": 102,
        "node": "aghub",
        "name": "aghaos",
        "proxmox_status": "running",
        "vcpus": 2,
        "memory_mb": 4096,
        "disk_gb": 32.0,
        "observation": observation or _complete_observation("agent_interfaces"),
        "interfaces": interfaces or {"config_interfaces": [], "agent_interfaces": [], "joined_interfaces": [], "unmatched": []},
    }
    guest.update(overrides)
    return guest


def _lxc_guest(*, interfaces=None, observation=None, **overrides):
    guest = {
        "guest_type": "lxc",
        "vmid": 108,
        "node": "aghub",
        "name": "agdnsmasq",
        "proxmox_status": "running",
        "vcpus": 1,
        "memory_mb": 512,
        "disk_gb": 7.78,
        "observation": observation or _complete_observation("config"),
        "interfaces": interfaces or {"config_interfaces": [], "agent_interfaces": [], "joined_interfaces": [], "unmatched": []},
        "rootfs": {"storage": "local-lvm", "volume": "vm-108-disk-0", "size_gb": 8.0},
    }
    guest.update(overrides)
    return guest


def joined_qemu(config_slot="net0", mac="aa:bb:cc:dd:ee:01", bridge="vmbr0", guest_if="eth0", addrs=None):
    return {
        "config_slot": config_slot,
        "mac_address": mac,
        "bridge": bridge,
        "guest_interface_name": guest_if,
        "ip_addresses": addrs if addrs is not None else [{"address": "10.0.0.5", "type": "ipv4", "prefix": 24}],
    }


def joined_lxc(config_slot="net0", mac="aa:bb:cc:dd:ee:02", bridge="vmbr0", guest_if="eth0", ip="10.0.0.6/24"):
    entry = {"config_slot": config_slot, "mac_address": mac, "bridge": bridge, "guest_interface_name": guest_if}
    if ip is not None:
        entry["ip"] = ip
    return entry


def run_ingest(facts, env, *, observer_device_id="device-uuid-1"):
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
        guest_atomic=contextlib.nullcontext,
        vminterface_manager=env["vminterface_manager"],
        make_interface=env["make_interface"],
        resolve_host=env["resolve_host"],
        find_parent_prefix=env["find_parent_prefix"],
        create_ip=env["create_ip"],
        find_ip_by_id=env["find_ip_by_id"],
        ip_related_elsewhere=env["ip_related_elsewhere"],
        attach_ip=env["attach_ip"],
        detach_ip=env["detach_ip"],
    )


# --------------------------------------------------------------------------------------
# Pure IP-candidate / interface-candidate extraction unit tests
# --------------------------------------------------------------------------------------


class IpCandidateExtractionTests(unittest.TestCase):
    def test_qemu_ipv4_with_prefix(self) -> None:
        out = proxmox_interfaces.qemu_ip_candidates(joined_qemu())
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].address, "10.0.0.5")
        self.assertEqual(out[0].prefix, 24)
        self.assertEqual(out[0].family, 4)

    def test_qemu_ipv6_with_prefix(self) -> None:
        entry = joined_qemu(addrs=[{"address": "2001:db8::5", "type": "ipv6", "prefix": 64}])
        out = proxmox_interfaces.qemu_ip_candidates(entry)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].family, 6)

    def test_qemu_missing_prefix_excluded(self) -> None:
        entry = joined_qemu(addrs=[{"address": "10.0.0.5", "type": "ipv4"}])
        self.assertEqual(proxmox_interfaces.qemu_ip_candidates(entry), [])

    def test_loopback_link_local_multicast_unspecified_excluded(self) -> None:
        for addr, prefix in [("127.0.0.1", 8), ("169.254.1.1", 16), ("224.0.0.1", 24), ("0.0.0.0", 0), ("::1", 128), ("fe80::1", 64)]:
            entry = joined_qemu(addrs=[{"address": addr, "type": "ipv4", "prefix": prefix}])
            self.assertEqual(proxmox_interfaces.qemu_ip_candidates(entry), [], addr)

    def test_malformed_address_excluded(self) -> None:
        entry = joined_qemu(addrs=[{"address": "not-an-ip", "type": "ipv4", "prefix": 24}])
        self.assertEqual(proxmox_interfaces.qemu_ip_candidates(entry), [])

    def test_lxc_static_cidr(self) -> None:
        out = proxmox_interfaces.lxc_ip_candidates({"ip": "10.0.0.6/24"})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].address, "10.0.0.6")
        self.assertEqual(out[0].prefix, 24)

    def test_lxc_dhcp_token_excluded(self) -> None:
        self.assertEqual(proxmox_interfaces.lxc_ip_candidates({"ip": "dhcp"}), [])
        self.assertEqual(proxmox_interfaces.lxc_ip_candidates({"ip": "DHCP"}), [])

    def test_lxc_missing_prefix_excluded(self) -> None:
        self.assertEqual(proxmox_interfaces.lxc_ip_candidates({"ip": "10.0.0.6"}), [])

    def test_lxc_missing_ip_key(self) -> None:
        self.assertEqual(proxmox_interfaces.lxc_ip_candidates({}), [])


# --------------------------------------------------------------------------------------
# QEMU interface scenario matrix (plan.md Section 8.2 "QEMU interface")
# --------------------------------------------------------------------------------------


class QemuInterfaceTests(unittest.TestCase):
    def test_unique_mac_join_creates_interface_and_ip(self) -> None:
        env = make_env()
        facts = _base_facts(qemu_vms=[_qemu_guest(interfaces={
            "config_interfaces": [], "agent_interfaces": [], "joined_interfaces": [joined_qemu()], "unmatched": [],
        })])
        result = run_ingest(facts, env)
        self.assertEqual(result["object_counts"]["vminterface"]["created"], 1)
        self.assertEqual(result["object_counts"]["ip"]["created"], 1)
        iface = env["iface_store"][0]
        self.assertEqual(iface.name, "net0")
        self.assertEqual(iface.mac_address, "aa:bb:cc:dd:ee:01")
        self.assertEqual(iface.custom_field_data["proxmox_interface_source"], "qemu_config_agent_join")
        self.assertEqual(iface.custom_field_data["proxmox_presence"], "present")
        self.assertEqual(len(env["ip_store"]), 1)
        self.assertEqual(env["ip_store"][0].address_str, "10.0.0.5")
        managed = iface.custom_field_data["proxmox_managed_ip_evidence"]["managed"]
        self.assertIn("10.0.0.5/24", managed)

    def test_config_only_creates_no_interface(self) -> None:
        env = make_env()
        # config-only: not present in joined_interfaces (matches nodeutils' contract).
        facts = _base_facts(qemu_vms=[_qemu_guest(interfaces={
            "config_interfaces": [{"config_slot": "net0", "mac_address": "aa:bb:cc:dd:ee:01", "bridge": "vmbr0"}],
            "agent_interfaces": [], "joined_interfaces": [], "unmatched": [{"config_slot": "net0", "mac_address": "aa:bb:cc:dd:ee:01"}],
        })])
        result = run_ingest(facts, env)
        self.assertEqual(result["object_counts"]["vminterface"]["created"], 0)
        self.assertEqual(env["iface_store"], [])

    def test_agent_only_creates_no_interface(self) -> None:
        env = make_env()
        facts = _base_facts(qemu_vms=[_qemu_guest(interfaces={
            "config_interfaces": [], "agent_interfaces": [{"guest_interface_name": "eth0", "mac_address": "aa:bb:cc:dd:ee:01"}],
            "joined_interfaces": [], "unmatched": [{"guest_interface_name": "eth0", "mac_address": "aa:bb:cc:dd:ee:01"}],
        })])
        result = run_ingest(facts, env)
        self.assertEqual(result["object_counts"]["vminterface"]["created"], 0)
        self.assertEqual(env["iface_store"], [])

    def test_unique_but_different_multi_nic_no_cross_pair(self) -> None:
        env = make_env()
        facts = _base_facts(qemu_vms=[_qemu_guest(interfaces={
            "config_interfaces": [], "agent_interfaces": [],
            "joined_interfaces": [
                joined_qemu(config_slot="net0", mac="aa:bb:cc:dd:ee:01", guest_if="eth0", addrs=[{"address": "10.0.0.5", "type": "ipv4", "prefix": 24}]),
                joined_qemu(config_slot="net1", mac="aa:bb:cc:dd:ee:02", guest_if="eth1", addrs=[{"address": "10.0.1.5", "type": "ipv4", "prefix": 24}]),
            ],
            "unmatched": [],
        })])
        result = run_ingest(facts, env)
        self.assertEqual(result["object_counts"]["vminterface"]["created"], 2)
        by_slot = {i.name: i for i in env["iface_store"]}
        self.assertEqual(by_slot["net0"].mac_address, "aa:bb:cc:dd:ee:01")
        self.assertEqual(by_slot["net1"].mac_address, "aa:bb:cc:dd:ee:02")
        net0_ips = {a.ip_address.address_str for a in env["assignment_store"] if a.vm_interface is by_slot["net0"]}
        net1_ips = {a.ip_address.address_str for a in env["assignment_store"] if a.vm_interface is by_slot["net1"]}
        self.assertEqual(net0_ips, {"10.0.0.5"})
        self.assertEqual(net1_ips, {"10.0.1.5"})

    def test_duplicate_config_mac_excluded_by_nodeutils_join(self) -> None:
        # nodeutils' join_qemu_interfaces() never places an ambiguous (duplicate) MAC into
        # joined_interfaces at all -- it stays in unmatched. Simulate that contract directly.
        env = make_env()
        facts = _base_facts(qemu_vms=[_qemu_guest(interfaces={
            "config_interfaces": [], "agent_interfaces": [], "joined_interfaces": [],
            "unmatched": [
                {"config_slot": "net0", "mac_address": "aa:bb:cc:dd:ee:01"},
                {"config_slot": "net1", "mac_address": "aa:bb:cc:dd:ee:01"},
            ],
        })])
        result = run_ingest(facts, env)
        self.assertEqual(result["object_counts"]["vminterface"]["created"], 0)

    def test_duplicate_agent_mac_excluded_by_nodeutils_join(self) -> None:
        env = make_env()
        facts = _base_facts(qemu_vms=[_qemu_guest(interfaces={
            "config_interfaces": [], "agent_interfaces": [], "joined_interfaces": [],
            "unmatched": [
                {"guest_interface_name": "eth0", "mac_address": "aa:bb:cc:dd:ee:01"},
                {"guest_interface_name": "eth1", "mac_address": "aa:bb:cc:dd:ee:01"},
            ],
        })])
        result = run_ingest(facts, env)
        self.assertEqual(result["object_counts"]["vminterface"]["created"], 0)

    def test_invalid_or_missing_mac_in_joined_never_materializes(self) -> None:
        env = make_env()
        facts = _base_facts(qemu_vms=[_qemu_guest(interfaces={
            "config_interfaces": [], "agent_interfaces": [],
            "joined_interfaces": [{"config_slot": "net0", "bridge": "vmbr0", "guest_interface_name": "eth0", "ip_addresses": []}],
            "unmatched": [],
        })])
        result = run_ingest(facts, env)
        self.assertEqual(result["object_counts"]["vminterface"]["created"], 0)
        self.assertEqual(env["iface_store"], [])

    def test_partial_agent_results_retain_relations_and_old_evidence_time(self) -> None:
        env = make_env()
        facts1 = _base_facts(observed_at=T0, qemu_vms=[_qemu_guest(
            interfaces={"config_interfaces": [], "agent_interfaces": [], "joined_interfaces": [joined_qemu()], "unmatched": []},
            observation=_complete_observation("agent_interfaces", T0),
        )])
        run_ingest(facts1, env)
        iface = env["iface_store"][0]
        first_evidence = iface.custom_field_data["proxmox_observed_at"]
        first_managed = iface.custom_field_data["proxmox_managed_ip_evidence"]["managed"]

        # Generation 2: agent_interfaces section failed (partial) -- config still yields the
        # same joined candidate (nodeutils re-emits last-known config), but the section state
        # is now partial, so IP relations/evidence must be retained, never presented as fresh.
        facts2 = _base_facts(observed_at=T1, qemu_vms=[_qemu_guest(
            interfaces={"config_interfaces": [], "agent_interfaces": [], "joined_interfaces": [joined_qemu()], "unmatched": []},
            observation=_partial_observation("agent_interfaces"),
        )])
        result = run_ingest(facts2, env)
        iface_after = env["iface_store"][0]
        self.assertEqual(iface_after.custom_field_data["proxmox_managed_ip_evidence"]["managed"], first_managed)
        # The interface's own proxmox_observed_at is not advanced from partial evidence either,
        # since the sync path only advances it when the section produced a materializable candidate
        # with the *same* evidence timestamp source; here we assert IPs specifically stayed put.
        self.assertEqual(len(env["assignment_store"]), 1)


# --------------------------------------------------------------------------------------
# LXC interface scenario matrix (plan.md Section 8.2 "LXC interface")
# --------------------------------------------------------------------------------------


class LxcInterfaceTests(unittest.TestCase):
    def test_static_cidr_creates_interface_and_ip(self) -> None:
        env = make_env()
        facts = _base_facts(lxc_containers=[_lxc_guest(interfaces={
            "config_interfaces": [joined_lxc()], "agent_interfaces": [], "joined_interfaces": [joined_lxc()], "unmatched": [],
        })])
        result = run_ingest(facts, env)
        self.assertEqual(result["object_counts"]["vminterface"]["created"], 1)
        iface = env["iface_store"][0]
        self.assertEqual(iface.custom_field_data["proxmox_interface_source"], "lxc_config")
        self.assertEqual(len(env["ip_store"]), 1)
        self.assertEqual(env["ip_store"][0].address_str, "10.0.0.6")

    def test_dhcp_token_creates_interface_no_ip(self) -> None:
        env = make_env()
        iface_candidate = joined_lxc(ip="dhcp")
        facts = _base_facts(lxc_containers=[_lxc_guest(interfaces={
            "config_interfaces": [iface_candidate], "agent_interfaces": [], "joined_interfaces": [iface_candidate], "unmatched": [],
        })])
        result = run_ingest(facts, env)
        self.assertEqual(result["object_counts"]["vminterface"]["created"], 1)
        self.assertEqual(env["ip_store"], [])
        self.assertEqual(env["assignment_store"], [])

    def test_missing_mac_creates_no_interface(self) -> None:
        env = make_env()
        candidate = {"config_slot": "net0", "bridge": "vmbr0", "guest_interface_name": "eth0", "ip": "10.0.0.6/24"}
        facts = _base_facts(lxc_containers=[_lxc_guest(interfaces={
            "config_interfaces": [candidate], "agent_interfaces": [], "joined_interfaces": [candidate], "unmatched": [],
        })])
        result = run_ingest(facts, env)
        self.assertEqual(result["object_counts"]["vminterface"]["created"], 0)
        self.assertEqual(env["iface_store"], [])

    def test_duplicate_mac_same_guest_excluded(self) -> None:
        env = make_env()
        c1 = joined_lxc(config_slot="net0", mac="aa:bb:cc:dd:ee:09")
        c2 = joined_lxc(config_slot="net1", mac="aa:bb:cc:dd:ee:09")
        facts = _base_facts(lxc_containers=[_lxc_guest(interfaces={
            "config_interfaces": [c1, c2], "agent_interfaces": [], "joined_interfaces": [c1, c2], "unmatched": [],
        })])
        result = run_ingest(facts, env)
        self.assertEqual(result["object_counts"]["vminterface"]["created"], 0)

    def test_multiple_net_slots_each_own_interface(self) -> None:
        env = make_env()
        c1 = joined_lxc(config_slot="net0", mac="aa:bb:cc:dd:ee:01", ip="10.0.0.6/24")
        c2 = joined_lxc(config_slot="net1", mac="aa:bb:cc:dd:ee:02", ip="10.0.1.6/24")
        facts = _base_facts(lxc_containers=[_lxc_guest(interfaces={
            "config_interfaces": [c1, c2], "agent_interfaces": [], "joined_interfaces": [c1, c2], "unmatched": [],
        })])
        result = run_ingest(facts, env)
        self.assertEqual(result["object_counts"]["vminterface"]["created"], 2)
        self.assertEqual({i.name for i in env["iface_store"]}, {"net0", "net1"})


# --------------------------------------------------------------------------------------
# IP/interface convergence (plan.md Section 8.2 "IP/interface convergence")
# --------------------------------------------------------------------------------------


class ConvergenceTests(unittest.TestCase):
    def _ingest_with_ip(self, env, ip_addr="10.0.0.5", prefix=24, observed_at=T0, guest_atomic=None):
        facts = _base_facts(observed_at=observed_at, qemu_vms=[_qemu_guest(
            interfaces={"config_interfaces": [], "agent_interfaces": [], "joined_interfaces": [
                joined_qemu(addrs=[{"address": ip_addr, "type": "ipv4", "prefix": prefix}])
            ], "unmatched": []},
            observation=_complete_observation("agent_interfaces", observed_at),
        )])
        return run_ingest(facts, env)

    def test_device_dual_relation_left_alone(self) -> None:
        env = make_env()
        # Pre-seed an IPAddress already related to a *Device* interface (dual-layer evidence),
        # not a VMInterface. This must not block the VMInterface from also relating to it.
        ip = env["create_ip"]("10.0.0.5", 24)
        fake_device_iface = object()
        from types import SimpleNamespace
        env["assignment_store"].append(SimpleNamespace(ip_address=ip, vm_interface=None, interface=fake_device_iface))
        result = self._ingest_with_ip(env)
        self.assertEqual(result["object_counts"]["vminterface"]["created"], 1)
        iface = env["iface_store"][0]
        vm_assignments = [a for a in env["assignment_store"] if getattr(a, "vm_interface", None) is iface]
        self.assertEqual(len(vm_assignments), 1)
        self.assertIs(vm_assignments[0].ip_address, ip)
        # Foreign device-level relation is untouched.
        device_assignments = [a for a in env["assignment_store"] if getattr(a, "interface", None) is fake_device_iface]
        self.assertEqual(len(device_assignments), 1)

    def test_foreign_vm_interface_relation_untouched(self) -> None:
        env = make_env()
        ip = env["create_ip"]("10.0.0.5", 24)
        foreign_iface = env["make_interface"]()
        env["save_fn"](foreign_iface)
        env["attach_ip"](foreign_iface, ip)

        result = self._ingest_with_ip(env)
        self.assertEqual(result["object_counts"]["vminterface"]["created"], 1)
        new_iface = next(i for i in env["iface_store"] if i is not foreign_iface)
        # New interface got a skip/conflict recorded, not a stolen relation.
        new_assignments = [a for a in env["assignment_store"] if a.vm_interface is new_iface]
        self.assertEqual(new_assignments, [])
        foreign_assignments = [a for a in env["assignment_store"] if a.vm_interface is foreign_iface]
        self.assertEqual(len(foreign_assignments), 1)
        codes = [e["code"] for e in result["guest_errors"]]
        self.assertIn("foreign_ip_relation", codes)

    def test_complete_ip_change_converges_only_managed_set(self) -> None:
        env = make_env()
        self._ingest_with_ip(env, ip_addr="10.0.0.5", observed_at=T0)
        iface = env["iface_store"][0]
        self.assertEqual(len(env["assignment_store"]), 1)

        result = self._ingest_with_ip(env, ip_addr="10.0.0.9", observed_at=T1)
        self.assertEqual(result["object_counts"]["ip"]["created"], 1)
        addrs = {a.ip_address.address_str for a in env["assignment_store"] if a.vm_interface is iface}
        self.assertEqual(addrs, {"10.0.0.9"})

    def test_prefix_only_evidence_change_does_not_detach_the_same_ip(self) -> None:
        # sidefix2/plan.md Section 3.4: prior managed key at one prefix, new observed key at
        # the same host but a different prefix, both resolving to the same IP identity — the
        # relation must never look detached merely because the evidence key's mask changed.
        env = make_env()
        self._ingest_with_ip(env, ip_addr="10.0.0.5", prefix=24, observed_at=T0)
        iface = env["iface_store"][0]
        ip_obj = env["ip_store"][0]
        self.assertEqual(len(env["assignment_store"]), 1)

        result = self._ingest_with_ip(env, ip_addr="10.0.0.5", prefix=32, observed_at=T1)
        self.assertEqual(result["object_counts"]["ip"]["created"], 0, "must reuse, not create a second IPAddress")
        self.assertEqual(len(env["ip_store"]), 1)
        assignments = [a for a in env["assignment_store"] if a.vm_interface is iface]
        self.assertEqual(len(assignments), 1, "the relation must remain attached throughout")
        self.assertIs(assignments[0].ip_address, ip_obj)
        managed = iface.custom_field_data["proxmox_managed_ip_evidence"]["managed"]
        self.assertEqual(set(managed), {"10.0.0.5/32"})
        self.assertEqual(managed["10.0.0.5/32"]["ip_id"], str(ip_obj.pk))

    def test_same_host_multiple_prefixes_one_generation_is_ambiguous(self) -> None:
        # sidefix2/plan.md Section 3.4 step 2 / Section 5.1 case 9: one generation reporting
        # the same host with two different prefixes must fail closed, not pick by order.
        env = make_env()
        facts = _base_facts(qemu_vms=[_qemu_guest(
            interfaces={"config_interfaces": [], "agent_interfaces": [], "joined_interfaces": [
                joined_qemu(addrs=[
                    {"address": "10.0.0.5", "type": "ipv4", "prefix": 24},
                    {"address": "10.0.0.5", "type": "ipv4", "prefix": 32},
                ])
            ], "unmatched": []},
        )])
        result = run_ingest(facts, env)
        self.assertEqual(env["ip_store"], [])
        self.assertEqual(env["assignment_store"], [])
        codes = [e["code"] for e in result["guest_errors"]]
        self.assertIn("ip_observed_prefix_ambiguous", codes)

    def test_missing_parent_prefix_is_bounded_conflict_not_exception(self) -> None:
        env = make_env()
        env["find_parent_prefix"] = lambda address: None
        result = self._ingest_with_ip(env, ip_addr="10.0.0.5", prefix=24)
        self.assertEqual(env["ip_store"], [])
        codes = [e["code"] for e in result["guest_errors"]]
        self.assertIn("ip_parent_prefix_missing", codes)

    def test_authoritative_empty_detaches_managed(self) -> None:
        env = make_env()
        self._ingest_with_ip(env, observed_at=T0)
        iface = env["iface_store"][0]
        self.assertEqual(len(env["assignment_store"]), 1)

        facts_empty = _base_facts(observed_at=T1, qemu_vms=[_qemu_guest(
            interfaces={"config_interfaces": [], "agent_interfaces": [], "joined_interfaces": [
                joined_qemu(addrs=[])
            ], "unmatched": []},
            observation=_complete_observation("agent_interfaces", T1),
        )])
        run_ingest(facts_empty, env)
        remaining = [a for a in env["assignment_store"] if a.vm_interface is iface]
        self.assertEqual(remaining, [])
        self.assertEqual(iface.custom_field_data["proxmox_managed_ip_evidence"]["managed"], {})
        # The IPAddress object itself is never deleted.
        self.assertEqual(len(env["ip_store"]), 1)

    def test_partial_retention_keeps_old_evidence_time(self) -> None:
        env = make_env()
        self._ingest_with_ip(env, observed_at=T0)
        iface = env["iface_store"][0]
        managed_before = iface.custom_field_data["proxmox_managed_ip_evidence"]["managed"]
        evidence_before = managed_before["10.0.0.5/24"]["evidence_observed_at"]

        facts_partial = _base_facts(observed_at=T1, qemu_vms=[_qemu_guest(
            interfaces={"config_interfaces": [], "agent_interfaces": [], "joined_interfaces": [joined_qemu()], "unmatched": []},
            observation=_partial_observation("agent_interfaces"),
        )])
        run_ingest(facts_partial, env)
        managed_after = iface.custom_field_data["proxmox_managed_ip_evidence"]["managed"]
        self.assertEqual(managed_after, managed_before)
        self.assertEqual(managed_after["10.0.0.5/24"]["evidence_observed_at"], evidence_before)

    def test_mac_change_is_conflict_not_auto_fixed(self) -> None:
        env = make_env()
        self._ingest_with_ip(env, observed_at=T0)
        iface = env["iface_store"][0]
        original_mac = iface.mac_address
        self.assertEqual(len(env["assignment_store"]), 1)

        facts_mac_change = _base_facts(observed_at=T1, qemu_vms=[_qemu_guest(
            interfaces={"config_interfaces": [], "agent_interfaces": [], "joined_interfaces": [
                joined_qemu(mac="aa:bb:cc:dd:ee:99")
            ], "unmatched": []},
            observation=_complete_observation("agent_interfaces", T1),
        )])
        result = run_ingest(facts_mac_change, env)
        self.assertEqual(iface.mac_address, original_mac)  # never rewritten
        self.assertEqual(len(env["assignment_store"]), 1)  # relations untouched
        codes = [e["code"] for e in result["guest_errors"]]
        self.assertIn("interface_mac_changed", codes)

    def test_complete_disappearance_sets_absent_and_detaches(self) -> None:
        env = make_env()
        self._ingest_with_ip(env, observed_at=T0)
        iface = env["iface_store"][0]
        self.assertEqual(len(env["assignment_store"]), 1)

        facts_gone = _base_facts(observed_at=T1, qemu_vms=[_qemu_guest(
            interfaces={"config_interfaces": [], "agent_interfaces": [], "joined_interfaces": [], "unmatched": []},
            observation=_complete_observation("agent_interfaces", T1),
        )])
        run_ingest(facts_gone, env)
        self.assertEqual(iface.custom_field_data["proxmox_presence"], "absent")
        remaining = [a for a in env["assignment_store"] if a.vm_interface is iface]
        self.assertEqual(remaining, [])
        # Row itself retained.
        self.assertIn(iface, env["iface_store"])

    def test_later_recovery_reattaches(self) -> None:
        env = make_env()
        self._ingest_with_ip(env, observed_at=T0)
        iface = env["iface_store"][0]

        facts_gone = _base_facts(observed_at=T1, qemu_vms=[_qemu_guest(
            interfaces={"config_interfaces": [], "agent_interfaces": [], "joined_interfaces": [], "unmatched": []},
            observation=_complete_observation("agent_interfaces", T1),
        )])
        run_ingest(facts_gone, env)
        self.assertEqual(iface.custom_field_data["proxmox_presence"], "absent")

        self._ingest_with_ip(env, observed_at=T2)
        self.assertEqual(iface.custom_field_data["proxmox_presence"], "present")
        remaining = [a for a in env["assignment_store"] if a.vm_interface is iface]
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].ip_address.address_str, "10.0.0.5")
        # No second VMInterface was created for the same slot.
        self.assertEqual(len(env["iface_store"]), 1)


class MacConflictAcrossVmsTests(unittest.TestCase):
    def test_mac_used_by_incompatible_interface_in_same_cluster_is_conflict(self) -> None:
        env = make_env()
        facts_a = _base_facts(qemu_vms=[_qemu_guest(vmid=201, name="vm-a", interfaces={
            "config_interfaces": [], "agent_interfaces": [], "joined_interfaces": [joined_qemu(mac="aa:bb:cc:dd:ee:77")], "unmatched": [],
        })])
        run_ingest(facts_a, env)
        self.assertEqual(len(env["iface_store"]), 1)

        facts_b = _base_facts(observed_at=T1, qemu_vms=[_qemu_guest(vmid=202, name="vm-b", interfaces={
            "config_interfaces": [], "agent_interfaces": [], "joined_interfaces": [joined_qemu(mac="aa:bb:cc:dd:ee:77")], "unmatched": [],
        })])
        result = run_ingest(facts_b, env)
        self.assertEqual(len(env["iface_store"]), 1)  # second interface not created
        codes = [e["code"] for e in result["guest_errors"]]
        self.assertIn("mac_conflict", codes)


if __name__ == "__main__":
    unittest.main()
