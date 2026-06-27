"""Pure deterministic drift evaluation for service placement review.

These helpers compare the desired convergence target (persisted nintent
``DesiredService`` rows plus their active ``DesiredServicePlacement`` rows)
against observed Device facts.  They are intentionally free of Django and
Nautobot imports so the drift logic can be unit-tested over plain data, and they
never mutate their inputs: a missing observation annotates a placement with
drift, it never removes the placement from the desired membership.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

# Single normalization of the observed nodeutils ``facts.system`` value into the
# host_os enum, used here only to detect desired/actual OS drift.  It mirrors the
# authoritative production exporter mapping; this review never exports host_os.
_OBSERVED_SYSTEM_MAP = {"Linux": "linux", "Darwin": "macos"}

# Separate, explicit drift codes so each disagreement is reported on its own and
# is never silently folded into another.
DRIFT_MISSING_SERVICE = "missing_service"
DRIFT_STALE_OBSERVATION = "stale_observation"
DRIFT_INSUFFICIENT_ACTUAL_FACTS = "insufficient_actual_facts"
DRIFT_OS_MISMATCH = "os_mismatch"
LOCATION_WRONG_NODE = "wrong_node"

RUNNING_STATES = {"running", "active"}


def clean_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def normalize_observed_os(value: Any) -> str | None:
    """Normalize an observed nodeutils system string into the host_os enum."""

    text = clean_str(value)
    if text is None:
        return None
    return _OBSERVED_SYSTEM_MAP.get(text)


def _observed_service_entry(device_facts: dict[str, Any], observed_key: str) -> dict[str, Any] | None:
    observed = device_facts.get("observed_services")
    if not isinstance(observed, dict):
        return None
    entry = observed.get(observed_key)
    return entry if isinstance(entry, dict) else None


def evaluate_active_placement(
    placement: dict[str, Any],
    observed_key: str,
    devices: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Evaluate one active placement and report each drift category separately.

    An absent observation never drops the placement: it is always returned as a
    desired member, only annotated with ``missing_service`` drift.  Inventory
    membership remains the desired convergence target.
    """

    device_name = placement.get("realized_device")
    policy = placement.get("actual_state_policy")
    report: dict[str, Any] = {
        "instance_name": placement.get("instance_name"),
        "instance_role": placement.get("instance_role"),
        "desired_node": placement.get("node_slug"),
        "realized_device": device_name,
        "actual_state_policy": policy,
        "observed_state": None,
        "drift": [],
        "details": {},
    }
    drift: list[str] = []
    details: dict[str, Any] = report["details"]

    # Declared nodes (such as HAOS) intentionally carry no nodeutils observation,
    # so observation-based drift does not apply and absence is not "missing".
    if policy == "declared":
        details["note"] = "declared node; nodeutils observation not expected"
        details["declared_host_os"] = placement.get("declared_host_os")
        report["drift"] = drift
        return report

    facts = devices.get(device_name) if device_name else None
    if facts is None:
        drift.append(DRIFT_INSUFFICIENT_ACTUAL_FACTS)
        details["reason"] = "no_realized_device" if not device_name else "device_facts_unavailable"
        report["drift"] = sorted(drift)
        return report

    if facts.get("is_stale"):
        drift.append(DRIFT_STALE_OBSERVATION)
        details["service_inventory_age_hours"] = facts.get("service_inventory_age_hours")

    normalized_os = normalize_observed_os(facts.get("observed_system"))
    expected_os = placement.get("expected_host_os")
    if normalized_os is None:
        drift.append(DRIFT_INSUFFICIENT_ACTUAL_FACTS)
        details["missing_fact"] = "observed_system"
    elif expected_os and normalized_os != expected_os:
        drift.append(DRIFT_OS_MISMATCH)
        details["expected_host_os"] = expected_os
        details["observed_host_os"] = normalized_os

    entry = _observed_service_entry(facts, observed_key)
    if entry is None:
        drift.append(DRIFT_MISSING_SERVICE)
    else:
        state = entry.get("state")
        report["observed_state"] = state
        details["observed_source"] = entry.get("source")
        details["observed_endpoint"] = entry.get("endpoint")
        if str(state or "").lower() not in RUNNING_STATES:
            drift.append(DRIFT_MISSING_SERVICE)
            details["observed_state"] = state

    report["drift"] = sorted(set(drift))
    return report


def evaluate_placement_drift(
    services: list[dict[str, Any]],
    placements: list[dict[str, Any]],
    devices: dict[str, dict[str, Any]],
    device_node_map: dict[str, str],
) -> dict[str, Any]:
    """Deterministically compare desired services + active placements to facts.

    Returns a per-service drift report.  This is a pure function over plain data
    so it can be tested without a database.  It never mutates inputs and never
    removes a placement because an observation is missing.
    """

    placements_by_service: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for placement in placements:
        placements_by_service[str(placement.get("service_key"))].append(placement)

    report: dict[str, Any] = {}
    for service in sorted(services, key=lambda item: str(item.get("key"))):
        key = str(service.get("key"))
        observed_key = service.get("observed_key") or service.get("name")
        service_placements = sorted(
            placements_by_service.get(key, []),
            key=lambda item: str(item.get("instance_name")),
        )

        target_devices = {
            placement.get("realized_device")
            for placement in service_placements
            if placement.get("realized_device")
        }

        placement_reports = [
            evaluate_active_placement(placement, observed_key, devices)
            for placement in service_placements
        ]

        # A service observed running on a node that is not an active placement
        # target is reported as a separate wrong-node location, not as desired
        # membership.
        unexpected_locations: list[dict[str, Any]] = []
        for device_name in sorted(devices):
            if device_name in target_devices:
                continue
            entry = _observed_service_entry(devices[device_name], observed_key)
            if entry is None:
                continue
            unexpected_locations.append(
                {
                    "device": device_name,
                    "node": device_node_map.get(device_name),
                    "state": entry.get("state"),
                    "drift": LOCATION_WRONG_NODE,
                }
            )

        if not service_placements:
            status = "no_active_placement"
        elif any(item["drift"] for item in placement_reports) or unexpected_locations:
            status = "drift"
        else:
            status = "satisfied"

        report[key] = {
            "service": {
                "name": service.get("name"),
                "display_name": service.get("display_name"),
                "lifecycle": service.get("lifecycle"),
                "service_type": service.get("service_type"),
                "intent_source": service.get("intent_source"),
                "catalog_namespace": service.get("catalog_namespace"),
                "catalog_metadata_name": service.get("catalog_metadata_name"),
            },
            "status": status,
            "active_placement_count": len(service_placements),
            "placements": placement_reports,
            "unexpected_locations": unexpected_locations,
        }
    return report
