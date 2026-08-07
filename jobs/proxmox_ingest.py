"""Pure ``nodeutils.proxmox.v1`` validation, independent of any ORM write.

This module only classifies a decoded ``facts.proxmox`` mapping (already parsed from the
report's JSON/YAML) into validated candidates plus bounded, closed-code errors. It never touches
Django models; ``ingest_nodeutils_inventory.py`` is the sole ORM writer and calls this module
first (devdocs/big/vm/p2/plan.md Section 5.1/5.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

PROXMOX_SCHEMA_VERSION = "nodeutils.proxmox.v1"
DEFAULT_MAX_FUTURE_SKEW_SECONDS = 300

_MAC_HINT = "mac_address"

_ENVELOPE_KEYS = {
    "schema_version",
    "enabled",
    "detected",
    "mode",
    "inventory_source",
    "observed_at",
    "collection",
    "cluster",
    "qemu_vms",
    "lxc_containers",
    "storage_content",
}
_CLUSTER_KEYS = {"name", "name_source", "identity_value", "node_count", "observed_node_names"}
_GUEST_COMMON_KEYS = {
    "guest_type",
    "vmid",
    "node",
    "name",
    "proxmox_status",
    "status",
    "vcpus",
    "memory_mb",
    "disk_gb",
    "observation",
    "interfaces",
}
_QEMU_KEYS = _GUEST_COMMON_KEYS
_LXC_KEYS = _GUEST_COMMON_KEYS | {"rootfs"}
_INTERFACES_KEYS = {"config_interfaces", "agent_interfaces", "joined_interfaces", "unmatched"}
_STORAGE_SCOPE_KEYS = {
    "node",
    "storage",
    "content_type",
    "state",
    "last_attempted_at",
    "evidence_observed_at",
    "omitted_error_count",
    "errors",
    "items",
}
_STORAGE_ITEM_KEYS = {"volid", "content", "format", "size_bytes"}
# Closed set of accepted storage-content scope types (matches nodeutils' collector).
STORAGE_CONTENT_TYPES = {"vztmpl", "iso"}
_NAME_SOURCES = {"proxmox_cluster_name", "standalone_node_fallback"}


class ProxmoxIngestError(RuntimeError):
    pass


@dataclass
class ProxmoxValidationResult:
    """Result of validating one report's ``facts.proxmox`` subtree.

    ``valid=False`` means the *entire* Proxmox subtree is rejected (unsupported/missing schema
    version, unknown envelope/cluster key, invalid/naive/beyond-skew timestamp, or unclassifiable
    cluster identity) — Section 5.3's "invalid shared platform identity" rule: no virtualization
    writes for that report. ``valid=True`` with a non-empty ``errors`` list means individual
    guest/storage items were isolated and the platform state is ``partial``.
    """

    valid: bool
    errors: list[dict[str, str]] = field(default_factory=list)
    state: str = "complete"
    observed_at: datetime | None = None
    cluster: dict[str, Any] | None = None
    qemu_vms: list[dict[str, Any]] = field(default_factory=list)
    lxc_containers: list[dict[str, Any]] = field(default_factory=list)
    storage_content: list[dict[str, Any]] = field(default_factory=list)


def _error(scope_kind: str, scope_id: str, section: str, code: str) -> dict[str, str]:
    return {"scope_kind": scope_kind, "scope_id": scope_id, "section": section, "code": code}


def _rejected(code: str, scope_id: str = "platform") -> ProxmoxValidationResult:
    return ProxmoxValidationResult(valid=False, errors=[_error("platform", scope_id, "envelope", code)])


def parse_aware_utc_timestamp(value: Any) -> datetime:
    """Parse an ISO-8601 timestamp; reject naive values; normalize to UTC."""
    if not isinstance(value, str) or not value:
        raise ProxmoxIngestError("timestamp must be a non-empty ISO-8601 string")
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ProxmoxIngestError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ProxmoxIngestError(f"naive timestamp is rejected: {value!r}")
    return parsed.astimezone(timezone.utc)


def _validate_guest(
    guest: dict[str, Any], guest_type: str, allowed_keys: set[str]
) -> tuple[bool, dict[str, str] | None]:
    node = guest.get("node")
    vmid = guest.get("vmid")
    scope_id = f"{guest_type}:{node}:{vmid}"
    if not isinstance(guest, dict) or set(guest) - allowed_keys:
        return False, _error("guest", scope_id, "identity", "unknown_key")
    if guest.get("guest_type") != guest_type:
        return False, _error("guest", scope_id, "identity", "guest_type_mismatch")
    if not isinstance(vmid, int) or vmid <= 0:
        return False, _error("guest", scope_id, "identity", "invalid_vmid")
    if not node or not isinstance(node, str):
        return False, _error("guest", scope_id, "identity", "invalid_node")
    interfaces = guest.get("interfaces")
    if not isinstance(interfaces, dict) or set(interfaces) - _INTERFACES_KEYS:
        return False, _error("guest", scope_id, "interfaces", "unknown_key")
    if guest_type == "lxc" and "rootfs" in guest:
        rootfs = guest["rootfs"]
        if not isinstance(rootfs, dict) or set(rootfs) - {"storage", "volume", "size_gb"}:
            return False, _error("guest", scope_id, "rootfs", "malformed_rootfs")
    return True, None


def _validate_storage_scope(scope: dict[str, Any]) -> tuple[bool, dict[str, str] | None]:
    node = scope.get("node")
    storage = scope.get("storage")
    scope_id = f"{node}:{storage}:{scope.get('content_type')}"
    if not isinstance(scope, dict) or set(scope) - _STORAGE_SCOPE_KEYS:
        return False, _error("storage", scope_id, "storage_inventory", "unknown_key")
    if scope.get("content_type") not in STORAGE_CONTENT_TYPES:
        return False, _error("storage", scope_id, "storage_inventory", "invalid_content_type")
    items = scope.get("items")
    if not isinstance(items, list):
        return False, _error("storage", scope_id, "storage_inventory", "malformed_storage_item")
    for item in items:
        if not isinstance(item, dict) or set(item) - _STORAGE_ITEM_KEYS or not item.get("volid"):
            return False, _error("storage", scope_id, "storage_inventory", "malformed_storage_item")
    return True, None


def validate_proxmox_facts(
    facts: Any,
    *,
    received_at: datetime,
    max_future_skew_seconds: int = DEFAULT_MAX_FUTURE_SKEW_SECONDS,
) -> ProxmoxValidationResult:
    """Validate one report's decoded ``facts.proxmox`` mapping.

    ``received_at`` must already be an aware UTC datetime (the ingest Job's receipt time).
    """
    if not isinstance(facts, dict):
        return _rejected("missing_schema_version")

    schema_version = facts.get("schema_version")
    if schema_version != PROXMOX_SCHEMA_VERSION:
        return _rejected("unsupported_schema_version")

    unknown_envelope_keys = set(facts) - _ENVELOPE_KEYS
    if unknown_envelope_keys:
        return _rejected("unknown_top_level_key")

    try:
        observed_at = parse_aware_utc_timestamp(facts.get("observed_at"))
    except ProxmoxIngestError:
        return _rejected("invalid_or_naive_timestamp")

    skew_seconds = (observed_at - received_at).total_seconds()
    if skew_seconds > max_future_skew_seconds:
        return _rejected("future_skew_exceeded")

    cluster = facts.get("cluster")
    if not isinstance(cluster, dict) or set(cluster) - _CLUSTER_KEYS:
        return _rejected("invalid_cluster_identity")
    if cluster.get("name_source") not in _NAME_SOURCES:
        return _rejected("invalid_cluster_identity")
    if not cluster.get("name") or not cluster.get("identity_value"):
        return _rejected("invalid_cluster_identity")

    errors: list[dict[str, str]] = []
    state = "complete"

    valid_qemu = []
    for guest in facts.get("qemu_vms", []) or []:
        ok, err = _validate_guest(guest, "qemu", _QEMU_KEYS)
        if ok:
            valid_qemu.append(guest)
        else:
            if err:
                errors.append(err)
            state = "partial"

    valid_lxc = []
    for guest in facts.get("lxc_containers", []) or []:
        ok, err = _validate_guest(guest, "lxc", _LXC_KEYS)
        if ok:
            valid_lxc.append(guest)
        else:
            if err:
                errors.append(err)
            state = "partial"

    valid_storage = []
    for scope in facts.get("storage_content", []) or []:
        ok, err = _validate_storage_scope(scope)
        if ok:
            valid_storage.append(scope)
        else:
            if err:
                errors.append(err)
            state = "partial"

    collection = facts.get("collection")
    if isinstance(collection, dict) and collection.get("state") == "partial":
        state = "partial"

    return ProxmoxValidationResult(
        valid=True,
        errors=errors,
        state=state,
        observed_at=observed_at,
        cluster=cluster,
        qemu_vms=valid_qemu,
        lxc_containers=valid_lxc,
        storage_content=valid_storage,
    )
