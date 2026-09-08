from __future__ import annotations

from copy import deepcopy
import json
import socket
import subprocess
import unittest
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()

from omh.workflows.web_qa_observation_plan import (
    HARD_LIMITS,
    REQUIRED_CHANNELS,
    WebQaObservationPlanError,
    build_web_qa_observation_plan,
)


def observation_request() -> dict[str, object]:
    return {
        "mode": "matrix",
        "subject": {
            "repository": "https://github.com/acme/storefront",
            "revision": "4c5c1e8f9a2b3c4d5e6f7081920a1b2c3d4e5f60",
            "observed_deploy_ref": "",
            "deployment": None,
        },
        "condition": {
            "routes": [
                {
                    "route_id": "checkout",
                    "state_id": "anonymous",
                    "expected_terminal": "document_ready",
                    "url": "https://qa.example.test/checkout?token=transient-secret#payment",
                },
                {
                    "route_id": "catalog",
                    "state_id": "anonymous",
                    "expected_terminal": "document_ready",
                    "url": "https://qa.example.test/catalog",
                },
            ],
            "viewports": [
                {"viewport_id": "desktop", "width": 1440, "height": 900, "dpr": 1},
                {"viewport_id": "mobile", "width": 390, "height": 844, "dpr": 3},
            ],
            "browsers": [
                {"browser_id": "chrome-126", "engine": "chromium", "version": "126.0.6478.61"},
                {"browser_id": "firefox-127", "engine": "firefox", "version": "127.0"},
            ],
            "locale": "en-US",
            "timezone": "America/Los_Angeles",
            "auth_fixture_ref": "fixture-anonymous-v1",
            "profiles": {
                "cache": "cold",
                "load": "normal",
                "device": "desktop",
                "cpu": "normal",
                "network": "online",
            },
            "feature_flags": [{"flag_id": "new-checkout", "state": "enabled"}],
            "budgets": {
                "visual": {"minimum_score": 90},
                "functional": {"maximum_terminal_failures": 0},
                "accessibility": {"maximum_serious_or_critical_findings": 0},
                "console": {"maximum_unallowlisted_exceptions": 0},
                "network": {"maximum_first_party_failures": 0, "slow_request_ms": 1000},
                "performance": {
                    "field_gate": "not_required",
                    "lab_evidence": "diagnostic",
                    "field_p75": {"lcp_ms_lt": 2500, "inp_ms_lt": 200, "cls_lt": 0.1},
                    "relative_tolerances": {"lcp_percent": 5, "inp_percent": 5, "cls_absolute": 0.01},
                },
            },
            "expected_noise_allowlist": [{"noise_id": "analytics-abort", "channel": "network"}],
            "environment": "staging",
            "interaction": "read_only",
        },
        "authorization": {"intent": "read_only", "staging_test_authorization_ref": ""},
        "limits": {},
        "round": {"round_id": "round-1", "ordinal": 1},
    }


