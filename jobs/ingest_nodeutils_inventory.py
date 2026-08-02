"""Ingest nodeutils inventory reports into Nautobot Devices."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from django.apps import apps
from django.core.exceptions import FieldDoesNotExist
from django.db import transaction

from nautobot.apps.jobs import IntegerVar, Job, StringVar

from . import proxmox_upsert
from .nodeutils_ingest_batch import IngestError, ReportInput, load_report_batch, parse_report_content
from .nodeutils_ingest_summary import build_ingest_summary
from .proxmox_ingest import validate_proxmox_facts

DEFAULT_POLICY_FILE = "seed/nodeutils_ingest.yaml"
DEFAULT_MAX_REPORT_BYTES = 2 * 1024 * 1024


@dataclass
class IpLookupResult:
    """Result of resolving an IPAddress by (Namespace, host) — sidefix2/plan.md Section 3.2.

    ``status`` is one of "found", "not_found", "ambiguous". Duck-typed rather than imported
    from ``proxmox_interfaces`` so this Django-facing module has no import-order dependency on
    that pure module's own loading mechanism (``_load_proxmox_interfaces()``).
    """

    status: str
    ip: Any | None = None


def get_model(*labels: str):
    for label in labels:
        try:
            return apps.get_model(label)
        except LookupError:
            continue
    raise LookupError(f"None of these Nautobot models exist: {', '.join(labels)}")


def _model_exists(label: str) -> bool:
    try:
        apps.get_model(label)
    except LookupError:
        return False
    return True


def has_field(model: Any, field_name: str) -> bool:
    try:
        model._meta.get_field(field_name)
    except FieldDoesNotExist:
        return False
    return True


def object_name(obj: Any) -> str | None:
    if obj is None:
        return None
    return getattr(obj, "name", None) or getattr(obj, "model", None) or str(obj)


def validated_save(obj: Any) -> None:
    if hasattr(obj, "validated_save"):
        obj.validated_save()
    else:
        obj.full_clean()
        obj.save()


def custom_field_data(obj: Any) -> dict[str, Any]:
    data = dict(getattr(obj, "custom_field_data", {}) or {})
    if data:
        return data
    if hasattr(obj, "cf"):
        return dict(obj.cf or {})
    return {}


def set_custom_field(obj: Any, key: str, value: Any) -> None:
    if hasattr(obj, "cf"):
        obj.cf[key] = value
        return
    data = getattr(obj, "custom_field_data", None)
    if isinstance(data, dict):
        data[key] = value
        return
    raise AttributeError("object does not expose writable custom field data")


def compact(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value not in (None, "", [], {})}


def list_value(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list | tuple | set):
        return [str(item) for item in value if item not in (None, "")]
    return [str(value)]


def parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise IngestError("collected_at must be a non-empty ISO timestamp")
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise IngestError(f"collected_at is not parseable: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class IngestNodeutilsInventory(Job):
    """Validate nodeutils reports and create/update Nautobot Devices."""

    report_batch = StringVar(
        default="",
        description="JSON/YAML batch payload with a top-level reports list. Each entry needs source and text.",
    )
    policy_file = StringVar(
        default=DEFAULT_POLICY_FILE,
        description="Path to nodeutils ingest policy YAML, relative to this repository root when not absolute.",
    )
    max_report_age_hours = IntegerVar(default=72, description="Reject reports older than this many hours.")
    max_report_bytes = IntegerVar(default=DEFAULT_MAX_REPORT_BYTES, description="Reject reports larger than this size.")

    class Meta:
        name = "Ingest Nodeutils Inventory"
        description = "Ingest nodeutils inventory reports with server-side policy."
        has_sensitive_variables = False

    def run(
        self,
        report_batch: str,
        policy_file: str,
        max_report_age_hours: int,
        max_report_bytes: int,
    ) -> None:
        policy = self.load_policy(policy_file)
        inputs = self.load_inputs(report_batch)

        results: list[dict[str, Any]] = []
        with transaction.atomic():
            for item in inputs:
                try:
                    report = self.parse_report(item, max_report_bytes)
                    self.validate_report(report, policy, max_report_age_hours)
                    results.append(self.ingest_report(report, policy, item.source))
                except IngestError as exc:
                    self.logger.warning("Skipping %s: %s", item.source, exc)
                    results.append(
                        {
                            "source": item.source,
                            "outcome": "skipped",
                            "changed_fields": [],
                            "error": str(exc),
                        }
                    )

            summary_payload = build_ingest_summary(results)
            counts = summary_payload["summary"]
            self.logger.info(
                "Batch summary: total=%s created=%s updated=%s unchanged=%s skipped=%s",
                counts["total"],
                counts["created"],
                counts["updated"],
                counts["unchanged"],
                counts["skipped"],
            )

        self.create_file(
            "nodeutils-ingest-summary.json",
            json.dumps(summary_payload, sort_keys=True, indent=2, ensure_ascii=True) + "\n",
        )

    def load_policy(self, policy_file: str) -> dict[str, Any]:
        path = Path(policy_file)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[1] / path
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise IngestError("policy root must be a mapping")
        return data

    def load_inputs(self, report_batch: str) -> list[ReportInput]:
        return load_report_batch(report_batch)

    def parse_report(self, item: ReportInput, max_report_bytes: int) -> dict[str, Any]:
        return parse_report_content(item, max_report_bytes)

    def validate_report(self, report: dict[str, Any], policy: dict[str, Any], max_report_age_hours: int) -> None:
        required = {"schema_version", "collector", "identity", "collected_at", "facts", "self_reported"}
        missing = sorted(required - set(report))
        if missing:
            raise IngestError("missing required top-level keys: " + ", ".join(missing))
        supported_versions = set(list_value(policy.get("schema_versions")))
        if report.get("schema_version") not in supported_versions:
            raise IngestError(f"unsupported schema_version: {report.get('schema_version')}")
        if not isinstance(report.get("identity"), dict):
            raise IngestError("identity must be a mapping")
        if not isinstance(report.get("facts"), dict):
            raise IngestError("facts must be a mapping")
        if not isinstance(report.get("self_reported"), dict):
            raise IngestError("self_reported must be a mapping")

        collected_at = parse_timestamp(report.get("collected_at"))
        if collected_at < datetime.now(timezone.utc) - timedelta(hours=max_report_age_hours):
            raise IngestError(f"report is stale: collected_at={collected_at.isoformat()}")

    def ingest_report(self, report: dict[str, Any], policy: dict[str, Any], source: str) -> dict[str, Any]:
        """Run one normal Device (+ Proxmox, when present) persistence path."""
        identity = report["identity"]
        facts = report["facts"]
        device = self.match_device(identity)
        device_is_new = device is None
        defaults = policy.get("defaults") if isinstance(policy.get("defaults"), dict) else {}
        allow_create = defaults.get("allow_create", True)
        allow_update = defaults.get("allow_update", True)

        action = "create" if device_is_new else "update"
        if action == "create" and not allow_create:
            raise IngestError("policy does not allow creating new Devices")
        if action == "update" and not allow_update:
            raise IngestError("policy does not allow updating existing Devices")

        resolved = self.resolve_policy_objects(policy, facts)
        payload = self.build_device_payload(report, policy, resolved)
        changes = self.diff_device(device, payload) if device is not None else sorted(payload)
        report_hash = hashlib.sha256(json.dumps(report, sort_keys=True, default=str).encode("utf-8")).hexdigest()

        self.logger.info(
            "%s: matched_device=%s action=%s report_hash=%s changed_fields=%s",
            source,
            getattr(device, "name", None),
            action,
            report_hash,
            ", ".join(changes) if changes else "none",
        )
        outcome = "created" if device_is_new else "updated" if changes else "unchanged"
        result = {
            "source": source,
            "outcome": outcome,
            "device": getattr(device, "name", None) or identity.get("hostname") or identity.get("fqdn"),
            "changed_fields": changes,
            "report_hash": report_hash,
        }

        if device_is_new:
            device = self.create_device(payload)
            self.logger.info("Created Device %s from %s", device.name, source)
        elif changes:
            self.update_device(device, payload)
            self.logger.info("Updated Device %s from %s", device.name, source)
        else:
            self.logger.info("No Device changes needed for %s", device.name)
        result["device"] = device.name

        proxmox_facts = facts.get("proxmox")
        if isinstance(proxmox_facts, dict):
            if device_is_new:
                # Section 4.4: a rolled-back preview's Device UUID is not apply-stable, so its
                # derived Proxmox standalone-fallback scope key would not be either. Report a
                # truthful two-stage precondition instead of an unstable Cluster scope; the
                # operator applies the Device first, then reruns preview with the persisted UUID.
                result["proxmox"] = self.build_new_device_proxmox_precondition(proxmox_facts, source)
            else:
                result["proxmox"] = self.ingest_proxmox(proxmox_facts, device, policy, source)
        return result

    def build_new_device_proxmox_precondition(self, proxmox_facts: dict[str, Any], source: str) -> dict[str, Any]:
        """Section 4.4 two-stage precondition for a not-yet-persisted observer Device.

        Uses the pure ``validate_proxmox_facts`` only to surface a bounded, informational
        ``cluster_name``/``identity_source`` for operator review; it never derives or claims a
        stable ``scope_key`` or performs any Cluster/VM/VMInterface/IP write for this report.
        """
        received_at = datetime.now(timezone.utc)
        validation = validate_proxmox_facts(proxmox_facts, received_at=received_at)
        cluster_info = validation.cluster if validation.valid and validation.cluster else {}
        self.logger.warning(
            "%s: observer Device is not yet persisted; Proxmox ingest deferred until after Device apply.",
            source,
        )
        return {
            "identity_source": cluster_info.get("name_source"),
            "scope_key": None,
            "cluster_name": cluster_info.get("name"),
            "cluster_id": None,
            "observation_state": "partial",
            "object_counts": {
                kind: {a: 0 for a in ("created", "updated", "unchanged", "skipped")}
                for kind in ("cluster", "vm", "vminterface", "ip")
            },
            "changed_fields": {},
            "guest_errors": [
                {
                    "scope_kind": "platform",
                    "scope_id": "cluster",
                    "section": "cluster_identity",
                    "code": "observer_device_not_persisted",
                }
            ],
        }

    def ingest_proxmox(
        self, proxmox_facts: dict[str, Any], device: Any, policy: dict[str, Any], source: str
    ) -> dict[str, Any]:
        """Validate and upsert one report's ``facts.proxmox`` subtree (plan.md Section 5.5).

        Wraps ``proxmox_upsert.ingest_proxmox_platform`` with real Nautobot managers/lookups
        and a real per-guest ``transaction.atomic()`` savepoint. A malformed report or invalid
        shared platform identity produces no virtualization writes for this report (validation
        happens before any Cluster/VM object is touched); a single bad guest rolls back only
        that guest inside its own savepoint and marks the platform observation ``partial``.
        """
        proxmox_policy = policy.get("proxmox") if isinstance(policy.get("proxmox"), dict) else {}
        max_skew = int(proxmox_policy.get("max_future_skew_seconds", 300))
        received_at = datetime.now(timezone.utc)

        validation = validate_proxmox_facts(proxmox_facts, received_at=received_at, max_future_skew_seconds=max_skew)
        if not validation.valid:
            self.logger.warning("%s: Proxmox facts rejected: %s", source, validation.errors)
            return {
                "identity_source": None,
                "scope_key": None,
                "cluster_name": None,
                "cluster_id": None,
                "observation_state": "partial",
                "object_counts": {kind: {a: 0 for a in ("created", "updated", "unchanged", "skipped")} for kind in ("cluster", "vm", "vminterface", "ip")},
                "changed_fields": {},
                "guest_errors": validation.errors,
            }

        Cluster = get_model("virtualization.Cluster")
        ClusterType = get_model("virtualization.ClusterType")
        VirtualMachine = get_model("virtualization.VirtualMachine")
        Role = get_model("extras.Role")

        cluster_type = self.lookup_name_or_slug(ClusterType, "Proxmox VE")
        if cluster_type is None:
            self.logger.warning("%s: Proxmox VE ClusterType is not seeded; skipping virtualization writes.", source)
            return {
                "identity_source": validation.cluster.get("name_source") if validation.cluster else None,
                "scope_key": None,
                "cluster_name": validation.cluster.get("name") if validation.cluster else None,
                "cluster_id": None,
                "observation_state": "partial",
                "object_counts": {kind: {a: 0 for a in ("created", "updated", "unchanged", "skipped")} for kind in ("cluster", "vm", "vminterface", "ip")},
                "changed_fields": {},
                "guest_errors": [
                    {"scope_kind": "platform", "scope_id": "cluster", "section": "cluster_identity", "code": "missing_seeded_prerequisite"}
                ],
            }

        observer_device_id = str(device.pk) if device is not None and getattr(device, "pk", None) else None

        VMInterface = get_model("virtualization.VMInterface")
        IPAddress = get_model("ipam.IPAddress")
        Namespace = get_model("ipam.Namespace")
        Prefix = get_model("ipam.Prefix")
        # Nautobot's IP-to-interface relation is a through model exposing mutually exclusive
        # ``interface``/``vm_interface`` fields (report2.0.md Step 0 live introspection). We
        # resolve it lazily here rather than at module import time, since it is only reachable
        # once the Django app registry is populated.
        IPAddressToInterface = get_model(
            "ipam.IPAddressToInterface", "ipam.IPAddressAssignment"
        ) if _model_exists("ipam.IPAddressToInterface") or _model_exists("ipam.IPAddressAssignment") else None

        # sidefix2/plan.md Section 3.1: exactly one Namespace named "Global" is the intended
        # scope for every Proxmox-observed IP. Zero or multiple matches is a shared IPAM
        # prerequisite failure, not permission to select an arbitrary Namespace.
        global_namespace_matches = list(Namespace.objects.filter(name="Global"))
        if len(global_namespace_matches) != 1:
            self.logger.warning(
                "%s: Nautobot Namespace 'Global' is not uniquely resolvable (%d matches); skipping virtualization writes.",
                source, len(global_namespace_matches),
            )
            return {
                "identity_source": validation.cluster.get("name_source") if validation.cluster else None,
                "scope_key": None,
                "cluster_name": validation.cluster.get("name") if validation.cluster else None,
                "cluster_id": None,
                "observation_state": "partial",
                "object_counts": {kind: {a: 0 for a in ("created", "updated", "unchanged", "skipped")} for kind in ("cluster", "vm", "vminterface", "ip")},
                "changed_fields": {},
                "guest_errors": [
                    {"scope_kind": "platform", "scope_id": "namespace", "section": "cluster_identity", "code": "namespace_ambiguous"}
                ],
            }
        global_namespace = global_namespace_matches[0]

        def resolve_host(host: str) -> IpLookupResult:
            # sidefix2/plan.md Section 3.2: the real uniqueness key is (Namespace, host), never
            # (host, mask_length), and ``.first()`` must never mask a corrupt-data ambiguity.
            matches = list(IPAddress.objects.filter(host=host, parent__namespace=global_namespace))
            if not matches:
                return IpLookupResult(status="not_found")
            if len(matches) > 1:
                return IpLookupResult(status="ambiguous")
            return IpLookupResult(status="found", ip=matches[0])

        def find_parent_prefix(host: str) -> Any | None:
            # sidefix2/plan.md Section 3.3: mirrors IPAddress._get_closest_parent() exactly —
            # a missing parent Prefix is real ledger data, never invented here.
            try:
                return Prefix.objects.filter(namespace=global_namespace).get_closest_parent(host, include_self=True)
            except Prefix.DoesNotExist:
                return None

        def create_ip(address: str, prefix: int, parent_prefix: Any) -> Any:
            status = self.lookup_status("Active")
            ip = IPAddress(address=f"{address}/{prefix}", status=status, parent=parent_prefix)
            validated_save(ip)
            return ip

        def find_ip_by_id(ip_id: str | None) -> Any | None:
            if not ip_id:
                return None
            try:
                return IPAddress.objects.filter(pk=ip_id).first()
            except (ValueError, TypeError):
                return None

        def ip_related_elsewhere(ip_obj: Any, interface: Any) -> bool:
            if IPAddressToInterface is None:
                return False
            for assignment in IPAddressToInterface.objects.filter(ip_address=ip_obj):
                vm_interface = getattr(assignment, "vm_interface", None)
                if vm_interface is not None and vm_interface != interface:
                    return True
            return False

        def attach_ip(interface: Any, ip_obj: Any) -> None:
            if IPAddressToInterface is not None:
                IPAddressToInterface.objects.get_or_create(vm_interface=interface, ip_address=ip_obj)
            elif hasattr(interface, "ip_addresses"):
                interface.ip_addresses.add(ip_obj)

        def detach_ip(interface: Any, ip_obj: Any) -> None:
            if IPAddressToInterface is not None:
                IPAddressToInterface.objects.filter(vm_interface=interface, ip_address=ip_obj).delete()
            elif hasattr(interface, "ip_addresses"):
                interface.ip_addresses.remove(ip_obj)

        result = proxmox_upsert.ingest_proxmox_platform(
            validation=validation,
            cluster_manager=Cluster.objects,
            vm_manager=VirtualMachine.objects,
            cluster_type=cluster_type,
            make_cluster=lambda: Cluster(cluster_type=cluster_type),
            make_vm=lambda cluster: VirtualMachine(cluster=cluster),
            status_lookup=self.lookup_status,
            role_lookup=lambda name: self.lookup_name_or_slug(Role, name),
            observer_device_id=observer_device_id,
            save_fn=validated_save,
            guest_atomic=transaction.atomic,
            vminterface_manager=VMInterface.objects,
            # VMInterface.status is a required native field with no dedicated proxmox_* mapping
            # in plan.md Section 5.4; a fixed "Active" status (seeded for the
            # virtualization.vminterface content type) satisfies the model constraint without
            # claiming any Proxmox-observed evidence about interface operational state.
            make_interface=lambda: VMInterface(status=self.lookup_status("Active")),
            resolve_host=resolve_host,
            find_parent_prefix=find_parent_prefix,
            create_ip=create_ip,
            find_ip_by_id=find_ip_by_id,
            ip_related_elsewhere=ip_related_elsewhere,
            attach_ip=attach_ip,
            detach_ip=detach_ip,
            sanitize_created_ids=False,
        )
        self.logger.info(
            "%s: Proxmox cluster=%s scope_key=%s state=%s counts=%s",
            source,
            result["cluster_name"],
            result["scope_key"],
            result["observation_state"],
            result["object_counts"],
        )
        return result

    def match_device(self, identity: dict[str, Any]) -> Any | None:
        Device = get_model("dcim.Device")
        serial = identity.get("serial_number")
        if serial:
            found = Device.objects.filter(serial=str(serial)).first()
            if found:
                return found

        for name in (identity.get("fqdn"), identity.get("hostname")):
            if name:
                found = Device.objects.filter(name=str(name)).first()
                if found:
                    return found
        return None

    def resolve_policy_objects(self, policy: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
        defaults = policy.get("defaults") if isinstance(policy.get("defaults"), dict) else {}
        system = str(facts.get("system") or "")
        hardware = facts.get("hardware") if isinstance(facts.get("hardware"), dict) else {}

        location_name = str(defaults.get("location") or "")
        status_name = str(defaults.get("status") or "")
        role_name = str((policy.get("roles_by_system") or {}).get(system) or defaults.get("role") or "")
        device_type_name = str(
            (policy.get("device_types_by_system") or {}).get(system) or defaults.get("device_type") or ""
        )
        manufacturer_name = str(
            (policy.get("manufacturers_by_hardware") or {}).get(hardware.get("manufacturer"))
            or defaults.get("manufacturer")
            or hardware.get("manufacturer")
            or "Generic"
        )

        refs = {
            "location": self.lookup_name_or_slug(get_model("dcim.Location"), location_name),
            "status": self.lookup_status(status_name),
            "role": self.lookup_name_or_slug(get_model("extras.Role"), role_name),
            "manufacturer": self.lookup_name_or_slug(get_model("dcim.Manufacturer"), manufacturer_name),
            "device_type": self.lookup_device_type(device_type_name),
            "tags": [self.lookup_name_or_slug(get_model("extras.Tag"), str(tag)) for tag in defaults.get("tags", [])],
        }
        missing = [name for name, value in refs.items() if name != "tags" and value is None]
        if any(tag is None for tag in refs["tags"]):
            missing.append("tags")
        if missing:
            raise IngestError("missing Nautobot objects from policy: " + ", ".join(sorted(set(missing))))
        return refs

    def lookup_name_or_slug(self, model: Any, value: str) -> Any | None:
        if not value:
            return None
        for field in ("name", "slug"):
            if has_field(model, field):
                found = model.objects.filter(**{field: value}).first()
                if found:
                    return found
        return None

    def lookup_status(self, value: str) -> Any | None:
        Status = get_model("extras.Status")
        for field in ("name", "label", "slug"):
            if has_field(Status, field):
                found = Status.objects.filter(**{field: value}).first()
                if found:
                    return found
        return None

    def lookup_device_type(self, value: str) -> Any | None:
        DeviceType = get_model("dcim.DeviceType")
        for field in ("model", "slug"):
            if has_field(DeviceType, field):
                found = DeviceType.objects.filter(**{field: value}).first()
                if found:
                    return found
        return None

    def build_device_payload(
        self,
        report: dict[str, Any],
        policy: dict[str, Any],
        refs: dict[str, Any],
    ) -> dict[str, Any]:
        identity = report["identity"]
        facts = report["facts"]
        self_reported = report["self_reported"]
        hardware = facts.get("hardware") if isinstance(facts.get("hardware"), dict) else {}

        description = None
        allowed = policy.get("allowed_self_reported") if isinstance(policy.get("allowed_self_reported"), dict) else {}
        if allowed.get("description"):
            description = self_reported.get("description")
        if not description:
            description = f"{facts.get('os_name', '')} {facts.get('os_version', '')}".strip()

        payload = {
            "name": str(identity.get("hostname") or identity.get("fqdn")),
            "location": refs["location"],
            "status": refs["status"],
            "role": refs["role"],
            "device_type": refs["device_type"],
            "serial": identity.get("serial_number") or "",
            "description": description,
            "comments": "Managed by nauto nodeutils ingest.",
            "tags": refs["tags"],
            "custom_fields": self.build_custom_fields(report, policy),
        }
        manufacturer = refs.get("manufacturer")
        if manufacturer and hardware.get("manufacturer") and object_name(manufacturer) != hardware.get("manufacturer"):
            payload["comments"] += f" Hardware manufacturer reported as {hardware.get('manufacturer')}."
        return compact(payload)

    def build_custom_fields(self, report: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
        facts = report["facts"]
        identity = report["identity"]
        network = facts.get("network") if isinstance(facts.get("network"), dict) else {}
        primary_interface = network.get("primary_interface") if isinstance(network.get("primary_interface"), dict) else {}
        services = facts.get("services") if isinstance(facts.get("services"), dict) else {}
        docker = services.get("docker") if isinstance(services.get("docker"), dict) else {}
        workspaces = facts.get("workspaces") if isinstance(facts.get("workspaces"), dict) else {}

        custom_fields = {
            "last_seen": report.get("collected_at"),
            # Raw nodeutils platform.system() value; the production exporter
            # normalizes it to the host_os enum in one place. Persisted source,
            # not a desired value.
            "host_system": facts.get("system"),
            "primary_mac_address": network.get("primary_mac_address"),
            "primary_ip_address": network.get("primary_ip_address"),
            # Explicit primary interface name so the production exporter never
            # has to inspect the unrestricted inventory_raw_json blob for it.
            "network_interface": primary_interface.get("name"),
            "inventory_source": "nodeutils",
            "observed_services": services.get("observed_services"),
            "observed_workspaces": workspaces,
            "service_inventory_updated_at": docker.get("updated_at"),
            "inventory_raw_json": {
                "identity": identity,
                "facts": facts,
            },
        }
        return compact(custom_fields)

    def diff_device(self, device: Any, payload: dict[str, Any]) -> list[str]:
        changed = []
        for key in ("name", "location", "status", "role", "device_type", "serial", "description", "comments"):
            # `description` is retained in the portable report payload, but
            # Nautobot 3's Device model has no such native field. It is
            # therefore omitted by create/update and must not make an
            # identical report appear to mutate on every repeat.
            if key in payload and has_field(type(device), key) and getattr(device, key, None) != payload[key]:
                changed.append(key)
        current_cf = custom_field_data(device)
        for key, value in payload.get("custom_fields", {}).items():
            if current_cf.get(key) != value:
                changed.append(f"custom_fields.{key}")
        return changed

    def create_device(self, payload: dict[str, Any]) -> Any:
        Device = get_model("dcim.Device")
        tags = payload.pop("tags", [])
        custom_fields = payload.pop("custom_fields", {})
        device = Device(**{key: value for key, value in payload.items() if has_field(Device, key)})
        for key, value in custom_fields.items():
            set_custom_field(device, key, value)
        validated_save(device)
        if tags and hasattr(device, "tags"):
            device.tags.set(tags)
        return device

    def update_device(self, device: Any, payload: dict[str, Any]) -> None:
        tags = payload.get("tags", [])
        for key, value in payload.items():
            if key in {"tags", "custom_fields"}:
                continue
            if has_field(type(device), key):
                setattr(device, key, value)
        for key, value in payload.get("custom_fields", {}).items():
            set_custom_field(device, key, value)
        validated_save(device)
        if tags and hasattr(device, "tags"):
            device.tags.set(tags)
