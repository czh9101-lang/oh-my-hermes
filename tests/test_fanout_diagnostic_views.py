"""Current-source diagnostic consumer tests; fixtures are metadata, not native runs."""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _local_package import load_local_package

load_local_package()

from _cli_harness import run_cli
from omh.coding.fanout_failure_diagnostics import (
    FailureDiagnostic, build_failure_diagnostic, is_object_list, is_string_map,
)
from omh.coding.fanout_output import FanoutOutput
from omh.coding.fanout_journal import (
    build_fanout_run_journal, plan_fanout_resume, read_fanout_run_journal,
    write_fanout_run_journal,
)
from omh.coding.fanout_status import project_fanout_status, render_fanout_status_text
from omh.coding.status_board import build_status_board, render_status_board_text
from omh.runtime.artifacts import create_run, show_run, write_runtime_observation
from omh.runtime.records import validate_runtime_observation_record
from omh.system.paths import OmhPaths
from omh.workflows.observation_journal import (
    append_observation_event, build_observation_event, project_run_lifecycle,
)

# Explicit object-return boundaries keep tests checking the actual public JSON.
observe: Callable[..., object] = build_observation_event
append: Callable[..., object] = append_observation_event
lifecycle: Callable[..., object] = project_run_lifecycle
journal: Callable[..., object] = build_fanout_run_journal
resume: Callable[..., object] = plan_fanout_resume
read_journal: Callable[..., object] = read_fanout_run_journal
board: Callable[..., object] = build_status_board
roster: Callable[..., object] = project_fanout_status
show: Callable[..., object] = show_run
runtime_write: Callable[..., object] = write_runtime_observation
validate_runtime: Callable[..., object] = validate_runtime_observation_record
decode: Callable[..., object] = json.loads

FANOUT = "fanout-0123456789ab"
UNIT = "core"
RUN = f"{FANOUT}-{UNIT}"
ATTEMPT = "invocation-one-attempt-one"


def mapping(value: object) -> dict[str, object]:
    if not is_string_map(value):
        raise AssertionError(f"expected object, got {type(value)}")
    return value


def rows(value: object) -> list[dict[str, object]]:
    if not is_object_list(value):
        raise AssertionError("expected array")
    return [mapping(item) for item in value]


def diagnostic(*, stdout: bytes = b"", stderr: bytes = b"compiler failed\n",
               phase: str = "worker", reason: str = "nonzero", code: int | None = 3,
               source: str = "process") -> FailureDiagnostic:
    capture = FanoutOutput()
    capture.feed("stdout", stdout)
    capture.feed("stderr", stderr)
    capture.finish("stdout")
    capture.finish("stderr")
    result = build_failure_diagnostic(
        fanout_id=FANOUT, unit_id=UNIT, run_ref=RUN, owner="codex",
        attempt_id=ATTEMPT, worktree_ref="worktree-core", base_sha="a" * 40,
        observed_revision=None, phase=phase, reason=reason, returncode=code,
        exit_code_source=source, streams=capture.streams(),
    )
    if result is None:
        raise AssertionError("failure fixture missing")
    return result


def event(name: str = "worker_result", *, diag: object = None,
          attempt: str | None = ATTEMPT, status: str = "failed") -> dict[str, object]:
    result: dict[str, object] = {
        "event": name, "target_type": "run", "target_id": RUN, "run_id": RUN,
        "worker_ref": UNIT, "fanout_id": FANOUT, "runtime_profile": "codex",
        "worktree_ref": "worktree-core", "base_sha": "a" * 40,
        "observed_at": "2026-09-09T12:00:00Z", "status": status,
        "summary": "", "evidence_refs": [],
    }
    if attempt is not None:
        result["attempt_id"] = attempt
    if diag is not None:
        result["failure_diagnostic"] = diag
    return result


def unit_entry(diag: object = None, **changes: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "unit_id": UNIT, "run_ref": RUN, "owner": "codex", "attempt_id": ATTEMPT,
        "status": "failed", "exit_code": 3, "process_succeeded": False,
        "worktree_ref": "worktree-core", "base_sha": "a" * 40,
        "recovery": {"outcome": "patch_recorded"},
    }
    if diag is not None:
        entry["failure_diagnostic"] = diag
    entry.update(changes)
    return entry


def summary(entry: dict[str, object]) -> dict[str, object]:
    return {"fanout_id": FANOUT, "base_sha": "a" * 40,
            "merge_order": [UNIT], "units": [entry]}


