"""Parent clarification through the real dispatcher, Git, journal and CLI."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from _credential_fixtures import AWS_ACCESS_KEY_ID
from _local_package import load_local_package

load_local_package()
from _cli_harness import run_cli  # noqa: E402
from test_fanout_dispatch import _make_repo, _prompted_sidecar, _ready  # noqa: E402
from omh.coding.fanout import build_fanout_contract  # noqa: E402
from omh.coding.fanout_artifacts import write_fanout_contract, fanout_dispatch_summary_path  # noqa: E402
from omh.coding.fanout_dispatch import dispatch_fanout, stdout_fenced_json_blocks  # noqa: E402
from omh.coding.fanout_journal import read_fanout_run_journal  # noqa: E402
from omh.coding.fanout_failure_diagnostics import is_object_list, is_string_map  # noqa: E402
from omh.coding.fanout_unit_results import validate_unit_result  # noqa: E402
from omh.coding import unit_prompt_protocol  # noqa: E402
from omh.system.paths import OmhPaths  # noqa: E402
from omh.workflows.observation_journal import read_observation_events  # noqa: E402

GOAL = "Choose output format within each unit boundary"
QUESTION = {"decision_id": "decision-d1", "question": "Choose output format",
    "blocking_reason": "The output encoding determines the implementation",
    "answer_shape": {"kind": "options", "options": ["json", "text"]},
    "affected_unit_ids": ["unit-b"], "redacted_context": []}


def payload(unit: str = "unit-b") -> dict[str, object]:
    return {"schema_version": "fanout_unit_result/v1", "unit_id": unit,
        "run_id": "fanout-0123456789ab-" + unit, "fanout_id": "fanout-0123456789ab",
        "base_sha": "a" * 40, "head_sha": "a" * 40, "process_status": "input_required",
        "input_required": deepcopy(QUESTION), "changed_paths": [], "checks": [], "findings": []}


class LocalFanout:
    """Real Git and local subprocess returns; no provider or mocked dispatcher."""
    def __init__(self, root: Path, *, review: bool = False):
        self.paths = OmhPaths(omh_home=root / "qa-profile", hermes_home=root / "hermes")
        self.repo, self.sha = _make_repo(root)
        units = [{"unit_id": "unit-" + letter, "title": "unit-" + letter, "owner": "codex",
            "file_scope": [letter + "/"], "verification_commands": [sys.executable + " -c pass"],
            **({"role": "review"} if review and letter == "b" else {}),
            **({"depends_on": ["unit-b"]} if letter == "c" else {})} for letter in "abc"]
        self.contract = write_fanout_contract(self.paths, build_fanout_contract(GOAL, units))
        self.fanout_id = str(self.contract["fanout_id"])
        raw_units = self.contract["units"]
        assert is_object_list(raw_units)
        self.units = [row for row in raw_units if is_string_map(row)]
        self.calls: list[tuple[str, str]] = []
        self.ask = True
        self.ask_units = {"unit-b"}

    def runner(self, argv, **kwargs):
        if argv[0] == "git" or argv[0] == sys.executable:
            return subprocess.run(argv, **kwargs)
        prompt = argv[-1]
        unit = next(row for row in self.units if f"Work unit: {row['unit_id']}\n" in prompt)
        unit_id = str(unit["unit_id"])
        self.calls.append((unit_id, prompt))
        result = payload(unit_id)
        result.update(fanout_id=self.fanout_id, run_id=unit["run_ref"], base_sha=self.sha, head_sha=self.sha)
        result["input_required"] = {**QUESTION, "affected_unit_ids": [unit_id]}
        if unit_id not in self.ask_units or not self.ask:
            result.pop("input_required")
            result["process_status"] = "process_succeeded"
        return subprocess.run([sys.executable, "-c",
            "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])",
            str(_prompted_sidecar(argv)), json.dumps(result)], capture_output=True, text=True,
            cwd=kwargs["cwd"], timeout=kwargs["timeout"])

    def dispatch(self, prior=None):
        return dispatch_fanout(self.paths, self.contract, goal_text=GOAL, repo_root=self.repo,
            base_sha=self.sha, runner=self.runner, readiness=_ready, concurrency=2,
            max_retries=0, run_verification=True, resume_journal=prior,
            only_units=["unit-b"] if prior else None, goal_attempt_id="attempt-1")

    def cli(self, *args):
        return run_cli(["--omh-home", str(self.paths.omh_home), "--hermes-home", str(self.paths.hermes_home),
            "coding", "fanout", *args])

    def question(self):
        code, out, err = self.cli("clarifications", self.fanout_id, "--json")
        if code:
            raise AssertionError((code, out, err))
        return json.loads(out)["renderable"]

    def answer(self, request, *extra):
        return self.cli("answer", self.fanout_id, "--unit", "unit-b", "--decision", request["decision_id"],
            "--attempt-id", request["attempt_id"], "--round", str(request["round"]), *extra)

    def journal(self, summary):
        return read_fanout_run_journal(Path(summary["run_journal_path"]))


class ParentClarificationTests(unittest.TestCase):
    def setUp(self):
        home = TemporaryDirectory(prefix="omh-clarification-home-")
        self.addCleanup(home.cleanup)
        environment = patch.dict(os.environ, {"HOME": home.name, "HERMES_HOME": home.name,
            "XDG_CONFIG_HOME": home.name, "OMH_FANOUT_DEPTH": "0"})
        environment.start()
        self.addCleanup(environment.stop)

    def test_bounded_input_required_validation(self):
        # Given a documented child request; When validated; Then bounded consistent metadata only.
        self.assertEqual(validate_unit_result(payload())["input_required"], QUESTION)
        for missing in QUESTION:
            request = {key: value for key, value in QUESTION.items() if key != missing}
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                validate_unit_result({**payload(), "input_required": request})
        mutations = [{key: None} for key in QUESTION] + [
            {"question": "q" * 301}, {"answer_shape": {"kind": "options", "options": ["x"] * 9}},
            {"redacted_context": [AWS_ACCESS_KEY_ID]}, {"question": "$(touch /tmp/escape)"},
            {"affected_unit_ids": ["unit-a"]}, {"authority": "approve"},
            {"answer_shape": {"kind": "text", "max_chars": True}}]
        for changes in mutations:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_unit_result({**payload(), "input_required": {**QUESTION, **changes}})
        with self.assertRaises(ValueError):
            validate_unit_result({**payload(), "process_status": "process_succeeded"})

    def test_documented_machine_example(self):
        # Given the rendered protocol; When parsing its JSON; Then the machine schema accepts it.
        examples = stdout_fenced_json_blocks(unit_prompt_protocol.PARENT_CLARIFICATION_PROTOCOL)
        self.assertEqual(len(examples), 1)
        self.assertEqual(validate_unit_result(json.loads(examples[0]))["process_status"], "input_required")

    def test_waiting_unit_preserves_siblings(self):
        # Given a -> done, b -> question, c -> depends on b; When dispatched; Then no false failure.
        with TemporaryDirectory() as tmp:
            fixture = LocalFanout(Path(tmp))
            result = fixture.dispatch()
            rows = {row["unit_id"]: row for row in result["units"]}
            self.assertEqual(rows["unit-b"]["status"], "input_required")
            self.assertTrue(rows["unit-a"]["unit_verification_observed"])
            self.assertEqual(rows["unit-c"]["blocked_reasons"], {"unit-b": "awaiting_input"})
            self.assertFalse(rows["unit-b"]["unit_verification_observed"])
            self.assertEqual([row["terminal_state"] for row in fixture.journal(result)["units"]],
                ["succeeded", "input_required", "skipped_by_dependency"])

    def test_parent_answer_resumes_only_unit(self):
        # Given waiting b and observed sibling receipts; When root answers and explicitly resumes; Then only b runs.
        with TemporaryDirectory() as tmp:
            fixture = LocalFanout(Path(tmp))
            initial = fixture.dispatch()
            request = fixture.question()
            sibling = [e for e in read_observation_events(fixture.paths) if e.get("worker_ref") == "unit-a"]
            stored = json.loads(fanout_dispatch_summary_path(fixture.paths, fixture.fanout_id).read_text())["units"][0]
            self.assertEqual(fixture.answer(request, "--answer", "json")[0], 0)
            fixture.ask = False
            resumed = fixture.dispatch(fixture.journal(initial))
            self.assertEqual(sorted(unit for unit, _ in fixture.calls), ["unit-a", "unit-b", "unit-b"])
            self.assertEqual(fixture.calls[-1][0], "unit-b")
            row = next(row for row in resumed["units"] if row["unit_id"] == "unit-b")
            self.assertEqual(row["status"], "completed")
            self.assertEqual(row["resume"]["action"], "rerun_answered")
            decision = json.loads(fixture.calls[-1][1].split("[Parent decision]\n")[1])
            self.assertEqual((decision["answer"], decision["attempt_id"], decision["round"]),
                ("json", request["attempt_id"], request["round"]))
            self.assertEqual((row["run_ref"], resumed["base_sha"]), (request["run_ref"], request["base_sha"]))
            self.assertEqual(sibling, [e for e in read_observation_events(fixture.paths) if e.get("worker_ref") == "unit-a"])
            self.assertEqual(stored, json.loads(fanout_dispatch_summary_path(fixture.paths, fixture.fanout_id).read_text())["units"][0])

    def test_bounded_terminal_and_stale_answers(self):
        # Given a pending decision; When each terminal/stale action is applied; Then deterministic holds.
        with TemporaryDirectory() as tmp:
            fixture = LocalFanout(Path(tmp))
            initial = fixture.dispatch()
            request = fixture.question()
            self.assertIsNotNone(request)
            held = fixture.dispatch(fixture.journal(initial))
            self.assertEqual(held["resume"]["decisions"][1]["action"], "hold_input_required")
            for field, value in (("decision_id", "old-decision"), ("attempt_id", "old-attempt"), ("round", 0)):
                code, _out, err = fixture.answer({**request, field: value}, "--answer", "json")
                self.assertNotEqual(code, 0)
                self.assertIn("stale_" + field, err)
            with patch.dict(os.environ, {"OMH_FANOUT_DEPTH": "1"}):
                self.assertNotEqual(fixture.answer(request, "--answer", "json")[0], 0)
            from omh.coding.fanout_clarification import answer_clarification, ClarificationAnswer
            future = datetime.now(timezone.utc) + timedelta(days=2)
            with self.assertRaisesRegex(ValueError, "expired"):
                answer_clarification(fixture.paths, fixture.fanout_id,
                    ClarificationAnswer("unit-b", request["decision_id"], request["attempt_id"], request["round"], "json"), now=future)
            with patch("omh.coding.fanout_clarification.datetime", wraps=datetime) as clock:
                clock.now.return_value = future
                expired = fixture.dispatch(fixture.journal(initial))
            self.assertEqual(expired["resume"]["decisions"][1]["action"], "hold_input_expired")
            self.assertEqual(fixture.answer(request, "--cancel")[0], 0)
            cancelled = fixture.dispatch(fixture.journal(initial))
            self.assertEqual(cancelled["resume"]["decisions"][1]["action"], "hold_input_cancelled")
        with TemporaryDirectory() as tmp:
            fixture = LocalFanout(Path(tmp))
            current = fixture.dispatch()
            for _round in (1, 2):
                self.assertEqual(fixture.answer(fixture.question(), "--answer", "json")[0], 0)
                current = fixture.dispatch(fixture.journal(current))
            exhausted = fixture.dispatch(fixture.journal(current))
            self.assertEqual(exhausted["resume"]["decisions"][1]["action"], "hold_input_exhausted")

    def test_dispatch_refusal_preserves_answer(self):
        # Given an answered reviewer with no review allowance; When redispatch is refused; Then its answer remains usable.
        with TemporaryDirectory() as tmp:
            fixture = LocalFanout(Path(tmp), review=True)
            initial = fixture.dispatch()
            self.assertEqual(fixture.answer(fixture.question(), "--answer", "json")[0], 0)
            refused = dispatch_fanout(fixture.paths, fixture.contract, goal_text=GOAL, repo_root=fixture.repo,
                base_sha=fixture.sha, runner=fixture.runner, readiness=_ready,
                only_units=["unit-b"], resume_journal=fixture.journal(initial))
            self.assertEqual(refused["units"][1]["status"], "review_dispatch_budget_exhausted")
            from omh.coding.fanout_clarification import read_clarification, clarification_path
            record = read_clarification(clarification_path(fixture.paths, fixture.fanout_id, "unit-b"))
            assert record is not None
            self.assertEqual(record["state"], "answered")
            unselected = dispatch_fanout(fixture.paths, fixture.contract, goal_text=GOAL, repo_root=fixture.repo,
                base_sha=fixture.sha, runner=fixture.runner, readiness=_ready, resume_journal=fixture.journal(refused))
            self.assertEqual(unselected["resume"]["decisions"][1]["action"], "hold_input_explicit_unit_required")

    def test_only_one_question_is_renderable(self):
        # Given two independent requests; When root reads the view; Then one is renderable, one queued.
        with TemporaryDirectory() as tmp:
            fixture = LocalFanout(Path(tmp))
            fixture.ask_units = {"unit-a", "unit-b"}
            fixture.dispatch()
            code, out, err = fixture.cli("clarifications", fixture.fanout_id, "--json")
            self.assertEqual(code, 0, err)
            view = json.loads(out)
            self.assertEqual(view["renderable"]["unit_id"], "unit-a")
            self.assertEqual(view["queued_units"], ["unit-b"])

    def test_unbacked_answer_receipt_is_not_observed(self):
        # Given a corrupted answer event reference; When root reads status; Then no observed answer is claimed.
        with TemporaryDirectory() as tmp:
            fixture = LocalFanout(Path(tmp))
            initial = fixture.dispatch()
            self.assertEqual(fixture.answer(fixture.question(), "--answer", "json")[0], 0)
            from omh.coding.fanout_clarification import clarification_path
            path = clarification_path(fixture.paths, fixture.fanout_id, "unit-b")
            record = json.loads(path.read_text())
            record["answer_event_ref"] = "missing-event"
            path.write_text(json.dumps(record))
            code, out, err = fixture.cli("status", "--fanout-id", fixture.fanout_id, "--unit", "unit-b", "--json")
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["units"][0]["clarification"]["answer"], "none")
            held = fixture.dispatch(fixture.journal(initial))
            self.assertEqual(held["resume"]["decisions"][1]["action"], "hold_input_stale")

    def test_clarification_evidence_separation(self):
        # Given a waiting unit; When inspected at request/answer/redispatch; Then evidence classes stay separate.
        with TemporaryDirectory() as tmp:
            fixture = LocalFanout(Path(tmp))
            initial = fixture.dispatch()
            def status():
                code, out, err = fixture.cli("status", "--fanout-id", fixture.fanout_id, "--unit", "unit-b", "--json")
                self.assertEqual(code, 0, err)
                return json.loads(out)["units"][0]
            before = status()
            self.assertEqual(before["clarification"], {"request": "prepared", "answer": "none", "redispatch": "none"})
            self.assertEqual(before["lifecycle_state"], "input_required")
            self.assertFalse(before["unit_verification_observed"])
            self.assertEqual(fixture.answer(fixture.question(), "--answer", "json")[0], 0)
            answered = status()
            self.assertEqual(answered["clarification"]["answer"], "observed")
            self.assertEqual(answered["clarification"]["redispatch"], "none")
            fixture.ask = False
            fixture.dispatch(fixture.journal(initial))
            after = status()
            self.assertEqual(after["clarification"]["redispatch"], "observed")
            self.assertTrue(after["unit_verification_observed"])
