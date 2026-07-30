"""Pure Cluster/VirtualMachine matching, diffing, and freshness rules for Proxmox ingest.

This module contains no Django/Nautobot import so it can be unit tested without a live
environment (mirrors ``jobs/proxmox_ingest.py``). It operates on duck-typed "manager" and
"model" objects: a manager exposes ``.filter(**kwargs)`` returning an iterable of model
objects, and a model object exposes plain attributes plus either a ``.cf`` mapping or a
``.custom_field_data`` dict for custom fields — exactly the interface real Nautobot
Cluster/VirtualMachine querysets and instances provide. ``jobs/ingest_nodeutils_inventory.py``
is the sole caller that wires this to the real ORM (devdocs/big/vm/p2/plan.md Section 5.1: the
normal nauto Job is the sole Cluster/VM/VMInterface/IP writer).

Only Cluster and VirtualMachine matching/upsert are implemented here (Phase 2 Step 4).
VMInterface/IPAddress matching is Step 5's scope.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable

STATUS_ACTIVE = "Active"
STATUS_OFFLINE = "Offline"
STATUS_UNKNOWN = "Unknown"

ROLE_QEMU = "virtual-machine"
ROLE_LXC = "lxc-container"

CLUSTER_NAME_SOURCES = {"proxmox_cluster_name", "standalone_node_fallback"}


def _load_proxmox_interfaces():
    """Import ``proxmox_interfaces`` whether this module is loaded as part of the real
    ``nauto.jobs`` package (production) or ad hoc via ``importlib.util.spec_from_file_location``
    with no parent package (the pattern the test suite uses for every pure module in this
    directory, mirroring ``proxmox_ingest``/``proxmox_upsert`` itself)."""
    if __package__:
        from importlib import import_module

        return import_module(f"{__package__}.proxmox_interfaces")
    import importlib.util
    import sys
    from pathlib import Path

    module_name = "proxmox_interfaces"
    if module_name in sys.modules:
        return sys.modules[module_name]
    path = Path(__file__).resolve().parent / "proxmox_interfaces.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class ProxmoxUpsertError(RuntimeError):
    """Raised for a Cluster/guest-level failure that must roll back its own scope only."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


# --------------------------------------------------------------------------------------
# Custom-field access helpers (duplicated intentionally from ingest_nodeutils_inventory.py:
# that module imports Django at module scope, which this pure module must not do).
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


def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


# --------------------------------------------------------------------------------------
# Scope-key derivation (Section 5.5 Cluster matching rules 1 and 3)
# --------------------------------------------------------------------------------------


def derive_cluster_scope_key(name_source: str, *, identity_value: str, observer_device_id: str) -> str:
    if name_source == "proxmox_cluster_name":
        return f"cluster-name:{identity_value}"
    if name_source == "standalone_node_fallback":
        return f"standalone-device:{observer_device_id}"
    raise ProxmoxUpsertError("invalid_name_source", f"unknown cluster name_source: {name_source!r}")


# --------------------------------------------------------------------------------------
# Cluster matching (Section 5.5 Cluster matching rules)
# --------------------------------------------------------------------------------------


@dataclass
class MatchResult:
    obj: Any | None
    error_code: str | None = None


def match_cluster(
    cluster_manager: Any,
    *,
    cluster_type: Any,
    scope_key: str,
    name: str,
    name_source: str,
    observer_device_id: str,
) -> MatchResult:
    """Implement Cluster matching rules 1-2, 4-7 (rule 3 is scope-key derivation)."""
    candidates = list(cluster_manager.filter(cluster_type=cluster_type))

    scope_matches = [c for c in candidates if cf_get(c, "proxmox_scope_key") == scope_key]
    if len(scope_matches) > 1:
        return MatchResult(None, "duplicate_scope_key")
    if len(scope_matches) == 1:
        return MatchResult(scope_matches[0], None)

    # Zero scope-key candidates: same-name, observer, and observed-node conflict checks
    # (rule 5) before authorizing create.
    name_conflicts = [
        c
        for c in candidates
        if getattr(c, "name", None) == name and cf_get(c, "proxmox_scope_key") != scope_key
    ]
    if name_conflicts:
        return MatchResult(None, "same_name_disjoint_scope_conflict")

    if name_source == "standalone_node_fallback":
        # Rule 4: a fallback observation whose observer Device UUID changed, or a transition
        # between fallback and provider-name identity for what looks like the same observer,
        # is a migration that must not be auto-applied.
        observer_conflicts = [
            c
            for c in candidates
            if cf_get(c, "proxmox_identity_source") == "standalone_node_fallback"
            and cf_get(c, "proxmox_observer_device_id") not in (None, observer_device_id)
            and cf_get(c, "proxmox_observer_device_id") is not None
            and cf_get(c, "proxmox_scope_key") != scope_key
            and getattr(c, "name", None) == name
        ]
        if observer_conflicts:
            return MatchResult(None, "fallback_scope_migration_required")

    return MatchResult(None, None)


