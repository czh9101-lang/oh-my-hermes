"""Synthetic metric matrix layered on the real #1503 fixture chain, not unittest."""
from collections.abc import Callable
from copy import deepcopy
import json
from pathlib import Path
from typing import Final

from omh.workflows.lifecycle_growth_configuration_values import artifact_digest, record
from omh.workflows.lifecycle_growth_metrics import build_metric_coverage
from omh.workflows.lifecycle_growth_metric_values import MetricResult, parse_result

METRIC_SCENARIOS: Final = (
    ("metric-complete", "ship", None),
    ("metric-missing", "insufficient_data", "metric_result_missing"),
    ("metric-errored", "insufficient_data", "metric_result_errored"),
    ("metric-indeterminate", "insufficient_data", "metric_result_indeterminate"),
    ("metric-primary-indeterminate", "insufficient_data", "metric_result_indeterminate"),
    ("metric-duplicate", None, None), ("metric-unexpected", None, None), ("metric-role", None, None),
    ("metric-raw-error", None, None),
    ("metric-aggregate", "review", "metric_aggregate_mismatch"),
    ("metric-composite-complete", "ship", None),
    ("metric-composite-dropped", "insufficient_data", "metric_result_missing"),
    ("metric-composite-drift", "insufficient_data", "metric_composite_members_mismatch"),
    ("metric-composite-unresolved", "insufficient_data", "metric_composite_members_mismatch"),
    ("metric-composite-unexpected", None, None),
    ("metric-rollback-missing", "rollback", "metric_result_missing"),
    ("metric-rollback-errored", "rollback", "metric_result_errored"),
    ("metric-rollback-drift", "rollback", "configuration_drift"),
    ("metric-rollback-identity-absent", "rollback", "configuration_identity_missing"),
    ("metric-legacy", "insufficient_data", "metric_coverage_legacy"),
)


def load(path: Path) -> dict[str, object]:
    return dict(record(json.loads(path.read_text(encoding="utf-8"))))


def metric(ref: object, role: str, state: str) -> MetricResult:
    return parse_result({"metric_ref": ref, "role": role, "state": state,
            "evidence_refs": [] if state == "missing" else ["receipt_metric_v1"],
            "error_category": "timeout" if state == "errored" else None})


def metric_request(payload: dict[str, object]) -> dict[str, object]:
    plan = record(payload["experiment"])
    guards = plan["guardrail_metric_refs"]
    if not isinstance(guards, list):
        raise ValueError("fixture_guardrails_invalid")
    return {"experiment": plan, "readout": payload["readout"], "configuration_binding": payload["configuration_binding"],
            "results": [metric(plan["primary_metric_ref"], "primary", "improved"),
                        *[metric(ref, "guardrail", "passed") for ref in guards]]}