class WebQaObservationPlanTests(unittest.TestCase):
    def test_builds_closed_full_matrix_with_all_typed_channels(self) -> None:
        plan = build_web_qa_observation_plan(observation_request())

        self.assertEqual(plan["schema_version"], "web_qa_observation_plan/v1")
        self.assertEqual(plan["required_channels"], list(REQUIRED_CHANNELS))
        self.assertEqual(len(plan["matrix"]), 8)
        self.assertEqual(len({cell["cell_id"] for cell in plan["matrix"]}), 8)
        self.assertEqual(plan["condition"]["budgets"]["visual"]["minimum_score"], 90)
        self.assertEqual(plan["condition"]["budgets"]["performance"]["lab_evidence"], "diagnostic")
        self.assertEqual(plan["condition"]["budgets"]["performance"]["field_gate"], "not_required")

    def test_subject_and_condition_digests_are_independent(self) -> None:
        baseline = build_web_qa_observation_plan(observation_request())

        revised = observation_request()
        revised["subject"]["revision"] = "5d6d2f9a2b3c4d5e6f7081920a1b2c3d4e5f6071"
        changed_subject = build_web_qa_observation_plan(revised)
        self.assertNotEqual(changed_subject["subject_digest"], baseline["subject_digest"])
        self.assertEqual(changed_subject["condition_digest"], baseline["condition_digest"])

        changed_route = observation_request()
        changed_route["condition"]["routes"][0]["state_id"] = "authenticated"
        changed_condition = build_web_qa_observation_plan(changed_route)
        self.assertEqual(changed_condition["subject_digest"], baseline["subject_digest"])
        self.assertNotEqual(changed_condition["condition_digest"], baseline["condition_digest"])

    def test_query_and_fragment_bind_condition_without_persisting_raw_values(self) -> None:
        baseline = build_web_qa_observation_plan(observation_request())
        changed = observation_request()
        changed["condition"]["routes"][0]["url"] = "https://qa.example.test/checkout?token=other-secret#confirmed"
        changed_plan = build_web_qa_observation_plan(changed)
        persisted = json.dumps(changed_plan, sort_keys=True)

        self.assertNotEqual(changed_plan["condition_digest"], baseline["condition_digest"])
        self.assertNotIn("other-secret", persisted)
        self.assertNotIn("confirmed", persisted)
        self.assertNotIn("url", changed_plan["condition"]["routes"][0])
        self.assertEqual(changed_plan["condition"]["routes"][0]["origin"], "https://qa.example.test")

    def test_same_identity_is_deterministic_and_order_independent(self) -> None:
        first = build_web_qa_observation_plan(observation_request())
        reordered = observation_request()
        reordered["condition"]["routes"].reverse()
        reordered["condition"]["viewports"].reverse()
        reordered["condition"]["browsers"].reverse()

        self.assertEqual(build_web_qa_observation_plan(reordered), first)

    def test_caps_authorization_and_round_change_plan_identity(self) -> None:
        baseline = build_web_qa_observation_plan(observation_request())

        tightened = observation_request()
        tightened["limits"] = {"max_run_seconds": 600}
        self.assertNotEqual(build_web_qa_observation_plan(tightened)["plan_digest"], baseline["plan_digest"])

        authorized = observation_request()
        authorized["condition"]["interaction"] = "login"
        authorized["authorization"] = {"intent": "login", "staging_test_authorization_ref": "approval-staging-read-7"}
        self.assertNotEqual(build_web_qa_observation_plan(authorized)["plan_digest"], baseline["plan_digest"])

        next_round = observation_request()
        next_round["round"] = {"round_id": "round-2", "ordinal": 2}
        self.assertNotEqual(build_web_qa_observation_plan(next_round)["plan_digest"], baseline["plan_digest"])

    def test_rejects_malformed_oversized_cap_raising_and_unpinned_inputs(self) -> None:
        unknown = observation_request()
        unknown["condition"]["attacker-controlled-unknown"] = "not-allowed"
        with self.assertRaisesRegex(WebQaObservationPlanError, "unsupported keys") as raised:
            build_web_qa_observation_plan(unknown)
        self.assertNotIn("attacker-controlled-unknown", str(raised.exception))

        wrong_type = observation_request()
        wrong_type["condition"]["viewports"][0]["width"] = True
        with self.assertRaisesRegex(WebQaObservationPlanError, "width must be an integer"):
            build_web_qa_observation_plan(wrong_type)

        unpinned = observation_request()
        unpinned["subject"]["revision"] = "main"
        with self.assertRaisesRegex(WebQaObservationPlanError, "revision has invalid format"):
            build_web_qa_observation_plan(unpinned)

        raised_cap = observation_request()
        raised_cap["limits"] = {"max_cells": HARD_LIMITS["max_cells"] + 1}
        with self.assertRaisesRegex(WebQaObservationPlanError, "may only tighten"):
            build_web_qa_observation_plan(raised_cap)

        oversized_string = observation_request()
        oversized_string["condition"]["routes"][0]["url"] = "https://qa.example.test/" + ("x" * 70_000)
        with self.assertRaisesRegex(WebQaObservationPlanError, "byte bound"):
            build_web_qa_observation_plan(oversized_string)

        oversized_container = observation_request()
        oversized_container["condition"]["routes"] = [
            {
                "route_id": f"route-{number}",
                "state_id": "anonymous",
                "expected_terminal": "document_ready",
                "url": "https://qa.example.test/",
            }
            for number in range(3_000)
        ]
        with self.assertRaisesRegex(WebQaObservationPlanError, "node bound"):
            build_web_qa_observation_plan(oversized_container)

    def test_rejects_duplicate_route_state_and_invalid_budget_thresholds(self) -> None:
        duplicate = observation_request()
        duplicate["condition"]["routes"].append(
            {
                **deepcopy(duplicate["condition"]["routes"][0]),
                "expected_terminal": "same_origin",
            }
        )
        with self.assertRaisesRegex(WebQaObservationPlanError, "duplicate route/state"):
            build_web_qa_observation_plan(duplicate)

        low_visual = observation_request()
        low_visual["condition"]["budgets"]["visual"]["minimum_score"] = 89
        with self.assertRaisesRegex(WebQaObservationPlanError, "between 90 and 100"):
            build_web_qa_observation_plan(low_visual)

        zero_field_bar = observation_request()
        zero_field_bar["condition"]["budgets"]["performance"]["field_p75"]["cls_lt"] = 0
        with self.assertRaisesRegex(WebQaObservationPlanError, "cls_lt must be positive"):
            build_web_qa_observation_plan(zero_field_bar)

        weak_field_bar = observation_request()
        weak_field_bar["condition"]["budgets"]["performance"]["field_p75"]["lcp_ms_lt"] = 2501
        with self.assertRaisesRegex(WebQaObservationPlanError, "published genuine-field bars"):
            build_web_qa_observation_plan(weak_field_bar)

        no_slow_threshold = observation_request()
        no_slow_threshold["condition"]["budgets"]["network"]["slow_request_ms"] = 0
        with self.assertRaisesRegex(WebQaObservationPlanError, "slow_request_ms must be positive"):
            build_web_qa_observation_plan(no_slow_threshold)

    def test_canonicalizes_ipv6_and_scheme_specific_ports(self) -> None:
        request = observation_request()
        request["condition"]["routes"] = [
            {
                "route_id": "ipv6-default",
                "state_id": "anonymous",
                "expected_terminal": "document_ready",
                "url": "https://[2001:db8::1]:443/checkout",
            },
            {
                "route_id": "ipv6-non-default",
                "state_id": "anonymous",
                "expected_terminal": "document_ready",
                "url": "http://[2001:db8::2]:8080/catalog",
            },
        ]
        routes = build_web_qa_observation_plan(request)["condition"]["routes"]

        self.assertEqual(routes[0]["origin"], "https://[2001:db8::1]")
        self.assertEqual(routes[1]["origin"], "http://[2001:db8::2]:8080")

    def test_canary_parses_bounded_utc_window_and_requires_production_read_only(self) -> None:
        request = observation_request()
        request["mode"] = "canary"
        request["condition"]["environment"] = "production"
        request["subject"]["observed_deploy_ref"] = "receipt-deploy-401"
        request["subject"]["deployment"] = {
            "deployment_id": "deploy-401",
            "environment": "production",
            "rollout_phase": "canary",
            "window": {
                "starts_at": "2026-09-07T10:00:00-07:00",
                "ends_at": "2026-09-07T10:10:00-07:00",
            },
        }
        plan = build_web_qa_observation_plan(request)
        window = plan["subject"]["deployment"]["window"]
        self.assertEqual(window["starts_at"], "2026-09-07T17:00:00Z")
        self.assertEqual(window["ends_at"], "2026-09-07T17:10:00Z")
        self.assertEqual(window["duration_seconds"], 600)

        request["condition"]["interaction"] = "mutation"
        request["authorization"] = {"intent": "mutation", "staging_test_authorization_ref": "approval-test-1"}
        with self.assertRaisesRegex(WebQaObservationPlanError, "production plans must be read_only"):
            build_web_qa_observation_plan(request)

    def test_login_or_mutation_needs_matching_intent_and_staging_test_reference(self) -> None:
        request = observation_request()
        request["condition"]["interaction"] = "mutation"
        request["authorization"] = {"intent": "read_only", "staging_test_authorization_ref": "approval-staging-9"}
        with self.assertRaisesRegex(WebQaObservationPlanError, "intent must match"):
            build_web_qa_observation_plan(request)

        request["authorization"] = {"intent": "mutation", "staging_test_authorization_ref": ""}
        with self.assertRaisesRegex(WebQaObservationPlanError, "requires staging/test authorization"):
            build_web_qa_observation_plan(request)

        request["authorization"] = {"intent": "mutation", "staging_test_authorization_ref": "approval-staging-9"}
        plan = build_web_qa_observation_plan(request)
        self.assertEqual(plan["authorization"]["intent"], "mutation")
        self.assertIn("mutation", plan["does_not_authorize"])

    def test_planning_performs_no_file_provider_network_or_subprocess_calls(self) -> None:
        with (
            patch("builtins.open", side_effect=AssertionError("file I/O")),
            patch.object(socket, "socket", side_effect=AssertionError("network I/O")),
            patch.object(subprocess, "run", side_effect=AssertionError("subprocess")),
        ):
            plan = build_web_qa_observation_plan(observation_request())

        self.assertTrue(plan["run_id"].startswith("web-qa-"))


if __name__ == "__main__":
    unittest.main()