# --------------------------------------------------------------------------------------
# Guest matching (Section 5.5 Guest matching rules)
# --------------------------------------------------------------------------------------


def match_guest(
    vm_manager: Any,
    *,
    cluster: Any,
    guest_type: str,
    vmid: int,
    name: str,
) -> MatchResult:
    same_identity = [
        v
        for v in vm_manager.filter()
        if cf_get(v, "proxmox_guest_type") == guest_type and cf_get(v, "proxmox_vmid") == vmid
    ]
    cross_cluster = [v for v in same_identity if getattr(v, "cluster", None) != cluster]
    if cross_cluster:
        return MatchResult(None, "cross_cluster_conflict")

    within_cluster = [v for v in same_identity if getattr(v, "cluster", None) == cluster]
    if len(within_cluster) > 1:
        return MatchResult(None, "duplicate_vmid_kind")
    if len(within_cluster) == 1:
        return MatchResult(within_cluster[0], None)

    same_name = [v for v in vm_manager.filter(cluster=cluster, name=name)]
    if same_name:
        return MatchResult(None, "same_name_conflict")

    return MatchResult(None, None)


# --------------------------------------------------------------------------------------
# Native status/role/capacity mapping (Section 5.4 VirtualMachine table)
# --------------------------------------------------------------------------------------


def map_status(raw_status: Any) -> str:
    if raw_status == "running":
        return STATUS_ACTIVE
    if raw_status in ("stopped", "paused"):
        return STATUS_OFFLINE
    return STATUS_UNKNOWN


def map_role(guest_type: str) -> str:
    if guest_type == "qemu":
        return ROLE_QEMU
    if guest_type == "lxc":
        return ROLE_LXC
    raise ProxmoxUpsertError("invalid_guest_type", f"unknown guest_type: {guest_type!r}")


def build_lxc_rootfs(guest: dict[str, Any]) -> dict[str, Any] | None:
    rootfs = guest.get("rootfs")
    if not isinstance(rootfs, dict):
        return None
    return {
        "storage": rootfs.get("storage"),
        "volume": rootfs.get("volume"),
        "size_gb": rootfs.get("size_gb"),
    }


def guest_disk_gb(guest_type: str, guest: dict[str, Any]) -> Any:
    """Section 5.4: native ``disk`` comes only from parsed LXC rootfs.size_gb, never disk_gb."""
    if guest_type != "lxc":
        return None
    rootfs = guest.get("rootfs")
    if isinstance(rootfs, dict) and rootfs.get("size_gb") is not None:
        return rootfs["size_gb"]
    return None


# --------------------------------------------------------------------------------------
# Storage-content ledger mapping (Section 5.4 Cluster table ``proxmox_storage_content``;
# Section 5.3 multi-generation merge key ``(Cluster id, node, storage, content_type)``).
# --------------------------------------------------------------------------------------


def storage_content_key(scope: dict[str, Any]) -> str:
    return f"{scope.get('node')}:{scope.get('storage')}:{scope.get('content_type')}"


def build_storage_content_entry(scope: dict[str, Any]) -> dict[str, Any]:
    items = list(scope.get("items") or [])[:2048]
    return {
        "node": scope.get("node"),
        "storage": scope.get("storage"),
        "content_type": scope.get("content_type"),
        "state": scope.get("state"),
        "last_attempted_at": scope.get("last_attempted_at"),
        "evidence_observed_at": scope.get("evidence_observed_at"),
        "omitted_error_count": scope.get("omitted_error_count", 0),
        "errors": list(scope.get("errors") or [])[:128],
        "items": [
            {
                "volid": item.get("volid"),
                "content": item.get("content"),
                "format": item.get("format"),
                "size_bytes": item.get("size_bytes"),
            }
            for item in items
        ],
    }


