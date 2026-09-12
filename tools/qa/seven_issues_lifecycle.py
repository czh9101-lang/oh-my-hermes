#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# How to run: uv sync --group lint; uv run python tools/qa/seven_issues_lifecycle.py
# --issue 1503 --output-dir /path/to/caller-owned/fixtures
"""Real local CLI scenarios over synthetic metadata; never provider execution."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from omh.workflows.lifecycle_growth_configuration import build_configuration_binding
from omh.workflows.lifecycle_growth_configuration_identity import build_configuration_identity
from omh.workflows.lifecycle_growth_configuration_values import record

ROOT = Path(__file__).resolve().parents[2]


class ScenarioFailure(RuntimeError):
    """Bounded local QA failure, not a provider error."""


def check(condition: bool, category: str) -> None:
    if not condition:
        raise ScenarioFailure(category)


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def fixtures(output: Path) -> None:
    """Build complete fixture chains using public OMH builders, not test execution."""
    base = record(json.loads((ROOT / "tests/fixtures/lifecycle_growth_configuration/base.json").read_text(encoding="utf-8")))
    artifacts = {slot: base[slot] for slot in ("experiment", "audience_review", "exposure_evidence", "readout")}
    metadata = {"identity_state": "observed", "configuration_ref": "configuration_fixture",
                "revision_ref": "revision_launch_v1", "observed_at": "2026-09-01T09:00:00Z", "evidence_refs": ["receipt_launch_v1"]}
    identity = build_configuration_identity(artifacts, metadata)
    first = {"kind": "launch", "observed_at": metadata["observed_at"], "evidence_ref": "receipt_launch_v1",
             "configuration_digest": identity["configuration_digest"], "revision_ref": metadata["revision_ref"]}
    request = {"artifacts": artifacts, "metadata": metadata, "observations": [first], "predecessor_seal": None}
    binding = build_configuration_binding(request)
    matching = {**artifacts, "configuration_binding": binding}
    write_json(output / "configuration-input.json", request)
    write_json(output / "matching.json", matching)
    write_json(output / "sealed-launch.json", binding)
    write_json(output / "identity.json", identity)
    write_json(output / "prepare.json", {**base, "configuration_binding": binding})
    expected = {"artifacts": {slot: artifacts[slot] for slot in ("experiment", "audience_review")},
                "metadata": {**metadata, "identity_state": "unknown", "observed_at": None, "evidence_refs": []},
                "observations": [], "predecessor_seal": None}
    write_json(output / "prepare-first.json", {**{key: value for key, value in base.items() if key != "readout"},
        "configuration_binding": build_configuration_binding(expected)})
    for name, field, value in (("drift-share", "rollout_share", 60), ("drift-rule", "rule_ref", "rule_changed")):
        review = record(deepcopy(artifacts["audience_review"]))
        rules = review["rules"]
        if not isinstance(rules, list):
            raise ScenarioFailure("fixture_rules_invalid")
        changed = [{**record(rule), **({field: value} if index == 0 else {})} for index, rule in enumerate(rules)]
        write_json(output / f"{name}.json", {**matching, "audience_review": {**review, "rules": changed}})
    analysis = deepcopy(binding)
    analysis["bindings"]["analysis_status"]["configuration_digest"] = "sha256:" + "f" * 64
    write_json(output / "analysis-mismatch.json", {**matching, "configuration_binding": analysis})
    unknown = deepcopy(binding)
    unknown["identity"].update(identity_state="unknown", observed_at=None, evidence_refs=[])
    write_json(output / "unknown.json", {**matching, "configuration_binding": unknown})
    legacy = {key: value for key, value in matching.items() if key != "configuration_binding"}
    write_json(output / "legacy.json", legacy)
    drift = record(json.loads((output / "drift-share.json").read_text(encoding="utf-8")))
    write_json(output / "rollback-drift.json", {**drift, "readout": {
        **record(drift["readout"]), "guardrail_state": "failed", "disposition": "rollback"}})
    write_json(output / "malformed.json", {**matching, "configuration_binding": {**binding, "raw_audience": "PRIVATE_SENTINEL"}})
    successor_artifacts = {key: value for key, value in drift.items() if key != "configuration_binding"}
    successor_metadata = {**metadata, "revision_ref": "revision_changed_v2", "observed_at": "2026-09-02T09:00:00Z",
                          "evidence_refs": ["receipt_changed_v2"]}
    successor_identity = build_configuration_identity(successor_artifacts, successor_metadata)
    successor = build_configuration_binding({"artifacts": successor_artifacts, "metadata": successor_metadata,
        "predecessor_seal": binding["seal"], "observations": [{**first, "kind": "exposure",
            "configuration_digest": successor_identity["configuration_digest"], "revision_ref": "revision_changed_v2",
            "observed_at": successor_metadata["observed_at"], "evidence_ref": "receipt_changed_v2"}]})
    write_json(output / "sealed-drift.json", successor)
    write_json(output / "successor-drift.json", {**successor_artifacts, "configuration_binding": successor})


def run(output: Path) -> dict[str, object]:
    fixtures(output)
    results: list[dict[str, object]] = []
    with TemporaryDirectory(prefix="omh-seven-lifecycle-") as temporary:
        scratch = Path(temporary)
        env = {**os.environ, "OMH_HOME": str(scratch / "omh"), "HERMES_HOME": str(scratch / "hermes"),
               "HOME": str(scratch), "PYTHONDONTWRITEBYTECODE": "1"}

        def cli(operation: str, fixture: str, expected_exit: int = 0) -> dict[str, object]:
            command = [sys.executable, "-P", "-m", "omh.cli", "runtime", "workflow-artifact", "lifecycle-growth",
                       operation, "--input", str(output / f"{fixture}.json")]
            child = subprocess.run(command, env=env, cwd=scratch, capture_output=True, text=True, timeout=30)
            check(child.returncode == expected_exit, f"{fixture}_exit_{child.returncode}")
            check(len(child.stdout) + len(child.stderr) <= 262144, "cli_output_bound")
            result = dict(record(record(json.loads(child.stdout))["result"])) if child.returncode == 0 else {}
            check("PRIVATE_SENTINEL" not in child.stdout + child.stderr, "raw_input_echo")
            receipt = {"command": command, "exit": child.returncode, "stdout_sha256": hashlib.sha256(child.stdout.encode()).hexdigest(),
                       "stderr": child.stderr[:512], "result": result}
            write_json(output / f"{fixture}-{operation}-output.json", receipt)
            results.append({"fixture": fixture, "operation": operation, "exit": child.returncode,
                            "disposition": result.get("disposition"), "verdict": result.get("verdict"),
                            "reason_codes": result.get("evidence_reason_codes"), "blocked": result.get("blocked"),
                            "configuration_integrity": result.get("configuration_integrity"), "stdout_sha256": receipt["stdout_sha256"],
                            "command": command})
            return result

        built = cli("configuration", "configuration-input")
        check(built == json.loads((output / "sealed-launch.json").read_text(encoding="utf-8")), "cli_configuration_binding_parity")
        check(cli("validate", "identity")["valid"] is True, "identity_schema")
        check(cli("validate", "sealed-launch")["valid"] is True, "binding_schema")
        check(cli("prepare", "prepare")["verdict"] == "READY", "prepare_forwarding")
        prepared = cli("prepare", "prepare-first")
        check(prepared["verdict"] == "READY" and prepared["configuration_integrity"] is False, "first_launch_boundary")
        matching = cli("evaluate", "matching")
        check(matching["disposition"] == "ship" and matching["configuration_integrity"] is True and matching["blocked"] is False, "matching_ship")
        for fixture, disposition, reason in (
            ("drift-share", "review", "configuration_drift"), ("drift-rule", "review", "configuration_drift"),
            ("analysis-mismatch", "review", "configuration_binding_mismatch"),
            ("unknown", "insufficient_data", "configuration_identity_unknown"),
            ("legacy", "insufficient_data", "configuration_identity_missing"),
            ("rollback-drift", "rollback", "configuration_drift"), ("successor-drift", "review", "configuration_drift"),
        ):
            result = cli("readout", fixture)
            codes = result["evidence_reason_codes"]
            check(result["disposition"] == disposition and result["blocked"] is True
                  and isinstance(codes, list) and reason in codes, fixture + "_decision")
        cli("evaluate", "malformed", 2)
        check(not (scratch / "omh").exists() and not (scratch / "hermes").exists(), "unexpected_provider_or_omh_state")
    # The invocation-owned cleanup receipt is required by this QA protocol.
    absent = not scratch.exists()
    check(absent, "scratch_cleanup_failed")
    return {"issue": 1503, "pass": True, "scenario_count": len(results), "provider_execution_observed": False,
            "scenarios": results, "cleanup": {"owned_resources": [str(scratch)], "verified_absent": absent}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue", choices=("1503",), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = run(args.output_dir.resolve())
    except (ScenarioFailure, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"issue": 1503, "pass": False, "error_category": type(exc).__name__, "detail": str(exc)[:160]}))
        return 1
    write_json(args.output_dir / "result.json", result)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
