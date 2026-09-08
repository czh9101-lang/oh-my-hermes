from __future__ import annotations

import json
import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests._cli_harness import run_cli
from tests.test_web_qa_comparison import REVISION_2, deployment_observation, plan, receipt
from tests.test_web_qa_observation_plan import observation_request
from tests.test_web_qa_observation_store import PNG


class WebQaObservationEntryTests(unittest.TestCase):
    def test_public_cli_prepares_a_project_plan_without_writing_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            source = root / "request.json"
            source.write_text(json.dumps(observation_request()), encoding="utf-8")

            status, stdout, stderr = run_cli([
                "web-qa", "observation", "plan",
                "--project-root", str(root), "--plan-json", str(source),
            ])

            self.assertEqual(status, 0, stderr)
            result = json.loads(stdout)
            self.assertEqual(result["completion_state"], "not_found")
            self.assertEqual(result["plan"]["schema_version"], "web_qa_observation_plan/v1")
            self.assertFalse((root / ".omh").exists())

    def test_public_canary_comparison_requires_matching_observed_deployment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            image = root / "capture.png"
            image.write_bytes(PNG)
            image_digest = hashlib.sha256(PNG).hexdigest()
            baseline = plan(environment="production")
            candidate = plan(revision=REVISION_2, mode="canary")
            for label, prepared, captured_at in (
                ("baseline", baseline, "2026-09-07T09:59:00Z"),
                ("candidate", candidate, "2026-09-07T10:00:02Z"),
            ):
                observed = receipt(prepared, captured_at=captured_at)
                _mapping(observed["execution"])["artifact_bytes"] = len(PNG)
                cells = observed["cells"]
                if not isinstance(cells, list):
                    self.fail("receipt fixture cells must be a list")
                channels = _mapping(_mapping(cells[0])["channels"])
                capture = _mapping(_mapping(channels["screenshot"])["evidence"])
                capture["capture_sha256"] = image_digest
                capture["byte_size"] = len(PNG)
                _mapping(capture["review"])["capture_sha256"] = image_digest
                plan_path = root / f"{label}-plan.json"
                receipt_path = root / f"{label}-receipt.json"
                plan_path.write_text(json.dumps(prepared), encoding="utf-8")
                receipt_path.write_text(json.dumps(observed), encoding="utf-8")
                status, stdout, stderr = run_cli([
                    "web-qa", "observation", "import", "--project-root", str(root),
                    "--plan-json", str(plan_path), "--receipt-json", str(receipt_path),
                    "--capture", f"{image_digest}={image}",
                ])
                self.assertEqual(status, 0, stderr)
                self.assertEqual(json.loads(stdout)["observation"]["verdict"], "PASS")

            arguments = [
                "web-qa", "observation", "compare", "--project-root", str(root),
                "--baseline-run-id", str(baseline["run_id"]),
                "--candidate-run-id", str(candidate["run_id"]),
            ]
            status, stdout, stderr = run_cli(arguments)
            self.assertEqual(status, 0, stderr)
            self.assertEqual(json.loads(stdout)["verdict"], "BLOCK")

            deployment_path = root / "deployment.json"
            deployment = deployment_observation(candidate)
            deployment_path.write_text(json.dumps(deployment), encoding="utf-8")
            arguments.extend(["--deployment-observation-json", str(deployment_path)])
            status, stdout, stderr = run_cli(arguments)
            self.assertEqual(status, 0, stderr)
            result = json.loads(stdout)
            self.assertEqual(result["verdict"], "PASS")
            self.assertFalse(result["rollback_authorized"])

            deployment["observed_at"] = "2026-09-07T09:58:00Z"
            deployment_path.write_text(json.dumps(deployment), encoding="utf-8")
            status, stdout, stderr = run_cli(arguments)
            self.assertEqual(status, 0, stderr)
            result = json.loads(stdout)
            self.assertEqual(result["verdict"], "BLOCK")
            self.assertIn("canary_baseline_observation_not_before_deployment", result["blockers"])


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AssertionError("fixture value must be an object")
    return value