def merge_storage_content(
    existing: dict[str, Any] | None, new_scopes: Iterable[dict[str, Any]]
) -> tuple[dict[str, Any], bool]:
    """Merge freshly validated storage-content scopes into the persisted Cluster map.

    A ``complete`` scope fully replaces its key's prior entry. A ``partial`` scope (attempted
    but failed collection this generation) advances only ``last_attempted_at``/errors while
    retaining the prior key's ``evidence_observed_at``/``items`` when a prior entry exists
    (Section 5.3: "a failed/unobserved key retains its prior evidence ... advances only
    last_attempted_at/error state"). A key absent from ``new_scopes`` is left untouched.
    Returns ``(merged_map, any_partial)`` where ``any_partial`` is true if any scope in this
    generation was partial, so the caller can fold that into platform completeness (Section
    5.5: "Cluster final freshness/completeness is written only after guest/storage processing
    determines the final state.").
    """
    merged = dict(existing or {})
    any_partial = False
    for scope in new_scopes:
        key = storage_content_key(scope)
        entry = build_storage_content_entry(scope)
        if entry["state"] == "partial":
            any_partial = True
            prior = merged.get(key)
            if prior is not None:
                entry = {
                    **prior,
                    "state": "partial",
                    "last_attempted_at": entry["last_attempted_at"],
                    "omitted_error_count": entry["omitted_error_count"],
                    "errors": entry["errors"],
                }
        merged[key] = entry
    return merged, any_partial


# --------------------------------------------------------------------------------------
# Freshness-aware generic upsert (Section 5.3 freshness rules, Section 5.5 no-op rule)
# --------------------------------------------------------------------------------------


@dataclass
class UpsertOutcome:
    obj: Any
    action: str  # create | update | noop | stale_evidence | conflicting_same_generation
    changed_fields: list[str] = field(default_factory=list)
    error_code: str | None = None


def upsert_with_freshness(
    *,
    existing: Any | None,
    make_new: Callable[[], Any],
    native_fields: dict[str, Any],
    cf_fields: dict[str, Any],
    observed_at_cf_key: str,
    observed_at_value: datetime,
    save_fn: Callable[[Any], None],
) -> UpsertOutcome:
    """Apply Section 5.3/5.5 freshness and no-op-diff rules to one Cluster or VM row.

    ``native_fields``/``cf_fields`` are the full allowlisted target values for this
    generation. Equal timestamps with equal values are a no-op; equal timestamps with any
    differing allowlisted value are ``conflicting_same_generation``; an older incoming
    timestamp is ``stale_evidence`` and never mutates the object.

    Always calls ``save_fn`` for a create/update (sidefix1 Section 5.1/5.3: preview safety is
    the caller's transaction-rollback boundary, not a lower-level save-suppression Boolean).
    """
    observed_at_str = observed_at_value.isoformat()

    if existing is None:
        obj = make_new()
        for key, value in native_fields.items():
            setattr(obj, key, value)
        for key, value in cf_fields.items():
            cf_set(obj, key, value)
        cf_set(obj, observed_at_cf_key, observed_at_str)
        changed = sorted([*native_fields, *cf_fields, observed_at_cf_key])
        save_fn(obj)
        return UpsertOutcome(obj, "create", changed)

    existing_observed = parse_iso(cf_get(existing, observed_at_cf_key))

    if existing_observed is not None:
        if observed_at_value < existing_observed:
            return UpsertOutcome(existing, "stale_evidence", [], error_code="stale_evidence")
        if observed_at_value == existing_observed:
            conflicts = [key for key, value in native_fields.items() if getattr(existing, key, None) != value]
            conflicts += [key for key, value in cf_fields.items() if cf_get(existing, key) != value]
            if conflicts:
                return UpsertOutcome(
                    existing, "conflicting_same_generation", sorted(conflicts), error_code="conflicting_same_generation"
                )
            return UpsertOutcome(existing, "noop", [])

    changed: list[str] = []
    for key, value in native_fields.items():
        if getattr(existing, key, None) != value:
            setattr(existing, key, value)
            changed.append(key)
    for key, value in cf_fields.items():
        if cf_get(existing, key) != value:
            cf_set(existing, key, value)
            changed.append(key)
    if cf_get(existing, observed_at_cf_key) != observed_at_str:
        cf_set(existing, observed_at_cf_key, observed_at_str)
        changed.append(observed_at_cf_key)

    if not changed:
        return UpsertOutcome(existing, "noop", [])
    save_fn(existing)
    return UpsertOutcome(existing, "update", sorted(changed))


