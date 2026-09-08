from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import hashlib
import unittest

from _local_package import load_local_package

load_local_package()

from omh.workflows.web_qa_comparison import compare_web_qa_observations
from omh.workflows.web_qa_observation_plan import build_web_qa_observation_plan
from test_web_qa_observation import command_digest, good_receipt
from test_web_qa_observation_plan import observation_request


REVISION_1 = "4c5c1e8f9a2b3c4d5e6f7081920a1b2c3d4e5f60"
REVISION_2 = "5d6d2f9a2b3c4d5e6f7081920a1b2c3d4e5f6071"
DEPLOYMENT_WINDOW = {
    "starts_at": "2026-09-07T10:00:00Z",
    "ends_at": "2026-09-07T10:10:00Z",
}


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def plan(
    *,
    revision: str = REVISION_1,
    environment: str = "staging",
    mode: str = "matrix",
    ordinal: int = 1,
) -> dict[str, object]:
    request = observation_request()
    request["condition"]["routes"] = request["condition"]["routes"][:1]
    request["condition"]["viewports"] = request["condition"]["viewports"][:1]
    request["condition"]["browsers"] = request["condition"]["browsers"][:1]
    request["subject"]["revision"] = revision
    request["condition"]["environment"] = environment
    request["round"] = {"round_id": f"round-{ordinal}", "ordinal": ordinal}
    request["mode"] = mode
    if mode == "canary":
        request["condition"]["environment"] = "production"
        request["subject"]["observed_deploy_ref"] = "deploy-observation-1"
        request["subject"]["deployment"] = {
            "deployment_id": "deployment-1",
            "environment": "production",
            "rollout_phase": "canary",
            "window": DEPLOYMENT_WINDOW,
        }
    return build_web_qa_observation_plan(request)


