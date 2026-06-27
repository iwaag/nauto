"""Review desired service placement drift against observed Device facts.

Desired service membership is owned by the persisted nintent models
(``DesiredService`` and its active ``DesiredServicePlacement`` rows), not by a
file catalog.  This Job is read-only: it explains drift between the desired
convergence target and the observed reality, and never mutates active
placements.  Acting on a proposal is an explicit, separate operator action.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.apps import apps
from django.core.exceptions import FieldDoesNotExist, ObjectDoesNotExist
from nautobot.apps.jobs import BooleanVar, IntegerVar, Job

from .service_placement_eval import clean_str, evaluate_placement_drift

MAX_LOG_REVIEW_LENGTH = 12000
MAX_LLM_REVIEW_LENGTH = 20000
SELF_REGISTERED_TAG = "self-registered"


@dataclass(frozen=True)
class PlacementReviewResult:
    model: str
    review: dict[str, Any]
    metadata: dict[str, Any]


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


def _plain_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple, set)):
        return [_plain_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain_value(item) for key, item in sorted(value.items())}
    return str(value)


def _object_name(obj: Any) -> str | None:
    if obj is None:
        return None
    return getattr(obj, "name", None) or str(obj)


def _custom_field_data(device: Any) -> dict[str, Any]:
    data = dict(getattr(device, "custom_field_data", {}) or {})
    if data:
        return data
    if hasattr(device, "cf"):
        return dict(device.cf or {})
    return {}


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_hours(value: Any, now: datetime) -> float | None:
    parsed = _parse_time(value)
    if parsed is None:
        return None
    return round((now - parsed).total_seconds() / 3600, 2)


def _truncate_for_log(value: Any, max_length: int = MAX_LOG_REVIEW_LENGTH) -> str:
    text = str(value or "")
    if len(text) <= max_length:
        return text
    return text[: max_length - 3].rstrip() + "..."


def _response_metadata(body: dict[str, Any]) -> dict[str, Any]:
    response = body.get("response")
    metadata: dict[str, Any] = {
        "response_length": len(response) if isinstance(response, str) else None,
        "done": body.get("done"),
        "done_reason": body.get("done_reason"),
        "error": body.get("error"),
        "model": body.get("model"),
        "created_at": body.get("created_at"),
        "total_duration": body.get("total_duration"),
        "load_duration": body.get("load_duration"),
        "prompt_eval_count": body.get("prompt_eval_count"),
        "eval_count": body.get("eval_count"),
        "top_level_keys": sorted(str(key) for key in body),
    }
    return {key: value for key, value in metadata.items() if value is not None}


class ServicePlacementReview(Job):
    """Explain drift between desired service placements and observed Device facts."""

    self_registered_only = BooleanVar(
        default=True,
        description="Only include Devices tagged self-registered when possible.",
    )
    stale_after_hours = IntegerVar(
        default=24,
        description="Mark Device service data as stale after this many hours.",
    )
    dry_run = BooleanVar(
        default=True,
        description="Build facts and log deterministic drift without calling the LLM.",
    )

    class Meta:
        name = "Service Placement Review"
        description = (
            "Explain drift between persisted desired service placements and observed "
            "Device facts. Advisory only; never mutates active placements."
        )
        has_sensitive_variables = False

    def run(
        self,
        self_registered_only: bool,
        stale_after_hours: int,
        dry_run: bool,
    ) -> None:
        loaded = self.load_services_and_placements()
        if loaded is None:
            self.logger.warning(
                "nautobot_intent_catalog is not installed; cannot read desired placements."
            )
            return
        services, placements = loaded

        now = datetime.now(timezone.utc)
        devices = self.load_device_facts(self_registered_only, now, stale_after_hours)
        device_node_map = self.load_device_node_map()
        drift = evaluate_placement_drift(services, placements, devices, device_node_map)

        facts = {
            "generated_at": now.isoformat(),
            "stale_after_hours": stale_after_hours,
            "services": services,
            "active_placements": placements,
            "devices": devices,
            "drift": drift,
        }

        self.logger.info(
            "Evaluated placement drift: services=%s active_placements=%s devices=%s",
            len(services),
            len(placements),
            len(devices),
        )
        self.logger.info("Deterministic placement drift: %s", json.dumps(drift, sort_keys=True))
        self.logger.warning(
            "Placement review is advisory only and never mutates active placements; "
            "operators apply any change explicitly."
        )

        if dry_run:
            self.logger.warning("Dry run complete; skipping LLM request.")
            self.logger.info(
                "Prompt facts preview: %s", _truncate_for_log(json.dumps(facts, sort_keys=True), 4000)
            )
            return

        prompt = self.build_prompt(facts)
        try:
            result = self.generate_review(prompt)
        except RuntimeError as exc:
            self.logger.warning("Could not generate service placement review: %s", exc)
            return

        review_json = json.dumps(result.review, sort_keys=True, ensure_ascii=True)
        self.logger.info(
            "Service placement review generated using %s. response_metadata=%s",
            result.model,
            json.dumps(result.metadata, sort_keys=True, ensure_ascii=True),
        )
        self.logger.info(
            "Service placement review JSON: %s", _truncate_for_log(review_json, MAX_LOG_REVIEW_LENGTH)
        )

    def load_services_and_placements(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
        """Read desired services and active placements from persisted nintent models."""

        try:
            DesiredService = get_model("nautobot_intent_catalog.DesiredService")
            DesiredServicePlacement = get_model("nautobot_intent_catalog.DesiredServicePlacement")
        except LookupError:
            return None

        services: list[dict[str, Any]] = []
        for service in DesiredService.objects.select_related("intent_source").order_by("name"):
            intent_source = getattr(service, "intent_source", None)
            services.append(
                {
                    "key": str(service.pk),
                    "name": service.name,
                    # observed_services is keyed by the service name self-reported
                    # by nodeutils; match on it.
                    "observed_key": service.name,
                    "display_name": service.display_name,
                    "lifecycle": service.lifecycle,
                    "service_type": service.service_type,
                    "intent_source": getattr(intent_source, "slug", None),
                    "catalog_namespace": service.catalog_namespace,
                    "catalog_metadata_name": service.catalog_metadata_name,
                }
            )

        placements: list[dict[str, Any]] = []
        queryset = (
            DesiredServicePlacement.objects.filter(desired_state="active")
            .select_related(
                "desired_node",
                "desired_node__realized_device",
                "desired_node__operational_config",
            )
            .order_by("desired_service__name", "instance_name")
        )
        for placement in queryset:
            node = placement.desired_node
            realized = getattr(node, "realized_device", None)
            try:
                operational = node.operational_config
            except ObjectDoesNotExist:
                operational = None
            placements.append(
                {
                    "service_key": str(placement.desired_service_id),
                    "instance_name": placement.instance_name,
                    "instance_role": placement.instance_role,
                    "node_slug": node.slug,
                    "realized_device": getattr(realized, "name", None),
                    "actual_state_policy": getattr(operational, "actual_state_policy", None),
                    "expected_host_os": getattr(operational, "expected_host_os", None),
                    "declared_host_os": getattr(operational, "declared_host_os", None),
                }
            )

        return services, placements

    def load_device_node_map(self) -> dict[str, str]:
        """Map realized Device name to its DesiredNode slug for wrong-node detection."""

        DesiredNode = get_model("nautobot_intent_catalog.DesiredNode")
        mapping: dict[str, str] = {}
        for node in (
            DesiredNode.objects.exclude(realized_device__isnull=True)
            .select_related("realized_device")
        ):
            device_name = getattr(node.realized_device, "name", None)
            if device_name:
                mapping[device_name] = node.slug
        return mapping

    def load_device_facts(
        self,
        self_registered_only: bool,
        now: datetime,
        stale_after_hours: int,
    ) -> dict[str, dict[str, Any]]:
        Device = get_model("dcim.Device")
        queryset = Device.objects.all()
        if self_registered_only and has_field(Device, "tags"):
            try:
                queryset = queryset.filter(tags__slug=SELF_REGISTERED_TAG).distinct()
            except Exception:
                self.logger.warning("Could not filter Devices by tag; including all Devices.")

        devices: dict[str, dict[str, Any]] = {}
        for device in queryset:
            facts = self.build_device_facts(device, now, stale_after_hours)
            name = facts.get("device_name")
            if name:
                devices[name] = facts
        return devices

    def build_device_facts(self, device: Any, now: datetime, stale_after_hours: int) -> dict[str, Any]:
        cf_data = _custom_field_data(device)
        role = getattr(device, "role", None) or getattr(device, "device_role", None)
        tags: list[str] = []
        tag_manager = getattr(device, "tags", None)
        if tag_manager is not None:
            try:
                tags = sorted(str(tag) for tag in tag_manager.all())
            except Exception:
                tags = []

        service_age_hours = _age_hours(cf_data.get("service_inventory_updated_at"), now)
        observed_services = cf_data.get("observed_services")
        if not isinstance(observed_services, dict):
            observed_services = {}

        return {
            "device_name": getattr(device, "name", None),
            "role": _object_name(role),
            "location": _object_name(getattr(device, "location", None)),
            "status": _object_name(getattr(device, "status", None)),
            "tags": tags,
            "observed_system": clean_str(cf_data.get("host_system")),
            "last_seen": cf_data.get("last_seen"),
            "last_seen_age_hours": _age_hours(cf_data.get("last_seen"), now),
            "service_inventory_updated_at": cf_data.get("service_inventory_updated_at"),
            "service_inventory_age_hours": service_age_hours,
            "is_stale": service_age_hours is None or service_age_hours > stale_after_hours,
            "observed_services": _plain_value(observed_services),
        }

    def build_prompt(self, facts: dict[str, Any]) -> str:
        facts_text = json.dumps(facts, sort_keys=True, indent=2, ensure_ascii=True)
        return (
            "You are reviewing desired service placement drift for an automation home cluster.\n\n"
            "Return JSON only. Do not include Markdown.\n"
            "The JSON must have this shape:\n"
            "{"
            '"generated_at": string, '
            '"services": {'
            '"service_key": {'
            '"status": "satisfied|drift|no_active_placement", '
            '"placements": [{"instance_name": string, "desired_node": string, '
            '"drift": ["missing_service|stale_observation|insufficient_actual_facts|os_mismatch"]}], '
            '"unexpected_locations": [{"device": string, "node": string|null}], '
            '"proposed_actions": [string], '
            '"cautions": [string], '
            '"confidence": "low|medium|high"'
            "}}}\n\n"
            "Rules:\n"
            "- Use only the provided facts.\n"
            "- Active placements in nintent are the authoritative desired convergence target.\n"
            "- A missing or stopped observed service is drift, never a reason to remove a placement.\n"
            "- Observed services are recent Device self-reports, not live capacity guarantees.\n"
            "- Report missing service, wrong node, stale observation, insufficient actual facts, and\n"
            "  desired/actual OS mismatch as distinct drift, never folded together.\n"
            "- Do not invent endpoints, Devices, placements, or availability.\n"
            "- Treat stale Device reports as caution, not as confirmed convergence.\n"
            "- proposed_actions are advisory only; this review never mutates placements.\n\n"
            f"Facts:\n{facts_text}\n"
        )

    def generate_review(self, prompt: str) -> PlacementReviewResult:
        url = (
            os.environ.get("SERVICE_PLACEMENT_REVIEW_URL")
            or os.environ.get("AI_RESOURCE_REVIEW_URL")
            or "http://localhost:11434/api/generate"
        )
        model = (
            os.environ.get("SERVICE_PLACEMENT_REVIEW_MODEL")
            or os.environ.get("AI_RESOURCE_REVIEW_MODEL")
            or "llama3.1:8b"
        )
        timeout_raw = os.environ.get("SERVICE_PLACEMENT_REVIEW_TIMEOUT") or os.environ.get(
            "AI_RESOURCE_REVIEW_TIMEOUT",
            "45",
        )
        log_prompt = os.environ.get("SERVICE_PLACEMENT_REVIEW_LOG_PROMPT", "").lower() in {"1", "true", "yes", "on"}

        try:
            timeout = float(timeout_raw)
        except ValueError as exc:
            raise RuntimeError(f"invalid SERVICE_PLACEMENT_REVIEW_TIMEOUT={timeout_raw!r}") from exc

        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "format": "json",
            "options": {
                "temperature": 0.1,
                "num_predict": 1200,
            },
        }
        self.logger.info(
            "Requesting service placement review: url=%s model=%s timeout=%s prompt_chars=%s",
            url,
            model,
            timeout,
            len(prompt),
        )
        if log_prompt:
            self.logger.info("Service placement prompt preview: %s", _truncate_for_log(prompt, 4000))

        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )

        try:
            with urlopen(request, timeout=timeout) as response:
                raw_body = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LLM endpoint returned HTTP {exc.code}: {body[:200]}") from exc
        except URLError as exc:
            raise RuntimeError(f"LLM endpoint request failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RuntimeError("LLM endpoint request timed out") from exc

        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("LLM endpoint returned invalid JSON") from exc

        response_text = body.get("response")
        if not isinstance(response_text, str):
            metadata = _response_metadata(body)
            raise RuntimeError(
                "LLM endpoint JSON did not contain a string response field: "
                + json.dumps(metadata, sort_keys=True, ensure_ascii=True)
            )
        if len(response_text) > MAX_LLM_REVIEW_LENGTH:
            raise RuntimeError(f"LLM review is too large: {len(response_text)} chars")

        try:
            review = json.loads(response_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError("LLM response field was not valid JSON") from exc
        if not isinstance(review, dict) or not isinstance(review.get("services"), dict):
            raise RuntimeError("LLM review JSON must be an object with a services object")

        metadata = _response_metadata(body)
        metadata["review_service_count"] = len(review.get("services", {}))
        return PlacementReviewResult(model=model, review=review, metadata=metadata)
