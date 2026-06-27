from __future__ import annotations

import copy
import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "jobs" / "service_placement_eval.py"
SPEC = importlib.util.spec_from_file_location("service_placement_eval", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"failed to load {MODULE_PATH}")
service_placement_eval = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = service_placement_eval
SPEC.loader.exec_module(service_placement_eval)

evaluate_placement_drift = service_placement_eval.evaluate_placement_drift
normalize_observed_os = service_placement_eval.normalize_observed_os

DRIFT_MISSING_SERVICE = service_placement_eval.DRIFT_MISSING_SERVICE
DRIFT_STALE_OBSERVATION = service_placement_eval.DRIFT_STALE_OBSERVATION
DRIFT_INSUFFICIENT_ACTUAL_FACTS = service_placement_eval.DRIFT_INSUFFICIENT_ACTUAL_FACTS
DRIFT_OS_MISMATCH = service_placement_eval.DRIFT_OS_MISMATCH
LOCATION_WRONG_NODE = service_placement_eval.LOCATION_WRONG_NODE


def service(key: str = "svc-1", name: str = "ollama", **overrides):
    base = {
        "key": key,
        "name": name,
        "observed_key": name,
        "display_name": name.title(),
        "lifecycle": "active",
        "service_type": "service",
        "intent_source": "infrastructure",
        "catalog_namespace": "default",
        "catalog_metadata_name": name,
    }
    base.update(overrides)
    return base


def placement(
    service_key: str = "svc-1",
    instance_name: str = "primary",
    node_slug: str = "agpc",
    realized_device: str | None = "agpc",
    actual_state_policy: str | None = "required",
    expected_host_os: str | None = "linux",
    declared_host_os: str | None = None,
    instance_role: str | None = "primary",
):
    return {
        "service_key": service_key,
        "instance_name": instance_name,
        "node_slug": node_slug,
        "realized_device": realized_device,
        "actual_state_policy": actual_state_policy,
        "expected_host_os": expected_host_os,
        "declared_host_os": declared_host_os,
        "instance_role": instance_role,
    }


def device(
    observed_system: str | None = "Linux",
    observed_services: dict | None = None,
    is_stale: bool = False,
    service_inventory_age_hours: float | None = 1.0,
):
    return {
        "observed_system": observed_system,
        "observed_services": observed_services if observed_services is not None else {},
        "is_stale": is_stale,
        "service_inventory_age_hours": service_inventory_age_hours,
    }


def running_observation(name: str = "ollama"):
    return {name: {"state": "running", "source": "docker", "endpoint": "http://agpc:11434"}}


class NormalizeObservedOsTest(unittest.TestCase):
    def test_maps_known_systems(self):
        self.assertEqual(normalize_observed_os("Linux"), "linux")
        self.assertEqual(normalize_observed_os(" Darwin "), "macos")

    def test_unknown_or_empty_is_none(self):
        self.assertIsNone(normalize_observed_os("Windows"))
        self.assertIsNone(normalize_observed_os(None))
        self.assertIsNone(normalize_observed_os(""))