# --------------------------------------------------------------------------------------
# Bounded observation detail (Section 5.4 proxmox_observation_detail)
# --------------------------------------------------------------------------------------


def build_observation_detail(*, state: str, errors: Iterable[dict[str, str]] | None = None) -> dict[str, Any]:
    bounded_errors = list(errors or [])[:128]
    return {
        "state": state,
        "omitted_error_count": max(0, len(list(errors or [])) - len(bounded_errors)),
        "errors": bounded_errors,
    }


def _count_key(action: str) -> str:
    return {"create": "created", "update": "updated", "noop": "unchanged"}.get(action, "skipped")


def _section(
    *,
    identity_source: str | None,
    scope_key: str | None,
    cluster_name: str | None,
    cluster_id: Any,
    observation_state: str,
    counts: dict[str, dict[str, int]],
    changed_fields: dict[str, list[str]],
    guest_errors: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "identity_source": identity_source,
        "scope_key": scope_key,
        "cluster_name": cluster_name,
        "cluster_id": str(cluster_id) if cluster_id is not None else None,
        "observation_state": observation_state,
        "object_counts": counts,
        "changed_fields": changed_fields,
        "guest_errors": guest_errors[:128],
    }


# --------------------------------------------------------------------------------------
# Full platform orchestration: Cluster upsert, per-guest savepoints, deferred completeness
# finalization (Section 5.5 transaction boundaries; Step 4 sub-items 1-11). This function is
# the single owner of Cluster/VM matching+diff+transaction sequencing; the real Job
# (ingest_nodeutils_inventory.py) supplies real Nautobot managers/lookups/save/atomic and
# calls this unchanged, so tests exercise the exact same code path with fake ORM doubles.
# --------------------------------------------------------------------------------------


