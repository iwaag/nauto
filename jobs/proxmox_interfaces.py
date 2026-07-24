"""Pure VMInterface/IPAddress matching, creation, and convergence for Proxmox ingest.

This module contains no Django/Nautobot import (mirrors ``proxmox_ingest.py`` and
``proxmox_upsert.py``) so it can be unit tested without a live environment. It consumes only
the already-validated ``interfaces`` subtree that ``proxmox_ingest.validate_proxmox_facts()``
passes through unchanged for each guest (``config_interfaces``, ``agent_interfaces``,
``joined_interfaces``, ``unmatched`` — shapes produced by
``nodeutils/proxmox_inventory.py``'s ``join_qemu_interfaces()`` and ``config_interfaces()``).

Only ``joined_interfaces`` entries are eligible for VMInterface materialization
(devdocs/big/vm/p2/plan.md Section 3.2 non-goals and Section 5.5 "Interface matching" rule 1):

- QEMU: ``joined_interfaces`` items are the deterministic 1:1 config/agent MAC join nodeutils
  already computed; config-only and agent-only evidence never reaches this list.
- LXC: nodeutils' ``build_lxc_guest`` sets ``joined_interfaces`` to the same list as
  ``config_interfaces`` (explicit config is sufficient; there is no guest-agent join for LXC).

``jobs/proxmox_upsert.py`` calls this module once per successfully-matched guest, inside the
same per-guest savepoint Step 4 already established.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any, Callable

_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


# --------------------------------------------------------------------------------------
# Custom-field access helpers (duplicated intentionally from proxmox_upsert.py, which itself
# duplicates them from ingest_nodeutils_inventory.py — this module must stay Django-free and
# loadable via ad hoc ``importlib`` file loading in tests, which does not support package-
# relative imports).
# --------------------------------------------------------------------------------------


def cf_get(obj: Any, key: str, default: Any = None) -> Any:
    cf_attr = getattr(obj, "cf", None)
    if isinstance(cf_attr, dict) and key in cf_attr:
        return cf_attr[key]
    data = getattr(obj, "custom_field_data", None)
    if isinstance(data, dict):
        return data.get(key, default)
    return default


def cf_set(obj: Any, key: str, value: Any) -> None:
    if hasattr(obj, "cf"):
        obj.cf[key] = value
        return
    data = getattr(obj, "custom_field_data", None)
    if isinstance(data, dict):
        data[key] = value
        return
    raise AttributeError("object does not expose writable custom field data")

SOURCE_QEMU = "qemu_config_agent_join"
SOURCE_LXC = "lxc_config"

PRESENCE_PRESENT = "present"
PRESENCE_ABSENT = "absent"

MAX_MANAGED_IPS_PER_INTERFACE = 64
MAX_UNMATCHED_EVIDENCE_PER_VM = 64


def is_valid_mac(value: Any) -> bool:
    return isinstance(value, str) and bool(_MAC_RE.match(value))


# --------------------------------------------------------------------------------------
# IP candidate extraction and validation (Section 5.4 "Loopback, link-local, multicast,
# unspecified, malformed, prefix-less agent addresses, and LXC tokens such as dhcp create no
# IPAddress relation.")
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class IPCandidate:
    address: str
    prefix: int
    family: int
    key: str  # "<address>/<prefix>", the managed-evidence and matching key


def _classify_and_build(address_str: str, prefix: Any) -> IPCandidate | None:
    if prefix is None:
        return None
    try:
        prefix_int = int(prefix)
    except (TypeError, ValueError):
        return None
    try:
        interface = ipaddress.ip_interface(f"{address_str}/{prefix_int}")
    except ValueError:
        return None
    ip = interface.ip
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return None
    return IPCandidate(
        address=str(ip), prefix=int(interface.network.prefixlen), family=ip.version, key=str(interface)
    )


def qemu_ip_candidates(joined_iface: dict[str, Any]) -> list[IPCandidate]:
    """Extract exact reliable IP evidence from a QEMU joined-interface's agent addresses.

    Prefix-less agent addresses are excluded (Section 5.4); a prefix is required.
    """
    out: list[IPCandidate] = []
    seen: set[str] = set()
    for item in joined_iface.get("ip_addresses", []) or []:
        if not isinstance(item, dict):
            continue
        address = item.get("address")
        prefix = item.get("prefix")
        if not isinstance(address, str) or not address:
            continue
        candidate = _classify_and_build(address, prefix)
        if candidate is not None and candidate.key not in seen:
            seen.add(candidate.key)
            out.append(candidate)
    return out


def lxc_ip_candidates(candidate: dict[str, Any]) -> list[IPCandidate]:
    """Extract exact reliable IP evidence from an explicit LXC config ``ip=`` value.

    The literal ``dhcp`` token and any value without an explicit prefix create no IPAddress
    relation (Section 5.4).
    """
    ip_value = candidate.get("ip")
    if not isinstance(ip_value, str) or not ip_value:
        return []
    if ip_value.strip().lower() in ("dhcp", "auto", "manual"):
        return []
    if "/" not in ip_value:
        return []
    address_part, _, prefix_part = ip_value.partition("/")
    result = _classify_and_build(address_part, prefix_part)
    return [result] if result is not None else []


# --------------------------------------------------------------------------------------
# Interface candidate extraction (Section 5.5 "Interface matching" rule 1; Section 3.2
# non-goal: never create VMInterfaces from config-only QEMU or agent-only evidence)
# --------------------------------------------------------------------------------------


@dataclass
class InterfaceCandidate:
    config_slot: str
    mac_address: str
    bridge: str | None
    guest_interface_name: str | None
    source: str
    ip_candidates: list[IPCandidate] = field(default_factory=list)


def interface_candidates_for_guest(
    guest_type: str, guest: dict[str, Any]
) -> tuple[list[InterfaceCandidate], list[dict[str, Any]]]:
    """Return (eligible candidates, bounded diagnostic evidence entries) for one guest.

    Diagnostic entries cover joined items with a missing/invalid MAC (never eligible even
    though nodeutils placed them in ``joined_interfaces``) and every ``unmatched`` entry —
    these never create a VMInterface or IP relation; they are retained only in
    ``proxmox_interface_evidence`` (Section 5.5 item 9).
    """
    interfaces = guest.get("interfaces") or {}
    joined = interfaces.get("joined_interfaces") or []
    unmatched = interfaces.get("unmatched") or []
    source = SOURCE_QEMU if guest_type == "qemu" else SOURCE_LXC

    candidates: list[InterfaceCandidate] = []
    diagnostics: list[dict[str, Any]] = []

    seen_slots: dict[str, int] = {}
    seen_macs: dict[str, int] = {}
    for item in joined:
        if not isinstance(item, dict):
            continue
        slot = item.get("config_slot")
        mac = item.get("mac_address")
        seen_slots[slot] = seen_slots.get(slot, 0) + 1
        if not slot or not is_valid_mac(mac):
            diagnostics.append({"config_slot": slot, "reason": "missing_or_invalid_mac"})
            continue
        seen_macs[mac] = seen_macs.get(mac, 0) + 1
        ip_candidates = qemu_ip_candidates(item) if guest_type == "qemu" else lxc_ip_candidates(item)
        candidates.append(
            InterfaceCandidate(
                config_slot=slot,
                mac_address=mac,
                bridge=item.get("bridge"),
                guest_interface_name=item.get("guest_interface_name"),
                source=source,
                ip_candidates=ip_candidates,
            )
        )

    # Section 5.5 item 10: multi-NIC guests never cross-pair — a duplicate config_slot inside
    # one guest's own joined_interfaces would be a nodeutils-level defect; defend here too by
    # excluding any slot that appears more than once from materialization.
    duplicate_slots = {slot for slot, count in seen_slots.items() if count > 1}
    if duplicate_slots:
        candidates = [c for c in candidates if c.config_slot not in duplicate_slots]
        for slot in duplicate_slots:
            diagnostics.append({"config_slot": slot, "reason": "duplicate_config_slot"})

    # A MAC reused across two distinct config slots inside the same guest is ambiguous
    # ownership within one VM (Section 5.4: "a MAC already used by an incompatible interface
    # is a conflict"); nodeutils' QEMU join already excludes this case from
    # ``joined_interfaces``, but LXC's ``joined_interfaces`` is the raw explicit config list,
    # so nauto defends the same rule here for LXC (and defensively for QEMU too).
    duplicate_macs = {mac for mac, count in seen_macs.items() if count > 1}
    if duplicate_macs:
        candidates = [c for c in candidates if c.mac_address not in duplicate_macs]
        for mac in duplicate_macs:
            diagnostics.append({"config_slot": None, "mac_address": mac, "reason": "duplicate_mac_same_guest"})

    for item in unmatched:
        if isinstance(item, dict):
            diagnostics.append(
                {
                    "config_slot": item.get("config_slot"),
                    "guest_interface_name": item.get("guest_interface_name"),
                    "reason": "unmatched_evidence",
                }
            )

    return candidates, diagnostics[:MAX_UNMATCHED_EVIDENCE_PER_VM]


# --------------------------------------------------------------------------------------
# VMInterface matching (Section 5.5 "Interface matching" rules 2-4)
# --------------------------------------------------------------------------------------


def match_vm_interface(vminterface_manager: Any, *, vm: Any, config_slot: str) -> Any | None:
    found = list(vminterface_manager.filter(virtual_machine=vm, name=config_slot))
    return found[0] if found else None


def mac_conflict_in_cluster(
    vminterface_manager: Any, *, cluster: Any, mac_address: str, exclude: Any | None
) -> bool:
    """Section 5.5 "Interface matching" rule 3 and Section 5.4: a MAC already used by an
    *incompatible* interface within the same Proxmox Cluster is a conflict. Scoped to
    VMInterfaces only (the Device-level layer is explicitly exempt — Section 5.4 last
    paragraph)."""
    for candidate in vminterface_manager.filter(mac_address=mac_address):
        if exclude is not None and candidate is exclude:
            continue
        vm = getattr(candidate, "virtual_machine", None)
        if getattr(vm, "cluster", None) == cluster:
            return True
    return False


# --------------------------------------------------------------------------------------
# IP relation convergence (Section 5.5 "Interface/IP convergence" rules 1-7)
# --------------------------------------------------------------------------------------


@dataclass
class IpSyncOutcome:
    managed: dict[str, dict[str, Any]]
    created: int = 0
    attached_existing: int = 0
    detached: int = 0
    conflicts: list[dict[str, Any]] = field(default_factory=list)


def sync_interface_ips(
    *,
    interface: Any,
    candidates: list[IPCandidate],
    complete: bool,
    observed_at_str: str,
    find_ip: Callable[[str, int], Any | None],
    create_ip: Callable[[str, int], Any],
    ip_related_elsewhere: Callable[[Any, Any], bool],
    attach_ip: Callable[[Any, Any], None],
    detach_ip: Callable[[Any, Any], None],
) -> IpSyncOutcome:
    """Implement Section 5.5 rules 1-3, 6-7 for one already-matched, MAC-compatible interface.

    ``find_ip``/``create_ip`` operate on ``(address, prefix)``. ``ip_related_elsewhere``
    reports whether the given IPAddress already has an incompatible foreign VMInterface
    relation (rule 6): true means "do not touch; this is a local conflict for this
    candidate", never a detach of the foreign relation.
    """
    prior = cf_get(interface, "proxmox_managed_ip_evidence") or {}
    prior_managed: dict[str, dict[str, Any]] = dict(prior.get("managed") or {})

    if not complete:
        # Rule 3/7: partial input retains relations and their original evidence time; it is
        # never presented as fresh. No attach/detach happens.
        return IpSyncOutcome(managed=prior_managed)

    outcome = IpSyncOutcome(managed={})
    new_keys = {c.key: c for c in candidates}

    for key, candidate in new_keys.items():
        if key in prior_managed:
            # Still observed: keep the managed entry, refresh its evidence time.
            entry = dict(prior_managed[key])
            entry["evidence_observed_at"] = observed_at_str
            outcome.managed[key] = entry
            continue
        ip_obj = find_ip(candidate.address, candidate.prefix)
        if ip_obj is not None and ip_related_elsewhere(ip_obj, interface):
            outcome.conflicts.append({"key": key, "reason": "foreign_ip_relation"})
            continue
        if ip_obj is None:
            ip_obj = create_ip(candidate.address, candidate.prefix)
            outcome.created += 1
        else:
            outcome.attached_existing += 1
        attach_ip(interface, ip_obj)
        outcome.managed[key] = {
            "ip_id": str(getattr(ip_obj, "pk", None)),
            "evidence_observed_at": observed_at_str,
        }

    for key, entry in prior_managed.items():
        if key not in new_keys:
            ip_obj = find_ip(*_split_key(key))
            if ip_obj is not None:
                detach_ip(interface, ip_obj)
            outcome.detached += 1

    if len(outcome.managed) > MAX_MANAGED_IPS_PER_INTERFACE:
        outcome.managed = dict(list(outcome.managed.items())[:MAX_MANAGED_IPS_PER_INTERFACE])

    return outcome


def _split_key(key: str) -> tuple[str, int]:
    address, _, prefix = key.partition("/")
    return address, int(prefix) if prefix else 0


# --------------------------------------------------------------------------------------
# Per-guest orchestration: interface upsert + IP convergence + presence (Section 5.5 items
# 1-7 combined). Called by ``proxmox_upsert.ingest_proxmox_platform`` inside the same
# per-guest savepoint, only after the guest's VirtualMachine row itself upserted cleanly.
# --------------------------------------------------------------------------------------


@dataclass
class InterfaceSyncResult:
    counts: dict[str, dict[str, int]]
    errors: list[dict[str, str]]
    interface_evidence: dict[str, Any]


def sync_guest_interfaces(
    *,
    guest_type: str,
    guest: dict[str, Any],
    vm: Any,
    cluster: Any,
    vminterface_manager: Any,
    make_interface: Callable[[], Any],
    find_ip: Callable[[str, int], Any | None],
    create_ip: Callable[[str, int], Any],
    ip_related_elsewhere: Callable[[Any, Any], bool],
    attach_ip: Callable[[Any, Any], None],
    detach_ip: Callable[[Any, Any], None],
    save_fn: Callable[[Any], None],
    dry_run: bool,
    observed_at_str: str,
    config_complete: bool,
) -> InterfaceSyncResult:
    counts = {kind: {a: 0 for a in ("created", "updated", "unchanged", "skipped")} for kind in ("vminterface", "ip")}
    errors: list[dict[str, str]] = []

    candidates, diagnostics = interface_candidates_for_guest(guest_type, guest)
    candidate_slots = {c.config_slot for c in candidates}

    interface_evidence: dict[str, Any] = {}
    for diag in diagnostics:
        slot = diag.get("config_slot") or "unmatched"
        interface_evidence.setdefault(slot, {"evidence_observed_at": observed_at_str, "diagnostics": []})
        interface_evidence[slot]["diagnostics"].append(diag)

    for candidate in candidates:
        existing = match_vm_interface(vminterface_manager, vm=vm, config_slot=candidate.config_slot)

        if existing is None:
            if mac_conflict_in_cluster(
                vminterface_manager, cluster=cluster, mac_address=candidate.mac_address, exclude=None
            ):
                counts["vminterface"]["skipped"] += 1
                errors.append({"scope_kind": "interface", "scope_id": candidate.config_slot, "section": "interfaces", "code": "mac_conflict"})
                interface_evidence[candidate.config_slot] = {
                    "evidence_observed_at": observed_at_str,
                    "diagnostics": [{"reason": "mac_conflict"}],
                }
                continue

            iface = make_interface()
            iface.virtual_machine = vm
            iface.name = candidate.config_slot
            iface.mac_address = candidate.mac_address
            cf_set(iface, "proxmox_config_slot", candidate.config_slot)
            cf_set(iface, "proxmox_guest_interface_name", candidate.guest_interface_name)
            cf_set(iface, "proxmox_bridge", candidate.bridge)
            cf_set(iface, "proxmox_interface_source", candidate.source)
            cf_set(iface, "proxmox_observed_at", observed_at_str)
            cf_set(iface, "proxmox_presence", PRESENCE_PRESENT)
            cf_set(iface, "proxmox_managed_ip_evidence", {"managed": {}})
            if not dry_run:
                save_fn(iface)
            counts["vminterface"]["created"] += 1

            ip_outcome = sync_interface_ips(
                interface=iface, candidates=candidate.ip_candidates, complete=config_complete,
                observed_at_str=observed_at_str, find_ip=find_ip, create_ip=create_ip,
                ip_related_elsewhere=ip_related_elsewhere, attach_ip=attach_ip, detach_ip=detach_ip,
            )
            cf_set(iface, "proxmox_managed_ip_evidence", {"managed": ip_outcome.managed, "evidence_observed_at": observed_at_str})
            if not dry_run:
                save_fn(iface)
            counts["ip"]["created"] += ip_outcome.created
            counts["ip"]["updated"] += ip_outcome.attached_existing
            counts["ip"]["skipped"] += ip_outcome.detached + len(ip_outcome.conflicts)
            for conflict in ip_outcome.conflicts:
                errors.append({"scope_kind": "interface", "scope_id": candidate.config_slot, "section": "ip", "code": conflict["reason"]})
            continue

        # Existing interface: MAC-change is an unsupported target-local conflict (rule 4) —
        # never rewrite the MAC and never touch its relations.
        if getattr(existing, "mac_address", None) != candidate.mac_address:
            counts["vminterface"]["skipped"] += 1
            errors.append({"scope_kind": "interface", "scope_id": candidate.config_slot, "section": "interfaces", "code": "interface_mac_changed"})
            continue

        changed = False
        for key, value in (
            ("proxmox_guest_interface_name", candidate.guest_interface_name),
            ("proxmox_bridge", candidate.bridge),
            ("proxmox_interface_source", candidate.source),
        ):
            if cf_get(existing, key) != value:
                cf_set(existing, key, value)
                changed = True
        if cf_get(existing, "proxmox_observed_at") != observed_at_str:
            cf_set(existing, "proxmox_observed_at", observed_at_str)
            changed = True
        if cf_get(existing, "proxmox_presence") != PRESENCE_PRESENT:
            cf_set(existing, "proxmox_presence", PRESENCE_PRESENT)
            changed = True

        ip_outcome = sync_interface_ips(
            interface=existing, candidates=candidate.ip_candidates, complete=config_complete,
            observed_at_str=observed_at_str, find_ip=find_ip, create_ip=create_ip,
            ip_related_elsewhere=ip_related_elsewhere, attach_ip=attach_ip, detach_ip=detach_ip,
        )
        new_evidence = {"managed": ip_outcome.managed, "evidence_observed_at": observed_at_str if config_complete else cf_get(existing, "proxmox_managed_ip_evidence", {}).get("evidence_observed_at")}
        if cf_get(existing, "proxmox_managed_ip_evidence") != new_evidence:
            cf_set(existing, "proxmox_managed_ip_evidence", new_evidence)
            changed = True

        if changed:
            if not dry_run:
                save_fn(existing)
            counts["vminterface"]["updated"] += 1
        else:
            counts["vminterface"]["unchanged"] += 1
        counts["ip"]["created"] += ip_outcome.created
        counts["ip"]["updated"] += ip_outcome.attached_existing
        counts["ip"]["skipped"] += ip_outcome.detached + len(ip_outcome.conflicts)
        for conflict in ip_outcome.conflicts:
            errors.append({"scope_kind": "interface", "scope_id": candidate.config_slot, "section": "ip", "code": conflict["reason"]})

    # Presence convergence (Section 5.5 rule 5): a *complete, untruncated* config enumeration
    # marks any previously-managed VMInterface for this VM+source, absent from this
    # generation's candidate slot set, as proxmox_presence=absent and detaches its managed IPs.
    # Partial enumeration never marks absence.
    if config_complete:
        for existing in list(vminterface_manager.filter(virtual_machine=vm)):
            slot = cf_get(existing, "proxmox_config_slot")
            source = cf_get(existing, "proxmox_interface_source")
            if source != (SOURCE_QEMU if guest_type == "qemu" else SOURCE_LXC):
                continue
            if slot in candidate_slots:
                continue
            if cf_get(existing, "proxmox_presence") == PRESENCE_ABSENT:
                continue
            prior = cf_get(existing, "proxmox_managed_ip_evidence") or {}
            prior_managed = dict(prior.get("managed") or {})
            for key in list(prior_managed):
                ip_obj = find_ip(*_split_key(key))
                if ip_obj is not None:
                    detach_ip(existing, ip_obj)
                counts["ip"]["skipped"] += 1
            cf_set(existing, "proxmox_presence", PRESENCE_ABSENT)
            cf_set(existing, "proxmox_managed_ip_evidence", {"managed": {}, "evidence_observed_at": observed_at_str})
            if not dry_run:
                save_fn(existing)
            counts["vminterface"]["updated"] += 1

    return InterfaceSyncResult(counts=counts, errors=errors, interface_evidence=interface_evidence)
