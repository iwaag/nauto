"""Generate an LLM review for nodeutils-managed Device resources."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from nautobot.apps.jobs import JobHookReceiver
from nautobot.extras.choices import ObjectChangeActionChoices

REVIEW_FIELD = "ai_resource_review"
REVIEW_UPDATED_AT_FIELD = "ai_resource_review_updated_at"
REVIEW_MODEL_FIELD = "ai_resource_review_model"
REVIEW_SOURCE_HASH_FIELD = "ai_resource_review_source_hash"

INPUT_CUSTOM_FIELDS = (
    "agent_task_state",
    "ai_resource_summary",
    "os_name",
    "os_version",
    "architecture",
    "cpu_model",
    "cpu_cores",
    "memory_gb",
    "gpu_count",
    "gpu_models",
    "gpu_memory_gb",
    "gpu_accelerator_summary",
    "disk_total_gb",
    "last_seen",
    "purpose",
    "observed_services",
    "docker_engine_state",
    "docker_container_running_count",
    "docker_container_total_count",
    "docker_compose_projects",
    "docker_published_ports",
    "docker_service_summary",
    "service_inventory_updated_at",
)

MAX_REVIEW_LENGTH = 2000
MAX_LOG_PREVIEW_LENGTH = 500


@dataclass(frozen=True)
class LLMReviewResult:
    model: str
    review: str
    metadata: dict[str, Any]


def _plain_value(value: Any) -> Any:
    """Return a compact JSON-safe representation for prompt facts."""
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


def _set_custom_field(device: Any, key: str, value: Any) -> None:
    if hasattr(device, "cf"):
        device.cf[key] = value
        return

    data = getattr(device, "custom_field_data", None)
    if isinstance(data, dict):
        data[key] = value
        return

    raise AttributeError("Device object does not expose writable custom field data")


def _validated_save(obj: Any) -> None:
    if hasattr(obj, "validated_save"):
        obj.validated_save()
    else:
        obj.full_clean()
        obj.save()


def _truncate_for_log(value: Any, max_length: int = MAX_LOG_PREVIEW_LENGTH) -> str:
    text = str(value or "")
    if len(text) <= max_length:
        return text
    return text[: max_length - 3].rstrip() + "..."


def _response_metadata(body: dict[str, Any]) -> dict[str, Any]:
    review = body.get("response")
    thinking = body.get("thinking")
    metadata: dict[str, Any] = {
        "response_length": len(review) if isinstance(review, str) else None,
        "thinking_length": len(thinking) if isinstance(thinking, str) else None,
        "done": body.get("done"),
        "done_reason": body.get("done_reason"),
        "error": body.get("error"),
        "model": body.get("model"),
        "created_at": body.get("created_at"),
        "total_duration": body.get("total_duration"),
        "load_duration": body.get("load_duration"),
        "prompt_eval_count": body.get("prompt_eval_count"),
        "prompt_eval_duration": body.get("prompt_eval_duration"),
        "eval_count": body.get("eval_count"),
        "eval_duration": body.get("eval_duration"),
        "top_level_keys": sorted(str(key) for key in body),
    }
    return {key: value for key, value in metadata.items() if value is not None}


class AIResourceReview(JobHookReceiver):
    """Create a concise agent-facing review for a Device resource."""

    class Meta:
        name = "AI Resource Review"
        description = "Generate an LLM review of a Device's suitability for automated agent workloads."
        has_sensitive_variables = False

    def receive_job_hook(self, change, action, changed_object) -> None:
        if action == ObjectChangeActionChoices.ACTION_DELETE:
            self.logger.info("Skipping delete event.")
            return

        if changed_object is None or getattr(changed_object._meta, "label_lower", "") != "dcim.device":
            self.logger.info("Skipping non-Device object change.")
            return

        facts = self.build_facts(changed_object)
        source_json = json.dumps(facts, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        source_hash = hashlib.sha256(source_json.encode("utf-8")).hexdigest()

        cf_data = _custom_field_data(changed_object)
        if cf_data.get(REVIEW_SOURCE_HASH_FIELD) == source_hash:
            self.logger.info("Skipping %s; review source facts are unchanged.", changed_object.name)
            return

        prompt = self.build_prompt(facts)
        try:
            result = self.generate_review(prompt)
        except RuntimeError as exc:
            self.logger.warning("Could not generate AI resource review for %s: %s", changed_object.name, exc)
            return

        if not result.review.strip():
            self.logger.warning(
                "LLM returned an empty review for %s; leaving Device unchanged. response_metadata=%s",
                changed_object.name,
                json.dumps(result.metadata, sort_keys=True, ensure_ascii=True),
            )
            return

        review = result.review.strip()
        if len(review) > MAX_REVIEW_LENGTH:
            review = review[: MAX_REVIEW_LENGTH - 3].rstrip() + "..."

        _set_custom_field(changed_object, REVIEW_FIELD, review)
        _set_custom_field(changed_object, REVIEW_UPDATED_AT_FIELD, datetime.now(timezone.utc).isoformat())
        _set_custom_field(changed_object, REVIEW_MODEL_FIELD, result.model)
        _set_custom_field(changed_object, REVIEW_SOURCE_HASH_FIELD, source_hash)
        _validated_save(changed_object)

        self.logger.info(
            "Updated AI resource review for %s using %s. response_metadata=%s",
            changed_object.name,
            result.model,
            json.dumps(result.metadata, sort_keys=True, ensure_ascii=True),
        )

    def build_facts(self, device) -> dict[str, Any]:
        cf_data = _custom_field_data(device)
        role = getattr(device, "role", None) or getattr(device, "device_role", None)

        tags = []
        tag_manager = getattr(device, "tags", None)
        if tag_manager is not None:
            try:
                tags = sorted(str(tag) for tag in tag_manager.all())
            except Exception:  # pragma: no cover - defensive for Nautobot object variants
                tags = []

        facts: dict[str, Any] = {
            "device_name": getattr(device, "name", None),
            "role": _object_name(role),
            "location": _object_name(getattr(device, "location", None)),
            "status": _object_name(getattr(device, "status", None)),
            "tags": tags,
        }
        for key in INPUT_CUSTOM_FIELDS:
            facts[key] = _plain_value(cf_data.get(key))
        return facts

    def build_prompt(self, facts: dict[str, Any]) -> str:
        facts_text = json.dumps(facts, sort_keys=True, indent=2, ensure_ascii=True)
        return (
            "You are reviewing a computer resource for an automation scheduler.\n\n"
            "Return a concise review in 3 short lines:\n"
            "1. capability: summarize compute capacity and OS suitability\n"
            "2. best_for: list suitable task types\n"
            "3. cautions: mention limitations or stale data only if relevant\n\n"
            "Use only the provided facts. Do not invent availability. "
            "You may mention agent_task_state and preferred service placement, but do not infer idleness "
            "or live service capacity from hardware specs or Docker inventory.\n\n"
            f"Facts:\n{facts_text}\n"
        )

    def generate_review(self, prompt: str) -> LLMReviewResult:
        url = os.environ.get("AI_RESOURCE_REVIEW_URL", "http://localhost:11434/api/generate")
        model = os.environ.get("AI_RESOURCE_REVIEW_MODEL", "llama3.1:8b")
        timeout_raw = os.environ.get("AI_RESOURCE_REVIEW_TIMEOUT", "30")
        log_prompt = os.environ.get("AI_RESOURCE_REVIEW_LOG_PROMPT", "").lower() in {"1", "true", "yes", "on"}

        try:
            timeout = float(timeout_raw)
        except ValueError as exc:
            raise RuntimeError(f"invalid AI_RESOURCE_REVIEW_TIMEOUT={timeout_raw!r}") from exc

        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "options": {
                "temperature": 0.2,
                "num_predict": 220,
            },
        }
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        self.logger.info(
            "Requesting AI resource review: url=%s model=%s timeout=%s think=false prompt_chars=%s prompt_sha256=%s",
            url,
            model,
            timeout,
            len(prompt),
            prompt_hash,
        )
        if log_prompt:
            self.logger.info("AI resource review prompt preview: %s", _truncate_for_log(prompt, 4000))

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

        review = body.get("response")
        if not isinstance(review, str):
            metadata = _response_metadata(body)
            raise RuntimeError(
                "LLM endpoint JSON did not contain a string response field: "
                + json.dumps(metadata, sort_keys=True, ensure_ascii=True)
            )

        metadata = _response_metadata(body)
        metadata["response_preview"] = _truncate_for_log(review)
        return LLMReviewResult(model=model, review=review, metadata=metadata)
