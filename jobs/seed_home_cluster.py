"""Seed prerequisite Nautobot objects for home inventory ingest.

Install this repository as a Nautobot Git Jobs repository, or copy this file
under JOBS_ROOT with the sibling seed/ directory. The YAML file is the source
of truth; this Job applies it idempotently with get-or-create/update behavior.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import FieldDoesNotExist
from django.db import transaction

from nautobot.apps.jobs import BooleanVar, Job, StringVar

try:
    from nautobot_intent_catalog.models import DesiredService, IntentSource
except ImportError:  # pragma: no cover
    DesiredService = None  # type: ignore[assignment,misc]
    IntentSource = None  # type: ignore[assignment,misc]


def slugify(value: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "item"


def get_model(*labels: str):
    for label in labels:
        try:
            return apps.get_model(label)
        except LookupError:
            continue
    raise LookupError(f"None of these Nautobot models exist: {', '.join(labels)}")


def has_field(model, field_name: str) -> bool:
    try:
        model._meta.get_field(field_name)
    except FieldDoesNotExist:
        return False
    return True


def content_type(value: str) -> ContentType:
    app_label, model = value.split(".", 1)
    return ContentType.objects.get(app_label=app_label, model=model)


def validated_save(obj: Any) -> None:
    if hasattr(obj, "validated_save"):
        obj.validated_save()
    else:
        obj.full_clean()
        obj.save()


class SeedHomeCluster(Job):
    """Create or update the base objects required by host inventory ingest."""

    seed_file = StringVar(
        default="seed/home_cluster.yaml",
        description="Path to the seed YAML, relative to the repository root when not absolute.",
    )
    dry_run = BooleanVar(default=True, description="Log planned changes without writing to Nautobot.")
    update_existing = BooleanVar(default=True, description="Update existing objects when seed values differ.")

    class Meta:
        name = "Seed Home Cluster"
        description = "Create/update Location, Role, Status, Device Type, Tag, and Custom Field data from YAML."
        has_sensitive_variables = False

    def run(self, seed_file: str, dry_run: bool, update_existing: bool) -> None:
        seed_path = Path(seed_file)
        if not seed_path.is_absolute():
            seed_path = Path(__file__).resolve().parents[1] / seed_path
        data = yaml.safe_load(seed_path.read_text(encoding="utf-8")) or {}

        self.dry_run = dry_run
        self.update_existing = update_existing

        with transaction.atomic():
            statuses = self.ensure_statuses(data.get("statuses", []))
            location_types = self.ensure_location_types(data.get("location_types", []))
            self.ensure_locations(data.get("locations", []), location_types, statuses)
            self.ensure_roles(data.get("roles", []))
            manufacturers = self.ensure_manufacturers(data.get("manufacturers", []))
            self.ensure_device_types(data.get("device_types", []), manufacturers)
            self.ensure_tags(data.get("tags", []))
            self.ensure_custom_fields(data.get("custom_fields", []))
            intent_sources = self.ensure_intent_sources(data.get("intent_sources", []))
            self.ensure_desired_services(data.get("desired_services", []), intent_sources)

            if dry_run:
                transaction.set_rollback(True)
                self.logger.warning("Dry run complete; no changes were committed.")

    def ensure_object(
        self,
        model,
        kind: str,
        lookup: dict[str, Any],
        defaults: dict[str, Any],
        m2m: dict[str, list[str]] | None = None,
    ):
        obj = model.objects.filter(**lookup).first()
        name = next(iter(lookup.values()))
        if obj is None:
            obj = model(**lookup)
            for key, value in defaults.items():
                if has_field(model, key):
                    setattr(obj, key, value)
            if self.dry_run:
                self.logger.info("Would create %s %s", kind, name)
                return obj
            validated_save(obj)
            for field_name, values in (m2m or {}).items():
                if hasattr(obj, field_name):
                    getattr(obj, field_name).set([content_type(value) for value in values])
            self.logger.info("Created %s %s", kind, name)
            return obj

        changed = False
        for key, value in defaults.items():
            if has_field(model, key) and getattr(obj, key) != value:
                setattr(obj, key, value)
                changed = True

        if changed and self.update_existing:
            if self.dry_run:
                self.logger.info("Would update %s %s", kind, name)
            else:
                validated_save(obj)
                self.logger.info("Updated %s %s", kind, name)
        else:
            self.logger.info("Exists %s %s", kind, name)

        if m2m and self.update_existing:
            if self.dry_run:
                self.logger.info("Would update %s relationships for %s %s", ", ".join(m2m), kind, name)
            else:
                for field_name, values in m2m.items():
                    if hasattr(obj, field_name):
                        getattr(obj, field_name).set([content_type(value) for value in values])
        return obj

    def ensure_statuses(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        Status = get_model("extras.Status")
        refs = {}
        for item in items:
            name_value = item["name"]
            obj = self.ensure_object(
                Status,
                "status",
                {"name": name_value},
                {
                    "slug": item.get("slug") or slugify(name_value),
                    "color": item.get("color", "4caf50"),
                    "description": item.get("description", ""),
                },
                {"content_types": item.get("content_types", ["dcim.device"])},
            )
            refs[name_value] = obj
        return refs

    def ensure_location_types(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        refs = {}
        LocationType = get_model("dcim.LocationType")
        for item in items:
            name_value = item["name"]
            obj = self.ensure_object(
                LocationType,
                "location type",
                {"name": name_value},
                {
                    "slug": item.get("slug") or slugify(name_value),
                    "description": item.get("description", ""),
                },
                {"content_types": item.get("content_types", ["dcim.device"])},
            )
            refs[name_value] = obj
        return refs

    def ensure_locations(
        self,
        items: list[dict[str, Any]],
        location_types: dict[str, Any],
        statuses: dict[str, Any],
    ) -> dict[str, Any]:
        refs = {}
        Location = get_model("dcim.Location")
        for item in items:
            name_value = item["name"]
            location_type = location_types.get(item.get("location_type"))
            status = statuses.get(item.get("status"))
            parent = refs.get(item.get("parent"))
            obj = self.ensure_object(
                Location,
                "location",
                {"name": name_value},
                {
                    "slug": item.get("slug") or slugify(name_value),
                    "location_type": location_type,
                    "status": status,
                    "parent": parent,
                    "description": item.get("description", ""),
                },
            )
            refs[name_value] = obj
        return refs

    def ensure_roles(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        Role = get_model("extras.Role")
        refs = {}
        for item in items:
            name_value = item["name"]
            obj = self.ensure_object(
                Role,
                "role",
                {"name": name_value},
                {
                    "slug": item.get("slug") or slugify(name_value),
                    "color": item.get("color", "2196f3"),
                    "description": item.get("description", ""),
                },
                {"content_types": item.get("content_types", ["dcim.device"])},
            )
            refs[name_value] = obj
        return refs

    def ensure_manufacturers(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        Manufacturer = get_model("dcim.Manufacturer")
        refs = {}
        for item in items:
            name_value = item["name"]
            obj = self.ensure_object(
                Manufacturer,
                "manufacturer",
                {"name": name_value},
                {
                    "slug": item.get("slug") or slugify(name_value),
                    "description": item.get("description", ""),
                },
            )
            refs[name_value] = obj
        return refs

    def ensure_device_types(self, items: list[dict[str, Any]], manufacturers: dict[str, Any]) -> dict[str, Any]:
        DeviceType = get_model("dcim.DeviceType")
        refs = {}
        for item in items:
            model_value = item["model"]
            manufacturer = manufacturers[item.get("manufacturer", "Generic")]
            obj = self.ensure_object(
                DeviceType,
                "device type",
                {"model": model_value},
                {
                    "slug": item.get("slug") or slugify(model_value),
                    "manufacturer": manufacturer,
                    "part_number": item.get("part_number", ""),
                    "u_height": item.get("u_height", 0),
                    "is_full_depth": item.get("is_full_depth", False),
                },
            )
            refs[model_value] = obj
        return refs

    def ensure_tags(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        Tag = get_model("extras.Tag")
        refs = {}
        for item in items:
            name_value = item["name"]
            obj = self.ensure_object(
                Tag,
                "tag",
                {"name": name_value},
                {
                    "slug": item.get("slug") or slugify(name_value),
                    "color": item.get("color", "9e9e9e"),
                    "description": item.get("description", ""),
                },
                {"content_types": item.get("content_types", ["dcim.device"])},
            )
            refs[name_value] = obj
        return refs

    def ensure_intent_sources(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        if IntentSource is None:
            self.logger.warning("nautobot_intent_catalog is not installed; skipping intent_sources.")
            return {}
        refs: dict[str, Any] = {}
        for item in items:
            slug = item["slug"]
            obj = IntentSource.objects.filter(slug=slug).first()
            if obj is None:
                obj = IntentSource(
                    slug=slug,
                    name=item.get("name") or slug,
                    source_type=item.get("source_type", "manual"),
                    enabled=item.get("enabled", True),
                )
                if self.dry_run:
                    self.logger.info("Would create IntentSource %s", slug)
                else:
                    obj.full_clean()
                    obj.save()
                    self.logger.info("Created IntentSource %s", slug)
            else:
                self.logger.info("Exists IntentSource %s", slug)
            refs[slug] = obj
        return refs

    def ensure_desired_services(
        self,
        items: list[dict[str, Any]],
        intent_sources: dict[str, Any],
    ) -> None:
        if DesiredService is None:
            self.logger.warning("nautobot_intent_catalog is not installed; skipping desired_services.")
            return
        for item in items:
            source_slug = item["intent_source"]
            intent_source = intent_sources.get(source_slug)
            if intent_source is None:
                self.logger.warning(
                    "desired_services: intent_source %r not found; skipping %s.",
                    source_slug,
                    item.get("catalog_metadata_name"),
                )
                continue
            lookup = {
                "intent_source": intent_source,
                "catalog_namespace": item.get("catalog_namespace", "default"),
                "catalog_metadata_name": item["catalog_metadata_name"],
                "service_type": item.get("service_type", "service"),
            }
            obj = DesiredService.objects.filter(**lookup).first()
            name = item.get("name") or item["catalog_metadata_name"]
            defaults = {
                "name": name,
                "slug": slugify(name),
                "display_name": item.get("display_name") or name,
                "lifecycle": item.get("lifecycle", "active"),
            }
            if obj is None:
                obj = DesiredService(**lookup)
                for key, value in defaults.items():
                    setattr(obj, key, value)
                if self.dry_run:
                    self.logger.info("Would create DesiredService %s", name)
                else:
                    obj.full_clean()
                    obj.save()
                    self.logger.info("Created DesiredService %s", name)
            else:
                changed = False
                if self.update_existing:
                    for key, value in defaults.items():
                        if getattr(obj, key, None) != value:
                            setattr(obj, key, value)
                            changed = True
                if changed:
                    if self.dry_run:
                        self.logger.info("Would update DesiredService %s", name)
                    else:
                        obj.full_clean()
                        obj.save()
                        self.logger.info("Updated DesiredService %s", name)
                else:
                    self.logger.info("Exists DesiredService %s", name)

    def ensure_custom_fields(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        CustomField = get_model("extras.CustomField")
        refs = {}
        for item in items:
            key = item["key"]
            label = item.get("label") or key.replace("_", " ").title()
            defaults = {
                "label": label,
                "type": item.get("type", "text"),
                "description": item.get("description", ""),
                "required": item.get("required", False),
                "weight": item.get("weight", 100),
                "default": item.get("default"),
                "filter_logic": item.get("filter_logic", "loose"),
            }
            obj = self.ensure_object(
                CustomField,
                "custom field",
                {"key": key},
                defaults,
                {"content_types": item.get("content_types", ["dcim.device"])},
            )
            refs[key] = obj
        return refs
