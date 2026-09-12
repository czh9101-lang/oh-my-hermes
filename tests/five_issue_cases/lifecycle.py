"""Real current-source CLI scenarios for L1-L6, not native/provider execution.

All inputs are synthetic metadata. The producer retains deciding fields and
exit codes only, and removes its isolated homes/input files after reaping CLI
children. L7 checks the parsed source-review registry fields and gates only.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import TypeGuard

from . import CaseResult, JsonValue, unavailable_case


ROOT = Path(__file__).resolve().parents[2]
decode_json: Callable[[str], object] = json.loads
CLI = [sys.executable, "-P", "-c",
       "from _local_package import load_local_package; load_local_package(); "
       + "from omh.cli import main; raise SystemExit(main())"]


def _is_mapping(value: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(value, Mapping)


def _is_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if _is_list(value):
        return [_json_value(item) for item in value]
    if _is_mapping(value):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("non_string_json_key")
            result[key] = _json_value(item)
        return result
    raise ValueError("non_json_value")


def _record(value: object) -> dict[str, JsonValue]:
    result = _json_value(value)
    if not isinstance(result, dict):
        raise ValueError("expected_json_object")
    return result


def _rule(**changes: JsonValue) -> dict[str, JsonValue]:
    return {"rule_ref": "first", "evaluation_domain_ref": "people",
            "bucketing_domain_ref": "people_bucket", "bucketing_subject": "person",
            "condition_refs": [], "rollout_share": 100, "result_kind": "on",
            "variant_ref": None, **changes}


def _audience(rules: list[JsonValue], *, semantics: str = "first_match") -> dict[str, JsonValue]:
    return {"lifecycle_growth_id": "launch_a", "evaluation_semantics": semantics,
            "rules": rules, "holdout_exclusion_share": 10}


def _promotion(**changes: JsonValue) -> dict[str, JsonValue]:
    return {"lifecycle_growth_id": "launch_a", "source_environment_ref": "stage",
            "target_environment_ref": "production", "dependency_refs_satisfied": ["dep_a"],
            "dependency_refs_to_create": ["dep_b"], "schedule_refs": ["schedule_a"],
            "approvals": {"carry_dependencies": False, "carry_schedules": False},
            "safety": {"workflow_content_state": "development_draft",
                       "mutation_route": "development_draft_then_promotion",
                       "promotion_decision_state": "approved", "promotion_result_state": "not_observed"},
            **changes}


def _readout_inputs() -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
    # Reuse synthetic artifact inputs, not assertions or a nested test execution.
    from test_lifecycle_growth_readiness import LifecycleGrowthReadinessTests

    class Fixture(LifecycleGrowthReadinessTests):
        def inputs(self) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
            return _record(self._experiment()), _record(self._readout())

    return Fixture().inputs()


class _Scenario:
    def __init__(self, home: Path, result: CaseResult) -> None:
        self.home: Path = home
        self.result: CaseResult = result
        self.exits: list[JsonValue] = []
        self.checks: dict[str, JsonValue] = {}

    def check(self, name: str, condition: bool) -> None:
        self.checks[name] = condition
        if not condition:
            raise AssertionError(name)

    def cli(self, operation: str, payload: JsonValue, *, file_input: bool = False,
            expected_exit: int = 0) -> dict[str, JsonValue]:
        text = json.dumps(payload, allow_nan=False)
        source = "-"
        if file_input:
            source = str(self.home / "input.json")
            _ = Path(source).write_text(text, encoding="utf-8")
        argv = [*CLI, "--omh-home", str(self.home / "omh"), "--hermes-home", str(self.home / "hermes"),
                "runtime", "workflow-artifact", "lifecycle-growth", operation, "--input", source]
        self.result["commands"].append(argv)
        environment = {**os.environ, "PYTHONPATH": str(ROOT / "tests"), "UV_NO_SYNC": "1",
                       "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(self.home),
                       "OMH_HOME": str(self.home / "omh"), "HERMES_HOME": str(self.home / "hermes"),
                       "OMH_OUTPUT": "json"}
        completed = subprocess.run(argv, cwd=ROOT, env=environment, input=text,
                                   capture_output=True, text=True, timeout=15, check=False)
        self.exits.append(completed.returncode)
        self.check(f"exit_{len(self.exits)}", completed.returncode == expected_exit)
        self.check(f"no_persistence_{len(self.exits)}",
                   not (self.home / "omh").exists() and not (self.home / "hermes").exists())
        if expected_exit:
            self.check(f"refusal_{len(self.exits)}", not completed.stdout and bool(completed.stderr)
                       and "Traceback" not in completed.stderr and "PRIVATE_INPUT_SENTINEL" not in completed.stderr)
            return {}
        self.check(f"stderr_empty_{len(self.exits)}", completed.stderr == "")
        envelope = _record(decode_json(completed.stdout))
        self.check(f"envelope_{len(self.exits)}", envelope["operation"] == operation
                   and envelope["workflow"] == "lifecycle-growth"
                   and envelope["schema_version"] == "workflow_artifact_operation_result/v1")
        return _record(envelope["result"])

    def audience(self) -> None:
        result = self.cli("audience", _audience([_rule(), _rule(rule_ref="later", condition_refs=["paid"], rollout_share=50)]), file_input=True)
        self.check("later_unreachable", result["unreachable_rule_refs"] == ["later"])
        self.check("configuration_only", result["status"] == "prepared_not_observed" and "actual_exposure_count" not in result)
        for index, first in enumerate((_rule(rollout_share=99), _rule(condition_refs=["paid"]), _rule(bucketing_domain_ref="other"))):
            control = self.cli("audience", _audience([first, _rule(rule_ref="later")]))
            self.check(f"not_shadowed_{index}", control["unreachable_rule_refs"] == [])
        unknown = self.cli("audience", _audience([_rule()], semantics="unknown"))
        self.check("unknown_hold", unknown["unknown_rule_refs"] == ["first"] and unknown["verdict"] == "HOLD")

    def subjects(self) -> None:
        for subject in ("person", "group", "device"):
            item = _rule(bucketing_subject=subject, rollout_share=50, result_kind="variant", variant_ref="treatment")
            result = self.cli("audience", _audience([item]))
            self.check(subject + "_preserved", result["rules"] == [dict(item, reachable=True)]
                       and result["holdout_exclusion_share"] == 10
                       and result["status"] == "prepared_not_observed" and "actual_exposure_count" not in result)
        experiment, readout = _readout_inputs()
        readout.update(displayed_count=0, acted_count=0, outcome_count=0,
                       actual_exposure_evidence_refs=[], disposition="insufficient_data")
        result = self.cli("evaluate", {"experiment": experiment, "readout": readout,
                                      "evaluation_context": {"experiment_reference_state": "resolved", "baseline_exposure_state": "observed"}})
        self.check("configuration_not_exposure", result["actual_exposure_count"] == 0 and result["delivery_count"] == 7
                   and result["disposition"] == "insufficient_data"
                   and result["evidence_reason_codes"] == ["exposure_evidence_missing", "exposure_absent", "configuration_identity_missing", "metric_coverage_legacy"])

    def promotion(self) -> None:
        for approved in (False, True):
            result = self.cli("promote", _promotion(approvals={"carry_dependencies": approved, "carry_schedules": approved}))
            self.check(f"disabled_{approved}", result["target_enabled_state"] == "disabled")
            self.check(f"carry_{approved}", result["carried_dependency_refs"] == (["dep_b"] if approved else [])
                       and result["carried_schedule_refs"] == (["schedule_a"] if approved else []))
            self.check(f"distinct_{approved}", result["dependency_refs_satisfied"] == ["dep_a"]
                       and result["dependency_refs_to_create"] == ["dep_b"])
            self.check(f"not_promoted_{approved}", _record(result["promotion_gate"])["promotion_result_state"] == "not_observed"
                       and result["status"] == "prepared_not_observed")
        held = self.cli("promote", _promotion(safety=None, approvals={"carry_dependencies": True, "carry_schedules": True}))
        self.check("carry_not_approval", held["verdict"] == "HOLD")

    def graduation(self) -> None:
        for index, (rollout, refs, rollback) in enumerate((
            ("complete", ["rollout_evidence"], "satisfied"), ("complete", [], "satisfied"),
            ("partial", ["rollout_evidence"], "satisfied"), ("complete", ["rollout_evidence"], "unsatisfied"),
        )):
            payload: dict[str, JsonValue] = {"lifecycle_growth_id": "launch_a", "rollout_observed_state": rollout,
                                              "evidence_refs": list(refs), "rollback_conditions_state": rollback}
            result = self.cli("graduate", payload)
            self.check(f"proposal_{index}", result["cleanup_action"] == ("proposed" if index == 0 else "not_proposed")
                       and result["status"] == "prepared_not_observed" and "gate_deleted" not in result)

    def context(self) -> None:
        experiment, readout = _readout_inputs()
        for reference, baseline, reason in (("deleted", "observed", "experiment_reference_deleted"),
                                            ("resolved", "absent", "baseline_exposure_absent"),
                                            ("unknown", "observed", "experiment_reference_unknown")):
            result = self.cli("evaluate", {"experiment": experiment, "readout": readout,
                                          "evaluation_context": {"experiment_reference_state": reference, "baseline_exposure_state": baseline}})
            self.check(reason, result["evidence_reason_codes"] == ["exposure_evidence_missing", reason, "configuration_identity_missing", "metric_coverage_legacy"]
                       and result["blocked"] is True
                       and result["disposition"] == "insufficient_data" and result["interpretation_state"] == "HOLD")
        _ = self.cli("evaluate", {"experiment": experiment, "readout": readout, "evaluation_context": {
            "experiment_reference_state": "resolved", "baseline_exposure_state": "multiple"}}, expected_exit=2)
        self.subjects()

    def compatibility(self) -> None:
        semantic = _record(decode_json((ROOT / "examples/workflow-artifacts/lifecycle-growth-build-semantic.json").read_text()))
        artifacts = self.cli("build", semantic, file_input=True)
        self.check("five_prepared", set(artifacts) == {"brief", "audience", "safety", "experiment", "handoff"})
        for name, artifact in artifacts.items():
            self.check(name + "_valid", self.cli("validate", artifact)["valid"] is True)
        self.check("unapproved_launch_hold", self.cli("prepare", artifacts)["verdict"] == "HOLD")
        experiment, readout = _readout_inputs()
        self.check("sixth_artifact_valid", self.cli("validate", readout)["valid"] is True)
        source: dict[str, JsonValue] = {"experiment": experiment, "readout": readout}
        old = self.cli("evaluate", source)
        self.check("absent_context_equal", self.cli("evaluate", dict(source, evaluation_context=None)) == old)
        self.check("readout_separate", self.cli("readout", readout)["actual_exposure_count"] == 6
                   and old["delivery_count"] == 7 and old["assignment_unit"] == "account" and old["exposure_unit"] == "account")
        self.check("prepared_action", _record(artifacts["handoff"])["timing_state"] == "not_scheduled")
        self.check("legacy_cannot_expand", old["disposition"] == "insufficient_data" and old["blocked"] is True)
        self.exposure()
        self.promotion()
        self.graduation()
        self.subjects()

    def exposure(self) -> None:
        from test_lifecycle_growth_exposure import exposure_inputs

        from _lifecycle_configuration import bind
        from _lifecycle_metrics import cover
        full = _record(cover(bind(exposure_inputs())))
        observed = self.cli("evaluate", full)
        self.check("observed_expansion", observed["disposition"] == "ship" and observed["blocked"] is False
                   and observed["populations"] == {"eligible": 10, "assigned": 8, "attempted": 8, "reached": 8, "converted": 1})
        cases: tuple[tuple[str, JsonValue], ...] = (("assigned_count", None), ("contact_pressure_state", "unknown"), ("overlap_state", "detected"),
                             ("population_reconciliation_state", "inconsistent"), ("channels", []))
        for field, value in cases:
            payload = _record(exposure_inputs())
            payload["exposure_evidence"] = dict(_record(payload["exposure_evidence"]), **{field: value})
            result = self.cli("evaluate", payload)
            self.check(field + "_holds", result["interpretation_state"] == "HOLD" and result["blocked"] is True)
        rollback = _record(exposure_inputs())
        rollback["readout"] = dict(_record(rollback["readout"]), guardrail_state="failed", disposition="rollback")
        del rollback["exposure_evidence"]
        self.check("rollback_preserved", self.cli("evaluate", rollback)["disposition"] == "rollback")


POSTHOG_REPO = "https://github.com/PostHog/posthog"
POSTHOG_REVIEWED_REF = "ae880d309f33eaf236cb4e46991f249a88e1c16e"
UNCHANGED_LIFECYCLE_ROWS: dict[str, tuple[str, str]] = {
    "https://github.com/growthbook/growthbook": ("2026-09-09", "095f61643e148f03ce0b442c78e6030c045f6d7b"),
    "https://github.com/dittofeed/dittofeed": ("2026-09-07", "52b2bee909744d07dd5d409fd3974d4b95c66766"),
    "https://github.com/novuhq/novu": ("2026-09-08", "c7bc772fc0b7722909ef1bdb9bcf04991996fdd8"),
}


def _registry_rows(registry: Path) -> list[dict[str, str]]:
    lines = registry.read_text(encoding="utf-8").splitlines()
    header = next(line for line in lines if line.startswith("| OMH skill |"))
    columns = [cell.strip() for cell in header.strip().strip("|").split("|")]
    rows: list[dict[str, str]] = []
    for line in lines:
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == len(columns):
            rows.append(dict(zip(columns, cells, strict=True)))
    return rows


def _run_l7_source_review() -> CaseResult:
    """L7: only the reviewed PostHog registry row advanced; no vendor code; docs gates hold.

    Machine-consumed fields only (registry ref/date, import scan, docs claims
    gate). The adoption/rejection prose is reviewed by read, not pinned here.
    """
    import re

    root = Path(__file__).resolve().parents[2]
    result = unavailable_case("L7", "scenario_incomplete")
    result["provenance"] = {"kind": "local", "scope": "surface", "native_required": False, "native_available": False}
    result["inputs_metadata"] = {"registry": "docs/SKILL-SOURCES.md", "reviewed_ref": POSTHOG_REVIEWED_REF, "provider_calls": 0}
    checks: dict[str, JsonValue] = {}
    exits: list[JsonValue] = []
    rows = [r for r in _registry_rows(root / "docs" / "SKILL-SOURCES.md") if r.get("OMH skill", "").startswith("`lifecycle-growth`")]
    by_repo = {r["Upstream repo"]: r for r in rows}
    posthog = by_repo.get(POSTHOG_REPO)
    checks["posthog_row_present"] = posthog is not None
    checks["posthog_ref_advanced"] = posthog is not None and posthog.get("reviewed_ref") == POSTHOG_REVIEWED_REF
    checks["posthog_reviewed_on_iso"] = posthog is not None and re.fullmatch(r"\d{4}-\d{2}-\d{2}", posthog.get("reviewed_on", "")) is not None
    checks["posthog_reviewed_on_after_prior_pin"] = posthog is not None and posthog.get("reviewed_on", "") > "2026-09-07"
    checks["sibling_rows_unchanged"] = all(
        (by_repo.get(repo, {}).get("reviewed_on"), by_repo.get(repo, {}).get("reviewed_ref")) == expected
        for repo, expected in UNCHANGED_LIFECYCLE_ROWS.items())
    # Adoption/exclusion rationale (docs/LIFECYCLE-GROWTH.md, the `ee/` exclusion
    # note) is reviewed by read, never pinned here: only machine-consumed values.
    offenders = [str(p.relative_to(root)) for p in sorted((root / "src").rglob("*.py"))
                 if any(line.lstrip().startswith(("import posthog", "from posthog")) for line in p.read_text(encoding="utf-8").splitlines())]
    checks["no_vendor_import"] = offenders == []
    argv = [*CLI, "docs", "claims", "--check", "--json"]
    result["commands"].append(list(argv))
    child = subprocess.run(argv, capture_output=True, text=True, timeout=120, cwd=root,
                           env=dict(os.environ, PYTHONPATH=str(ROOT / "tests"), PYTHONDONTWRITEBYTECODE="1", UV_NO_SYNC="1"))
    exits.append(child.returncode)
    checks["docs_claims_check"] = child.returncode == 0
    result["observations"] = {"checks": checks, "exit_codes": exits, "command_count": len(result["commands"]),
                              "provider_execution_observed": False}
    passed = all(v is True for v in checks.values())
    result["pass"] = passed
    result["blocked_reason"] = None if passed else "source_review_registry_mismatch"
    return result


def run_case(case_id: str) -> CaseResult:
    """Execute the real CLI on isolated local synthetic metadata and clean up."""
    scenarios = {"L1": "audience", "L2": "subjects", "L3": "promotion",
                 "L4": "graduation", "L5": "context", "L6": "compatibility"}
    if case_id == "L7":
        return _run_l7_source_review()
    if case_id not in scenarios:
        return unavailable_case(case_id, "unsupported_case")
    result = unavailable_case(case_id, "scenario_incomplete")
    result["provenance"]["scope"] = "surface"
    result["inputs_metadata"] = {"synthetic": True, "scenario": scenarios[case_id], "provider_calls": 0}
    with TemporaryDirectory(prefix="lifecycle-surface-") as temporary:
        home = Path(temporary)
        result["cleanup"]["owned_resources"] = [temporary]
        scenario = _Scenario(home, result)
        actions = {"L1": scenario.audience, "L2": scenario.subjects, "L3": scenario.promotion,
                   "L4": scenario.graduation, "L5": scenario.context, "L6": scenario.compatibility}
        try:
            actions[case_id]()
        except (AssertionError, ValueError, KeyError, OSError, subprocess.TimeoutExpired) as error:
            result["blocked_reason"] = "scenario_failed:" + type(error).__name__
        else:
            result["pass"] = True
            result["blocked_reason"] = None
        finally:
            result["observations"] = {"checks": scenario.checks, "exit_codes": scenario.exits,
                                      "command_count": len(result["commands"]), "provider_execution_observed": False}
    result["cleanup"]["removed_paths"] = [temporary]
    result["cleanup"]["verified_absent"] = not home.exists()
    if home.exists():
        result["pass"] = False
        result["blocked_reason"] = "cleanup_failed"
        result["cleanup"]["errors"] = ["temporary_root_remains"]
    return result
