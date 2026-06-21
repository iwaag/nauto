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

from nautobot.apps.jobs import BooleanVar, IntegerVar, Job, StringVar

DEFAULT_POLICY_FILE = "seed/nodeutils_ingest.yaml"
DEFAULT_MAX_REPORT_BYTES = 2 * 1024 * 1024


class IngestError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReportInput:
    source: str
    text: str


def get_model(*labels: str):
    for label in labels:
        try:
            return apps.get_model(label)
        except LookupError:
            continue
    raise LookupError(f"None of these Nautobot models exist: {', '.join(labels)}")


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

    report_path = StringVar(
        default="",
        required=False,
        description="Path to one report file or a directory of .json/.yaml reports on the Nautobot server.",
    )
    report_text = StringVar(
        default="",
        required=False,
        description="Optional pasted JSON/YAML report for manual testing.",
    )
    policy_file = StringVar(
        default=DEFAULT_POLICY_FILE,
        description="Path to nodeutils ingest policy YAML, relative to this repository root when not absolute.",
    )
    dry_run = BooleanVar(default=True, description="Log planned changes without writing to Nautobot.")
    max_report_age_hours = IntegerVar(default=72, description="Reject reports older than this many hours.")
    max_report_bytes = IntegerVar(default=DEFAULT_MAX_REPORT_BYTES, description="Reject reports larger than this size.")

    class Meta:
        name = "Ingest Nodeutils Inventory"
        description = "Ingest nodeutils inventory reports with server-side policy."
        has_sensitive_variables = False

    def run(
        self,
        report_path: str,
        report_text: str,
        policy_file: str,
        dry_run: bool,
        max_report_age_hours: int,
        max_report_bytes: int,
    ) -> None:
        policy = self.load_policy(policy_file)
        inputs = self.load_inputs(report_path, report_text, max_report_bytes)
        if not inputs:
            raise IngestError("provide report_path, report_text, or both")

        self.dry_run = dry_run
        with transaction.atomic():
            for item in inputs:
                try:
                    report = self.parse_report(item, max_report_bytes)
                    self.validate_report(report, policy, max_report_age_hours)
                    self.ingest_report(report, policy, item.source)
                except IngestError as exc:
                    self.logger.warning("Skipping %s: %s", item.source, exc)

            if dry_run:
                transaction.set_rollback(True)
                self.logger.warning("Dry run complete; no changes were committed.")

    def load_policy(self, policy_file: str) -> dict[str, Any]:
        path = Path(policy_file)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[1] / path
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise IngestError("policy root must be a mapping")
        return data

    def load_inputs(self, report_path: str, report_text: str, max_report_bytes: int) -> list[ReportInput]:
        inputs = []
        if report_text.strip():
            inputs.append(ReportInput("pasted report_text", report_text))

        if report_path.strip():
            path = Path(report_path)
            if path.is_dir():
                for child in sorted(path.iterdir()):
                    if child.suffix.lower() not in {".json", ".yaml", ".yml"} or not child.is_file():
                        continue
                    inputs.append(self.read_report_file(child, max_report_bytes))
            else:
                inputs.append(self.read_report_file(path, max_report_bytes))
        return inputs

    def read_report_file(self, path: Path, max_report_bytes: int) -> ReportInput:
        size = path.stat().st_size
        if size > max_report_bytes:
            raise IngestError(f"{path} is too large: {size} bytes > {max_report_bytes} bytes")
        return ReportInput(str(path), path.read_text(encoding="utf-8"))

    def parse_report(self, item: ReportInput, max_report_bytes: int) -> dict[str, Any]:
        size = len(item.text.encode("utf-8"))
        if size > max_report_bytes:
            raise IngestError(f"report is too large: {size} bytes > {max_report_bytes} bytes")
        try:
            loaded = yaml.safe_load(item.text)
        except yaml.YAMLError as exc:
            raise IngestError(f"failed to parse report: {exc}") from exc
        if not isinstance(loaded, dict):
            raise IngestError("report root must be a mapping")
        return loaded

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

    def ingest_report(self, report: dict[str, Any], policy: dict[str, Any], source: str) -> None:
        identity = report["identity"]
        facts = report["facts"]
        device = self.match_device(identity)
        defaults = policy.get("defaults") if isinstance(policy.get("defaults"), dict) else {}
        allow_create = defaults.get("allow_create", True)
        allow_update = defaults.get("allow_update", True)

        action = "create" if device is None else "update"
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
        if self.dry_run:
            return

        if device is None:
            device = self.create_device(payload)
            self.logger.info("Created Device %s from %s", device.name, source)
        elif changes:
            self.update_device(device, payload)
            self.logger.info("Updated Device %s from %s", device.name, source)
        else:
            self.logger.info("No Device changes needed for %s", device.name)

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
        self_reported = report["self_reported"]
        allowed = policy.get("allowed_self_reported") if isinstance(policy.get("allowed_self_reported"), dict) else {}
        cpu = facts.get("cpu") if isinstance(facts.get("cpu"), dict) else {}
        memory = facts.get("memory") if isinstance(facts.get("memory"), dict) else {}
        disk = facts.get("disk") if isinstance(facts.get("disk"), dict) else {}
        network = facts.get("network") if isinstance(facts.get("network"), dict) else {}
        gpu = facts.get("gpu") if isinstance(facts.get("gpu"), dict) else {}
        services = facts.get("services") if isinstance(facts.get("services"), dict) else {}
        docker = services.get("docker") if isinstance(services.get("docker"), dict) else {}

        custom_fields = {
            "last_seen": report.get("collected_at"),
            "os_name": facts.get("os_name"),
            "os_version": facts.get("os_version"),
            "kernel_version": facts.get("kernel_version"),
            "architecture": facts.get("architecture"),
            "cpu_model": cpu.get("model"),
            "cpu_cores": cpu.get("logical_cores"),
            "memory_gb": str(memory["total_gb"]) if memory.get("total_gb") is not None else None,
            "gpu_count": gpu.get("count"),
            "gpu_models": gpu.get("models"),
            "gpu_memory_gb": str(gpu["memory_gb"]) if gpu.get("memory_gb") is not None else None,
            "gpu_accelerator_summary": gpu.get("accelerator_summary"),
            "disk_total_gb": str(disk["root_total_gb"]) if disk.get("root_total_gb") is not None else None,
            "serial_number": identity.get("serial_number"),
            "primary_mac_address": network.get("primary_mac_address"),
            "primary_ip_address": network.get("primary_ip_address"),
            "inventory_source": "nodeutils",
            "ai_resource_summary": self.make_ai_resource_summary(report),
            "service_roles": ", ".join(list_value(self_reported.get("service_roles")))
            if allowed.get("service_roles")
            else None,
            "preferred_services": self_reported.get("preferred_services") if allowed.get("preferred_services") else None,
            "observed_services": services.get("observed_services"),
            "docker_engine_state": docker.get("engine_state"),
            "docker_container_running_count": docker.get("container_running_count"),
            "docker_container_total_count": docker.get("container_total_count"),
            "docker_compose_projects": ", ".join(docker.get("compose_projects") or []),
            "docker_published_ports": ", ".join(docker.get("published_ports") or []),
            "docker_service_summary": self.make_docker_service_summary(services),
            "service_inventory_updated_at": docker.get("updated_at"),
            "inventory_raw_json": {
                "identity": identity,
                "facts": {
                    "hardware": facts.get("hardware"),
                    "gpu": gpu,
                    "disk": disk,
                    "network": network,
                    "software": facts.get("software"),
                    "services": services,
                },
            },
        }
        if allowed.get("owner"):
            custom_fields["owner"] = self_reported.get("owner")
        if allowed.get("purpose"):
            custom_fields["purpose"] = self_reported.get("purpose")
        return compact(custom_fields)

    def make_ai_resource_summary(self, report: dict[str, Any]) -> str:
        facts = report["facts"]
        identity = report["identity"]
        self_reported = report["self_reported"]
        cpu = facts.get("cpu") if isinstance(facts.get("cpu"), dict) else {}
        memory = facts.get("memory") if isinstance(facts.get("memory"), dict) else {}
        disk = facts.get("disk") if isinstance(facts.get("disk"), dict) else {}
        network = facts.get("network") if isinstance(facts.get("network"), dict) else {}
        gpu = facts.get("gpu") if isinstance(facts.get("gpu"), dict) else {}
        services = facts.get("services") if isinstance(facts.get("services"), dict) else {}

        fields = {
            "host": identity.get("hostname"),
            "os": f"{facts.get('os_name')} {facts.get('os_version')}".strip(),
            "arch": facts.get("architecture"),
            "cpu": cpu.get("model"),
            "cores": cpu.get("logical_cores"),
            "memory_gb": memory.get("total_gb"),
            "gpu": gpu.get("accelerator_summary"),
            "disk_gb": disk.get("root_total_gb"),
            "purpose": self_reported.get("purpose"),
            "ip": network.get("primary_ip_address"),
            "services": ",".join(list_value(self_reported.get("service_roles"))),
            "observed": ",".join(sorted((services.get("observed_services") or {}).keys()))
            if isinstance(services.get("observed_services"), dict)
            else None,
            "docker": self.make_docker_service_summary(services),
        }
        return "; ".join(f"{key}={value}" for key, value in fields.items() if value not in (None, ""))

    def make_docker_service_summary(self, services: dict[str, Any]) -> str | None:
        docker = services.get("docker") if isinstance(services.get("docker"), dict) else {}
        if not docker:
            return None
        important = docker.get("important_services") if isinstance(docker.get("important_services"), list) else []
        service_bits = []
        for item in important:
            if not isinstance(item, dict):
                continue
            name = item.get("service") or item.get("name")
            state = item.get("state")
            ports = ",".join(item.get("ports") or [])
            bit = str(name)
            if state:
                bit = f"{bit}:{state}"
            if ports:
                bit = f"{bit}@{ports}"
            service_bits.append(bit)
        fields = {
            "engine": docker.get("engine_state"),
            "containers": f"{docker.get('container_running_count')}/{docker.get('container_total_count')}"
            if docker.get("container_running_count") is not None and docker.get("container_total_count") is not None
            else None,
            "compose": ",".join(docker.get("compose_projects") or []),
            "ports": ",".join(docker.get("published_ports") or []),
            "important": ",".join(service_bits),
        }
        return "; ".join(f"{key}={value}" for key, value in fields.items() if value not in (None, ""))

    def diff_device(self, device: Any, payload: dict[str, Any]) -> list[str]:
        changed = []
        for key in ("name", "location", "status", "role", "device_type", "serial", "description", "comments"):
            if key in payload and getattr(device, key, None) != payload[key]:
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
