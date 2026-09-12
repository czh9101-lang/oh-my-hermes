"""Synthetic adapter inputs; no provider observation is performed."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TypedDict, TypeGuard

from omh.system.paths import OmhPaths
from omh.workflows.lifecycle_growth_launch import build_launch_audience_review
from omh.workflows.workflow_artifact_operations import run_workflow_artifact_operation


class ConfigurationRequest(TypedDict):
    artifacts: dict[str, dict[str, object]]
    metadata: dict[str, object]
    observations: list[dict[str, object]]
    predecessor_seal: object


def is_mutable_record(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def at(value: object, path: str = "") -> dict[str, object]:
    """Navigate mutable synthetic JSON for adversarial fixture changes."""
    current = value
    for key in path.split(".") if path else ():
        if isinstance(current, list):
            current = current[int(key)]
        else:
            assert is_mutable_record(current)
            current = current[key]
    assert is_mutable_record(current)
    return current


def sequence(value: object) -> list[object]:
    assert isinstance(value, list)
    return value


def operation(name: str, payload) -> dict[str, object]:
    with TemporaryDirectory(prefix="omh-configuration-test-") as scratch:
        paths = OmhPaths(Path(scratch) / "omh", Path(scratch) / "hermes")
        return at(run_workflow_artifact_operation(paths, "lifecycle-growth", name, payload)["result"])


def audience_review(lifecycle_id: str) -> dict[str, object]:
    return build_launch_audience_review(
        lifecycle_growth_id=lifecycle_id, evaluation_semantics="first_match",
        holdout_exclusion_share=10,
        rules=[{"rule_ref": "rule_first", "evaluation_domain_ref": "domain_accounts_v1",
                "bucketing_domain_ref": "bucket_accounts_v1", "bucketing_subject": "group",
                "condition_refs": ["condition_eligible_v1", "condition_consent_v1"],
                "rollout_share": 50, "result_kind": "variant", "variant_ref": "variant_treatment_v1"},
               {"rule_ref": "rule_second", "evaluation_domain_ref": "domain_accounts_v1",
                "bucketing_domain_ref": "bucket_accounts_v1", "bucketing_subject": "group",
                "condition_refs": [], "rollout_share": 25, "result_kind": "on", "variant_ref": None}],
    )


def configuration_input(payload: dict[str, dict[str, object]], *, observed: bool = True) -> ConfigurationRequest:
    artifacts = deepcopy(payload)
    artifacts.pop("configuration_binding", None)
    lifecycle_id = artifacts["experiment"]["lifecycle_growth_id"]
    assert isinstance(lifecycle_id, str)
    artifacts.setdefault("audience_review", audience_review(lifecycle_id))
    return {"artifacts": artifacts, "metadata": {
        "identity_state": "observed" if observed else "unknown",
        "configuration_ref": "configuration_fixture", "revision_ref": "revision_launch_v1",
        "observed_at": "2026-09-01T09:00:00Z" if observed else None,
        "evidence_refs": ["receipt_launch_v1"] if observed else [],
    }, "observations": [], "predecessor_seal": None}


def bind(payload: dict[str, dict[str, object]]) -> dict[str, dict[str, object]]:
    request = configuration_input(payload)
    # Compute reviewed identity before an external adapter supplies its receipt.
    prepared = operation("configuration", {**request, "metadata": {
        **request["metadata"], "identity_state": "unknown", "observed_at": None, "evidence_refs": []}})
    request["observations"] = [{"kind": "launch", "observed_at": "2026-09-01T09:00:00Z",
        "evidence_ref": "receipt_launch_v1", "configuration_digest": at(prepared, "identity")["configuration_digest"],
        "revision_ref": "revision_launch_v1"}]
    result = deepcopy(payload)
    result["audience_review"] = request["artifacts"]["audience_review"]
    result["configuration_binding"] = operation("configuration", request)
    return result