def receipt(plan_value: dict[str, object], *, captured_at: str, capture: str = "capture") -> dict[str, object]:
    result = good_receipt(plan_value)
    captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00")).astimezone(UTC)
    result["execution"]["started_at"] = (captured - timedelta(seconds=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
    result["execution"]["ended_at"] = (captured + timedelta(seconds=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
    screenshot = result["cells"][0]["channels"]["screenshot"]["evidence"]
    screenshot["captured_at"] = captured_at
    screenshot["capture_sha256"] = digest(capture)
    screenshot["review"]["capture_sha256"] = digest(capture)
    return result


def envelope(plan_value: dict[str, object], *, captured_at: str = "2026-09-07T10:00:01Z", capture: str = "capture") -> dict[str, object]:
    return {"plan": plan_value, "receipt": receipt(plan_value, captured_at=captured_at, capture=capture)}


def deployment_observation(plan_value: dict[str, object], *, status: str = "succeeded") -> dict[str, object]:
    subject = plan_value["subject"]
    deployment = subject["deployment"]
    return {
        "schema_version": "host_deployment_observation/v1",
        "deployment_ref": subject["observed_deploy_ref"],
        "repository": subject["repository"],
        "revision": subject["revision"],
        "deployment_id": deployment["deployment_id"],
        "environment": deployment["environment"],
        "rollout_phase": deployment["rollout_phase"],
        "status": status,
        "window": deployment["window"],
        "observed_at": "2026-09-07T10:00:00Z",
        "observation_source_ref": "host-deploy-monitor-1",
        "evidence_digests": [digest("deployment-evidence")],
    }


class WebQaComparisonTests(unittest.TestCase):
    def test_equal_conditions_re_admit_and_pass_with_stable_identity(self) -> None:
        baseline_plan = plan()
        candidate_plan = plan(revision=REVISION_2)
        baseline = envelope(baseline_plan)
        candidate = envelope(candidate_plan, capture="candidate")

        first = compare_web_qa_observations(baseline, candidate)
        second = compare_web_qa_observations(deepcopy(baseline), deepcopy(candidate))

        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], "web_qa_comparison/v1")
        self.assertEqual(first["status"], "comparable")
        self.assertEqual(first["verdict"], "PASS")
        self.assertEqual(first["baseline"]["run_id"], baseline_plan["run_id"])
        self.assertIn("rollback", first["does_not_authorize"])
        self.assertFalse(first["rollback_authorized"])

        changed_receipt = deepcopy(candidate)
        changed_receipt["receipt"]["cells"][0]["channels"]["keyboard"] = {
            "status": "blocked", "blocker_id": "keyboard_not_observed", "evidence": {}
        }
        changed = compare_web_qa_observations(baseline, changed_receipt)
        self.assertNotEqual(first["comparison_id"], changed["comparison_id"])
        self.assertEqual(changed["verdict"], "BLOCK")

    def test_mismatched_nested_condition_is_not_comparable(self) -> None:
        baseline = envelope(plan())
        changed_request = observation_request()
        changed_request["condition"]["routes"] = changed_request["condition"]["routes"][:1]
        changed_request["condition"]["viewports"] = changed_request["condition"]["viewports"][:1]
        changed_request["condition"]["browsers"] = changed_request["condition"]["browsers"][:1]
        changed_request["condition"]["profiles"]["network"] = "high_latency"
        changed_request["condition"]["feature_flags"].extend(
            [
                {"flag_id": "one-more", "state": "enabled"},
                {"flag_id": "third-flag", "state": "disabled"},
            ]
        )
        candidate_plan = build_web_qa_observation_plan(changed_request)

        result = compare_web_qa_observations(baseline, envelope(candidate_plan))

        self.assertEqual(result["status"], "not_comparable")
        self.assertEqual(result["verdict"], "BLOCK")
        self.assertIn("condition.profiles.network", [item["field"] for item in result["condition_differences"]])

    def test_staging_to_production_cannot_claim_regression_or_rollback_authority(self) -> None:
        baseline = envelope(plan(environment="staging"))
        candidate = envelope(plan(revision=REVISION_2, environment="production"))

        result = compare_web_qa_observations(baseline, candidate)

        self.assertEqual(result["status"], "not_comparable")
        self.assertEqual(result["verdict"], "BLOCK")
        self.assertIn("condition.environment", [item["field"] for item in result["condition_differences"]])
        self.assertFalse(result["rollback_authorized"])

    def test_canary_requires_successful_exact_trusted_deployment_observation(self) -> None:
        baseline = envelope(plan(environment="production"), captured_at="2026-09-07T09:59:01Z", capture="baseline")
        candidate_plan = plan(revision=REVISION_2, mode="canary")
        candidate = envelope(candidate_plan, capture="candidate")

        missing = compare_web_qa_observations(baseline, candidate)
        self.assertIn("canary_trusted_deployment_resolver_missing", missing["blockers"])
        self.assertEqual(missing["verdict"], "BLOCK")

        for status in ("prepared", "unknown"):
            with self.subTest(status=status):
                result = compare_web_qa_observations(
                    baseline,
                    candidate,
                    trusted_deployment_resolver=lambda _: deployment_observation(candidate_plan, status=status),
                )
                self.assertIn("canary_deployment_not_succeeded", result["blockers"])
                self.assertEqual(result["verdict"], "BLOCK")

        wrong = deployment_observation(candidate_plan)
        wrong["revision"] = REVISION_1
        result = compare_web_qa_observations(baseline, candidate, trusted_deployment_resolver=lambda _: wrong)
        self.assertIn("canary_deployment_binding_mismatch", result["blockers"])

        passed = compare_web_qa_observations(
            baseline,
            candidate,
            trusted_deployment_resolver=lambda reference: deployment_observation(candidate_plan) if reference == "deploy-observation-1" else None,
        )
        self.assertEqual(passed["verdict"], "PASS")

        changed_evidence = deployment_observation(candidate_plan)
        changed_evidence["evidence_digests"] = [digest("new-deployment-evidence")]
        changed = compare_web_qa_observations(baseline, candidate, trusted_deployment_resolver=lambda _: changed_evidence)
        self.assertNotEqual(passed["comparison_id"], changed["comparison_id"])

        malformed = deployment_observation(candidate_plan)
        malformed["observation_source_ref"] = "x" * 129
        result = compare_web_qa_observations(baseline, candidate, trusted_deployment_resolver=lambda _: malformed)
        self.assertIn("canary_deployment_receipt_unresolved", result["blockers"])

    def test_canary_requires_pre_deployment_baseline_and_candidate_window(self) -> None:
        candidate_plan = plan(revision=REVISION_2, mode="canary")
        resolver = lambda _: deployment_observation(candidate_plan)
        baseline = envelope(plan(environment="production"), captured_at="2026-09-07T10:00:01Z", capture="baseline")
        result = compare_web_qa_observations(baseline, envelope(candidate_plan, capture="candidate"), trusted_deployment_resolver=resolver)
        self.assertTrue(any(item.endswith("canary_baseline_not_before_deployment") for item in result["blockers"]))

        straddling = envelope(plan(environment="production"), captured_at="2026-09-07T09:59:59Z", capture="baseline")
        result = compare_web_qa_observations(straddling, envelope(candidate_plan, capture="candidate"), trusted_deployment_resolver=resolver)
        self.assertIn("canary_baseline_observation_not_before_deployment", result["blockers"])

        late = deployment_observation(candidate_plan)
        late["observed_at"] = "2026-09-07T10:00:01Z"
        result = compare_web_qa_observations(
            envelope(plan(environment="production"), captured_at="2026-09-07T09:59:01Z", capture="baseline"),
            envelope(candidate_plan, capture="candidate"),
            trusted_deployment_resolver=lambda _: late,
        )
        self.assertIn("canary_deployment_observed_after_canary_window_start", result["blockers"])

        outside = envelope(candidate_plan, captured_at="2026-09-07T10:10:01Z", capture="candidate")
        result = compare_web_qa_observations(
            envelope(plan(environment="production"), captured_at="2026-09-07T09:59:01Z", capture="baseline"),
            outside,
            trusted_deployment_resolver=resolver,
        )
        self.assertIn("canary_candidate_observation_outside_deployment_window", result["blockers"])

    def test_same_condition_field_performance_tolerance_breach_revises_but_lab_is_diagnostic(self) -> None:
        baseline_plan = plan()
        candidate_plan = plan(revision=REVISION_2)
        baseline = envelope(baseline_plan, capture="baseline")
        candidate = envelope(candidate_plan, capture="candidate")
        self._field_metrics(baseline, lcp=0, inp=10, cls=0.01)
        self._field_metrics(candidate, lcp=1, inp=10, cls=0.01)

        result = compare_web_qa_observations(baseline, candidate)
        self.assertEqual(result["verdict"], "REVISE")
        self.assertIn("field_lcp_regression_beyond_tolerance", result["revision_reasons"][0])

        lab_candidate = envelope(candidate_plan, capture="candidate")
        lab_candidate["receipt"]["cells"][0]["channels"]["performance"]["evidence"]["lab"]["lcp_ms"] = 999999
        result = compare_web_qa_observations(envelope(baseline_plan, capture="baseline"), lab_candidate)
        self.assertEqual(result["verdict"], "PASS")

    def test_candidate_block_never_softens_to_pass(self) -> None:
        baseline = envelope(plan())
        candidate = envelope(plan(revision=REVISION_2), capture="candidate")
        candidate["receipt"]["cells"][0]["channels"]["keyboard"] = {
            "status": "blocked", "blocker_id": "keyboard_not_observed", "evidence": {}
        }

        result = compare_web_qa_observations(baseline, candidate)

        self.assertEqual(result["verdict"], "BLOCK")
        self.assertIn("candidate_observation_blocked", result["blockers"])

    def test_visual_failure_requires_changed_revision_round_and_recapture(self) -> None:
        baseline_plan = plan()
        baseline = envelope(baseline_plan, captured_at="2026-09-07T10:00:01Z", capture="failed")
        baseline["receipt"]["cells"][0]["channels"]["screenshot"]["evidence"]["review"]["score"] = 89
        unchanged = envelope(plan(revision=REVISION_2, ordinal=2), captured_at="2026-09-07T10:00:02Z", capture="failed")

        result = compare_web_qa_observations(baseline, unchanged)
        self.assertEqual(result["verdict"], "BLOCK")
        self.assertIn("visual_retest_requires_changed_capture", result["blockers"][0])

        recaptured = envelope(plan(revision=REVISION_2, ordinal=2), captured_at="2026-09-07T10:00:02Z", capture="fresh")
        self.assertEqual(compare_web_qa_observations(baseline, recaptured)["verdict"], "PASS")

        no_change = envelope(plan(ordinal=2), captured_at="2026-09-07T10:00:02Z", capture="fresh")
        result = compare_web_qa_observations(baseline, no_change)
        self.assertIn("visual_retest_requires_source_revision_change", result["blockers"])

        skipped_round = envelope(plan(revision=REVISION_2, ordinal=3), captured_at="2026-09-07T10:00:02Z", capture="fresh")
        result = compare_web_qa_observations(baseline, skipped_round)
        self.assertIn("visual_retest_requires_incremented_round", result["blockers"])

    def test_invalid_or_cap_exceeding_envelope_blocks(self) -> None:
        baseline = envelope(plan())
        invalid_candidate = envelope(plan(revision=REVISION_2))
        invalid_candidate["plan"]["round"]["ordinal"] = 4

        result = compare_web_qa_observations(baseline, invalid_candidate)

        self.assertEqual(result["verdict"], "BLOCK")
        self.assertEqual(result["status"], "not_comparable")
        self.assertIn("candidate_admission_rejected", result["blockers"])

    def _field_metrics(self, item: dict[str, object], *, lcp: int, inp: int, cls: float) -> None:
        plan_value = item["plan"]
        evidence = item["receipt"]["cells"][0]["channels"]["performance"]["evidence"]
        command = {
            "cell_id": plan_value["matrix"][0]["cell_id"],
            "operation": "field_vitals",
            "success": True,
            "returncode": 0,
            "duration_ms": 10,
            "stdout_sha256": digest("field_vitals:out"),
            "stderr_sha256": digest("field_vitals:err"),
            "stdout_bytes": 10,
            "stderr_bytes": 0,
        }
        item["receipt"]["execution"]["command_results"].append(command)
        evidence["operation_digests"] = [command_digest(command)]
        evidence["evidence_class"] = "field"
        evidence["field"] = {
            "source_class": "rum",
            "source_ref": "rum-1",
            "source_digest": digest("rum"),
            "condition_digest": plan_value["condition_digest"],
            "sample_count": 10,
            "window": {"started_at": "2026-09-07T09:00:00Z", "ended_at": "2026-09-07T10:00:00Z"},
            "p75": {"lcp_ms": lcp, "inp_ms": inp, "cls": cls},
        }


if __name__ == "__main__":
    unittest.main()