class DiagnosticViewTests(unittest.TestCase):
    def test_d1_observed_and_runtime_journals_preserve_stderr_failure(self) -> None:
        expected = diagnostic()
        built = mapping(observe(event(diag=expected)))
        self.assertEqual(built.get("failure_diagnostic"), expected)
        with TemporaryDirectory(prefix="diagnostic-views-") as tmp:
            paths = OmhPaths(omh_home=Path(tmp) / "omh", hermes_home=Path(tmp) / "hermes")
            _ = create_run(paths, {"run_id": RUN, "skill": "coding", "harness": "test"})
            _ = append(paths, event("worker_dispatch", status="observed"))
            runtime_expected = expected.copy()
            runtime_expected["owner"] = "omx-runtime"
            stored = mapping(runtime_write(paths.runtime_runs_dir / RUN, {
                **event(diag=runtime_expected), "event_type": "worker_result",
                "runtime_profile": "omx-runtime", "evidence_refs": ["exit:3"],
            }))
            self.assertEqual(validate_runtime(stored), [])
            self.assertNotIn("failure_diagnostic", stored)
            _ = append(paths, event("unit_result_missing", status="observed"))
            shown = mapping(show(paths, RUN, history_limit=1))
            self.assertEqual(mapping(shown["lifecycle"]).get("failure_diagnostic"), runtime_expected)
            self.assertEqual(len(rows(shown["journal_events"])), 1)
            self.assertEqual(rows(shown["runtime_observations"])[0], stored)
        self.assertFalse(Path(tmp).exists())

    def test_d2_separate_streams_survive_journal_round_trip(self) -> None:
        expected = diagnostic(stdout=b"phase A failed\n", stderr=b"phase B failed\n")
        result = mapping(journal(summary(unit_entry(expected))))
        self.assertEqual(rows(result["units"])[0].get("failure_diagnostic"), expected)
        with TemporaryDirectory(prefix="diagnostic-views-") as tmp:
            path = Path(tmp) / "journal.json"
            _ = write_fanout_run_journal(path, result)
            restored = mapping(read_journal(path, expected_fanout_id=FANOUT))
            self.assertEqual(rows(restored["units"])[0].get("failure_diagnostic"), expected)
        self.assertEqual([stream["stream"] for stream in expected["streams"]], ["stdout", "stderr"])
        self.assertEqual([stream["text"] for stream in expected["streams"]],
                         ["phase A failed\n", "phase B failed\n"])

    def test_d3_limits_counts_and_digest_survive_projection(self) -> None:
        for raw in (b"compiler failed\n" * 200, ("\uac00\U0001f642\n" * 1001).encode()):
            with self.subTest(size=len(raw)):
                expected = diagnostic(stderr=raw)
                projected = mapping(observe(event(diag=expected)))
                self.assertEqual(projected.get("failure_diagnostic"), expected)
                stream = expected["streams"][1]
                self.assertEqual(stream["original_bytes"], len(raw))
                self.assertEqual(stream["original_lines"], raw.count(b"\n"))
                self.assertEqual((stream["limit_bytes"], stream["limit_lines"]), (2000, 20))
                self.assertTrue(stream["truncated"])
                self.assertLessEqual(stream["kept_bytes"], 2000)
                self.assertEqual(stream["digest"], hashlib.sha256(stream["text"].encode()).hexdigest())

    def test_d4_sanitized_data_reaches_board_but_tampering_does_not(self) -> None:
        for raw in (b"Authorization: Bearer SECRET_SENTINEL", b"def SOURCE_SENTINEL(): pass",
                    b"\x1b]52;c;CONTROL_SENTINEL\x07", b'{"content":"PROMPT_SENTINEL"}'):
            with self.subTest(raw=raw):
                expected = diagnostic(stderr=raw)
                with TemporaryDirectory(prefix="diagnostic-views-") as tmp:
                    paths = OmhPaths(omh_home=Path(tmp) / "omh", hermes_home=Path(tmp) / "hermes")
                    directory = paths.fanout_contracts_dir / FANOUT
                    directory.mkdir(parents=True)
                    _ = (directory / "dispatch_summary.json").write_text(json.dumps(summary(unit_entry(expected))))
                    payload = mapping(board(paths, now="2026-09-09T12:00:00Z"))
                    self.assertEqual(rows(payload["units"])[0].get("failure_diagnostic"), expected)
                    rendered = render_status_board_text(payload)
                    for sentinel in ("SECRET_SENTINEL", "SOURCE_SENTINEL", "CONTROL_SENTINEL", "PROMPT_SENTINEL"):
                        self.assertNotIn(sentinel, json.dumps(payload) + rendered)
                    self.assertNotIn("\x1b", rendered)
        tampered = dict(diagnostic())
        tampered["spill_ref"] = "/must/not/read/PRIVATE_SENTINEL"
        self.assertNotIn("failure_diagnostic", mapping(observe(event(diag=tampered))))

    def test_d5_batch_failure_preserves_explicit_nullable_attempt(self) -> None:
        expected = build_failure_diagnostic(
            fanout_id=FANOUT, unit_id=None, run_ref=FANOUT, owner="codex",
            attempt_id=None, worktree_ref=None, base_sha=None, observed_revision=None,
            phase="dispatcher", reason="internal_error", returncode=None,
            exit_code_source="not_observed",
        )
        record = mapping(observe({
            "run_id": FANOUT, "target_id": FANOUT, "target_type": "run",
            "event": "failed", "status": "failed", "runtime_profile": "codex",
            "attempt_id": None, "failure_diagnostic": expected,
        }))
        self.assertEqual(record.get("failure_diagnostic"), expected)
        self.assertEqual(mapping(lifecycle([record], run_id=FANOUT)).get("failure_diagnostic"), expected)
        record["attempt_id"] = 42
        self.assertNotIn("failure_diagnostic", mapping(observe(record)))

    def test_d5_phase_and_exit_provenance_never_promote_execution(self) -> None:
        cases = [("worker", "nonzero", 127, "process"), ("launch", "missing_binary", 127, "synthetic"),
                 ("worktree", "denial", None, "not_observed"), ("timeout", "deadline", 124, "synthetic"),
                 ("verification", "nonzero", 1, "process"), ("dispatcher", "internal_error", None, "not_observed")]
        for phase, reason, code, source in cases:
            with self.subTest(phase=phase):
                expected = diagnostic(phase=phase, reason=reason, code=code, source=source)
                result = mapping(lifecycle([event(diag=expected)], run_id=RUN))
                self.assertEqual(result.get("failure_diagnostic"), expected)
                self.assertFalse(result["execution_observed"])
                self.assertFalse(result["verification_observed"])
                self.assertFalse(result["merge_observed"])
        expected = diagnostic(phase="verification", code=1)
        result = mapping(journal(summary(unit_entry(expected, status="completed", process_succeeded=True, exit_code=0))))
        self.assertEqual(rows(result["units"])[0]["terminal_state"], "succeeded")
        self.assertEqual(rows(result["units"])[0].get("failure_diagnostic"), expected)

    def test_d6_later_sidecar_preserves_worker_but_new_attempt_clears(self) -> None:
        expected = diagnostic()
        events = [event("worker_dispatch", status="observed"), event(diag=expected),
                  event("unit_result_missing", status="observed", attempt=None)]
        self.assertEqual(mapping(lifecycle(events, run_id=RUN)).get("failure_diagnostic"), expected)
        events.append(event("worker_dispatch", status="observed", attempt="invocation-two-attempt-one"))
        self.assertNotIn("failure_diagnostic", mapping(lifecycle(events, run_id=RUN)))
        # A delayed old-attempt event cannot become current merely by arriving last.
        events.append(event(diag=expected))
        self.assertNotIn("failure_diagnostic", mapping(lifecycle(events, run_id=RUN)))
        # A legacy redispatch is also a boundary, not permission to borrow.
        events = [event(diag=expected), event("worker_dispatch", status="observed", attempt=None)]
        self.assertNotIn("failure_diagnostic", mapping(lifecycle(events, run_id=RUN)))

    def test_d6_foreign_missing_and_malformed_bindings_are_unavailable(self) -> None:
        expected = diagnostic()
        for key, value in (("attempt_id", "old"), ("run_ref", "foreign"), ("unit_id", "docs"),
                           ("fanout_id", "fanout-ffffffffffff"), ("owner", "claude-code"),
                           ("worktree_ref", "foreign"), ("base_sha", "b" * 40), ("schema_version", "unknown")):
            with self.subTest(key=key):
                bad = dict(expected)
                bad[key] = value
                self.assertNotIn("failure_diagnostic", mapping(observe(event(diag=bad))))
                projected = mapping(journal(summary(unit_entry(bad))))
                self.assertNotIn("failure_diagnostic", rows(projected["units"])[0])
        self.assertNotIn("failure_diagnostic", mapping(observe(event(diag=expected, attempt=None))))
        for raw in (b"", b"\xff\xfe\x80err\n", b'{"broken":'):
            expected = diagnostic(stderr=raw)
            self.assertEqual(mapping(observe(event(diag=expected))).get("failure_diagnostic"), expected)

    def test_d6_cli_brief_status_and_show_use_current_diagnostic(self) -> None:
        expected = diagnostic()
        with TemporaryDirectory(prefix="diagnostic-views-") as tmp:
            paths = OmhPaths(omh_home=Path(tmp) / "omh", hermes_home=Path(tmp) / "hermes")
            directory = paths.fanout_contracts_dir / FANOUT
            directory.mkdir(parents=True)
            _ = (directory / "fanout_contract.json").write_text(json.dumps({
                "fanout_id": FANOUT, "units": [{"unit_id": UNIT, "run_ref": RUN, "owner": "codex"}],
                "merge_plan": {"merge_order": [UNIT]},
            }))
            _ = (directory / "dispatch_summary.json").write_text(json.dumps(summary(unit_entry(expected))))
            _ = create_run(paths, {"run_id": RUN, "skill": "coding", "harness": "test"})
            _ = append(paths, event("worker_dispatch", status="observed"))
            _ = append(paths, event(diag=expected))
            _ = append(paths, event("unit_result_missing", status="observed"))
            base = ["--omh-home", str(paths.omh_home), "--hermes-home", str(paths.hermes_home)]
            for command, args in (("brief", [FANOUT]), ("status", ["--fanout-id", FANOUT]), ("show", [FANOUT, "--full"])):
                code, out, err = run_cli(base + ["coding", "fanout", command, *args])
                self.assertEqual(code, 0, err)
                payload = mapping(decode(out))
                units = payload["units"]
                row = mapping(mapping(units)[UNIT]) if command == "show" else rows(units)[0]
                self.assertEqual(row.get("failure_diagnostic"), expected, command)
            result = mapping(roster(paths, FANOUT))
            self.assertFalse(rows(result["units"])[0]["process_succeeded"])
            self.assertIn("stderr", render_fanout_status_text(result))
            # Journal knows a new attempt; the still-old summary cannot override it.
            _ = append(paths, event("worker_dispatch", status="observed", attempt="new-invocation"))
            code, out, err = run_cli(base + ["coding", "fanout", "brief", FANOUT, "--json"])
            self.assertEqual(code, 0, err)
            self.assertNotIn("failure_diagnostic", rows(mapping(decode(out))["units"])[0])
        self.assertFalse(Path(tmp).exists())

    def test_d6_board_rejects_stale_summary_after_journal_redispatch(self) -> None:
        expected = diagnostic()
        with TemporaryDirectory(prefix="diagnostic-views-") as tmp:
            paths = OmhPaths(omh_home=Path(tmp) / "omh", hermes_home=Path(tmp) / "hermes")
            directory = paths.fanout_contracts_dir / FANOUT
            directory.mkdir(parents=True)
            _ = (directory / "dispatch_summary.json").write_text(json.dumps(summary(unit_entry(expected))))
            _ = append(paths, event("worker_dispatch", status="observed"))
            _ = append(paths, event(diag=expected))
            self.assertEqual(rows(mapping(board(paths))["units"])[0].get("failure_diagnostic"), expected)
            _ = append(paths, event("worker_dispatch", status="observed", attempt="new-invocation"))
            self.assertNotIn("failure_diagnostic", rows(mapping(board(paths))["units"])[0])

    def test_d6_outer_workspace_and_process_exit_must_match(self) -> None:
        for changes in ({"worktree_path": "foreign"}, {"exit_code": 4}):
            with self.subTest(changes=changes):
                result = mapping(journal(summary(unit_entry(diagnostic(), **changes))))
                self.assertNotIn("failure_diagnostic", rows(result["units"])[0])

    def test_d7_legacy_success_and_recovery_authority_are_unchanged(self) -> None:
        result = mapping(journal(summary(unit_entry(status="completed", process_succeeded=True, exit_code=0))))
        row = rows(result["units"])[0]
        self.assertEqual(row["terminal_state"], "succeeded")
        self.assertNotIn("failure_diagnostic", row)
        plan = mapping(resume(result, order=[UNIT], depends_on={UNIT: []}))
        self.assertEqual(plan["selected_units"], [])
        self.assertEqual(plan["held_units"], [UNIT])
        failed = mapping(journal(summary(unit_entry())))
        plan = mapping(resume(failed, order=[UNIT], depends_on={UNIT: []}))
        self.assertEqual(rows(plan["decisions"])[0]["action"], "hold_replay_unsafe")

    def test_d7_held_diagnostic_survives_without_becoming_rerun_evidence(self) -> None:
        expected = diagnostic()
        original = mapping(journal(summary(unit_entry(expected))))
        plan = mapping(resume(original, order=[UNIT], depends_on={UNIT: []}))
        held = unit_entry(status="not_selected", resume=rows(plan["decisions"])[0])
        replayed = mapping(journal(summary(held)))
        self.assertEqual(rows(replayed["units"])[0].get("failure_diagnostic"), expected)
        # A copied carry-forward for another unit must not override this unit.
        foreign = deepcopy(held)
        foreign["unit_id"] = "docs"
        result = mapping(journal({"fanout_id": FANOUT, "merge_order": ["docs"], "units": [foreign]}))
        self.assertNotIn("failure_diagnostic", rows(result["units"])[0])


if __name__ == "__main__":
    _ = unittest.main()
