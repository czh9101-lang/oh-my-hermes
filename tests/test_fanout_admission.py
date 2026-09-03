from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import threading
import unittest

from omh.coding.fanout import build_fanout_contract
from omh.coding.fanout_artifacts import write_fanout_contract
from omh.coding.fanout_dispatch import dispatch_fanout
from omh.system.paths import OmhPaths

_GOAL = "admit a ready frontier through an adaptive window"


class _FakeCompleted:
    returncode = 0
    stdout = "done"
    stderr = ""


class _ControlledRunner:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.active = 0
        self.max_active = 0
        self._released: set[str] = set()
        self._release_future = False
        self._condition = threading.Condition()

    def __call__(self, argv, **kwargs):
        if argv[0] == "git":
            return subprocess.run(argv, **kwargs)
        unit_id = Path(str(kwargs["cwd"])).name.rsplit("-fanout-", 1)[-1]
        with self._condition:
            self.started.append(unit_id)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self._condition.notify_all()
            released = self._condition.wait_for(
                lambda: self._release_future or unit_id in self._released,
                timeout=5,
            )
            self.active -= 1
            self._condition.notify_all()
        if not released:
            raise AssertionError(f"timed out waiting to release {unit_id}")
        return _FakeCompleted()

    def wait_for_started(self, count: int, timeout: float = 2.0) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: len(set(self.started)) >= count,
                timeout=timeout,
            )

    def release(self, unit_id: str) -> None:
        with self._condition:
            self._released.add(unit_id)
            self._condition.notify_all()

    def release_all(self) -> None:
        with self._condition:
            self._release_future = True
            self._condition.notify_all()


def _ready(paths, profile, **kwargs):
    return {"status": "ready", "profile": profile}


class FanoutAdaptiveSchedulerTests(unittest.TestCase):
    def test_adaptive_scheduler_starts_at_two_then_grows_after_a_clean_completion(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
                cwd=repo,
                check=True,
            )
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            units = [
                {
                    "unit_id": f"unit-{index}",
                    "title": f"Unit {index}",
                    "owner": "codex",
                    "file_scope": [f"src/unit-{index}/"],
                }
                for index in range(4)
            ]
            contract = write_fanout_contract(
                paths,
                build_fanout_contract(_GOAL, units),
            )
            runner = _ControlledRunner()

            with ThreadPoolExecutor(max_workers=1) as caller:
                future = caller.submit(
                    dispatch_fanout,
                    paths,
                    contract,
                    goal_text=_GOAL,
                    repo_root=repo,
                    base_sha=base_sha,
                    concurrency=4,
                    adaptive_concurrency=True,
                    runner=runner,
                    readiness=_ready,
                )
                if not runner.wait_for_started(2):
                    future.result(timeout=0)
                    self.fail("adaptive dispatch did not admit its initial window")
                self.assertFalse(runner.wait_for_started(3, timeout=0.2))
                runner.release(runner.started[0])
                self.assertTrue(runner.wait_for_started(4))
                runner.release_all()
                summary = future.result(timeout=5)

        self.assertEqual(runner.max_active, 3)
        self.assertTrue(all(unit["status"] == "completed" for unit in summary["units"]))


class AdaptiveAdmissionReceiptTests(unittest.TestCase):
    def test_receipt_bounds_provider_pressure_and_recovered_retry_adjustments(self) -> None:
        from omh.coding.fanout_admission import AdaptiveFanoutAdmission

        admission = AdaptiveFanoutAdmission(ceiling=4)
        admission.observe(
            "clean",
            {"status": "completed", "exit_code": 0, "process_succeeded": True},
        )
        admission.observe(
            "recovered-limit",
            {
                "status": "completed",
                "exit_code": 0,
                "process_succeeded": True,
                "retry": {
                    "decisions": [
                        {"failure_class": "transient_provider_limit", "decision": "retrying"}
                    ]
                },
            },
        )
        for index, failure_kind in enumerate(
            ("auth_shaped", "timeout", "binary_missing", "crash")
        ):
            admission.observe(
                f"other-{index}",
                {"status": "failed", "exit_code": 1, "failure_kind": failure_kind},
            )
        admission.observe(
            "transport",
            {
                "status": "failed",
                "exit_code": 1,
                "failure_kind": "crash",
                "retry": {"decisions": [{"failure_class": "transient_transport"}]},
            },
        )
        for index in range(40):
            admission.observe(
                f"clean-{index}",
                {"status": "completed", "exit_code": 0, "process_succeeded": True},
            )
            admission.observe(
                f"limit-{index}",
                {"status": "failed", "exit_code": 1, "failure_kind": "limit_shaped"},
            )

        receipt = admission.receipt()

        self.assertEqual(receipt["schema_version"], "fanout_admission/v1")
        self.assertEqual(receipt["mode"], "adaptive")
        self.assertTrue(receipt["requested"])
        self.assertEqual(receipt["initial_window"], 2)
        self.assertEqual(receipt["ceiling"], 4)
        self.assertEqual(receipt["minimum_window"], 1)
        self.assertEqual(receipt["observed_provider_pressure_count"], 41)
        recovered = next(
            row for row in receipt["adjustments"] if row["unit_id"] == "recovered-limit"
        )
        self.assertEqual(recovered["status_class"], "provider_limit_pressure")
        self.assertEqual((recovered["window_before"], recovered["window_after"]), (3, 1))
        self.assertLessEqual(len(receipt["adjustments"]), 32)
        self.assertGreater(receipt["adjustments_omitted"], 0)
        self.assertNotIn("raw_output", str(receipt))
        self.assertIn("not provider quota truth", receipt["claim_boundary"])


