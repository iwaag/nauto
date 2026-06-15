"""Review cluster-level desired service placement against Device facts."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml
from django.apps import apps
from django.core.exceptions import FieldDoesNotExist
from nautobot.apps.jobs import BooleanVar, IntegerVar, Job, StringVar

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


def _list_value(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item not in (None, "")]
    return [str(value)]


def _float_value(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_value(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
    """Review desired cluster services and recommend candidate Device placement."""

    desired_services_file = StringVar(
        default="seed/desired_services.yaml",
        description="Path to desired services YAML, relative to the repository root when not absolute.",
    )
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
        description="Build facts and log deterministic status without calling the LLM.",
    )

    class Meta:
        name = "Service Placement Review"
        description = "Review cluster-level desired services against self-registered Device facts."
        has_sensitive_variables = False

    def run(
        self,
        desired_services_file: str,
        self_registered_only: bool,
        stale_after_hours: int,
        dry_run: bool,
    ) -> None:
        desired_services = self.load_desired_services(desired_services_file)
        devices = self.load_devices(self_registered_only)
        facts = self.build_facts(desired_services, devices, stale_after_hours)

        self.logger.info(
            "Built service placement facts: desired_services=%s devices=%s",
            len(desired_services),
            len(devices),
        )
        self.logger.info("Deterministic service status: %s", json.dumps(facts["deterministic_status"], sort_keys=True))

        if dry_run:
            self.logger.warning("Dry run complete; skipping LLM request.")
            self.logger.info("Prompt facts preview: %s", _truncate_for_log(json.dumps(facts, sort_keys=True), 4000))
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
        self.logger.info("Service placement review JSON: %s", _truncate_for_log(review_json, MAX_LOG_REVIEW_LENGTH))

    def load_desired_services(self, desired_services_file: str) -> list[dict[str, Any]]:
        path = Path(desired_services_file)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[1] / path
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        services = data.get("desired_services", [])
        if not isinstance(services, list):
            raise ValueError("desired_services must be a list")
        normalized = []
        for item in services:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            normalized.append(_plain_value(item))
        return normalized

    def load_devices(self, self_registered_only: bool) -> list[Any]:
        Device = get_model("dcim.Device")
        queryset = Device.objects.all()
        if self_registered_only and has_field(Device, "tags"):
            try:
                queryset = queryset.filter(tags__slug=SELF_REGISTERED_TAG).distinct()
            except Exception:
                self.logger.warning("Could not filter Devices by tag; including all Devices.")
        return list(queryset)

    def build_facts(
        self,
        desired_services: list[dict[str, Any]],
        devices: list[Any],
        stale_after_hours: int,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        device_facts = [self.build_device_facts(device, now, stale_after_hours) for device in devices]
        deterministic_status = self.build_deterministic_status(desired_services, device_facts)
        return {
            "generated_at": now.isoformat(),
            "stale_after_hours": stale_after_hours,
            "desired_services": desired_services,
            "devices": device_facts,
            "deterministic_status": deterministic_status,
        }

    def build_device_facts(self, device: Any, now: datetime, stale_after_hours: int) -> dict[str, Any]:
        cf_data = _custom_field_data(device)
        role = getattr(device, "role", None) or getattr(device, "device_role", None)
        tags = []
        tag_manager = getattr(device, "tags", None)
        if tag_manager is not None:
            try:
                tags = sorted(str(tag) for tag in tag_manager.all())
            except Exception:
                tags = []

        last_seen_age_hours = _age_hours(cf_data.get("last_seen"), now)
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
            "last_seen": cf_data.get("last_seen"),
            "last_seen_age_hours": last_seen_age_hours,
            "service_inventory_updated_at": cf_data.get("service_inventory_updated_at"),
            "service_inventory_age_hours": service_age_hours,
            "is_stale": service_age_hours is None or service_age_hours > stale_after_hours,
            "agent_task_state": cf_data.get("agent_task_state"),
            "cpu_cores": _int_value(cf_data.get("cpu_cores")),
            "memory_gb": _float_value(cf_data.get("memory_gb")),
            "gpu_count": _int_value(cf_data.get("gpu_count")),
            "gpu_models": cf_data.get("gpu_models"),
            "gpu_memory_gb": _float_value(cf_data.get("gpu_memory_gb")),
            "service_roles": _list_value(cf_data.get("service_roles")),
            "preferred_services": _plain_value(cf_data.get("preferred_services") or {}),
            "observed_services": _plain_value(observed_services),
            "docker_engine_state": cf_data.get("docker_engine_state"),
            "docker_container_running_count": _int_value(cf_data.get("docker_container_running_count")),
            "docker_service_summary": cf_data.get("docker_service_summary"),
            "ai_resource_summary": cf_data.get("ai_resource_summary"),
            "ai_resource_review": cf_data.get("ai_resource_review"),
        }

    def build_deterministic_status(
        self,
        desired_services: list[dict[str, Any]],
        device_facts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        status: dict[str, Any] = {}
        for service in desired_services:
            name = str(service.get("name"))
            observed_instances = []
            candidates = []
            min_memory = _float_value(service.get("min_memory_gb"))
            prefers_gpu = bool(service.get("prefers_gpu"))
            min_instances = int(service.get("min_instances") or 1)

            for device in device_facts:
                observed = device.get("observed_services")
                if not isinstance(observed, dict):
                    observed = {}
                preferred = device.get("preferred_services")
                if not isinstance(preferred, dict):
                    preferred = {}
                observed_service = observed.get(name) if isinstance(observed.get(name), dict) else None
                preferred_service = preferred.get(name) if isinstance(preferred.get(name), dict) else None
                has_memory = min_memory is None or (
                    isinstance(device.get("memory_gb"), (int, float)) and float(device["memory_gb"]) >= min_memory
                )
                has_gpu = not prefers_gpu or bool(device.get("gpu_count"))

                if observed_service:
                    observed_instances.append(
                        {
                            "device": device.get("device_name"),
                            "state": observed_service.get("state"),
                            "source": observed_service.get("source"),
                            "endpoint": observed_service.get("endpoint"),
                            "is_stale": device.get("is_stale"),
                        }
                    )

                candidate_score = 0
                candidate_reasons = []
                if observed_service:
                    candidate_score += 50
                    candidate_reasons.append("already_observed")
                if preferred_service:
                    candidate_score += 20
                    candidate_reasons.append("host_preferred")
                if has_memory:
                    candidate_score += 10
                    candidate_reasons.append("meets_min_memory")
                if has_gpu:
                    candidate_score += 10
                    candidate_reasons.append("meets_gpu_preference")
                if not device.get("is_stale"):
                    candidate_score += 10
                    candidate_reasons.append("recent_service_inventory")

                candidates.append(
                    {
                        "device": device.get("device_name"),
                        "score": candidate_score,
                        "reasons": candidate_reasons,
                        "meets_min_memory": has_memory,
                        "has_gpu_when_preferred": has_gpu,
                        "already_running": bool(observed_service),
                        "has_preferred_endpoint": bool(preferred_service),
                        "recently_seen": not device.get("is_stale"),
                    }
                )

            running_instances = [
                item for item in observed_instances if str(item.get("state", "")).lower() in {"running", "active"}
            ]
            if len(running_instances) >= min_instances:
                service_status = "satisfied"
            elif observed_instances:
                service_status = "under_replicated"
            else:
                service_status = "missing"

            status[name] = {
                "status": service_status,
                "required": bool(service.get("required")),
                "min_instances": min_instances,
                "observed_count": len(observed_instances),
                "running_count": len(running_instances),
                "observed_instances": observed_instances,
                "top_candidates": sorted(candidates, key=lambda item: item["score"], reverse=True)[:5],
            }
        return status

    def build_prompt(self, facts: dict[str, Any]) -> str:
        facts_text = json.dumps(facts, sort_keys=True, indent=2, ensure_ascii=True)
        return (
            "You are reviewing service placement for an automation home cluster.\n\n"
            "Return JSON only. Do not include Markdown.\n"
            "The JSON must have this shape:\n"
            "{"
            '"generated_at": string, '
            '"services": {'
            '"service_name": {'
            '"status": "satisfied|under_replicated|over_replicated|missing|stale|conflicting|unknown", '
            '"observed_instances": [{"device": string, "endpoint": string|null, "state": string|null}], '
            '"recommended_primary": string|null, '
            '"recommended_fallbacks": [string], '
            '"cautions": [string], '
            '"confidence": "low|medium|high"'
            "}}}\n\n"
            "Rules:\n"
            "- Use only the provided facts.\n"
            "- Desired services are cluster-level intent, not Device facts.\n"
            "- Observed services are recent Device self-reports, not live capacity guarantees.\n"
            "- Do not invent endpoints, Devices, or availability.\n"
            "- Treat stale Device reports as caution or unknown.\n"
            "- Prefer existing observed services when placement policy says prefer_existing.\n"
            "- Do not recommend starting new instances when allow_start_new is false.\n"
            "- Mention monitoring checks when live load or health matters.\n\n"
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
