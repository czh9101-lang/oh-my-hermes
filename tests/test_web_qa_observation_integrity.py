from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()

from omh.workflows.web_qa_observation import WebQaObservationError, build_web_qa_observation
from omh.workflows.web_qa_observation_plan import build_web_qa_observation_plan
from test_web_qa_observation import command_digest, digest, good_receipt, json_digest, qa_plan
from test_web_qa_observation_plan import observation_request


def rebind_plan(plan: dict[str, object]) -> None:
    plan["subject_digest"] = json_digest(plan["subject"])
    plan["condition_digest"] = json_digest(plan["condition"])
    identity = {key: plan[key] for key in (
        "schema_version", "mode", "subject_digest", "condition_digest",
        "required_channels", "limits", "authorization", "round",
    )}
    plan["plan_digest"] = json_digest(identity)
    plan["run_id"] = f"web-qa-{plan['plan_digest'][:24]}"


class WebQaObservationIntegrityTests(unittest.TestCase):
    def test_failed_screenshot_command_cannot_attest_observed_capture(self) -> None:
        plan = qa_plan()
        receipt = good_receipt(plan)
        command = next(item for item in receipt["execution"]["command_results"] if item["operation"] == "screenshot")
        command.update(success=False, returncode=1)
        receipt["cells"][0]["channels"]["screenshot"]["evidence"]["operation_digests"] = [command_digest(command)]

        with self.assertRaises(WebQaObservationError):
            build_web_qa_observation(plan, receipt)

    def test_production_read_only_plan_rejects_observed_submission(self) -> None:
        request = observation_request()
        request["condition"]["environment"] = "production"
        request["condition"]["routes"] = request["condition"]["routes"][:1]
        request["condition"]["viewports"] = request["condition"]["viewports"][:1]
        request["condition"]["browsers"] = request["condition"]["browsers"][:1]
        plan = build_web_qa_observation_plan(request)
        receipt = good_receipt(plan)
        receipt["cells"][0]["channels"]["critical_flow"]["evidence"]["steps"][0]["action"] = "submit"

        with self.assertRaises(WebQaObservationError):
            build_web_qa_observation(plan, receipt)

    def test_rehashing_a_plan_cannot_raise_its_round_cap(self) -> None:
        plan = qa_plan()
        plan["round"]["ordinal"] = plan["limits"]["max_rounds"] + 1
        rebind_plan(plan)

        with self.assertRaises(WebQaObservationError):
            build_web_qa_observation(plan, good_receipt(plan))

    def test_rehashing_a_plan_cannot_persist_private_profile_fields(self) -> None:
        plan = qa_plan()
        plan["condition"]["profiles"]["headers"] = {"Authorization": "Bearer private"}
        rebind_plan(plan)

        with self.assertRaises(WebQaObservationError):
            build_web_qa_observation(plan, good_receipt(plan))

    def test_unknown_accessibility_impact_is_not_a_clean_audit(self) -> None:
        plan = qa_plan()
        receipt = good_receipt(plan)
        receipt["cells"][0]["channels"]["accessibility"]["evidence"]["findings"] = [
            {"rule_id": "color-contrast", "impact": "seriuos", "node_count": 1},
        ]

        with self.assertRaises(WebQaObservationError):
            build_web_qa_observation(plan, receipt)

    def test_transient_attempt_exhaustion_is_terminal_block(self) -> None:
        plan = qa_plan()
        receipt = good_receipt(plan)
        operation_digest = command_digest(next(item for item in receipt["execution"]["command_results"] if item["operation"] == "navigate"))
        receipt["cells"][0]["channels"]["critical_flow"]["evidence"]["steps"][0]["attempts"] = [
            {"query_identity": digest(f"query-{number}"), "operation_digest": operation_digest, "outcome": "failure", "failure_class": "timeout"}
            for number in range(plan["limits"]["max_attempts_per_read"])
        ]

        result = build_web_qa_observation(plan, receipt)

        self.assertEqual(result["verdict"], "BLOCK")

    def test_visual_score_cannot_exceed_rubric_maximum(self) -> None:
        plan = qa_plan()
        receipt = good_receipt(plan)
        receipt["cells"][0]["channels"]["screenshot"]["evidence"]["review"]["score"] = 101

        with self.assertRaises(WebQaObservationError):
            build_web_qa_observation(plan, receipt)

    def test_missing_channel_requires_a_named_blocker(self) -> None:
        plan = qa_plan()
        receipt = good_receipt(plan)
        receipt["cells"][0]["channels"]["keyboard"] = {
            "status": "blocked", "blocker_id": "", "evidence": {},
        }

        with self.assertRaises(WebQaObservationError):
            build_web_qa_observation(plan, receipt)

    def test_negative_cost_is_not_an_observed_budget(self) -> None:
        plan = qa_plan()
        receipt = good_receipt(plan)
        receipt["execution"]["cost_units"] = -1

        with self.assertRaises(WebQaObservationError):
            build_web_qa_observation(plan, receipt)