class _RetryControlledRunner:
    def __init__(self, pressure_unit: str) -> None:
        self.pressure_unit = pressure_unit
        self.attempts: dict[str, int] = {}
        self.started: list[tuple[str, int]] = []
        self._released: set[tuple[str, int]] = set()
        self._release_future = False
        self._condition = threading.Condition()

    def __call__(self, argv, **kwargs):
        if argv[0] == "git":
            return subprocess.run(argv, **kwargs)
        unit_id = Path(str(kwargs["cwd"])).name.rsplit("-fanout-", 1)[-1]
        with self._condition:
            attempt = self.attempts.get(unit_id, 0) + 1
            self.attempts[unit_id] = attempt
            key = (unit_id, attempt)
            self.started.append(key)
            self._condition.notify_all()
            released = self._condition.wait_for(
                lambda: self._release_future or key in self._released,
                timeout=5,
            )
        if not released:
            raise AssertionError(f"timed out waiting to release {key}")
        if key == (self.pressure_unit, 1):
            return _FakeLimitCompleted()
        return _FakeCompleted()

    def wait_for_distinct_units(self, count: int, timeout: float = 2.0) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: len({unit_id for unit_id, _attempt in self.started}) >= count,
                timeout=timeout,
            )

    def wait_for_attempt(self, unit_id: str, attempt: int, timeout: float = 2.0) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: (unit_id, attempt) in self.started,
                timeout=timeout,
            )

    def release(self, unit_id: str, attempt: int = 1) -> None:
        with self._condition:
            self._released.add((unit_id, attempt))
            self._condition.notify_all()

    def release_all(self) -> None:
        with self._condition:
            self._release_future = True
            self._condition.notify_all()


class _FakeLimitCompleted:
    returncode = 1
    stdout = "Error: You have hit your usage limit. Try again later."
    stderr = ""


class FanoutRecoveredPressureIntegrationTests(unittest.TestCase):
    def test_recovered_limit_pressure_reduces_admission_before_the_next_queued_unit(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
                cwd=repo,
                check=True,
            )
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            unit_ids = ["a-clean", "b-pressure", "c-hold", "d-hold", "e-later", "f-later"]
            units = [
                {
                    "unit_id": unit_id,
                    "title": unit_id,
                    "owner": "codex",
                    "file_scope": [f"src/{unit_id}/"],
                }
                for unit_id in unit_ids
            ]
            contract = write_fanout_contract(
                paths,
                build_fanout_contract(
                    _GOAL,
                    units,
                    spawn_plan={
                        "why_parallel": "The six file scopes are independent.",
                        "why_not_single_unit": "The admission boundary requires a queued frontier.",
                        "independence": "No fixture unit depends on another.",
                        "expected_evidence_shape": "One process result per unit plus one retry.",
                    },
                ),
            )
            runner = _RetryControlledRunner("b-pressure")

            with ThreadPoolExecutor(max_workers=1) as caller:
                future = caller.submit(
                    dispatch_fanout,
                    paths,
                    contract,
                    goal_text=_GOAL,
                    repo_root=repo,
                    base_sha=base_sha,
                    concurrency=4,
                    adaptive_concurrency=True,
                    max_retries=1,
                    rng=lambda: 0.0,
                    sleep=lambda _seconds: None,
                    runner=runner,
                    readiness=_ready,
                )
                self.assertTrue(runner.wait_for_distinct_units(2))
                runner.release("a-clean")
                self.assertTrue(runner.wait_for_distinct_units(4))
                runner.release("b-pressure")
                self.assertTrue(runner.wait_for_attempt("b-pressure", 2))
                runner.release("b-pressure", 2)
                self.assertFalse(runner.wait_for_distinct_units(5, timeout=0.2))
                runner.release_all()
                summary = future.result(timeout=5)

        pressure = next(
            row
            for row in summary["adaptive_admission"]["adjustments"]
            if row["unit_id"] == "b-pressure"
        )
        self.assertEqual(pressure["status_class"], "provider_limit_pressure")
        self.assertEqual((pressure["window_before"], pressure["window_after"]), (3, 1))
        self.assertEqual(runner.attempts["b-pressure"], 2)


class AdaptiveAdmissionDryRunTests(unittest.TestCase):
    def test_dry_run_receipt_makes_no_observed_execution_or_pressure_claim(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
                cwd=repo,
                check=True,
            )
            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            contract = write_fanout_contract(
                paths,
                build_fanout_contract(
                    _GOAL,
                    [
                        {
                            "unit_id": "dry-run",
                            "title": "Dry run",
                            "owner": "codex",
                            "file_scope": ["src/dry-run/"],
                        }
                    ],
                ),
            )
            runner = _ControlledRunner()

            summary = dispatch_fanout(
                paths,
                contract,
                goal_text=_GOAL,
                repo_root=repo,
                base_sha=base_sha,
                concurrency=4,
                adaptive_concurrency=True,
                dry_run=True,
                runner=runner,
                readiness=_ready,
            )

        receipt = summary["adaptive_admission"]
        self.assertEqual(receipt["observation_status"], "not_observed_dry_run")
        self.assertEqual(receipt["observed_completion_count"], 0)
        self.assertEqual(receipt["observed_provider_pressure_count"], 0)
        self.assertEqual(receipt["adjustments"], [])
        self.assertEqual(receipt["final_window"], receipt["initial_window"])
        self.assertEqual(runner.started, [])
        self.assertIn("Dry-run plans are not observed execution", receipt["claim_boundary"])


if __name__ == "__main__":
    unittest.main()
