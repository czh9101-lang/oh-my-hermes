"""Foundation proofs only; public operations and registry adoption need integration."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import importlib
import importlib.util
from itertools import product
from typing import Protocol, TypeGuard, runtime_checkable
import unittest

from _local_package import load_local_package

load_local_package()
from omh.workflows import lifecycle_growth_contracts as contracts
from omh.workflows.lifecycle_growth_values import ARTIFACT_KEYS, PREPARED_STATUS
import test_lifecycle_growth_contracts as legacy_contracts
import test_lifecycle_growth_readiness as legacy_readiness


MODULE = "omh.workflows.lifecycle_growth_launch"


@runtime_checkable
class FoundationAPI(Protocol):
    def __call__(self, *args: object, **kwargs: object) -> dict[str, object]: ...


@runtime_checkable
class FoundationQA(Protocol):
    def run_case(self, case_id: str) -> dict[str, object]: ...


def is_mapping(value: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(value, Mapping)


def is_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def record(value: object) -> dict[str, object]:
    if not is_mapping(value):
        raise AssertionError("expected a metadata record")
    result: dict[str, object] = {}
    for key, entry in value.items():
        if not isinstance(key, str):
            raise AssertionError("expected string metadata keys")
        result[key] = entry
    return result


def audience_rules(result: Mapping[str, object]) -> list[dict[str, object]]:
    values = result["rules"]
    if not is_list(values):
        raise AssertionError("expected audience rule list")
    return [record(value) for value in values]


def readiness_fixture():
    # Local subclasses expose existing fixtures without collecting their tests.
    class Fixture(legacy_readiness.LifecycleGrowthReadinessTests):
        def experiment(self) -> dict[str, object]:
            return self._experiment()

        def readout(self, **changes: object) -> dict[str, object]:
            return self._readout(**changes)
    return Fixture()


def launch_artifacts() -> dict[str, dict[str, object]]:
    class Fixture(legacy_contracts.LifecycleGrowthContractTests):
        def records(self) -> dict[str, dict[str, object]]:
            return self._launch_artifacts()
    return Fixture().records()


def rule(**changes: object) -> dict[str, object]:
    result: dict[str, object] = {
        "rule_ref": "rule_first", "evaluation_domain_ref": "domain_accounts",
        "bucketing_domain_ref": "bucket_accounts", "bucketing_subject": "person",
        "condition_refs": [], "rollout_share": 100, "result_kind": "on", "variant_ref": None,
    }
    result.update(changes)
    return result


def audience(**changes: object) -> dict[str, object]:
    result: dict[str, object] = {
        "lifecycle_growth_id": "launch_a", "evaluation_semantics": "first_match",
        "rules": [rule(), rule(rule_ref="rule_second", condition_refs=["condition_paid"], rollout_share=50)],
        "holdout_exclusion_share": 10,
    }
    result.update(changes)
    return result


def promotion(**changes: object) -> dict[str, object]:
    result: dict[str, object] = {
        "lifecycle_growth_id": "launch_a", "source_environment_ref": "stage",
        "target_environment_ref": "production", "dependency_refs_satisfied": ["dep_a"],
        "dependency_refs_to_create": ["dep_b"], "schedule_refs": ["schedule_a"],
        "approvals": {"carry_dependencies": False, "carry_schedules": False},
        "safety": {"workflow_content_state": "development_draft",
                   "mutation_route": "development_draft_then_promotion",
                   "promotion_decision_state": "approved", "promotion_result_state": "not_observed"},
    }
    result.update(changes)
    return result


class LifecycleGrowthUpstreamTests(unittest.TestCase):
    def api(self, name: str) -> FoundationAPI:
        self.assertIsNotNone(importlib.util.find_spec(MODULE), f"missing lifecycle foundation API: {name}")
        function: object = getattr(importlib.import_module(MODULE), name, None)
        if not isinstance(function, FoundationAPI) or not callable(function):
            self.fail(f"missing lifecycle foundation API: {name}")
        return function

    def test_l1_ordered_same_domain_catch_all(self):
        build = self.api("build_launch_audience_review")
        source = audience()
        before = deepcopy(source)
        result = build(**source)
        self.assertEqual([r["reachable"] for r in audience_rules(result)], [True, False])
        self.assertEqual(result["verdict"], "HOLD")
        self.assertEqual(result["unreachable_rule_refs"], ["rule_second"])
        self.assertEqual(source, before)
        for subject in ("person", "group", "device"):
            with self.subTest(subject=subject):
                rules = [rule(bucketing_subject=subject), rule(rule_ref="later", bucketing_subject=subject)]
                self.assertFalse(audience_rules(build(**audience(rules=rules)))[1]["reachable"])

    def test_l1_partial_conditional_and_different_domains_do_not_shadow(self):
        build = self.api("build_launch_audience_review")
        for first in (rule(rollout_share=99), rule(condition_refs=["condition_a"])):
            with self.subTest(first=first):
                result = build(**audience(rules=[first, rule(rule_ref="later")]))
                self.assertTrue(audience_rules(result)[1]["reachable"])
        for field, value in (("evaluation_domain_ref", "domain_b"),
                             ("bucketing_domain_ref", "bucket_b"), ("bucketing_subject", "group")):
            with self.subTest(field=field):
                result = build(**audience(rules=[rule(), rule(rule_ref="later", **{field: value})]))
                self.assertTrue(audience_rules(result)[1]["reachable"])

    def test_l1_unknown_semantics_or_domains_are_unknown_hold(self):
        build = self.api("build_launch_audience_review")
        unknown = build(**audience(evaluation_semantics="unknown"))
        self.assertEqual([r["reachable"] for r in audience_rules(unknown)], [None, None])
        self.assertEqual(unknown["verdict"], "HOLD")
        for field in ("evaluation_domain_ref", "bucketing_domain_ref"):
            with self.subTest(field=field):
                result = build(**audience(rules=[rule(**{field: None}), rule(rule_ref="later")]))
                self.assertIsNone(audience_rules(result)[0]["reachable"])
                self.assertTrue(audience_rules(result)[1]["reachable"])
                self.assertEqual(result["verdict"], "HOLD")

    def test_l2_subject_variant_split_partial_and_exclusion_are_configuration(self):
        build = self.api("build_launch_audience_review")
        for subject, kind, share in product(("person", "group", "device"), ("on", "variant", "split"), (0, 12.5, 100)):
            with self.subTest(subject=subject, kind=kind, share=share):
                item = rule(bucketing_subject=subject, rollout_share=share, result_kind=kind,
                            variant_ref="variant_a" if kind == "variant" else None)
                result = build(**audience(rules=[item]))
                for key, value in item.items():
                    self.assertEqual(audience_rules(result)[0][key], value)
                self.assertEqual(result["holdout_exclusion_share"], 10)
                self.assertEqual(result["status"], PREPARED_STATUS)
                self.assertEqual(result["schema_version"], "launch_audience_review/v1")
                self.assertFalse({"displayed_count", "actual_exposure_count", "exposure_evidence_refs"} & result.keys())

    def test_l2_bounded_metadata_and_malformed_audiences_rejected(self):
        build = self.api("build_launch_audience_review")
        bad_rules = [None, {}, rule(unknown="raw"), rule(condition_refs="condition_a"),
                     rule(condition_refs=["ref"] * 9), rule(rule_ref="https://private.example"),
                     rule(rule_ref=123), rule(bucketing_subject="tenant"),
                     rule(result_kind="variant"), rule(result_kind="on", variant_ref="variant_a"),
                     rule(result_kind="invented"), rule(evaluation_domain_ref=""),
                     rule(bucketing_domain_ref={}), rule(condition_refs=[None])]
        for value in (-1, 101, True, "50", float("nan"), float("inf"), None):
            bad_rules.append(rule(rollout_share=value))
        for bad in bad_rules:
            with self.subTest(rule=bad), self.assertRaises(ValueError):
                _ = build(**audience(rules=[bad]))
        invalid: list[dict[str, object]] = [{"rules": None}, {"rules": "raw"}, {"rules": [rule()] * 33},
                        {"rules": [rule(), rule()]}, {"evaluation_semantics": []},
                        {"holdout_exclusion_share": True}, {"holdout_exclusion_share": 101},
                        {"lifecycle_growth_id": "x" * 1000}]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                _ = build(**audience(**changes))
        self.assertEqual(len(audience_rules(build(**audience(rules=[rule(rule_ref=f"r_{n}", condition_refs=["ref"] * 8) for n in range(32)])))), 32)

    def test_l3_disabled_promotion_carries_only_approved_to_create_refs(self):
        build = self.api("build_launch_promotion_preflight")
        for dependencies, schedules in product((False, True), repeat=2):
            with self.subTest(dependencies=dependencies, schedules=schedules):
                source = promotion(approvals={"carry_dependencies": dependencies, "carry_schedules": schedules})
                before = deepcopy(source)
                result = build(**source)
                self.assertEqual(result["schema_version"], "launch_promotion_preflight/v1")
                self.assertEqual(result["target_enabled_state"], "disabled")
                self.assertEqual(result["dependency_refs_satisfied"], ["dep_a"])
                self.assertEqual(result["dependency_refs_to_create"], ["dep_b"])
                self.assertEqual(result["carried_dependency_refs"], ["dep_b"] if dependencies else [])
                self.assertEqual(result["carried_schedule_refs"], ["schedule_a"] if schedules else [])
                self.assertEqual(result["verdict"], "READY" if dependencies else "HOLD")
                self.assertEqual(bool(result["warnings"]), not dependencies)
                self.assertEqual(result["status"], PREPARED_STATUS)
                self.assertEqual(source, before)

    def test_l3_carry_approval_does_not_bypass_existing_safety_gate(self):
        build = self.api("build_launch_promotion_preflight")
        for safety in (None, {}, {"workflow_content_state": "production_read_only",
                                   "mutation_route": "development_draft_then_promotion",
                                   "promotion_decision_state": "approved", "promotion_result_state": "not_observed"}):
            with self.subTest(safety=safety):
                result = build(**promotion(safety=safety, approvals={"carry_dependencies": True, "carry_schedules": True}))
                self.assertEqual(result["verdict"], "HOLD")
                self.assertEqual(record(result["promotion_gate"])["verdict"], "HOLD")
        result = build(**promotion(dependency_refs_to_create=[]))
        self.assertEqual(result["verdict"], "READY")
        self.assertEqual(result["warnings"], [])

    def test_l3_malformed_approvals_and_dependencies_rejected(self):
        build = self.api("build_launch_promotion_preflight")
        invalid: list[dict[str, object]] = [{"approvals": None}, {"approvals": {}},
                        {"approvals": {"carry_dependencies": 1, "carry_schedules": False}},
                        {"approvals": {"carry_dependencies": True, "carry_schedules": True, "extra": True}},
                        {"dependency_refs_to_create": ["dep_a"]}, {"dependency_refs_satisfied": ["ref"] * 9},
                        {"schedule_refs": "schedule"}, {"target_environment_ref": "https://private.example"},
                        {"safety": []}]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                _ = build(**promotion(**changes))

    def test_l4_cleanup_requires_complete_evidence_and_satisfied_rollback(self):
        build = self.api("build_launch_graduation_check")
        for rollout, refs, rollback in product(("complete", "partial", "unknown"), ([], ["evidence_rollout"]), ("satisfied", "unsatisfied", "unknown")):
            with self.subTest(rollout=rollout, refs=refs, rollback=rollback):
                result = build(lifecycle_growth_id="launch_a", rollout_observed_state=rollout,
                               evidence_refs=refs, rollback_conditions_state=rollback)
                ready = rollout == "complete" and bool(refs) and rollback == "satisfied"
                self.assertEqual(result["schema_version"], "launch_graduation_check/v1")
                self.assertEqual(result["cleanup_action"], "proposed" if ready else "not_proposed")
                self.assertEqual(result["verdict"], "READY" if ready else "HOLD")
                self.assertEqual(bool(result["reason_codes"]), not ready)
                self.assertEqual(result["evidence_refs"], refs)
                self.assertEqual(result["status"], PREPARED_STATUS)
                self.assertNotIn("gate_deleted", result)

    def test_l4_invalid_graduation_metadata_rejected(self):
        build = self.api("build_launch_graduation_check")
        values = {"lifecycle_growth_id": "launch_a", "rollout_observed_state": "complete",
                  "evidence_refs": ["evidence_a"], "rollback_conditions_state": "satisfied"}
        for changes in ({"rollout_observed_state": "prepared"}, {"rollback_conditions_state": True},
                        {"evidence_refs": ["ref"] * 9}, {"evidence_refs": ["https://private.example"]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                _ = build(**(values | changes))

    def test_l5_context_reasons_are_distinct_and_never_runtime_outage(self):
        overlay = self.api("lifecycle_growth_evaluation_context")
        for reference, baseline, count in product(("resolved", "deleted", "unknown"), ("observed", "absent", "unknown"), (0, 6)):
            with self.subTest(reference=reference, baseline=baseline, count=count):
                context = {"experiment_reference_state": reference, "baseline_exposure_state": baseline}
                expected: list[str] = []
                if reference != "resolved":
                    expected.append("experiment_reference_" + reference)
                if baseline != "observed":
                    expected.append("baseline_exposure_" + baseline)
                if count == 0:
                    expected.append("exposure_absent")
                result = overlay(context, displayed_count=count)
                self.assertEqual(result["evidence_reason_codes"], expected)
                self.assertEqual(result["blocked"], reference == "deleted")
                if expected:
                    self.assertEqual(result["disposition"], "insufficient_data")
                    self.assertEqual(result["interpretation_state"], "HOLD")
                else:
                    self.assertNotIn("disposition", result)
                    self.assertNotIn("interpretation_state", result)
        self.assertEqual(overlay(None, displayed_count=0), {})

    def test_l5_invalid_context_is_value_error_not_outage(self):
        overlay = self.api("lifecycle_growth_evaluation_context")
        invalid: list[object] = [False, [], "raw", {}, {"experiment_reference_state": "deleted"},
                        {"experiment_reference_state": [], "baseline_exposure_state": "observed"},
                        {"experiment_reference_state": "resolved", "baseline_exposure_state": "multiple"},
                        {"experiment_reference_state": "resolved", "baseline_exposure_state": "observed", "baselines": [1, 2]}]
        for context in invalid:
            with self.subTest(context=context), self.assertRaises(ValueError):
                _ = overlay(context, displayed_count=6)
        for count in (-1, True, "6"):
            with self.subTest(count=count), self.assertRaises(ValueError):
                _ = overlay({"experiment_reference_state": "resolved", "baseline_exposure_state": "observed"}, displayed_count=count)

    def test_l5_existing_zero_exposure_and_runtime_characterization(self):
        fixture = readiness_fixture()
        cases: list[dict[str, object]] = [{"displayed_count": 0, "acted_count": 0, "outcome_count": 0, "actual_exposure_evidence_refs": []},
                        {"runtime_days_observed": 13}]
        for changes in cases:
            with self.subTest(changes=changes):
                result = contracts.evaluate_lifecycle_growth(fixture.experiment(), fixture.readout(**changes))
                self.assertEqual(result["disposition"], "insufficient_data")
                self.assertEqual(result["interpretation_state"], "HOLD")

    def test_l6_existing_closed_artifacts_and_evaluation_characterization(self):
        fixture = readiness_fixture()
        records = launch_artifacts()
        records["readout"] = fixture.readout()
        self.assertEqual(len(ARTIFACT_KEYS), 6)
        for record in records.values():
            schema = record["schema_version"]
            assert isinstance(schema, str)
            self.assertEqual(set(record), ARTIFACT_KEYS[schema])
            self.assertEqual(contracts.validate_lifecycle_growth_artifact(record), [])
            self.assertTrue(contracts.validate_lifecycle_growth_artifact(dict(record, evaluation_context={})))
        result = contracts.evaluate_lifecycle_growth(fixture.experiment(), fixture.readout())
        self.assertEqual(set(result), {"schema_version", "interpretation_state", "disposition", "assignment_unit", "exposure_unit", "actual_exposure_count", "delivery_count", "runtime_days_observed", "artifact_errors", "claim_boundary"})
        self.assertEqual(result["disposition"], "ship")
        self.assertEqual((result["delivery_count"], result["actual_exposure_count"]), (7, 6))
        self.assertEqual((result["assignment_unit"], result["exposure_unit"]), ("account", "account"))
        self.assertEqual(result["artifact_errors"], [])

    def test_l6_context_overlay_never_upgrades_existing_hold(self):
        overlay = self.api("lifecycle_growth_evaluation_context")
        fixture = readiness_fixture()
        for changes in ({"instrumentation_state": "broken"}, {"runtime_days_observed": 13}, {"guardrail_state": "failed"}):
            with self.subTest(changes=changes):
                base = contracts.evaluate_lifecycle_growth(fixture.experiment(), fixture.readout(**changes))
                result = base | overlay({"experiment_reference_state": "resolved", "baseline_exposure_state": "observed"}, displayed_count=6)
                self.assertEqual(result["disposition"], base["disposition"])
                self.assertEqual(result["interpretation_state"], "HOLD")
                self.assertEqual(base | overlay(None, displayed_count=6), base)

    def test_l6_qa_foundation_never_claims_integrated_pass(self):
        from five_issue_cases import lifecycle as qa
        assert isinstance(qa, FoundationQA)
        for case in ("L1", "L2", "L3", "L4", "L5", "L6", "L7"):
            with self.subTest(case=case):
                result = qa.run_case(case)
                self.assertFalse(result["pass"])
                self.assertEqual(result["case"], case)
                self.assertEqual(record(result["provenance"])["scope"], "foundation")
                self.assertTrue(result["blocked_reason"])
                self.assertEqual(record(result["cleanup"])["owned_resources"], [])
                self.assertTrue(record(result["cleanup"])["verified_absent"])


if __name__ == "__main__":
    _ = unittest.main()