def ingest_proxmox_platform(
    *,
    validation: Any,
    cluster_manager: Any,
    vm_manager: Any,
    cluster_type: Any,
    make_cluster: Callable[[], Any],
    make_vm: Callable[[Any], Any],
    status_lookup: Callable[[str], Any | None],
    role_lookup: Callable[[str], Any | None],
    observer_device_id: str | None,
    save_fn: Callable[[Any], None],
    guest_atomic: Callable[[], Any] = contextlib.nullcontext,
    vminterface_manager: Any | None = None,
    make_interface: Callable[[], Any] | None = None,
    resolve_host: Callable[[str], Any] | None = None,
    find_parent_prefix: Callable[[str], Any | None] | None = None,
    create_ip: Callable[[str, int, Any], Any] | None = None,
    find_ip_by_id: Callable[[str | None], Any | None] | None = None,
    ip_related_elsewhere: Callable[[Any, Any], bool] | None = None,
    attach_ip: Callable[[Any, Any], None] | None = None,
    detach_ip: Callable[[Any, Any], None] | None = None,
    sanitize_created_ids: bool = False,
) -> dict[str, Any]:
    """Validate-and-upsert one report's already-validated ``facts.proxmox`` subtree.

    ``validation`` is a ``proxmox_ingest.ProxmoxValidationResult`` with ``valid=True`` (the
    caller must reject invalid reports before calling this — Section 5.5: "Invalid top-level
    report or unsupported nested schema: no writes for that report"). Returns the bounded
    ``proxmox`` summary section described in plan.md Section 5.5.

    ``sanitize_created_ids`` never changes what gets written (Step 1 already made every save
    unconditional); it only controls the returned ``cluster_id``. When true and this call's
    own Cluster match found no pre-existing row, ``cluster_id`` is reported as ``None`` instead
    of the real-but-rollback-only primary key the caller's transaction just allocated
    (sidefix1 problem_fixplan.md Section 5.4: "Database IDs allocated to rows created inside the
    preview transaction are temporary and must not be presented as apply-stable identifiers.").
    """
    counts = {kind: {a: 0 for a in ("created", "updated", "unchanged", "skipped")} for kind in ("cluster", "vm", "vminterface", "ip")}
    changed_fields: dict[str, list[str]] = {}
    guest_errors: list[dict[str, str]] = list(validation.errors)
    platform_partial = validation.state == "partial"

    interfaces_enabled = vminterface_manager is not None and make_interface is not None
    # Guest and interface convergence share one presence vocabulary. Load the pure interface
    # module even when interface materialization is disabled so the guest rule cannot invent a
    # parallel spelling.
    proxmox_interfaces = _load_proxmox_interfaces()

    cluster_info = validation.cluster or {}
    name_source = cluster_info.get("name_source")
    name = cluster_info.get("name")
    identity_value = cluster_info.get("identity_value")

    if not observer_device_id:
        guest_errors.append(
            {"scope_kind": "platform", "scope_id": "cluster", "section": "cluster_identity", "code": "no_observer_device"}
        )
        return _section(
            identity_source=name_source, scope_key=None, cluster_name=name, cluster_id=None,
            observation_state="partial", counts=counts, changed_fields=changed_fields, guest_errors=guest_errors,
        )

    try:
        scope_key = derive_cluster_scope_key(name_source, identity_value=identity_value, observer_device_id=observer_device_id)
    except ProxmoxUpsertError as exc:
        guest_errors.append({"scope_kind": "platform", "scope_id": "cluster", "section": "cluster_identity", "code": exc.code})
        return _section(
            identity_source=name_source, scope_key=None, cluster_name=name, cluster_id=None,
            observation_state="partial", counts=counts, changed_fields=changed_fields, guest_errors=guest_errors,
        )

    match = match_cluster(
        cluster_manager, cluster_type=cluster_type, scope_key=scope_key, name=name,
        name_source=name_source, observer_device_id=observer_device_id,
    )
    cluster_is_new = match.obj is None
    if match.error_code:
        guest_errors.append({"scope_kind": "platform", "scope_id": scope_key, "section": "cluster_identity", "code": match.error_code})
        return _section(
            identity_source=name_source, scope_key=scope_key, cluster_name=name, cluster_id=None,
            observation_state="partial", counts=counts, changed_fields=changed_fields, guest_errors=guest_errors,
        )

    existing_storage_content = cf_get(match.obj, "proxmox_storage_content") if match.obj is not None else None
    storage_content_map, storage_partial = merge_storage_content(existing_storage_content, validation.storage_content)
    if storage_partial:
        platform_partial = True

    cluster_native = {"name": name, "comments": "Managed by nauto Proxmox ingest (devdocs/big/vm/p2)."}
    cluster_cf = {
        "proxmox_observer_device_id": observer_device_id,
        "proxmox_identity_source": name_source,
        "proxmox_scope_key": scope_key,
        "proxmox_observed_node_names": list(cluster_info.get("observed_node_names") or []),
        "proxmox_node_count": cluster_info.get("node_count"),
        "proxmox_storage_content": storage_content_map,
    }
    cluster_outcome = upsert_with_freshness(
        existing=match.obj, make_new=make_cluster, native_fields=cluster_native, cf_fields=cluster_cf,
        observed_at_cf_key="proxmox_observed_at", observed_at_value=validation.observed_at,
        save_fn=save_fn,
    )
    counts["cluster"][_count_key(cluster_outcome.action)] += 1
    if cluster_outcome.changed_fields:
        changed_fields["cluster"] = cluster_outcome.changed_fields
    cluster = cluster_outcome.obj
    reported_cluster_id = None if (sanitize_created_ids and cluster_is_new) else getattr(cluster, "pk", None)

    if cluster_outcome.action in ("stale_evidence", "conflicting_same_generation"):
        guest_errors.append(
            {"scope_kind": "platform", "scope_id": scope_key, "section": "cluster_identity", "code": cluster_outcome.error_code}
        )
        return _section(
            identity_source=name_source, scope_key=scope_key, cluster_name=name, cluster_id=reported_cluster_id,
            observation_state="partial", counts=counts, changed_fields=changed_fields, guest_errors=guest_errors,
        )

    for guest_type, guests in (("qemu", validation.qemu_vms), ("lxc", validation.lxc_containers)):
        for guest in guests:
            vmid = guest.get("vmid")
            guest_name = guest.get("name") or f"{guest_type}-{vmid}"
            scope_id = f"{guest_type}:{vmid}"
            # Section 3.6: allocate guest-local result state. Nothing here is merged into the
            # platform-level counts/changed_fields/guest_errors until guest_atomic() exits
            # successfully, so an exception anywhere in this guest's body leaves the platform
            # accumulators exactly as they were before this guest started.
            guest_counts = {kind: {a: 0 for a in ("created", "updated", "unchanged", "skipped")} for kind in ("vm", "vminterface", "ip")}
            guest_changed_fields: dict[str, list[str]] = {}
            guest_non_terminal_errors: list[dict[str, str]] = []
            try:
                with guest_atomic():
                    guest_match = match_guest(vm_manager, cluster=cluster, guest_type=guest_type, vmid=vmid, name=guest_name)
                    if guest_match.error_code:
                        raise ProxmoxUpsertError(guest_match.error_code)
                    status_obj = status_lookup(map_status(guest.get("proxmox_status")))
                    role_obj = role_lookup(map_role(guest_type))
                    if status_obj is None or role_obj is None:
                        raise ProxmoxUpsertError("missing_seeded_prerequisite")

                    native_fields: dict[str, Any] = {
                        "name": guest_name, "cluster": cluster, "status": status_obj, "role": role_obj,
                    }
                    if guest.get("vcpus") is not None:
                        native_fields["vcpus"] = guest["vcpus"]
                    if guest.get("memory_mb") is not None:
                        native_fields["memory"] = guest["memory_mb"]
                    disk_gb = guest_disk_gb(guest_type, guest)
                    if disk_gb is not None:
                        native_fields["disk"] = disk_gb

                    guest_cf = {
                        "proxmox_guest_type": guest_type,
                        "proxmox_vmid": vmid,
                        "proxmox_node": guest.get("node"),
                        "proxmox_status": guest.get("proxmox_status"),
                        "proxmox_observation_state": "complete",
                        "proxmox_observation_detail": build_observation_detail(state="complete", errors=[]),
                        "proxmox_lxc_rootfs": build_lxc_rootfs(guest) if guest_type == "lxc" else None,
                        "proxmox_presence": proxmox_interfaces.PRESENCE_PRESENT,
                    }
                    vm_outcome = upsert_with_freshness(
                        existing=guest_match.obj,
                        make_new=lambda cl=cluster: make_vm(cl),
                        native_fields=native_fields, cf_fields=guest_cf,
                        observed_at_cf_key="proxmox_observed_at", observed_at_value=validation.observed_at,
                        save_fn=save_fn,
                    )
                    if vm_outcome.action in ("stale_evidence", "conflicting_same_generation"):
                        raise ProxmoxUpsertError(vm_outcome.error_code)
                    guest_counts["vm"][_count_key(vm_outcome.action)] += 1
                    if vm_outcome.changed_fields:
                        guest_changed_fields[f"vm:{scope_id}"] = vm_outcome.changed_fields

                    if interfaces_enabled:
                        vm_obj = vm_outcome.obj
                        observation = guest.get("observation") if isinstance(guest.get("observation"), dict) else {}
                        sections = observation.get("sections") if isinstance(observation.get("sections"), dict) else {}
                        relevant_section = sections.get("agent_interfaces") if guest_type == "qemu" else sections.get("config")
                        section_state = None
                        section_time = None
                        if isinstance(relevant_section, dict):
                            section_state = relevant_section.get("state")
                            section_time = relevant_section.get("evidence_observed_at")
                        config_complete = (section_state or observation.get("state")) == "complete"
                        interface_observed_at = section_time or validation.observed_at.isoformat()

                        iface_result = proxmox_interfaces.sync_guest_interfaces(
                            guest_type=guest_type,
                            guest=guest,
                            vm=vm_obj,
                            cluster=cluster,
                            vminterface_manager=vminterface_manager,
                            make_interface=make_interface,
                            resolve_host=resolve_host,
                            find_parent_prefix=find_parent_prefix,
                            create_ip=create_ip,
                            find_ip_by_id=find_ip_by_id,
                            ip_related_elsewhere=ip_related_elsewhere,
                            attach_ip=attach_ip,
                            detach_ip=detach_ip,
                            save_fn=save_fn,
                            observed_at_str=interface_observed_at,
                            config_complete=config_complete,
                        )
                        for kind in ("vminterface", "ip"):
                            for action, value in iface_result.counts[kind].items():
                                guest_counts[kind][action] += value
                        if iface_result.errors:
                            guest_non_terminal_errors.extend(iface_result.errors)
                        if iface_result.interface_evidence:
                            existing_evidence = cf_get(vm_obj, "proxmox_interface_evidence") or {}
                            merged_evidence = {**existing_evidence, **iface_result.interface_evidence}
                            if cf_get(vm_obj, "proxmox_interface_evidence") != merged_evidence:
                                cf_set(vm_obj, "proxmox_interface_evidence", merged_evidence)
                                save_fn(vm_obj)
                                guest_changed_fields.setdefault(f"vm:{scope_id}", [])
                                if "proxmox_interface_evidence" not in guest_changed_fields[f"vm:{scope_id}"]:
                                    guest_changed_fields[f"vm:{scope_id}"].append("proxmox_interface_evidence")
            except Exception as exc:  # noqa: BLE001 - Section 5.5: "Any exception rolls back" this guest only
                # The guest_atomic() savepoint (if any) already rolled back every DB write for
                # this guest; discard every local claim too so counts/changed_fields never
                # describe a row that no longer exists inside this transaction.
                platform_partial = True
                counts["vm"]["skipped"] += 1
                code = getattr(exc, "code", None) or "guest_upsert_failed"
                guest_errors.append({"scope_kind": "guest", "scope_id": scope_id, "section": "identity", "code": code})
            else:
                # guest_atomic() exited successfully: merge this guest's local result state into
                # the platform-level accumulators exactly once.
                for kind in ("vm", "vminterface", "ip"):
                    for action, value in guest_counts[kind].items():
                        counts[kind][action] += value
                changed_fields.update(guest_changed_fields)
                guest_errors.extend(guest_non_terminal_errors)

    final_state = "partial" if platform_partial else "complete"
    # Omission establishes absence only when this generation fully enumerated the same Cluster
    # scope. Retain all last-known realization evidence; only presence and its evidence time
    # move forward.
    if final_state == "complete":
        observed_guest_keys = {
            (guest_type, guest.get("vmid"))
            for guest_type, guests in (("qemu", validation.qemu_vms), ("lxc", validation.lxc_containers))
            for guest in guests
        }
        try:
            for existing_vm in vm_manager.filter(cluster=cluster):
                guest_type = cf_get(existing_vm, "proxmox_guest_type")
                vmid = cf_get(existing_vm, "proxmox_vmid")
                if guest_type not in ("qemu", "lxc") or vmid is None or (guest_type, vmid) in observed_guest_keys:
                    continue
                existing_observed = parse_iso(cf_get(existing_vm, "proxmox_observed_at"))
                if existing_observed is not None and existing_observed > validation.observed_at:
                    continue
                if cf_get(existing_vm, "proxmox_presence") == proxmox_interfaces.PRESENCE_ABSENT:
                    continue
                cf_set(existing_vm, "proxmox_presence", proxmox_interfaces.PRESENCE_ABSENT)
                cf_set(existing_vm, "proxmox_observed_at", validation.observed_at.isoformat())
                save_fn(existing_vm)
                counts["vm"]["updated"] += 1
                changed = changed_fields.setdefault(f"vm:{guest_type}:{vmid}", [])
                for field_name in ("proxmox_presence", "proxmox_observed_at"):
                    if field_name not in changed:
                        changed.append(field_name)
        except Exception as exc:  # noqa: BLE001 - failed sweep makes absence untrustworthy
            platform_partial = True
            final_state = "partial"
            guest_errors.append({
                "scope_kind": "platform", "scope_id": scope_key, "section": "guest_presence",
                "code": getattr(exc, "code", None) or "guest_absence_sweep_failed",
            })
    final_detail = build_observation_detail(state=final_state, errors=guest_errors)
    if cf_get(cluster, "proxmox_observation_state") != final_state or cf_get(cluster, "proxmox_observation_detail") != final_detail:
        cf_set(cluster, "proxmox_observation_state", final_state)
        cf_set(cluster, "proxmox_observation_detail", final_detail)
        save_fn(cluster)
        cluster_changed = changed_fields.setdefault("cluster", [])
        for key in ("proxmox_observation_state", "proxmox_observation_detail"):
            if key not in cluster_changed:
                cluster_changed.append(key)

    return _section(
        identity_source=name_source, scope_key=scope_key, cluster_name=getattr(cluster, "name", name),
        cluster_id=reported_cluster_id, observation_state=final_state,
        counts=counts, changed_fields=changed_fields, guest_errors=guest_errors,
    )