class EvaluatePlacementDriftTest(unittest.TestCase):
    def test_satisfied_when_running_and_os_matches(self):
        report = evaluate_placement_drift(
            [service()],
            [placement()],
            {"agpc": device(observed_services=running_observation())},
            {"agpc": "agpc"},
        )
        entry = report["svc-1"]
        self.assertEqual(entry["status"], "satisfied")
        self.assertEqual(entry["active_placement_count"], 1)
        self.assertEqual(entry["placements"][0]["drift"], [])
        self.assertEqual(entry["placements"][0]["observed_state"], "running")
        self.assertEqual(entry["unexpected_locations"], [])

    def test_absent_observation_keeps_placement_as_desired_member(self):
        # Core invariant: a missing observation must NOT remove the desired
        # placement; it is still reported, only annotated with missing_service.
        report = evaluate_placement_drift(
            [service()],
            [placement()],
            {"agpc": device(observed_services={})},
            {"agpc": "agpc"},
        )
        entry = report["svc-1"]
        self.assertEqual(entry["active_placement_count"], 1)
        self.assertEqual(len(entry["placements"]), 1)
        placement_report = entry["placements"][0]
        self.assertEqual(placement_report["desired_node"], "agpc")
        self.assertIn(DRIFT_MISSING_SERVICE, placement_report["drift"])
        self.assertEqual(entry["status"], "drift")

    def test_stopped_service_is_missing(self):
        report = evaluate_placement_drift(
            [service()],
            [placement()],
            {"agpc": device(observed_services={"ollama": {"state": "exited"}})},
            {"agpc": "agpc"},
        )
        placement_report = report["svc-1"]["placements"][0]
        self.assertIn(DRIFT_MISSING_SERVICE, placement_report["drift"])
        self.assertEqual(placement_report["observed_state"], "exited")

    def test_wrong_node_reported_separately(self):
        # Service is placed on agpc but observed running only on agstudio.
        devices = {
            "agpc": device(observed_services={}),
            "agstudio": device(observed_services=running_observation()),
        }
        report = evaluate_placement_drift(
            [service()],
            [placement(realized_device="agpc")],
            devices,
            {"agpc": "agpc", "agstudio": "agstudio"},
        )
        entry = report["svc-1"]
        self.assertIn(DRIFT_MISSING_SERVICE, entry["placements"][0]["drift"])
        self.assertEqual(len(entry["unexpected_locations"]), 1)
        location = entry["unexpected_locations"][0]
        self.assertEqual(location["device"], "agstudio")
        self.assertEqual(location["node"], "agstudio")
        self.assertEqual(location["drift"], LOCATION_WRONG_NODE)

    def test_running_on_target_is_not_wrong_node(self):
        report = evaluate_placement_drift(
            [service()],
            [placement(realized_device="agpc")],
            {"agpc": device(observed_services=running_observation())},
            {"agpc": "agpc"},
        )
        self.assertEqual(report["svc-1"]["unexpected_locations"], [])

    def test_stale_observation(self):
        report = evaluate_placement_drift(
            [service()],
            [placement()],
            {"agpc": device(observed_services=running_observation(), is_stale=True, service_inventory_age_hours=99.0)},
            {"agpc": "agpc"},
        )
        placement_report = report["svc-1"]["placements"][0]
        self.assertIn(DRIFT_STALE_OBSERVATION, placement_report["drift"])
        self.assertEqual(placement_report["details"]["service_inventory_age_hours"], 99.0)

    def test_insufficient_actual_facts_when_no_realized_device(self):
        report = evaluate_placement_drift(
            [service()],
            [placement(realized_device=None)],
            {},
            {},
        )
        placement_report = report["svc-1"]["placements"][0]
        self.assertEqual(placement_report["drift"], [DRIFT_INSUFFICIENT_ACTUAL_FACTS])
        self.assertEqual(placement_report["details"]["reason"], "no_realized_device")

    def test_insufficient_actual_facts_when_observed_system_missing(self):
        report = evaluate_placement_drift(
            [service()],
            [placement()],
            {"agpc": device(observed_system=None, observed_services=running_observation())},
            {"agpc": "agpc"},
        )
        placement_report = report["svc-1"]["placements"][0]
        self.assertIn(DRIFT_INSUFFICIENT_ACTUAL_FACTS, placement_report["drift"])
        self.assertEqual(placement_report["details"]["missing_fact"], "observed_system")

    def test_os_mismatch_reported_without_using_expected_for_export(self):
        report = evaluate_placement_drift(
            [service()],
            [placement(expected_host_os="linux")],
            {"agpc": device(observed_system="Darwin", observed_services=running_observation())},
            {"agpc": "agpc"},
        )
        placement_report = report["svc-1"]["placements"][0]
        self.assertIn(DRIFT_OS_MISMATCH, placement_report["drift"])
        self.assertEqual(placement_report["details"]["expected_host_os"], "linux")
        self.assertEqual(placement_report["details"]["observed_host_os"], "macos")

    def test_drift_categories_are_separate(self):
        # Stale and OS mismatch occur together and are reported as distinct codes.
        report = evaluate_placement_drift(
            [service()],
            [placement(expected_host_os="linux")],
            {
                "agpc": device(
                    observed_system="Darwin",
                    observed_services=running_observation(),
                    is_stale=True,
                    service_inventory_age_hours=50.0,
                )
            },
            {"agpc": "agpc"},
        )
        drift = report["svc-1"]["placements"][0]["drift"]
        self.assertIn(DRIFT_STALE_OBSERVATION, drift)
        self.assertIn(DRIFT_OS_MISMATCH, drift)
        self.assertNotIn(DRIFT_MISSING_SERVICE, drift)

    def test_declared_node_has_no_observation_drift(self):
        report = evaluate_placement_drift(
            [service(key="svc-haos", name="home-assistant")],
            [
                placement(
                    service_key="svc-haos",
                    node_slug="aghaos",
                    realized_device=None,
                    actual_state_policy="declared",
                    expected_host_os=None,
                    declared_host_os="haos",
                )
            ],
            {},
            {},
        )
        entry = report["svc-haos"]
        placement_report = entry["placements"][0]
        self.assertEqual(placement_report["drift"], [])
        self.assertEqual(placement_report["actual_state_policy"], "declared")
        self.assertEqual(entry["status"], "satisfied")

    def test_no_active_placement_status(self):
        report = evaluate_placement_drift(
            [service()],
            [],
            {"agpc": device(observed_services={})},
            {"agpc": "agpc"},
        )
        entry = report["svc-1"]
        self.assertEqual(entry["status"], "no_active_placement")
        self.assertEqual(entry["placements"], [])

    def test_deterministic_and_does_not_mutate_inputs(self):
        services = [service(key="svc-2", name="grafana"), service(key="svc-1", name="ollama")]
        placements = [
            placement(service_key="svc-1", instance_name="b", realized_device="agpc"),
            placement(service_key="svc-1", instance_name="a", realized_device="agpc"),
        ]
        devices = {"agpc": device(observed_services=running_observation())}
        node_map = {"agpc": "agpc"}

        services_before = copy.deepcopy(services)
        placements_before = copy.deepcopy(placements)
        devices_before = copy.deepcopy(devices)

        first = evaluate_placement_drift(services, placements, devices, node_map)
        second = evaluate_placement_drift(services, placements, devices, node_map)

        self.assertEqual(first, second)
        # Service report keyed by service key; placements sorted by instance_name.
        self.assertEqual(list(first.keys()), ["svc-1", "svc-2"])
        instance_order = [p["instance_name"] for p in first["svc-1"]["placements"]]
        self.assertEqual(instance_order, ["a", "b"])
        # Inputs untouched.
        self.assertEqual(services, services_before)
        self.assertEqual(placements, placements_before)
        self.assertEqual(devices, devices_before)


if __name__ == "__main__":
    unittest.main()