def metric_fixtures(output: Path, write_json: Callable[[Path, object], None]) -> None:
    """Preserve predecessor identities, adding only explicit synthetic observations."""
    matching = load(output / "matching.json")
    request = metric_request(matching)
    coverage = build_metric_coverage(request)
    write_json(output / "metrics-input.json", request)
    write_json(output / "metric-coverage.json", coverage)
    write_json(output / "metric-legacy.json", matching)
    complete = {**matching, "metric_coverage": coverage}
    write_json(output / "metric-complete.json", complete)
    # Migrate positive configuration scenarios explicitly; do not weaken their gates.
    for name in ("matching", "prepare", "drift-share", "drift-rule", "analysis-mismatch", "unknown", "rollback-drift", "successor-drift"):
        payload = load(output / (name + ".json"))
        observed = build_metric_coverage(metric_request(payload))
        write_json(output / (name + ".json"), {**payload, "metric_coverage": observed})
    for state in ("missing", "errored", "indeterminate"):
        changed = deepcopy(coverage)
        guard = changed["results"].pop()
        if state != "missing":
            changed["results"].append(metric(guard["metric_ref"], "guardrail", state))
        write_json(output / ("metric-" + state + ".json"), {**matching, "metric_coverage": changed})
    primary_unknown = deepcopy(coverage)
    primary_unknown["results"][0] = metric(coverage["results"][0]["metric_ref"], "primary", "indeterminate")
    write_json(output / "metric-primary-indeterminate.json", {**matching, "metric_coverage": primary_unknown})
    for name, changes in (("unexpected", {"metric_ref": "metric_unexpected"}), ("role", {"role": "guardrail"}),
                          ("raw-error", {"raw_error": "PRIVATE_SENTINEL"}), ("aggregate", {"state": "unchanged"})):
        changed = deepcopy(dict(coverage))
        rows = [dict(item) for item in coverage["results"]]
        changed["results"] = [{**rows[0], **changes}, *rows[1:]]
        write_json(output / ("metric-" + name + ".json"), {**matching, "metric_coverage": changed})
    write_json(output / "metric-duplicate.json", {**matching, "metric_coverage": {**coverage, "results": [*coverage["results"], coverage["results"][0]]}})
    for state in ("missing", "errored", "drift"):
        changed = deepcopy(coverage)
        changed["results"][0] = metric(coverage["results"][0]["metric_ref"], "primary", "errored" if state == "drift" else state)
        changed["results"][1] = metric(coverage["results"][1]["metric_ref"], "guardrail", "failed")
        payload = load(output / "drift-share.json") if state == "drift" else matching
        write_json(output / ("metric-rollback-" + state + ".json"), {**payload, "metric_coverage": changed})
    harmful = load(output / "metric-rollback-missing.json")
    write_json(output / "metric-rollback-identity-absent.json", {key: value for key, value in harmful.items() if key != "configuration_binding"})
    parent = coverage["results"][1]["metric_ref"]
    projection = {"schema_version": "lifecycle_metric_member_set/v1", "metric_ref": parent,
                  "member_refs": ["metric_member_a", "metric_member_b"]}
    member_set = {**projection, "member_set_digest": artifact_digest(projection),
                  "observed_at": "2026-09-01T09:00:00Z", "evidence_refs": ["receipt_members_v1"]}
    plan_binding = {"schema_version": "lifecycle_growth_metric_plan_binding/v1", "experiment_digest": coverage["experiment_digest"],
                    "configuration_digest": coverage["configuration_digest"], "composite_memberships": [member_set]}
    members = [metric("metric_member_a", "guardrail", "passed"), metric("metric_member_b", "guardrail", "passed")]
    composite = build_metric_coverage({**request, "metric_plan_binding": plan_binding,
                                      "composites": [{"member_set": member_set, "results": members}]})
    for name in ("complete", "dropped", "drift", "unresolved"):
        changed = deepcopy(composite)
        if name == "dropped": changed["composites"][0]["results"].pop()
        if name == "drift":
            replacement = {**projection, "member_refs": ["metric_member_a", "metric_member_c"]}
            changed["composites"][0]["member_set"].update(member_refs=["metric_member_a", "metric_member_c"],
                member_set_digest=artifact_digest(replacement))
            changed["composites"][0]["results"][1] = metric("metric_member_c", "guardrail", "passed")
        if name == "unresolved": changed["composites"] = []
        write_json(output / ("metric-composite-" + name + ".json"),
                   {**matching, "metric_plan_binding": plan_binding, "metric_coverage": changed})
    unexpected = deepcopy(composite)
    unexpected["composites"][0]["member_set"].update(metric_ref="unconfigured_parent",
        member_set_digest=artifact_digest({**projection, "metric_ref": "unconfigured_parent"}))
    write_json(output / "metric-composite-unexpected.json", {**matching, "metric_plan_binding": plan_binding, "metric_coverage": unexpected})
    prepare = load(output / "prepare.json")
    write_json(output / "metric-prepare-missing.json", {**prepare, "metric_coverage": load(output / "metric-missing.json")["metric_coverage"]})
