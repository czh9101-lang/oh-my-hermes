"""Contracts for `omh update-check` and its two integration points:

- `omh install`/`omh update` recording a comparable remote identity into
  `state.json` (`release_source_commit`) only when update-check is opted in.
- The `omh`/`hermes` launch path (`commands/main.py`) acting on the check:
  a notice line for `notify`, reusing `omh update`'s own code path for
  `auto`, and a non-blocking lock so two launches never auto-update at once.

The probe spawns `curl` as a subprocess (never a Python-level network client
-- see `tests/test_handoff_safety_contract_enforcement.py` INVARIANT 2), so
every test here patches `omh.maintenance.update_check._run_curl` -- the one
name `fetch_remote_main_identity` resolves when no `runner` is passed
explicitly -- rather than the global `subprocess.run`, so a legitimate
subprocess call elsewhere in `omh update` (e.g. command-package self-update)
is never accidentally intercepted. None of these tests may spawn a real
process or reach a real socket, and the `off`-mode tests assert that too by
making the fake raise if it is ever called.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from _cli_harness import run_cli

from omh.commands import main as main_module
from omh.local_store import atomic_write_json, read_json_object
from omh.maintenance.update_check import acquire_auto_update_lock, update_check_cache_path, write_update_check_policy
from omh.paths import OmhPaths

_RUN_CURL_TARGET = "omh.maintenance.update_check._run_curl"


def _base(root: Path) -> list[str]:
    return ["--omh-home", str(root / ".omh"), "--hermes-home", str(root / ".hermes")]


def _paths(root: Path) -> OmhPaths:
    resolved = root.resolve()
    return OmhPaths(resolved / ".omh", resolved / ".hermes")


def _http_stdout(sha: str) -> str:
    return f'HTTP/2 200\netag: "fake"\n\n{json.dumps({"sha": sha})}'


def _fake_curl(sha: str):
    def runner(argv, timeout=None):
        return subprocess.CompletedProcess(argv, 0, stdout=_http_stdout(sha), stderr="")

    return runner


def _refusing_curl():
    def runner(argv, timeout=None):  # pragma: no cover - fails the test if reached
        raise AssertionError("update-check must not spawn curl here")

    return runner


class UpdateCheckCliTests(unittest.TestCase):
    def test_status_reports_the_shipped_default(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            status, stdout, stderr = run_cli(_base(root) + ["update-check", "status"], output_json=False)
            self.assertEqual((status, stderr), (0, ""))
            self.assertIn("Update-check mode: off", stdout)
            self.assertIn("Last checked: never", stdout)

    def test_set_writes_the_policy_and_status_reflects_it(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            status, stdout, stderr = run_cli(
                _base(root) + ["update-check", "set", "--mode", "notify", "--interval-hours", "6"],
                output_json=False,
            )
            self.assertEqual((status, stderr), (0, ""))
            self.assertIn("Update-check mode: notify", stdout)
            self.assertIn("Interval: every 6.0 hour(s)", stdout)

            status, stdout, _ = run_cli(_base(root) + ["update-check", "status"], output_json=False)
            self.assertEqual(status, 0)
            self.assertIn("Update-check mode: notify", stdout)
            self.assertIn("Interval: every 6.0 hour(s)", stdout)

    def test_set_without_arguments_is_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            status, _, stderr = run_cli(_base(root) + ["update-check", "set"], output_json=False)
            self.assertEqual(status, 2)
            self.assertIn("--mode", stderr)

    def test_set_rejects_an_unknown_mode(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(SystemExit) as raised:
                run_cli(_base(root) + ["update-check", "set", "--mode", "always"], output_json=False)
            self.assertEqual(raised.exception.code, 2)

    def test_status_json_reports_the_schema(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            status, stdout, stderr = run_cli(_base(root) + ["update-check", "status", "--json"])
            self.assertEqual((status, stderr), (0, ""))
            payload = json.loads(stdout)
            self.assertEqual(payload["schema_version"], "omh_update_check_status/v1")
            self.assertEqual(payload["policy"]["mode"], "off")


class InstallRecordsRemoteIdentityTests(unittest.TestCase):
    def _state(self, root: Path) -> dict[str, object]:
        return read_json_object(_paths(root).runtime_state_path) or {}

    def test_default_off_mode_never_touches_the_network_on_update(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch(_RUN_CURL_TARGET, _refusing_curl()):
                status, _, stderr = run_cli(_base(root) + ["update", "--yes", "--no-interactive"], output_json=False)
            self.assertEqual((status, stderr), (0, ""))
            self.assertNotIn("release_source_commit", self._state(root))

    def test_notify_mode_records_the_remote_commit_on_the_preview_channel(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_cli(_base(root) + ["update-check", "set", "--mode", "notify"], output_json=False)
            with patch(_RUN_CURL_TARGET, _fake_curl("c" * 40)):
                status, _, stderr = run_cli(_base(root) + ["update", "--yes", "--no-interactive"], output_json=False)
            self.assertEqual((status, stderr), (0, ""))
            self.assertEqual(self._state(root)["release_source_commit"], "c" * 40)

    def test_local_channel_never_records_a_main_commit(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            local_source = root / "local-skills"
            local_source.mkdir()
            run_cli(_base(root) + ["update-check", "set", "--mode", "auto"], output_json=False)
            with patch(_RUN_CURL_TARGET, _refusing_curl()):
                status, _, stderr = run_cli(
                    _base(root) + ["install", "--channel", "local", "--from-skills-dir", str(local_source)],
                    output_json=False,
                )
            self.assertEqual((status, stderr), (0, ""))
            self.assertNotIn("release_source_commit", self._state(root))

    def test_a_failed_probe_never_regresses_a_previously_recorded_identity(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_cli(_base(root) + ["update-check", "set", "--mode", "notify"], output_json=False)
            with patch(_RUN_CURL_TARGET, _fake_curl("d" * 40)):
                run_cli(_base(root) + ["update", "--yes", "--no-interactive"], output_json=False)
            self.assertEqual(self._state(root)["release_source_commit"], "d" * 40)

            def failing(argv, timeout=None):
                raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout or 1.5)

            with patch(_RUN_CURL_TARGET, failing):
                status, _, stderr = run_cli(_base(root) + ["update", "--yes", "--no-interactive"], output_json=False)
            self.assertEqual((status, stderr), (0, ""))
            self.assertEqual(self._state(root)["release_source_commit"], "d" * 40)


class StartupCheckLaunchIntegrationTests(unittest.TestCase):
    def _args(self, root: Path) -> argparse.Namespace:
        return argparse.Namespace(omh_home=str(root / ".omh"), hermes_home=str(root / ".hermes"), scope=None)

    def test_off_mode_prints_nothing_and_touches_no_network(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = self._args(root)
            buffer = io.StringIO()
            with patch(_RUN_CURL_TARGET, _refusing_curl()), contextlib.redirect_stdout(buffer):
                main_module._run_startup_update_check(args)
            self.assertEqual(buffer.getvalue(), "")

    def test_notify_mode_prints_the_one_line_notice_when_behind(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            write_update_check_policy(paths, mode="notify")
            paths.runtime_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(paths.runtime_state_path, {"release_source_commit": "a" * 40})
            args = self._args(root)
            buffer = io.StringIO()
            with patch(_RUN_CURL_TARGET, _fake_curl("b" * 40)), contextlib.redirect_stdout(buffer):
                main_module._run_startup_update_check(args)
            output = buffer.getvalue()
            self.assertIn("OMH update available:", output)
            self.assertIn("omh update", output)

    def test_auto_mode_reuses_omh_update_and_records_the_new_identity(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            write_update_check_policy(paths, mode="auto")
            paths.runtime_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(paths.runtime_state_path, {"release_source_commit": "a" * 40})
            args = self._args(root)
            buffer = io.StringIO()
            with patch(_RUN_CURL_TARGET, _fake_curl("b" * 40)), contextlib.redirect_stdout(buffer):
                main_module._run_startup_update_check(args)
            state = read_json_object(paths.runtime_state_path) or {}
            self.assertIn("last_update", state)
            self.assertEqual(state["release_source_commit"], "b" * 40)

    def test_auto_mode_skips_silently_when_another_launch_holds_the_lock(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            write_update_check_policy(paths, mode="auto")
            paths.runtime_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(paths.runtime_state_path, {"release_source_commit": "a" * 40})
            args = self._args(root)
            buffer = io.StringIO()
            with acquire_auto_update_lock(paths):
                with patch(_RUN_CURL_TARGET, _fake_curl("b" * 40)), contextlib.redirect_stdout(buffer):
                    main_module._run_startup_update_check(args)
            state = read_json_object(paths.runtime_state_path) or {}
            self.assertNotIn("last_update", state)

    def test_cache_path_matches_the_documented_runtime_location(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _paths(root)
            self.assertEqual(update_check_cache_path(paths), paths.omh_home / "runtime" / "update-check.json")


if __name__ == "__main__":
    unittest.main()
