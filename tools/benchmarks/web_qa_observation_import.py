#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# How to run:
# Install uv: curl -LsSf https://astral.sh/uv/install.sh | sh
# uv run tools/benchmarks/web_qa_observation_import.py \
#   --plan PLAN.json --receipt REVIEWED_RECEIPT.json --capture CAPTURE.png
"""Measure actual CLI import, read, comparison, and immutable reuse.

The receipt must already contain a genuine visual review. This benchmark never
creates a score or runs a browser, deployment, or model. Its temporary Git
project isolates all managed writes from the working checkout.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError("CLI output must contain an object")
    return value


def _invoke(project: Path, *arguments: str) -> tuple[dict[str, object], float]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["OMH_HOME"] = str(project / ".omh")
    environment["OMH_OUTPUT"] = "json"
    started = time.perf_counter_ns()
    result = subprocess.run(
        [sys.executable, "-m", "omh.cli", "web-qa", "observation",
         *arguments, "--project-root", str(project)],
        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=60,
        check=True,
    )
    elapsed = (time.perf_counter_ns() - started) / 1_000_000
    return _object(json.loads(result.stdout)), elapsed


def _files(directory: Path) -> dict[str, tuple[int, int, int]]:
    return {
        path.relative_to(directory).as_posix(): (
            path.stat().st_mtime_ns, path.stat().st_ctime_ns, path.stat().st_size,
        )
        for path in directory.rglob("*") if path.is_file()
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--capture", type=Path, required=True)
    args = parser.parse_args()
    plan_path, receipt_path, capture_path = (
        path.resolve(strict=True) for path in (args.plan, args.receipt, args.capture)
    )
    plan = _object(json.loads(plan_path.read_text(encoding="utf-8")))
    receipt = _object(json.loads(receipt_path.read_text(encoding="utf-8")))
    digest = hashlib.sha256(capture_path.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="omh-web-qa-import-") as temporary:
        project = Path(temporary)
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        arguments = (
            "import", "--plan-json", str(plan_path),
            "--receipt-json", str(receipt_path),
            "--capture", f"{digest}={capture_path}",
        )
        imported, import_ms = _invoke(project, *arguments)
        observation = _object(imported["observation"])
        if observation["verdict"] != "PASS":
            raise AssertionError("a genuinely reviewed positive capture must pass import")
        before = _files(project / ".omh")
        repeated, repeat_ms = _invoke(project, *arguments)
        after = _files(project / ".omh")
        if repeated != imported or after != before:
            raise AssertionError("completed import must reuse exact evidence without writes")
        run_id = str(plan["run_id"])
        shown, show_ms = _invoke(project, "show", "--run-id", run_id)
        if shown != imported:
            raise AssertionError("show must re-admit and preserve the imported evidence")
        compared, compare_ms = _invoke(
            project, "compare", "--baseline-run-id", run_id,
            "--candidate-run-id", run_id,
        )
        if compared["verdict"] != "PASS" or compared["rollback_authorized"] is not False:
            raise AssertionError("same-condition comparison must pass without rollback authority")

        execution = _object(receipt["execution"])
        started_at = datetime.fromisoformat(str(execution["started_at"]).replace("Z", "+00:00"))
        ended_at = datetime.fromisoformat(str(execution["ended_at"]).replace("Z", "+00:00"))
        writes = sum(after.get(path) != metadata for path, metadata in before.items())
        writes += len(set(after) - set(before))
        report = {
            "schema_version": "web_qa_cli_benchmark/v1",
            "arm": "host-owned native browser observation imported through the real CLI",
            "corpus": "one independently reviewed localhost fixture capture",
            "selection": "complete single-cell normalized plan and exact reviewed receipt",
            "run_id": run_id,
            "subject_digest": plan["subject_digest"],
            "condition_digest": plan["condition_digest"],
            "capture_sha256": digest,
            "adapter_observation_ms": (ended_at - started_at).total_seconds() * 1000,
            "omh_cli_wall_ms": {
                "import": import_ms, "repeat_import": repeat_ms,
                "show": show_ms, "compare": compare_ms,
            },
            "import_timings": imported["timings"],
            "attempts": execution["attempts"],
            "artifact_bytes": capture_path.stat().st_size,
            "repeat_changed_files": writes,
            "import_verdict": observation["verdict"],
            "comparison_verdict": compared["verdict"],
            "rollback_authorized": compared["rollback_authorized"],
            "claim_boundary": "One local fixture observation, not production deployment or universal browser coverage.",
        }
        print(json.dumps(report, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
