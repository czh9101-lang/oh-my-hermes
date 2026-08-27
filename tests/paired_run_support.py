from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

from omh.coding.routing_observation import (
    authenticate_child_observation,
    build_routing_observation,
)
from omh.quality.paired_run_values import exposure_digest


def paired_evaluation_binding(
    *,
    task_id: str,
    criteria_ref: str,
    input_digest: str,
    arm: str,
    executor: str,
    model: str,
    exposed_skills: tuple[str, ...],
    execution_revision: str,
    timeout_seconds: int = 900,
) -> dict[str, str | int]:
    return {
        "task_id": task_id,
        "acceptance_criteria_ref": criteria_ref,
        "input_digest": input_digest,
        "arm": arm,
        "executor": executor,
        "model": model,
        "exposure_digest": exposure_digest(tuple(sorted(exposed_skills))),
        "execution_revision": execution_revision,
        "timeout_seconds": timeout_seconds,
    }


def write_observed_receipt(
    omh_home: Path,
    run_id: str,
    status: str = "completed",
    observed_at: str = "2026-08-27T00:00:00Z",
    *,
    evaluation_binding: dict[str, str | int] | None = None,
) -> Path:
    root = omh_home / "coding" / "hermes-child"
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    observation = build_routing_observation(
        route={
            "selected_model": "fixture/model",
            "selected_reasoning_effort": "high",
            "role": "agent_maintainer",
            "executor_profile": "hermes_child",
            "chain": [],
        },
        child_dispatch=authenticate_child_observation(
            {"status": status, "run_id": run_id}
        ),
        run_id=run_id,
    )
    observation["observed_at"] = observed_at
    if evaluation_binding is not None:
        observation["evaluation_binding"] = evaluation_binding
    canonical = json.dumps(
        observation,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    key = b"k" * 32
    (root / ".observation-hmac-key").write_bytes(key)
    (run_dir / "observation.json").write_text(json.dumps(observation), encoding="utf-8")
    signature = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    (run_dir / "observation.signature.json").write_text(
        json.dumps(
            {
                "schema_version": "hermes_child_observation_signature/v1",
                "hmac_sha256": signature,
            }
        ),
        encoding="utf-8",
    )
    return run_dir


def resign_observation(run_dir: Path) -> None:
    observation = json.loads((run_dir / "observation.json").read_text(encoding="utf-8"))
    canonical = json.dumps(
        observation,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    key = (run_dir.parent / ".observation-hmac-key").read_bytes()
    signature = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    (run_dir / "observation.signature.json").write_text(
        json.dumps(
            {
                "schema_version": "hermes_child_observation_signature/v1",
                "hmac_sha256": signature,
            }
        ),
        encoding="utf-8",
    )
