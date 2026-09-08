from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import unittest

from _local_package import load_local_package

load_local_package()

from omh.workflows.web_qa_observation import WebQaObservationError, build_web_qa_observation
from omh.workflows.web_qa_observation_plan import build_web_qa_observation_plan
from test_web_qa_observation_plan import observation_request


def digest(value: object) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()


def command_digest(command: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(command, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def json_digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def object_value(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise AssertionError("test fixture must contain an object")
    return value


def object_list(value: object) -> list[dict[str, object]]:
    if type(value) is not list or any(type(item) is not dict for item in value):
        raise AssertionError("test fixture must contain object list")
    return value


def qa_plan(*, interaction: str = "read_only", field_gate: str = "not_required") -> dict[str, object]:
    request = observation_request()
    condition = object_value(request["condition"])
    condition["routes"] = object_list(condition["routes"])[:1]
    condition["viewports"] = object_list(condition["viewports"])[:1]
    condition["browsers"] = object_list(condition["browsers"])[:1]
    condition["interaction"] = interaction
    request["authorization"] = {"intent": interaction, "staging_test_authorization_ref": "approval-1" if interaction != "read_only" else ""}
    object_value(condition["budgets"])["performance"] = {**object_value(object_value(condition["budgets"])["performance"]), "field_gate": field_gate}
    return build_web_qa_observation_plan(request)


def locator(label: str, *, strategy: str = "test_id") -> dict[str, object]:
    return {"strategy": strategy, "value_digest": digest(label)}


def good_receipt(plan: dict[str, object]) -> dict[str, object]:
    condition = object_value(plan["condition"])
    cell = object_list(plan["matrix"])[0]
    route = object_list(condition["routes"])[0]
    viewport = object_list(condition["viewports"])[0]
    browser = object_list(condition["browsers"])[0]
    commands: dict[str, dict[str, object]] = {}
    for operation in ("screenshot", "console", "errors", "network_requests", "navigate", "accessibility_audit", "keyboard_tab", "focus_before", "focus_after", "lab_vitals"):
        commands[operation] = {
            "cell_id": cell["cell_id"], "operation": operation, "success": True, "returncode": 0, "duration_ms": 10,
            "stdout_sha256": digest(f"{operation}:out"), "stderr_sha256": digest(f"{operation}:err"),
            "stdout_bytes": 10, "stderr_bytes": 0,
        }
    bound = {name: [command_digest(commands[operation])] for name, operation in {
        "screenshot": "screenshot", "network": "network_requests", "critical_flow": "navigate", "accessibility": "accessibility_audit", "performance": "lab_vitals",
    }.items()}
    bound["console"] = [command_digest(commands[operation]) for operation in ("console", "errors")]
    bound["keyboard"] = [command_digest(commands[operation]) for operation in ("keyboard_tab", "focus_before", "focus_after")]
    capture = digest("capture")
    actual = {"origin": route["origin"], "navigation_digest": route["navigation_digest"], "viewport_id": viewport["viewport_id"], "width": viewport["width"], "height": viewport["height"], "dpr": viewport["dpr"], "browser_id": browser["browser_id"], "engine": browser["engine"], "version": browser["version"], "locale": condition["locale"], "timezone": condition["timezone"], "profiles": condition["profiles"], "auth_fixture_ref": condition["auth_fixture_ref"], "terminal_state": cell["expected_terminal"]}
    channels = {
        "screenshot": {"status": "observed", "blocker_id": "", "evidence": {
            "capture_sha256": capture, "byte_size": 100, "captured_at": "2026-09-07T10:00:01Z",
            "review": {"reviewer_id": "reviewer-1", "rubric_ref": "rubric-1", "evidence_ref": "evidence-1", "capture_sha256": capture, "subject_digest": plan["subject_digest"], "condition_digest": plan["condition_digest"], "score": 90},
            "operation_digests": bound["screenshot"],
        }},
        "console": {"status": "observed", "blocker_id": "", "evidence": {"exceptions": [], "operation_digests": bound["console"]}},
        "network": {"status": "observed", "blocker_id": "", "evidence": {"requests": [], "operation_digests": bound["network"]}},
        "critical_flow": {"status": "observed", "blocker_id": "", "evidence": {
            "steps": [{"step_id": "load", "action": "navigate", "locator": {"strategy": "route", "value_digest": route["navigation_digest"]}, "attempts": [{"query_identity": digest("query-1"), "operation_digest": command_digest(commands["navigate"]), "outcome": "success", "failure_class": "success"}]}],
            "trace_reference": None, "operation_digests": bound["critical_flow"],
        }},
        "accessibility": {"status": "observed", "blocker_id": "", "evidence": {"findings": [], "operation_digests": bound["accessibility"]}},
        "keyboard": {"status": "observed", "blocker_id": "", "evidence": {"action": "Tab", "focus_before": locator("body"), "focus_after": locator("continue"), "focus_changed": True, "operation_digests": bound["keyboard"]}},
        "performance": {"status": "observed", "blocker_id": "", "evidence": {"evidence_class": "lab", "lab": {"lcp_ms": 10, "inp_ms": 1, "cls": 0}, "field": None, "operation_digests": bound["performance"]}},
    }
    return {
        "schema_version": "host_web_qa_adapter_receipt/v1", "receipt_version": 1, "run_id": plan["run_id"],
        "subject_digest": plan["subject_digest"], "condition_digest": plan["condition_digest"],
        "adapter": {"adapter_id": "hermes-agent-browser/native-v1", "session_id_digest": digest("session")},
        "execution": {"command_results": list(commands.values()), "artifact_bytes": 100, "session_close_observed": True, "attempts": 1, "cost_units": 1, "peak_concurrency": 1, "started_at": "2026-09-07T10:00:00.123Z", "ended_at": "2026-09-07T10:00:02.456Z"},
        "cells": [{"cell_id": cell["cell_id"], "route_id": cell["route_id"], "state_id": cell["state_id"], "terminal_status": "observed", "terminal_blocker_id": "", "actual": actual, "channels": channels}],
        "release_status": "COLLECTED", "release_blockers": [], "redaction": {"status": "redacted_before_persistence", "forbidden": ["headers", "cookies", "credentials", "query_values", "request_bodies", "response_bodies"]},
    }


class WebQaObservationTests(unittest.TestCase):
    def test_complete_inline_critical_flow_with_distinct_operation_evidence_passes(self) -> None:
        plan = qa_plan()
        result = build_web_qa_observation(plan, good_receipt(plan))
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["mode"], plan["mode"])
        self.assertEqual(result["round"], plan["round"])
        self.assertEqual(result["limits"], plan["limits"])
        evidence = result["cells"][0]["channels"]
        self.assertNotEqual(evidence["screenshot"]["evidence"]["operation_digests"], evidence["keyboard"]["evidence"]["operation_digests"])

    def test_missing_evidence_blocks_without_demanding_operation_digest(self) -> None:
        plan = qa_plan()
        item = good_receipt(plan)
        item["cells"][0]["channels"]["keyboard"] = {"status": "blocked", "blocker_id": "keyboard_focus_not_observed", "evidence": {}}
        result = build_web_qa_observation(plan, item)
        self.assertEqual(result["verdict"], "BLOCK")
        self.assertIn("keyboard:keyboard_focus_not_observed", result["blockers"][0])

    def test_lineage_stale_review_and_field_claims_fail_closed(self) -> None:
        plan = qa_plan(field_gate="required")
        item = good_receipt(plan)
        self.assertEqual(build_web_qa_observation(plan, item)["verdict"], "BLOCK")
        performance = item["cells"][0]["channels"]["performance"]["evidence"]
        performance.update({"evidence_class": "field", "field": {"source_class": "rum", "source_ref": "rum-1", "source_digest": digest("rum"), "condition_digest": digest("wrong"), "sample_count": 10, "window": {"started_at": "2026-09-07T09:00:00Z", "ended_at": "2026-09-07T10:00:00Z"}, "p75": {"lcp_ms": 10, "inp_ms": 1, "cls": 0}}})
        lab_command = next(command for command in item["execution"]["command_results"] if command["operation"] == "lab_vitals")
        lab_command["operation"] = "field_vitals"
        performance["operation_digests"] = [command_digest(lab_command)]
        result = build_web_qa_observation(plan, item)
        self.assertEqual(result["verdict"], "BLOCK")
        self.assertIn("field_evidence_condition_mismatch", result["blockers"][0])
        item["cells"][0]["channels"]["screenshot"]["evidence"]["captured_at"] = "2026-09-06T10:00:00Z"
        self.assertEqual(build_web_qa_observation(plan, item)["verdict"], "BLOCK")

    def test_actual_conditions_and_field_operation_are_bound_to_the_cell(self) -> None:
        plan = qa_plan(); item = good_receipt(plan)
        item["cells"][0]["actual"]["locale"] = "fr-FR"
        with self.assertRaisesRegex(WebQaObservationError, "actual conditions"):
            build_web_qa_observation(plan, item)
        item = good_receipt(plan)
        performance = item["cells"][0]["channels"]["performance"]["evidence"]
        performance.update({"evidence_class": "field", "field": {"source_class": "crux", "source_ref": "crux-1", "source_digest": digest("crux"), "condition_digest": plan["condition_digest"], "sample_count": 10, "window": {"started_at": "2026-09-07T09:00:00Z", "ended_at": "2026-09-07T10:00:00Z"}, "p75": {"lcp_ms": 1, "inp_ms": 1, "cls": 0}}})
        with self.assertRaisesRegex(WebQaObservationError, "successful matching host operation"):
            build_web_qa_observation(plan, item)

    def test_execution_and_channel_operation_boundaries_fail_closed(self) -> None:
        plan = qa_plan(); item = good_receipt(plan)
        item["execution"]["command_results"][0]["duration_ms"] = plan["limits"]["max_step_seconds"] * 1000 + 1
        with self.assertRaisesRegex(WebQaObservationError, "max_step_seconds"):
            build_web_qa_observation(plan, item)
        item = good_receipt(plan); item["execution"]["peak_concurrency"] = plan["limits"]["max_concurrency"] + 1
        self.assertEqual(build_web_qa_observation(plan, item)["verdict"], "BLOCK")
        item = good_receipt(plan); item["execution"]["artifact_bytes"] = -1
        with self.assertRaisesRegex(WebQaObservationError, "artifact_bytes"):
            build_web_qa_observation(plan, item)
        item = good_receipt(plan); item["execution"]["attempts"] = -1
        with self.assertRaisesRegex(WebQaObservationError, "attempts"):
            build_web_qa_observation(plan, item)
        item = good_receipt(plan); item["cells"][0]["channels"]["console"]["evidence"]["operation_digests"] = item["cells"][0]["channels"]["console"]["evidence"]["operation_digests"][:1]
        with self.assertRaisesRegex(WebQaObservationError, "console and errors"):
            build_web_qa_observation(plan, item)
        item = good_receipt(plan); item["cells"][0]["channels"]["keyboard"]["evidence"]["operation_digests"] = item["cells"][0]["channels"]["keyboard"]["evidence"]["operation_digests"][:1]
        with self.assertRaisesRegex(WebQaObservationError, "keypress and focus"):
            build_web_qa_observation(plan, item)

    def test_network_lab_and_unobserved_cell_boundaries(self) -> None:
        plan = qa_plan(); item = good_receipt(plan)
        item["cells"][0]["channels"]["network"]["evidence"]["requests"] = [{"origin": "HTTPS://QA.EXAMPLE.TEST:443", "path_digest": digest("route"), "method": "GET", "status": 500, "duration_ms": 10, "classification": "failure", "allowlist_id": ""}]
        with self.assertRaisesRegex(WebQaObservationError, "canonical"):
            build_web_qa_observation(plan, item)
        item = good_receipt(plan); item["cells"][0]["channels"]["performance"]["evidence"]["lab"] = {"lcp_ms": None, "inp_ms": None, "cls": None}
        result = build_web_qa_observation(plan, item)
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["observation_window"]["started_at"], "2026-09-07T10:00:00.123000Z")
        item = good_receipt(plan); cell = item["cells"][0]; cell["terminal_status"] = "blocked"; cell["terminal_blocker_id"] = "navigation_timeout"; cell["actual"] = None
        for name in cell["channels"]:
            cell["channels"][name] = {"status": "blocked", "blocker_id": "navigation_timeout", "evidence": {}}
        self.assertEqual(build_web_qa_observation(plan, item)["verdict"], "BLOCK")

    def test_quality_gates_are_derived_from_closed_channel_data(self) -> None:
        mutations = (
            lambda item: item["cells"][0]["channels"]["console"]["evidence"].update({"exceptions": [{"exception_id": "error-1", "classification": "exception", "allowlist_id": ""}]}),
            lambda item: item["cells"][0]["channels"]["network"]["evidence"].update({"requests": [{"origin": "https://qa.example.test", "path_digest": digest("/checkout"), "method": "GET", "status": 500, "duration_ms": 10, "classification": "failure", "allowlist_id": ""}]}),
            lambda item: item["cells"][0]["channels"]["accessibility"]["evidence"].update({"findings": [{"rule_id": "color-contrast", "impact": "serious", "node_count": 1}]}),
            lambda item: item["cells"][0]["channels"]["keyboard"]["evidence"].update({"focus_changed": False}),
            lambda item: item["cells"][0]["channels"]["critical_flow"]["evidence"]["steps"][0].update({"attempts": [{"query_identity": digest("query-1"), "operation_digest": command_digest(next(command for command in item["execution"]["command_results"] if command["operation"] == "navigate")), "outcome": "failure", "failure_class": "timeout"}]}),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                plan = qa_plan(); item = good_receipt(plan); mutate(item)
                self.assertEqual(build_web_qa_observation(plan, item)["verdict"], "REVISE")

    def test_visual_review_and_raw_privacy_shape_fail_closed(self) -> None:
        plan = qa_plan(); item = good_receipt(plan)
        item["cells"][0]["channels"]["screenshot"]["evidence"]["review"] = None
        self.assertEqual(build_web_qa_observation(plan, item)["verdict"], "BLOCK")
        item = good_receipt(plan)
        item["cells"][0]["channels"]["screenshot"]["evidence"]["review"]["score"] = 89
        self.assertEqual(build_web_qa_observation(plan, item)["verdict"], "BLOCK")
        item = good_receipt(plan)
        item["cells"][0]["channels"]["network"]["evidence"]["headers"] = "Bearer secret"
        with self.assertRaisesRegex(WebQaObservationError, "closed keys"):
            build_web_qa_observation(plan, item)

    def test_mutation_retry_and_forged_production_action_fail_closed(self) -> None:
        mutation_plan = qa_plan(interaction="mutation")
        item = good_receipt(mutation_plan)
        item["cells"][0]["channels"]["critical_flow"]["evidence"]["steps"][0]["attempts"] = [
            {"query_identity": digest("first"), "operation_digest": command_digest(next(command for command in item["execution"]["command_results"] if command["operation"] == "navigate")), "outcome": "failure", "failure_class": "timeout"},
            {"query_identity": digest("second"), "operation_digest": command_digest(next(command for command in item["execution"]["command_results"] if command["operation"] == "navigate")), "outcome": "success", "failure_class": "success"},
        ]
        with self.assertRaisesRegex(WebQaObservationError, "read-only retry"):
            build_web_qa_observation(mutation_plan, item)
        forged = deepcopy(mutation_plan)
        forged["condition"]["environment"] = "production"
        forged["condition_digest"] = json_digest(forged["condition"])
        identity = {key: forged[key] for key in ("schema_version", "mode", "subject_digest", "condition_digest", "required_channels", "limits", "authorization", "round")}
        forged["plan_digest"] = json_digest(identity); forged["run_id"] = f"web-qa-{forged['plan_digest'][:24]}"
        with self.assertRaisesRegex(WebQaObservationError, "production plans must be read_only"):
            build_web_qa_observation(forged, good_receipt(mutation_plan))

    def test_retries_trace_plan_caps_and_malformed_channels_fail_closed(self) -> None:
        plan = qa_plan(); item = good_receipt(plan)
        attempts = item["cells"][0]["channels"]["critical_flow"]["evidence"]["steps"][0]["attempts"]
        attempts.append({"query_identity": digest("query-2"), "operation_digest": command_digest(next(command for command in item["execution"]["command_results"] if command["operation"] == "navigate")), "outcome": "success", "failure_class": "success"})
        with self.assertRaisesRegex(WebQaObservationError, "retry after success"):
            build_web_qa_observation(plan, item)
        item = good_receipt(plan)
        item["cells"][0]["channels"]["critical_flow"]["evidence"]["trace_reference"] = {"schema_version": "browser_workflow_trace_reference/v1", "trace_id": "bwt-1", "digest": digest("trace"), "project_identity": digest("project"), "origins": ["https://qa.example.test"], "lifecycle_status": "approved"}
        self.assertEqual(build_web_qa_observation(plan, item)["verdict"], "BLOCK")
        item = good_receipt(plan); item["execution"]["artifact_bytes"] = plan["limits"]["max_artifact_bytes"] + 1
        self.assertEqual(build_web_qa_observation(plan, item)["verdict"], "BLOCK")
        forged = deepcopy(plan); forged["limits"]["max_artifact_bytes"] = 999_999_999
        identity = {key: forged[key] for key in ("schema_version", "mode", "subject_digest", "condition_digest", "required_channels", "limits", "authorization", "round")}
        forged["plan_digest"] = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest(); forged["run_id"] = f"web-qa-{forged['plan_digest'][:24]}"
        with self.assertRaisesRegex(WebQaObservationError, "hard maximum"):
            build_web_qa_observation(forged, item)
        item = good_receipt(plan); del item["cells"][0]["channels"]["network"]
        with self.assertRaisesRegex(WebQaObservationError, "closed keys"):
            build_web_qa_observation(plan, item)


if __name__ == "__main__":
    unittest.main()
