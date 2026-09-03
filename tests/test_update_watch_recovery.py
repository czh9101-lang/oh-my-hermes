"""Contract for issue #1282: recover the update watch from rewritten branch history.

The startup update-availability check (`omh.maintenance.update_check`) stores
an upstream branch commit as a comparison cursor and re-checks it on a
schedule. When `origin/main` is rewound, force-updated, or recreated, plain
string equality misclassifies the relationship as "behind" and prints an
"update available" notice for a fast-forward that was never verified.

This file pins the recovery contract:

- every fresh probe classifies ancestry (`fast_forward`, `rewound`,
  `rewritten`, `branch_recreated`, `cursor_unreachable`,
  `default_branch_changed`, `unknown`) instead of asserting an unverified
  fast-forward;
- the repository-metadata read rides the same single bounded curl subprocess
  as the branch-head read (at most two HTTP requests, 1.5 s per transfer,
  the existing 2.0 s whole-process bound);
- an unreachable cursor opens a recorded coverage gap (old/new refs plus the
  uncertain interval) and never reports complete coverage;
- the cursor in `state.json` is pinned while a gap is open and unaccepted
  (`cursor_advance_allowed`), and a maintainer may accept a gap explicitly;
- repeated probes of the same rewritten head dedupe to one ledger entry per
  event key.

RED history: the first version of this file contained only
`CursorUnreachableRecoveryTests.test_unreachable_cursor_classifies_and_opens_a_gap`,
importing only the existing `omh.maintenance.update_check`. Against the
pre-implementation module it FAILED with `None != 'cursor_unreachable'` (the
result carried no ancestry at all) with zero errors; the demo imports below
were added only after that RED was recorded.

Every test fakes the `subprocess.run`-shaped `runner` callable; none of them
may spawn a real process or reach a real socket.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from omh.local_store import atomic_write_json, read_json_object
from omh.maintenance.update_check import (
    UPDATE_CHECK_CACHE_SCHEMA_VERSION,
    accept_update_check_gap,
    cursor_advance_allowed,
    evaluate_update_check,
    format_notice_line,
    read_update_check_cache,
    update_check_cache_path,
    write_update_check_policy,
)
from omh.paths import OmhPaths
from omh.runtime.update_watch_recovery import (
    UPDATE_WATCH_RECOVERY_DEMO_SCHEMA_VERSION,
    demo_rewrite_recovery,
)

_T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _paths(root: Path) -> OmhPaths:
    resolved = root.resolve()
    return OmhPaths(resolved / ".omh", resolved / ".hermes")


def _write_local_commit(paths: OmhPaths, sha: str) -> None:
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(paths.runtime_state_path, {"release_source_commit": sha})


def _http(status: int, body: object = None, *, etag: str = "") -> str:
    lines = [f"HTTP/2 {status}"]
    if etag:
        lines.append(f"etag: {etag}")
    payload = json.dumps(body) if body is not None else ""
    return "\n".join(lines) + "\n\n" + payload


def _runner(
    *,
    head_sha: str | None = "b" * 40,
    head_status: int = 200,
    head_time: str | None = "2026-01-02T00:00:00Z",
    default_branch: str = "main",
    metadata_status: int = 200,
    metadata_missing: bool = False,
    compare_status: int = 200,
    compare_payload: object = None,
    compare_raises: BaseException | None = None,
    tags_status: int = 200,
    tags_payload: object = None,
    releases_status: int = 200,
    releases_payload: object = None,
    calls: list | None = None,
):
    """Fake `subprocess.run` for the watch's curl probes.

    Dispatches on the URL(s) in argv: a compare or tags URL answers that
    single request; otherwise the branch-head response comes first and, when
    the probe carries a second URL, the repository-metadata response follows,
    separated by whatever `-w` write-out marker the probe supplied (mirroring
    real curl, which prints the write-out after each transfer).
    """

    def runner(argv, timeout=None):
        if calls is not None:
            calls.append((list(argv), timeout))
        urls = [arg for arg in argv if isinstance(arg, str) and arg.startswith("http")]
        write_out = ""
        if "-w" in argv:
            write_out = argv[argv.index("-w") + 1]
        if any("/compare/" in url for url in urls):
            if compare_raises is not None:
                raise compare_raises
            payload = compare_payload if compare_payload is not None else {"status": "ahead", "ahead_by": 1, "behind_by": 0}
            body = payload if compare_status == 200 else None
            return subprocess.CompletedProcess(argv, 0, stdout=_http(compare_status, body), stderr="")
        if any("/tags" in url for url in urls):
            payload = tags_payload if tags_payload is not None else [{"name": "v0.1.0"}]
            body = payload if tags_status == 200 else None
            return subprocess.CompletedProcess(argv, 0, stdout=_http(tags_status, body), stderr="")
        if any("/releases" in url for url in urls):
            payload = releases_payload if releases_payload is not None else [{"tag_name": "v0.1.0"}]
            body = payload if releases_status == 200 else None
            return subprocess.CompletedProcess(argv, 0, stdout=_http(releases_status, body), stderr="")
        head_body: dict[str, object] = {}
        if head_sha is not None:
            head_body["sha"] = head_sha
        if head_time is not None:
            head_body["commit"] = {"committer": {"date": head_time}}
        head = _http(head_status, head_body if head_status == 200 else None, etag='"fake"')
        if metadata_missing or len(urls) < 2:
            return subprocess.CompletedProcess(argv, 0, stdout=head, stderr="")
        metadata = _http(
            metadata_status,
            {"default_branch": default_branch} if metadata_status == 200 else None,
        )
        return subprocess.CompletedProcess(argv, 0, stdout=head + write_out + metadata + write_out, stderr="")

    return runner


def _refusing_runner():
    def runner(argv, timeout=None):  # pragma: no cover - fails the test if reached
        raise AssertionError("no subprocess spawn is allowed here")

    return runner


def _ledger_entries(cache: dict, *, source: str | None = None, event_key: str | None = None) -> list[dict]:
    entries = [e for e in cache.get("recovery_attempts", []) if isinstance(e, dict)]
    if source is not None:
        entries = [e for e in entries if e.get("source") == source]
    if event_key is not None:
        entries = [e for e in entries if event_key in (e.get("candidate_event_keys") or [])]
    return entries


class CursorUnreachableRecoveryTests(unittest.TestCase):
    def test_unreachable_cursor_classifies_and_opens_a_gap(self) -> None:
        # The pin: cursor a*40 is recorded, the new head b*40 is readable,
        # but the compare read answers HTTP 404 -- the cursor is unreachable
        # from the rewritten branch. The watch must classify
        # `cursor_unreachable`, open a coverage gap, record the observed
        # default branch, and pin the stored cursor -- never report the
        # rewrite as a routine "behind".
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            write_update_check_policy(paths, mode="notify")
            _write_local_commit(paths, "a" * 40)
            result = evaluate_update_check(paths, runner=_runner(compare_status=404))
            self.assertEqual(result.get("ancestry"), "cursor_unreachable")
            gap = result.get("gap") or {}
            self.assertEqual(gap.get("status"), "open")
            self.assertEqual(result.get("default_branch"), "main")
            self.assertNotEqual(result.get("outcome"), "behind")
            self.assertNotEqual(result.get("outcome"), "up_to_date")
            state = read_json_object(paths.runtime_state_path) or {}
            self.assertEqual(state.get("release_source_commit"), "a" * 40)

    def test_unreachable_cursor_records_refs_interval_and_sources(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            write_update_check_policy(paths, mode="notify")
            _write_local_commit(paths, "a" * 40)
            # Anchor a clean cutoff first so the gap's interval starts there.
            first = evaluate_update_check(paths, now=_T0, runner=_runner(head_sha="a" * 40))
            self.assertEqual(first["ancestry"], "fast_forward")
            later = _T0 + timedelta(hours=3)
            result = evaluate_update_check(
                paths, now=later, force=True, runner=_runner(head_sha="b" * 40, compare_status=404)
            )
            gap = result["gap"]
            self.assertEqual(gap["status"], "open")
            self.assertEqual(gap["since"], _T0.isoformat(timespec="seconds"))
            self.assertEqual(gap["until"], later.isoformat(timespec="seconds"))
            cache = read_update_check_cache(paths)
            compare = _ledger_entries(cache, source="compare")
            self.assertEqual(len(compare), 1)
            self.assertEqual(compare[0]["result"], "not_found")
            self.assertEqual(compare[0]["old_ref"], "a" * 40)
            self.assertEqual(compare[0]["new_ref"], "b" * 40)
            # The bounded tags recovery read fired and was recorded.
            self.assertEqual(len(_ledger_entries(cache, source="tags")), 1)
            # A readable new head is never complete coverage.
            self.assertFalse(cursor_advance_allowed(paths))

    def test_branch_recreation_has_a_distinct_replayable_outcome(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            write_update_check_policy(paths, mode="notify")
            _write_local_commit(paths, "a" * 40)
            evaluate_update_check(
                paths,
                now=_T0,
                runner=_runner(head_sha="a" * 40, head_time="2026-01-05T00:00:00Z"),
            )
            later = _T0 + timedelta(hours=1)
            result = evaluate_update_check(
                paths,
                now=later,
                force=True,
                runner=_runner(
                    head_sha="b" * 40,
                    head_time="2026-01-01T00:00:00Z",
                    compare_status=404,
                ),
            )
            self.assertEqual(result["ancestry"], "branch_recreated")
            self.assertNotEqual(result["ancestry"], "cursor_unreachable")
            self.assertNotEqual(result["ancestry"], "unknown")
            gap = result["gap"]
            self.assertEqual(gap["status"], "open")
            self.assertEqual(gap["reason"], "branch_recreated")
            self.assertEqual(gap["since"], _T0.isoformat(timespec="seconds"))
            self.assertEqual(gap["until"], later.isoformat(timespec="seconds"))
            cache = read_update_check_cache(paths)
            compare = _ledger_entries(cache, source="compare")
            self.assertEqual(len(compare), 1)
            self.assertEqual(compare[0]["old_ref"], "a" * 40)
            self.assertEqual(compare[0]["new_ref"], "b" * 40)
            self.assertFalse(cursor_advance_allowed(paths))
            state = read_json_object(paths.runtime_state_path) or {}
            self.assertEqual(state.get("release_source_commit"), "a" * 40)
            notice = format_notice_line(result)
            self.assertIn("ancestry: branch_recreated", notice)
            self.assertIn("coverage gap", notice)
            self.assertIn("re-anchor", notice)


class FastForwardContractTests(unittest.TestCase):
    def test_fast_forward_classifies_and_keeps_incremental_behavior(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            write_update_check_policy(paths, mode="auto")
            _write_local_commit(paths, "a" * 40)
            result = evaluate_update_check(paths, now=_T0, runner=_runner())
            self.assertEqual(result["ancestry"], "fast_forward")
            self.assertEqual(result["outcome"], "behind")
            self.assertTrue(result["should_auto_update"])
            cache = read_update_check_cache(paths)
            self.assertEqual(cache["schema_version"], "omh_update_check_cache/v2")
            self.assertEqual(cache["watched_branch"], "main")
            self.assertEqual(cache["default_branch"], "main")
            self.assertEqual(cache["remote_head_time"], "2026-01-02T00:00:00Z")
            self.assertEqual(cache["last_successful_cutoff"], _T0.isoformat(timespec="seconds"))
            self.assertEqual(cache["branch_generation"], 0)
            self.assertEqual((cache.get("gap") or {}).get("status"), "none")
            self.assertTrue(cursor_advance_allowed(paths))

    def test_rewound_head_is_not_reported_as_update_available(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            write_update_check_policy(paths, mode="auto")
            _write_local_commit(paths, "a" * 40)
            result = evaluate_update_check(
                paths, runner=_runner(compare_payload={"status": "behind", "ahead_by": 0, "behind_by": 3})
            )
            self.assertEqual(result["ancestry"], "rewound")
            self.assertNotEqual(result["outcome"], "behind")
            self.assertFalse(result["should_auto_update"])
            self.assertNotIn("OMH update available", format_notice_line(result))

    def test_diverged_history_classifies_rewritten_and_dedupes(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            write_update_check_policy(paths, mode="notify")
            _write_local_commit(paths, "a" * 40)
            diverged = _runner(compare_payload={"status": "diverged", "ahead_by": 1, "behind_by": 1})
            first = evaluate_update_check(paths, now=_T0, runner=diverged)
            self.assertEqual(first["ancestry"], "rewritten")
            self.assertEqual(first["outcome"], "inconclusive")
            self.assertEqual(first["gap"]["status"], "open")
            second = evaluate_update_check(paths, now=_T0 + timedelta(hours=1), force=True, runner=diverged)
            self.assertTrue(second["checked"])
            cache = read_update_check_cache(paths)
            # Re-probing the same rewritten head updates the existing ledger
            # entry instead of appending a duplicate finding.
            moved = _ledger_entries(cache, event_key=f"head_moved:main:{'b' * 40}")
            self.assertEqual(len(moved), 1)
            self.assertEqual(moved[0]["attempted_at"], (_T0 + timedelta(hours=1)).isoformat(timespec="seconds"))
            self.assertEqual(moved[0]["old_ref"], "a" * 40)
            self.assertEqual(moved[0]["new_ref"], "b" * 40)


class DefaultBranchChangedTests(unittest.TestCase):
    def test_default_branch_change_classifies_distinctly_and_pins_the_cursor(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            write_update_check_policy(paths, mode="auto")
            _write_local_commit(paths, "a" * 40)
            # Prime the persisted default branch with a clean observation.
            evaluate_update_check(paths, now=_T0, runner=_runner(head_sha="a" * 40))
            result = evaluate_update_check(
                paths, now=_T0 + timedelta(hours=1), force=True, runner=_runner(default_branch="trunk")
            )
            self.assertEqual(result["ancestry"], "default_branch_changed")
            self.assertNotEqual(result["outcome"], "behind")
            self.assertNotEqual(result["outcome"], "up_to_date")
            self.assertFalse(result["should_auto_update"])
            gap = result["gap"]
            self.assertEqual(gap["status"], "open")
            self.assertEqual(gap["reason"], "default_branch_changed")
            self.assertIn("main", gap["note"])
            self.assertIn("trunk", gap["note"])
            cache = read_update_check_cache(paths)
            metadata = _ledger_entries(cache, source="repo_metadata", event_key="default_branch_changed:main:trunk")
            self.assertEqual(len(metadata), 1)
            self.assertEqual(metadata[0]["old_ref"], "main")
            self.assertEqual(metadata[0]["new_ref"], "trunk")
            state = read_json_object(paths.runtime_state_path) or {}
            self.assertEqual(state.get("release_source_commit"), "a" * 40)
            self.assertFalse(cursor_advance_allowed(paths))
            notice = format_notice_line(result)
            self.assertIn("default branch changed", notice)
            self.assertNotIn("OMH update available", notice)

    def test_first_metadata_read_never_fires_a_spurious_default_branch_changed(self) -> None:
        # A v1 cache carries no default_branch; the first observation only
        # persists it -- no migration write, no spurious classification.
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            write_update_check_policy(paths, mode="notify")
            _write_local_commit(paths, "a" * 40)
            result = evaluate_update_check(paths, runner=_runner(head_sha="a" * 40))
            self.assertEqual(result["ancestry"], "fast_forward")
            self.assertEqual(result["outcome"], "up_to_date")
            self.assertEqual(read_update_check_cache(paths)["default_branch"], "main")


class ShallowOrDelayedVisibilityTests(unittest.TestCase):
    def test_missing_head_sha_is_unknown_with_an_open_gap(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            write_update_check_policy(paths, mode="notify")
            _write_local_commit(paths, "a" * 40)
            result = evaluate_update_check(paths, runner=_runner(head_sha=None))
            self.assertEqual(result["ancestry"], "unknown")
            self.assertEqual(result["gap"]["status"], "open")
            self.assertNotEqual(result["outcome"], "up_to_date")
            cache = read_update_check_cache(paths)
            self.assertEqual(cache["gap"]["reason"], "unknown")
            self.assertFalse(cursor_advance_allowed(paths))

    def test_regressing_head_time_is_unknown_with_an_open_gap(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            write_update_check_policy(paths, mode="notify")
            _write_local_commit(paths, "a" * 40)
            evaluate_update_check(paths, now=_T0, runner=_runner(head_sha="a" * 40, head_time="2026-01-05T00:00:00Z"))
            result = evaluate_update_check(
                paths,
                now=_T0 + timedelta(hours=1),
                force=True,
                runner=_runner(head_sha="b" * 40, head_time="2026-01-01T00:00:00Z"),
            )
            self.assertEqual(result["ancestry"], "unknown")
            self.assertEqual(result["gap"]["status"], "open")
            self.assertNotEqual(result["outcome"], "up_to_date")
            # The recorded head time never regresses.
            self.assertEqual(read_update_check_cache(paths)["remote_head_time"], "2026-01-05T00:00:00Z")


class ProbeBudgetTests(unittest.TestCase):
    def test_metadata_read_shares_the_probe_subprocess_and_budget(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            write_update_check_policy(paths, mode="notify")
            _write_local_commit(paths, "a" * 40)
            calls: list = []
            result = evaluate_update_check(paths, runner=_runner(head_sha="a" * 40, calls=calls))
            self.assertEqual(result["outcome"], "up_to_date")
            # Head == cursor: exactly one subprocess, carrying the head URL
            # first and the repository-metadata URL second.
            self.assertEqual(len(calls), 1)
            argv, timeout = calls[0]
            self.assertEqual(argv[0], "curl")
            urls = [arg for arg in argv if arg.startswith("http")]
            self.assertEqual(
                urls,
                [
                    "https://api.github.com/repos/rlaope/oh-my-hermes/commits/main",
                    "https://api.github.com/repos/rlaope/oh-my-hermes",
                ],
            )
            self.assertIn("--max-time", argv)
            self.assertEqual(argv[argv.index("--max-time") + 1], "1.5")
            self.assertEqual(timeout, 2.0)

    def test_off_mode_spawns_zero_processes(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            result = evaluate_update_check(paths, runner=_refusing_runner())
            self.assertEqual(result["outcome"], "skipped_off")
            self.assertEqual(result["ancestry"], "unknown")
            self.assertEqual((result.get("gap") or {}).get("status"), "none")

    def test_starved_metadata_read_is_a_recorded_partial_not_a_crash(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            write_update_check_policy(paths, mode="notify")
            _write_local_commit(paths, "a" * 40)
            result = evaluate_update_check(paths, runner=_runner(metadata_missing=True))
            # The head classification proceeds normally...
            self.assertEqual(result["ancestry"], "fast_forward")
            self.assertEqual(result["outcome"], "behind")
            # ...the default branch stays unobserved (never a false
            # default_branch_changed), and the partial read is recorded.
            self.assertEqual(result["default_branch"], "")
            cache = read_update_check_cache(paths)
            self.assertNotIn("default_branch", cache)
            partial = _ledger_entries(cache, source="repo_metadata")
            self.assertEqual(len(partial), 1)
            self.assertEqual(partial[0]["result"], "error")

    def test_compare_and_recovery_sources_never_fire_on_the_equal_sha_path(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            write_update_check_policy(paths, mode="notify")
            _write_local_commit(paths, "a" * 40)
            calls: list = []
            evaluate_update_check(paths, runner=_runner(head_sha="a" * 40, calls=calls))
            urls = [arg for argv, _ in calls for arg in argv if arg.startswith("http")]
            self.assertFalse(any("/compare/" in url for url in urls))
            self.assertFalse(any("/tags" in url for url in urls))
            self.assertFalse(any("/releases" in url for url in urls))


class AncestryProbeFailureTests(unittest.TestCase):
    def test_compare_failure_is_unknown_and_keeps_the_previous_outcome(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            write_update_check_policy(paths, mode="notify")
            _write_local_commit(paths, "a" * 40)
            first = evaluate_update_check(paths, now=_T0, runner=_runner(head_sha="a" * 40))
            self.assertEqual(first["outcome"], "up_to_date")
            cutoff = read_update_check_cache(paths)["last_successful_cutoff"]
            second = evaluate_update_check(
                paths,
                now=_T0 + timedelta(hours=1),
                force=True,
                runner=_runner(compare_raises=subprocess.TimeoutExpired(cmd=["curl"], timeout=1.5)),
            )
            self.assertEqual(second["ancestry"], "unknown")
            self.assertEqual(second["outcome"], "up_to_date")
            cache = read_update_check_cache(paths)
            self.assertEqual(cache["last_successful_cutoff"], cutoff)
            self.assertEqual((cache.get("gap") or {}).get("status"), "none")
            # A failed ancestry probe blocks cursor advancement.
            self.assertFalse(cursor_advance_allowed(paths))


class RecoveryWindowTests(unittest.TestCase):
    def _open_rewrite_gap(
        self,
        paths: OmhPaths,
        *,
        tags_status: int,
        releases_status: int = 200,
    ) -> None:
        write_update_check_policy(paths, mode="notify")
        _write_local_commit(paths, "a" * 40)
        evaluate_update_check(paths, now=_T0, runner=_runner(head_sha="a" * 40))
        result = evaluate_update_check(
            paths,
            now=_T0 + timedelta(hours=1),
            force=True,
            runner=_runner(
                head_sha="b" * 40,
                compare_status=404,
                tags_status=tags_status,
                releases_status=releases_status,
            ),
        )
        self.assertEqual(result["ancestry"], "cursor_unreachable")
        self.assertEqual(result["gap"]["status"], "open")

    def test_rewrite_enumerates_tags_and_releases(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            write_update_check_policy(paths, mode="notify")
            _write_local_commit(paths, "a" * 40)
            evaluate_update_check(paths, now=_T0, runner=_runner(head_sha="a" * 40))
            calls: list = []

            evaluate_update_check(
                paths,
                now=_T0 + timedelta(hours=1),
                force=True,
                runner=_runner(head_sha="b" * 40, compare_status=404, calls=calls),
            )

            urls = [arg for argv, _ in calls for arg in argv if arg.startswith("http")]
            self.assertTrue(any("/tags" in url for url in urls))
            self.assertTrue(any("/releases" in url for url in urls))
            cache = read_update_check_cache(paths)
            self.assertEqual(_ledger_entries(cache, source="tags")[0]["result"], "ok")
            self.assertEqual(_ledger_entries(cache, source="releases")[0]["result"], "ok")

    def test_partial_recovery_source_failure_keeps_the_gap_open(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            self._open_rewrite_gap(paths, tags_status=500)
            cache = read_update_check_cache(paths)
            tags = _ledger_entries(cache, source="tags")
            self.assertEqual(len(tags), 1)
            self.assertEqual(tags[0]["result"], "error")
            self.assertFalse(cursor_advance_allowed(paths))
            # The cursor converges (as after `omh update`), but the failed
            # source was never enumerated: coverage stays incomplete.
            _write_local_commit(paths, "b" * 40)
            converged = evaluate_update_check(
                paths,
                now=_T0 + timedelta(hours=2),
                force=True,
                runner=_runner(head_sha="b" * 40, compare_status=404, tags_status=500),
            )
            self.assertEqual(converged["outcome"], "up_to_date")
            self.assertEqual(converged["gap"]["status"], "open")
            self.assertFalse(cursor_advance_allowed(paths))
            # ...so the notice still reports the gap instead of staying silent.
            self.assertIn("coverage gap", format_notice_line(converged))

    def test_release_recovery_failure_keeps_the_gap_open(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            self._open_rewrite_gap(paths, tags_status=200, releases_status=500)
            cache = read_update_check_cache(paths)
            releases = _ledger_entries(cache, source="releases")
            self.assertEqual(len(releases), 1)
            self.assertEqual(releases[0]["result"], "error")
            self.assertFalse(cursor_advance_allowed(paths))
            _write_local_commit(paths, "b" * 40)

            converged = evaluate_update_check(
                paths,
                now=_T0 + timedelta(hours=2),
                force=True,
                runner=_runner(head_sha="b" * 40, tags_status=200, releases_status=500),
            )

            self.assertEqual(converged["gap"]["status"], "open")
            self.assertFalse(cursor_advance_allowed(paths))

    def test_successful_recovery_closes_the_gap_and_advances_the_generation(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            self._open_rewrite_gap(paths, tags_status=500)
            _write_local_commit(paths, "b" * 40)
            recovered = evaluate_update_check(
                paths,
                now=_T0 + timedelta(hours=2),
                force=True,
                runner=_runner(head_sha="b" * 40, compare_status=404, tags_status=200),
            )
            self.assertEqual(recovered["ancestry"], "fast_forward")
            self.assertEqual(recovered["gap"]["status"], "none")
            self.assertEqual(recovered["branch_generation"], 1)
            self.assertTrue(cursor_advance_allowed(paths))
            cache = read_update_check_cache(paths)
            self.assertEqual(cache["branch_generation"], 1)
            self.assertEqual(cache["gap"]["status"], "none")
            self.assertEqual(
                cache["last_successful_cutoff"], (_T0 + timedelta(hours=2)).isoformat(timespec="seconds")
            )


class GapAcceptanceTests(unittest.TestCase):
    def test_accept_gap_marks_policy_acceptance_and_unblocks_advancement(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            write_update_check_policy(paths, mode="notify")
            _write_local_commit(paths, "a" * 40)
            evaluate_update_check(paths, runner=_runner(compare_status=404))
            self.assertFalse(cursor_advance_allowed(paths))
            cache = accept_update_check_gap(paths)
            self.assertEqual(cache["gap"]["status"], "accepted")
            self.assertTrue(cursor_advance_allowed(paths))
            self.assertEqual(len(_ledger_entries(cache, source="maintainer")), 1)

    def test_accept_gap_is_idempotent_without_an_open_gap(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            cache = accept_update_check_gap(paths)
            self.assertEqual(cache, {})
            self.assertFalse(update_check_cache_path(paths).exists())


class V1CacheMigrationTests(unittest.TestCase):
    def test_v1_cursor_only_cache_reads_and_rewrites_as_v2(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            write_update_check_policy(paths, mode="notify")
            _write_local_commit(paths, "a" * 40)
            paths.runtime_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(
                update_check_cache_path(paths),
                {
                    "schema_version": "omh_update_check_cache/v1",
                    "last_checked_at": "2025-12-01T00:00:00+00:00",
                    "remote_commit": "a" * 40,
                    "remote_etag": '"old"',
                    "outcome": "up_to_date",
                },
            )
            result = evaluate_update_check(paths, now=_T0, runner=_runner(head_sha="a" * 40))
            self.assertEqual(result["ancestry"], "fast_forward")
            self.assertEqual(result["branch_generation"], 0)
            self.assertEqual(result["gap"]["status"], "none")
            cache = read_update_check_cache(paths)
            self.assertEqual(cache["schema_version"], UPDATE_CHECK_CACHE_SCHEMA_VERSION)
            self.assertEqual(cache["schema_version"], "omh_update_check_cache/v2")
            # Every v1 field survives the first v2 write (the fresh 200
            # probe refreshes the ETag exactly as the v1 probe always did).
            self.assertEqual(cache["remote_commit"], "a" * 40)
            self.assertEqual(cache["remote_etag"], '"fake"')
            self.assertEqual(cache["outcome"], "up_to_date")
            self.assertEqual(cache["branch_generation"], 0)


class DemoRewriteRecoveryTests(unittest.TestCase):
    def test_demo_is_deterministic(self) -> None:
        self.assertEqual(demo_rewrite_recovery(), demo_rewrite_recovery())

    def test_demo_proves_the_recovery_contract(self) -> None:
        payload = demo_rewrite_recovery()
        self.assertEqual(payload["schema_version"], UPDATE_WATCH_RECOVERY_DEMO_SCHEMA_VERSION)
        scenarios = {scenario["name"]: scenario for scenario in payload["scenarios"]}
        self.assertEqual(scenarios["fast_forward"]["ancestry"], "fast_forward")
        self.assertEqual(scenarios["fast_forward"]["outcome"], "behind")
        rewritten = scenarios["cursor_unreachable"]
        self.assertEqual(rewritten["ancestry"], "cursor_unreachable")
        self.assertEqual(rewritten["gap_status"], "open")
        self.assertEqual(rewritten["refs"]["old"], "a" * 40)
        self.assertEqual(rewritten["refs"]["new"], "c" * 40)
        self.assertTrue(rewritten["uncertain_interval"]["since"])
        self.assertTrue(rewritten["uncertain_interval"]["until"])
        # Dedupe: two probes of the same rewritten head, one ledger entry.
        self.assertEqual(payload["dedupe"]["probes"], 2)
        self.assertEqual(payload["dedupe"]["ledger_entries"], 1)
        # Incomplete coverage keeps the gap open and blocks advancement even
        # though the head is readable and the cursor converged.
        self.assertEqual(payload["coverage"]["incomplete"], "open")
        self.assertEqual(scenarios["incomplete_coverage"]["outcome"], "up_to_date")
        self.assertTrue(payload["advance_policy"]["blocked_while_recovery_incomplete"])
        # Completed recovery closes the gap and advances the generation.
        self.assertEqual(payload["coverage"]["recovered"], "none")
        self.assertEqual(scenarios["recovered"]["branch_generation"], 1)
        self.assertTrue(payload["advance_policy"]["allowed_after_recovery"])


if __name__ == "__main__":
    unittest.main()
