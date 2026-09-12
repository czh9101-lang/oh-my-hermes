"""Synthetic metric observations bound to the committed configuration fixture."""
from copy import deepcopy
import json
from pathlib import Path
from typing import TypeGuard

from _lifecycle_configuration import at, bind, is_mutable_record, operation, sequence
from omh.workflows.lifecycle_growth_configuration_values import artifact_digest

BASE = Path(__file__).parent / "fixtures/lifecycle_growth_configuration/base.json"


def is_rows(value: object) -> TypeGuard[list[dict[str, object]]]:
    return isinstance(value, list) and all(is_mutable_record(item) for item in value)


def rows(value: object) -> list[dict[str, object]]:
    assert is_rows(value)
    return value


def configured():
    base = json.loads(BASE.read_text(encoding="utf-8"))
    return bind({key: base[key] for key in ("experiment", "readout", "exposure_evidence", "audience_review")})


def result(metric_ref: object, role: str, state: str) -> dict[str, object]:
    return {"metric_ref": metric_ref, "role": role, "state": state,
            "evidence_refs": [] if state == "missing" else ["receipt_metric_v1"],
            "error_category": "timeout" if state == "errored" else None}


def request(payload):
    plan = payload["experiment"]
    return {"experiment": plan, "readout": payload["readout"],
            "configuration_binding": payload["configuration_binding"],
            "results": [result(plan["primary_metric_ref"], "primary", "improved"),
                        *[result(ref, "guardrail", "passed") for ref in plan["guardrail_metric_refs"]]]}


def cover(payload):
    return {**payload, "metric_coverage": operation("metrics", request(payload))}


def complete():
    return cover(configured())


def member_set(parent, members):
    projection = {"schema_version": "lifecycle_metric_member_set/v1", "metric_ref": parent,
                  "member_refs": sorted(members)}
    return {**projection, "member_set_digest": artifact_digest(projection),
            "observed_at": "2026-09-01T09:00:00Z", "evidence_refs": ["receipt_members_v1"]}


def composite_request():
    payload = configured()
    source = request(payload)
    parent = sequence(payload["experiment"]["guardrail_metric_refs"])[0]
    members = member_set(parent, ["metric_member_a", "metric_member_b"])
    binding = {"schema_version": "lifecycle_growth_metric_plan_binding/v1",
               "experiment_digest": artifact_digest(payload["experiment"]),
               "configuration_digest": at(payload, "configuration_binding.seal.observation")["configuration_digest"],
               "composite_memberships": [members]}
    source["metric_plan_binding"] = binding
    source["composites"] = [{"member_set": deepcopy(members),
                             "results": [result(ref, "guardrail", "passed") for ref in sequence(members["member_refs"])]}]
    payload["metric_plan_binding"] = binding
    return payload, source
