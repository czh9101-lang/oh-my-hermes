import contextlib
import io
import json
import os
from pathlib import Path, PureWindowsPath
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from _cli_harness import run_cli

from omh.commands import setup as setup_commands
from omh.commands.setup import _run_command_package_self_update
from omh.config_adapter import external_dirs
from omh.core.errors import OmhError
from omh.install import self_update, self_update_state
from omh.install.self_update import run_installer_self_update, switch_current
from omh.install.self_update_platform import SelfUpdatePlatform, _remove_directory_link
from omh.install.self_update_state import STATE_SCHEMA_VERSION, collect_garbage, pointer_target, record_pointer
from omh.system.local_store import atomic_write_json, file_lock


class StagedSelfUpdateTests(unittest.TestCase):
    @staticmethod
    def _platform() -> SelfUpdatePlatform:
        # Keep host pointer-swap semantics, but model the junction subprocess
        # with real links, as the dedicated Windows adapter tests do.
        def junction_runner(command, **kwargs):
            link = Path(kwargs["env"]["OMH_JUNCTION_LINK"])
            target = Path(kwargs["cwd"]) / kwargs["env"]["OMH_JUNCTION_TARGET"]
            link.symlink_to(target, target_is_directory=True)
            return subprocess.CompletedProcess(command, 0, "", "")

        return SelfUpdatePlatform(is_windows=SelfUpdatePlatform.host().is_windows, runner=junction_runner)

    def _fixture(self, root: Path, *, pointer: bool = False, launcher: bool = True):
        legacy = root / "venv"
        (legacy / "bin").mkdir(parents=True)
        (legacy / "bin" / "python").touch()
        (legacy / "bin" / "omh").touch()
        (root / "omh" / "skills").mkdir(parents=True)
        if launcher:
            (root / "bin").mkdir()
            (root / "bin" / "omh").symlink_to(legacy / "bin" / "omh")
        args = SimpleNamespace(
            json=True,
            omh_home=str(root / "omh"),
            hermes_home=str(root / "hermes"),
            recover_known_good=False,
        )
        plan = {
            "method": "installer",
            "release": SimpleNamespace(package_url="test://candidate", version="1.0.7"),
            "python": str(legacy / "bin" / "python"),
            "venv_dir": str(legacy),
        }
        if pointer:
            self._migrated(root, legacy, launcher=launcher)
        return legacy, args, plan

    def _migrated(self, root: Path, legacy: Path, *, launcher: bool = True) -> Path:
        bootstrap = root / "generations" / "bootstrap-legacy"
        bootstrap.mkdir(parents=True)
        (bootstrap / "venv").symlink_to(legacy, target_is_directory=True)
        (bootstrap / "skills").symlink_to(root / "omh" / "skills", target_is_directory=True)
        switch_current(root, bootstrap, platform=self._platform())
        if launcher:
            launcher_path = root / "bin" / "omh"
            launcher_path.unlink()
            launcher_path.symlink_to(root / "current" / "venv" / "bin" / "omh")
        (root / "hermes").mkdir()
        (root / "hermes" / "config.yaml").write_text(
            f"skills:\n  external_dirs:\n  - {root / 'current' / 'skills'}\n"
        )
        atomic_write_json(
            root / "self-update.json",
            {
                "schema_version": STATE_SCHEMA_VERSION,
                "active": self._entry(bootstrap, "bootstrap"),
                "previous_known_good": None,
                "pointer": {"path": str(root / "current"), "target": "generations/bootstrap-legacy"},
                "migration": {"status": "completed"},
                "activation_in_progress": None,
                "retained_generations": ["bootstrap-legacy"],
            },
            private=True,
        )
        return bootstrap

    @staticmethod
    def _entry(path: Path, kind: str = "generation") -> dict[str, str]:
        return {"id": path.name, "path": str(path), "kind": kind, "version": ""}

    def _runner(
        self,
        *,
        failure: str = "",
        version: str = "",
    ):
        def run(command, **_kwargs):
            if command[1:3] == ["-m", "venv"]:
                if failure == "venv":
                    return subprocess.CompletedProcess(command, 1, "", "venv unavailable")
                candidate = Path(command[-1]).parent
                (candidate / "venv" / "bin").mkdir(parents=True, exist_ok=True)
                (candidate / "venv" / "bin" / "python").touch()
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[1:4] == ["-m", "pip", "install"]:
                return subprocess.CompletedProcess(command, failure == "pip", "", "pip failed")
            if command[1:4] == ["-P", "-c", "import omh.cli"]:
                return subprocess.CompletedProcess(command, failure == "import", "", "import failed")
            if "--version" in command:
                return subprocess.CompletedProcess(command, 0, version or "1.0.7\n", "")
            if "update" in command:
                if failure == "pack":
                    return subprocess.CompletedProcess(command, 1, "", "pack failed")
                generation = Path(str(_kwargs["env"]["OMH_SELF_UPDATE_GENERATION"]))
                (generation / "skills" / "core").mkdir(parents=True, exist_ok=True)
                (generation / "skills" / "core" / "SKILL.md").write_text("candidate")
                return subprocess.CompletedProcess(command, 0, "", "")
            if failure == "timeout":
                raise subprocess.TimeoutExpired(command, 1)
            return subprocess.CompletedProcess(command, failure == "post", "", "post failed")

        return run

    def _run(self, root: Path, args, plan, runner):
        with patch.dict(
            os.environ,
            {"OMH_VENV_DIR": str(root / "venv"), "OMH_BIN_DIR": str(root / "bin")},
            clear=False,
        ):
            return run_installer_self_update(args, plan, runner=runner, platform=self._platform())

    def _assert_pair(self, root: Path, expected: Path | None = None) -> None:
        current = (root / "current").resolve()
        config = (root / "hermes" / "config.yaml").read_text()
        self.assertIn((root / "current" / "skills").as_posix(), config.replace("\\", "/"))
        launcher = root / "bin" / "omh"
        if launcher.exists() or launcher.is_symlink():
            self.assertIn(str(root / "current"), os.readlink(launcher))
        if expected is not None:
            self._assert_same_path(current, expected)

    def _assert_same_path(self, actual: Path | None, expected: Path) -> None:
        self.assertIsNotNone(actual)
        assert actual is not None
        self.assertTrue(os.path.samefile(actual, expected), f"{actual!s} does not identify {expected!s}")

    def test_fixture_uses_windows_pointer_strategy_without_starting_processes(self):
        with (
            TemporaryDirectory() as temporary,
            patch.object(SelfUpdatePlatform, "host", return_value=SelfUpdatePlatform(is_windows=True)),
            patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected fixture subprocess")) as spawn,
        ):
            root = Path(temporary)
            legacy, args, plan = self._fixture(root, pointer=True)
            previous = root / "generations" / "bootstrap-legacy"
            self._assert_pair(root, previous)
            self._assert_same_path(root / "current" / "venv", legacy)
            self._assert_same_path(root / "current" / "skills", root / "omh" / "skills")

            result = self._run(root, args, plan, self._runner(failure="post"))

            self.assertEqual(result["activation"]["status"], "ok")
            self.assertEqual(result["phase"], "post_activation")
            self.assertTrue(result["rollback"]["performed"])
            self._assert_pair(root, previous)
            self.assertFalse(any(root.glob(".current.*")))
            spawn.assert_not_called()

    def test_venv_and_pip_failures_delete_candidates_without_moving_the_pair(self):
        for failure in ("venv", "pip"):
            with self.subTest(failure), TemporaryDirectory() as temporary:
                root = Path(temporary)
                legacy, args, plan = self._fixture(root, pointer=True)
                before = (root / "current").resolve()
                result = self._run(root, args, plan, self._runner(failure=failure))
                self.assertFalse(result["ok"])
                self.assertEqual(result["phase"], "staging")
                self.assertEqual((root / "current").resolve(), before)
                self.assertFalse(Path(result["candidate"]["path"]).exists())
                self._assert_pair(root, before)

    def test_import_version_and_pack_smokes_never_touch_the_real_pair(self):
        for failure, version in (("import", ""), ("version", "1.0.6"), ("pack", "")):
            with self.subTest(failure), TemporaryDirectory() as temporary:
                root = Path(temporary)
                legacy, args, plan = self._fixture(root, pointer=True)
                plan["release"].version = "1.0.7"
                home_before = (root / "omh" / "skills").stat().st_mtime_ns
                config_before = (root / "hermes" / "config.yaml").read_bytes()
                result = self._run(root, args, plan, self._runner(failure=failure, version=version))
                self.assertFalse(result["ok"])
                self.assertEqual(result["phase"], "verification")
                self.assertFalse(Path(result["candidate"]["path"]).exists())
                self.assertEqual((root / "omh" / "skills").stat().st_mtime_ns, home_before)
                self.assertEqual((root / "hermes" / "config.yaml").read_bytes(), config_before)
                self._assert_pair(root)

    def test_migration_launcher_failure_keeps_old_pair_and_cleans_candidate(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy, args, plan = self._fixture(root)
            with patch.object(self_update_state, "_retarget_launcher", side_effect=OSError("locked")):
                result = self._run(root, args, plan, self._runner())
            self.assertEqual(result["phase"], "migration")
            self.assertFalse(Path(result["candidate"]["path"]).exists())
            self.assertEqual((root / "bin" / "omh").resolve(), (legacy / "bin" / "omh").resolve())

    def test_activation_replace_failure_is_recoverable_without_rollback(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy, args, plan = self._fixture(root, pointer=True)
            previous = (root / "current").resolve()
            original = self_update.switch_current

            def reject_candidate(transaction_root, target, **kwargs):
                if target.name != "bootstrap-legacy":
                    raise OmhError("replace failed")
                original(transaction_root, target, **kwargs)

            with patch.object(self_update, "switch_current", side_effect=reject_candidate):
                result = self._run(root, args, plan, self._runner())
            self.assertEqual(result["phase"], "activation")
            self.assertFalse(result["rollback"]["performed"])
            self.assertTrue(json.loads((root / "self-update.json").read_text())["activation_in_progress"])
            self._assert_pair(root, previous)

    def test_nonzero_and_timeout_post_activation_roll_back_and_reenter_previous(self):
        for failure in ("post", "timeout"):
            with self.subTest(failure), TemporaryDirectory() as temporary:
                root = Path(temporary)
                legacy, args, plan = self._fixture(root, pointer=True)
                previous = (root / "current").resolve()
                calls = []
                runner = self._runner(failure=failure)

                def recording_runner(command, **kwargs):
                    calls.append(command)
                    return runner(command, **kwargs)

                result = self._run(root, args, plan, recording_runner)
                self.assertFalse(result["ok"])
                self.assertEqual(result["phase"], "post_activation")
                self.assertTrue(result["rollback"]["performed"])
                # The re-entered update's stderr is the only place the cause
                # is written; it has to reach the result, not just the terminal.
                expected_reason = "post failed" if failure == "post" else "post-activation re-entry timed out"
                self.assertEqual(result["post_activation"]["reason"], expected_reason)
                self.assertEqual(sum("--command-package-updated" in call and "update" not in call for call in calls), 2)
                self._assert_pair(root, previous)
                # The refused candidate is deleted like a failed staging
                # candidate; the owner machine kept one on disk until the next
                # successful update's garbage collection.
                self.assertFalse(Path(result["candidate"]["path"]).exists())

    def test_rollback_reentry_is_marked_so_its_summary_says_rollback(self):
        # The rollback re-entry re-renders the known-good pack and, unmarked,
        # printed the ordinary "Installed release ... OMH update complete."
        # card -- the owner read that as a downgrade. Only the restoring
        # re-entry carries the marker; the candidate's re-entry does not.
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy, args, plan = self._fixture(root, pointer=True)
            reentries = []
            runner = self._runner(failure="post")

            def recording_runner(command, **kwargs):
                if "--command-package-updated" in command and "update" not in command:
                    reentries.append(dict(kwargs["env"]))
                return runner(command, **kwargs)

            result = self._run(root, args, plan, recording_runner)
            self.assertTrue(result["rollback"]["performed"])
            self.assertEqual(len(reentries), 2)
            self.assertNotIn(self_update.ROLLBACK_RESTORE_ENV, reentries[0])
            self.assertEqual(reentries[1].get(self_update.ROLLBACK_RESTORE_ENV), "1")

        payload = {
            "skills": [],
            "source": "builtin",
            "command_package": {"updated": True},
            "release_update": {"previous": {"version": "2.0.2"}, "current": {"version": "2.0.2"}},
        }
        for restoring in (False, True):
            with self.subTest(restoring=restoring):
                env = {self_update.ROLLBACK_RESTORE_ENV: "1"} if restoring else {}
                output = io.StringIO()
                with patch.dict(os.environ, env, clear=False), contextlib.redirect_stdout(output):
                    if not restoring:
                        os.environ.pop(self_update.ROLLBACK_RESTORE_ENV, None)
                    setup_commands._print_install_summary(payload, command="update", language="en")
                printed = output.getvalue()
                if restoring:
                    self.assertIn("OMH rollback complete", printed)
                    self.assertIn("Oh-My-Hermes Rollback", printed)
                    self.assertIn("Restored release: 2.0.2", printed)
                    self.assertNotIn("update complete", printed.lower())
                    self.assertNotIn("Installed release", printed)
                else:
                    self.assertIn("OMH update complete.", printed)
                    self.assertIn("Installed release: 2.0.2", printed)
                    self.assertNotIn("Rollback", printed)

    def test_lock_is_nonblocking_and_does_not_mutate_the_pair(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy, args, plan = self._fixture(root, pointer=True)
            with file_lock(root / "self-update.json", timeout_seconds=1, private=True):
                with self.assertRaisesRegex(OmhError, "another omh update"):
                    self._run(root, args, plan, self._runner())
            self._assert_pair(root)

    def test_interrupted_markers_reconcile_candidate_and_previous_deterministically(self):
        for at_candidate in (True, False):
            with self.subTest(at_candidate=at_candidate), TemporaryDirectory() as temporary:
                root = Path(temporary)
                legacy, args, plan = self._fixture(root, pointer=True)
                previous = (root / "current").resolve()
                candidate = root / "generations" / "interrupted"
                (candidate / "venv" / "bin").mkdir(parents=True)
                (candidate / "skills").mkdir()
                if at_candidate:
                    switch_current(root, candidate, platform=self._platform())
                state = json.loads((root / "self-update.json").read_text())
                state["activation_in_progress"] = {
                    "candidate": str(candidate), "previous": str(previous), "phase": "pre_switch"
                }
                atomic_write_json(root / "self-update.json", state, private=True)
                result = self._run(root, args, plan, self._runner(failure="pip"))
                expected = "completed_interrupted_activation" if at_candidate else "discarded_unswitched_candidate"
                expected_phase = "recovery" if at_candidate else "staging"
                self.assertEqual(result["phase"], expected_phase)
                self.assertEqual(result["recovery"]["action"], expected)
                self.assertEqual((root / "current").resolve(), (candidate if at_candidate else previous).resolve())
                self._assert_pair(root)

    def test_interrupted_rollback_reports_the_recovery_phase(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy, args, plan = self._fixture(root, pointer=True)
            previous = root / "generations" / "bootstrap-legacy"
            candidate = root / "generations" / "interrupted"
            (candidate / "venv" / "bin").mkdir(parents=True)
            (candidate / "skills").mkdir()
            switch_current(root, candidate, platform=self._platform())
            state = json.loads((root / "self-update.json").read_text())
            state["activation_in_progress"] = {
                "candidate": str(candidate),
                "previous": str(previous),
                "phase": "post_switch",
            }
            atomic_write_json(root / "self-update.json", state, private=True)
            result = self._run(root, args, plan, self._runner(failure="post"))
            self.assertEqual(result["phase"], "recovery")
            self.assertEqual(result["recovery"]["action"], "rolled_back_interrupted_activation")
            self.assertTrue(result["rollback"]["performed"])
            self.assertEqual((root / "current").resolve(), previous.resolve())

    def test_corrupt_and_newer_state_refuse_without_overwriting(self):
        for contents in ("not json", json.dumps({"schema_version": "self_update_state/v99"})):
            with self.subTest(contents=contents), TemporaryDirectory() as temporary:
                root = Path(temporary)
                legacy, args, plan = self._fixture(root, pointer=True)
                state_path = root / "self-update.json"
                state_path.write_text(contents)
                with self.assertRaises(OmhError):
                    self._run(root, args, plan, self._runner())
                self.assertEqual(state_path.read_text(), contents)
                self._assert_pair(root)

    def test_invalid_marker_candidates_never_delete_or_reconcile_outside_generations(self):
        for name, candidate in (
            ("traversal", "{root}/generations/../outside"),
            ("absolute", "{outside}"),
            ("symlink", "{link}"),
        ):
            with self.subTest(name), TemporaryDirectory() as temporary:
                root = Path(temporary)
                legacy, args, plan = self._fixture(root, pointer=True)
                previous = (root / "current").resolve()
                outside = root / "outside"
                outside.mkdir()
                sentinel = outside / "sentinel"
                sentinel.write_text("do not delete")
                link = root / "generations" / "escaped"
                link.symlink_to(outside, target_is_directory=True)
                raw_candidate = candidate.format(root=root, outside=outside, link=link)
                state = json.loads((root / "self-update.json").read_text())
                state["activation_in_progress"] = {
                    "candidate": raw_candidate,
                    "previous": str(previous),
                    "phase": "pre_switch",
                }
                atomic_write_json(root / "self-update.json", state, private=True)
                error = None
                try:
                    self._run(root, args, plan, self._runner(failure="pip"))
                except OmhError as exc:
                    error = exc
                self.assertTrue(sentinel.exists(), "invalid marker candidate deleted an external sentinel")
                self.assertIsInstance(error, OmhError, "invalid marker candidate was reconciled instead of refused")

    def test_interrupted_current_alias_is_confined_before_identity_comparison(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy, _args, _plan = self._fixture(root, pointer=True)
            previous = (root / "current").resolve()
            candidate = root / "generations" / "candidate"
            candidate.mkdir()
            outside_alias = root / "outside-alias"
            outside_alias.symlink_to(candidate, target_is_directory=True)
            (root / "current").unlink()
            (root / "current").symlink_to(outside_alias, target_is_directory=True)
            state = json.loads((root / "self-update.json").read_text())
            state["activation_in_progress"] = {
                "candidate": str(candidate),
                "previous": str(previous),
                "phase": "post_switch",
            }

            with self.assertRaisesRegex(OmhError, "current pointer target"):
                self_update_state.interrupted_activation(root, state)

            self.assertTrue(candidate.is_dir())
            self.assertTrue(legacy.is_dir())

    def test_invalid_persisted_generation_entries_never_switch_or_reenter(self):
        for name, mutate in (
            ("mismatched-id-path", lambda state, outside, link: state["active"].update(id="wrong-id")),
            ("mismatched-path", lambda state, outside, link: state["active"].update(path=str(outside.parent / "generations" / "other"))),
            ("external-active", lambda state, outside, link: state.update(active=self._entry(outside))),
            ("external-previous", lambda state, outside, link: state.update(previous_known_good=self._entry(outside))),
            ("symlink-active", lambda state, outside, link: state.update(active=self._entry(link))),
            ("external-pointer", lambda state, outside, link: state["pointer"].update(target="../../outside")),
        ):
            with self.subTest(name), TemporaryDirectory() as temporary:
                root = Path(temporary)
                legacy, args, plan = self._fixture(root, pointer=True)
                outside = root / "outside"
                (outside / "venv" / "bin").mkdir(parents=True)
                sentinel = outside / "venv" / "bin" / "sentinel"
                sentinel.write_text("do not execute")
                link = root / "generations" / "escaped-active"
                link.symlink_to(outside, target_is_directory=True)
                state_path = root / "self-update.json"
                state = json.loads(state_path.read_text())
                state["previous_known_good"] = self._entry((root / "current").resolve())
                mutate(state, outside, link)
                atomic_write_json(state_path, state, private=True)
                args.recover_known_good = True
                calls = []
                runner = self._runner()

                def recording_runner(command, **kwargs):
                    calls.append(command)
                    return runner(command, **kwargs)

                error = None
                try:
                    self._run(root, args, plan, recording_runner)
                except OmhError as exc:
                    error = exc
                self.assertTrue(sentinel.exists(), "invalid persisted entry reached an external generation")
                self.assertEqual(calls, [], "invalid persisted generation entry was re-entered")
                self.assertIsInstance(error, OmhError, "invalid persisted generation entry was switched or re-entered")

    def test_malformed_marker_refuses_without_overwriting_state(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy, args, plan = self._fixture(root, pointer=True)
            state_path = root / "self-update.json"
            state = json.loads(state_path.read_text())
            state["activation_in_progress"] = {"candidate": str(root / "outside")}
            atomic_write_json(state_path, state, private=True)
            before = state_path.read_bytes()
            with self.assertRaises(OmhError):
                self._run(root, args, plan, self._runner())
            self.assertEqual(state_path.read_bytes(), before)

    def test_gc_refuses_invalid_state_without_deleting_generation_entries(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()
            stale = root / "generations" / "stale"
            stale.mkdir(parents=True)
            state = {"active": self._entry(outside), "previous_known_good": None}
            result = {"cleanup": {"collected": []}}
            with self.assertRaises(OmhError):
                collect_garbage(root, state, result, running_generation=None)
            self.assertTrue(stale.exists(), "GC deleted an in-root generation from invalid state")

    def test_known_good_recovery_is_reentered_idempotent_and_refuses_missing_target(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy, args, plan = self._fixture(root, pointer=True)
            self.assertTrue(self._run(root, args, plan, self._runner())["ok"])
            restored = json.loads((root / "self-update.json").read_text())["previous_known_good"]
            args.recover_known_good = True
            first = self._run(root, args, plan, self._runner())
            second = self._run(root, args, plan, self._runner())
            self.assertTrue(first["ok"])
            self.assertTrue(second["ok"])
            self.assertEqual(first["recovery"]["selected"], second["recovery"]["selected"])
            self.assertEqual((root / "current").resolve(), Path(restored["path"]).resolve())
            import shutil
            shutil.rmtree(restored["path"])
            with self.assertRaisesRegex(OmhError, "no retained previous"):
                self._run(root, args, plan, self._runner())

    def test_gc_keeps_active_previous_bootstrap_and_running_generation(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy, args, plan = self._fixture(root, pointer=True)
            active = root / "generations" / "active"
            previous = root / "generations" / "previous"
            running = root / "generations" / "running"
            stale = root / "generations" / "stale"
            for generation in (active, previous, running, stale):
                generation.mkdir(parents=True, exist_ok=True)
            state = {"active": self._entry(active), "previous_known_good": self._entry(previous)}
            result = {"cleanup": {"collected": []}}
            with patch.object(self_update.sys, "executable", str(running / "venv" / "bin" / "python")):
                collect_garbage(root, state, result, running_generation=running)
            self.assertTrue(active.exists())
            self.assertTrue(previous.exists())
            self.assertTrue(running.exists())
            self.assertFalse(stale.exists())

    def test_no_launcher_and_platform_link_failure_are_fail_closed(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy, args, plan = self._fixture(root, pointer=True, launcher=False)
            result = self._run(root, args, plan, self._runner())
            self.assertTrue(result["ok"])
            self.assertEqual(result["activation"]["launcher"], "absent_manual_instruction")
            self.assertIn(
                (root / "current" / "skills").as_posix(),
                (root / "hermes" / "config.yaml").read_text().replace("\\", "/"),
            )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "generations" / "candidate"
            target.mkdir(parents=True)
            with patch.object(self_update.os, "symlink", side_effect=OSError("no junction")):
                with self.assertRaisesRegex(OmhError, "cannot atomically"):
                    switch_current(root, target, platform=SelfUpdatePlatform.host(is_windows=False))
            self.assertFalse((root / "current").exists())

    def test_windows_contract_activates_a_pointer_and_rewrites_the_shim(self):
        """The named platform seam must work without changing ``os.name``."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            previous = root / "generations" / "previous"
            candidate = root / "generations" / "candidate"
            previous.mkdir(parents=True)
            candidate.mkdir()
            calls = []

            def junction_runner(command, **kwargs):
                calls.append((command, kwargs))
                link = Path(kwargs["env"]["OMH_JUNCTION_LINK"])
                link.symlink_to(kwargs["env"]["OMH_JUNCTION_TARGET"], target_is_directory=True)
                return subprocess.CompletedProcess(command, 0, "", "")

            platform = SelfUpdatePlatform.windows(junction_runner)
            switch_current(root, previous, platform=platform)
            self._assert_same_path(pointer_target(root, platform=platform), previous)
            bootstrap = root / "generations" / "bootstrap-legacy"
            bootstrap.mkdir()
            platform.create_directory_link(root, bootstrap / "venv", previous)
            platform.create_directory_link(root, bootstrap / "skills", previous)
            self.assertEqual((bootstrap / "venv").resolve(), previous.resolve())
            self.assertEqual((bootstrap / "skills").resolve(), previous.resolve())
            directory = root / "bin"
            directory.mkdir()
            shim = directory / "omh.cmd"
            shim.write_text("old", newline="")
            with patch.dict(os.environ, {"OMH_BIN_DIR": str(directory)}, clear=False):
                self.assertTrue(self_update_state._retarget_launcher(root, platform))
            expected = str(root).replace("/", "\\") + "\\current\\venv\\Scripts\\omh.exe"
            self.assertIn(expected, shim.read_text())
            switch_current(root, candidate, platform=platform)
            switch_current(root, previous, platform=platform)
            self._assert_same_path(pointer_target(root, platform=platform), previous)
            self.assertTrue(all(call[0][:5] == ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command"] for call in calls))
            self.assertTrue(all(call[1]["shell"] is False for call in calls))
            self.assertTrue(all(call[1]["timeout"] > 0 for call in calls))

    def test_pointer_state_uses_cross_platform_posix_relative_target(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / "generations" / "candidate"
            state: dict[str, object] = {}

            with patch.object(self_update_state.os.path, "relpath", return_value=r"generations\candidate"):
                record_pointer(state, root, candidate)

            self.assertEqual(
                state["pointer"],
                {"path": str(root / "current"), "target": "generations/candidate"},
            )

    def test_windows_pointer_swap_never_replaces_an_existing_junction_in_place(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            previous = root / "generations" / "previous"
            candidate = root / "generations" / "candidate"
            previous.mkdir(parents=True)
            candidate.mkdir()

            def junction_runner(command, **kwargs):
                link = Path(kwargs["env"]["OMH_JUNCTION_LINK"])
                target = link.parent / kwargs["env"]["OMH_JUNCTION_TARGET"]
                link.symlink_to(target, target_is_directory=True)
                return subprocess.CompletedProcess(command, 0, "", "")

            real_rename = os.rename

            def windows_replace(source, destination):
                destination_path = Path(destination)
                if destination_path == root / "current" and (
                    destination_path.exists() or destination_path.is_symlink()
                ):
                    raise PermissionError("Windows cannot replace an existing junction in place")
                real_rename(source, destination)

            platform = SelfUpdatePlatform.windows(junction_runner)
            with patch("omh.install.self_update_platform.os.replace", side_effect=windows_replace):
                platform.replace_current(root, previous)
                platform.replace_current(root, candidate)

            self._assert_same_path(pointer_target(root, platform=platform), candidate)
            self.assertFalse(any(root.glob(".current.*")))

    def test_windows_pointer_swap_restores_previous_after_second_rename_failure(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            previous = root / "generations" / "previous"
            candidate = root / "generations" / "candidate"
            previous.mkdir(parents=True)
            candidate.mkdir()

            def junction_runner(command, **kwargs):
                link = Path(kwargs["env"]["OMH_JUNCTION_LINK"])
                target = link.parent / kwargs["env"]["OMH_JUNCTION_TARGET"]
                link.symlink_to(target, target_is_directory=True)
                return subprocess.CompletedProcess(command, 0, "", "")

            platform = SelfUpdatePlatform.windows(junction_runner)
            platform.replace_current(root, previous)
            real_replace = os.replace
            moves: list[tuple[Path, Path]] = []

            def fail_candidate_move(source, destination):
                source_path = Path(source)
                destination_path = Path(destination)
                moves.append((source_path, destination_path))
                if source_path.suffix == ".tmp" and destination_path == root / "current":
                    raise PermissionError("candidate rename failed")
                real_replace(source, destination)

            with (
                patch("omh.install.self_update_platform.os.replace", side_effect=fail_candidate_move),
                self.assertRaisesRegex(OmhError, "candidate rename failed"),
            ):
                platform.replace_current(root, candidate)

            self.assertTrue(any(destination.suffix == ".previous" for _source, destination in moves))
            self._assert_same_path(pointer_target(root, platform=platform), previous)
            self.assertFalse(any(root.glob(".current.*")))

    def test_windows_junction_creation_failure_keeps_the_previous_pointer(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            previous = root / "generations" / "previous"
            candidate = root / "generations" / "candidate"
            previous.mkdir(parents=True)
            candidate.mkdir()

            def junction_runner(command, **kwargs):
                target = Path(kwargs["env"]["OMH_JUNCTION_TARGET"])
                if target.name == "candidate":
                    return subprocess.CompletedProcess(command, 1, "", "access denied")
                Path(kwargs["env"]["OMH_JUNCTION_LINK"]).symlink_to(target, target_is_directory=True)
                return subprocess.CompletedProcess(command, 0, "", "")

            platform = SelfUpdatePlatform.windows(junction_runner)
            switch_current(root, previous, platform=platform)
            with self.assertRaisesRegex(OmhError, "directory junction"):
                switch_current(root, candidate, platform=platform)
            self._assert_same_path(pointer_target(root, platform=platform), previous)
            self.assertFalse(any(root.glob(".current.*.tmp")))

    def test_windows_junction_command_keeps_path_bytes_out_of_program_text(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "A&B%TEMP%^!()"
            link = root / ".current.A&B%TEMP%^!().tmp"
            target = root / "generations" / "A&B%TEMP%^!()"
            calls = []

            def junction_runner(command, **kwargs):
                calls.append((command, kwargs))
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.dict(os.environ, {"PRESERVED": "yes"}, clear=True):
                SelfUpdatePlatform.windows(junction_runner).create_directory_link(root, link, target)

            command, kwargs = calls[0]
            relative_target = os.path.relpath(target, link.parent)
            program_text = subprocess.list2cmdline(command)
            self.assertNotIn("A&B%TEMP%^!()", "\n".join(command))
            self.assertNotIn("A&B%TEMP%^!()", program_text)
            self.assertEqual(kwargs["env"], {
                "PRESERVED": "yes",
                "OMH_JUNCTION_LINK": str(link),
                "OMH_JUNCTION_TARGET": relative_target,
            })
            self.assertEqual(command[0], "powershell.exe")
            self.assertEqual(command[1:4], ["-NoLogo", "-NoProfile", "-NonInteractive"])
            self.assertTrue(kwargs["shell"] is False)
            self.assertEqual(kwargs["cwd"], str(link.parent))
            self.assertGreater(kwargs["timeout"], 0)

    def test_windows_broken_junction_is_removed_even_without_a_target(self):
        class BrokenJunction:
            removed = False

            def exists(self):
                return False

            def is_symlink(self):
                return False

            def is_junction(self):
                return True

            def rmdir(self):
                self.removed = True

            def unlink(self):
                raise AssertionError("junctions must be removed with rmdir")

        broken = BrokenJunction()
        _remove_directory_link(broken)  # type: ignore[arg-type]
        self.assertTrue(broken.removed)

    def test_windows_post_activation_failure_restores_the_previous_pair(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy, args, plan = self._fixture(root, pointer=True)
            previous = pointer_target(root)
            directory = root / "bin"
            (directory / "omh").unlink()
            shim = directory / "omh.cmd"
            shim.write_text('"legacy" %*\r\n', newline="")
            junctions = []

            def junction_runner(command, **kwargs):
                junctions.append((command, kwargs))
                Path(kwargs["env"]["OMH_JUNCTION_LINK"]).symlink_to(
                    kwargs["env"]["OMH_JUNCTION_TARGET"], target_is_directory=True
                )
                return subprocess.CompletedProcess(command, 0, "", "")

            platform = SelfUpdatePlatform.windows(junction_runner)
            with patch.dict(os.environ, {"OMH_VENV_DIR": str(legacy), "OMH_BIN_DIR": str(directory)}, clear=False):
                self.assertTrue(self_update_state._retarget_launcher(root, platform))
                result = run_installer_self_update(args, plan, runner=self._runner(failure="post"), platform=platform)
            self.assertEqual(result["activation"]["status"], "ok")
            self.assertTrue(result["rollback"]["performed"])
            self._assert_same_path(pointer_target(root, platform=platform), previous)
            self.assertIn("\\current\\venv\\Scripts\\omh.exe", shim.read_text())
            self.assertIn(
                (root / "current" / "skills").as_posix(),
                (root / "hermes" / "config.yaml").read_text().replace("\\", "/"),
            )
            self.assertEqual(len(junctions), 2)

    def test_json_and_human_results_never_claim_success_before_post_activation(self):
        for failure in ("venv", "pip", "import", "pack", "post", "timeout"):
            with self.subTest(failure), TemporaryDirectory() as temporary:
                root = Path(temporary)
                legacy, args, plan = self._fixture(root, pointer=True)
                result = self._run(root, args, plan, self._runner(failure=failure))
                self.assertFalse(result["ok"])
                self.assertNotEqual(result["post_activation"]["status"], "ok")
        output = io.StringIO()
        failed = {
            "ok": False,
            "phase": "post_activation",
            "post_activation": {"status": "failed", "reason": "Traceback line\nerror: local modifications detected; rerun with --force"},
            "rollback": {"performed": True, "restored": "bootstrap-legacy"},
        }
        with (
            patch("omh.install.self_update.run_installer_self_update", return_value=failed),
            contextlib.redirect_stdout(output),
        ):
            status = _run_command_package_self_update(SimpleNamespace(json=False), {"method": "installer"})
        self.assertEqual(status, 1)
        printed = output.getvalue()
        self.assertIn("stopped during post_activation", printed)
        # The phase alone sent people to the bug tracker; the line names the cause.
        self.assertIn("reason: error: local modifications detected; rerun with --force", printed)
        self.assertNotIn("Traceback line", printed)
        self.assertIn("rolled back to generation bootstrap-legacy", printed)
        self.assertIn("rerun `omh update`", printed)
        self.assertNotIn("update complete", printed.lower())
        bare = io.StringIO()
        with (
            patch("omh.install.self_update.run_installer_self_update", return_value={"ok": False, "phase": "staging"}),
            contextlib.redirect_stdout(bare),
        ):
            _run_command_package_self_update(SimpleNamespace(json=False), {"method": "installer"})
        self.assertIn("stopped during staging", bare.getvalue())
        self.assertNotIn("reason:", bare.getvalue())

    def test_reentry_judges_the_candidate_pack_by_its_own_manifest(self):
        # The home manifest describes the active generation's pack. Rendering
        # the candidate into a new generation and then judging it by that
        # manifest turned every catalog change since the last install into a
        # "local modification" and rolled the update back (reported on 2.0.1).
        from omh.install.installer import install_skill_pack
        from omh.system.paths import OmhPaths

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = OmhPaths(omh_home=root / "omh", hermes_home=root / "hermes")
            install_skill_pack(home)
            manifest_path = root / "omh" / "manifest.json"
            recorded = json.loads(manifest_path.read_text())
            self.assertEqual(recorded["skills_dir"], str(root / "omh" / "skills"))
            candidate = root / "generations" / "candidate" / "skills"
            smoke_home = OmhPaths(omh_home=root / "smoke-omh", hermes_home=root / "smoke-hermes", managed_skills_dir=candidate)
            install_skill_pack(smoke_home)
            # The active generation was installed from an older catalog.
            recorded["skills"][0]["sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(recorded))
            reentry = OmhPaths(omh_home=root / "omh", hermes_home=root / "hermes", managed_skills_dir=candidate)
            written = install_skill_pack(reentry)
            self.assertEqual(written["skills_dir"], str(candidate))
            self.assertEqual(json.loads(manifest_path.read_text())["skills_dir"], str(candidate))
            # The same manifest still guards the directory it does describe.
            (root / "omh" / "skills" / Path(recorded["skills"][0]["path"])).write_text("edited")
            manifest_path.write_text(json.dumps(recorded))
            with self.assertRaisesRegex(OmhError, "local modifications detected"):
                install_skill_pack(home)
            # A bootstrap generation reaching the legacy pack through a link is
            # the same pack, so its edits are still seen.
            bootstrap = root / "generations" / "bootstrap-legacy"
            bootstrap.mkdir(parents=True)
            (bootstrap / "skills").symlink_to(root / "omh" / "skills", target_is_directory=True)
            linked = OmhPaths(omh_home=root / "omh", hermes_home=root / "hermes", managed_skills_dir=bootstrap / "skills")
            with self.assertRaisesRegex(OmhError, "local modifications detected"):
                install_skill_pack(linked)

    def _older_pack_home(self, root: Path) -> Path:
        """A real legacy pack whose manifest an older catalog wrote."""
        from omh.install.installer import install_skill_pack
        from omh.system.paths import OmhPaths

        install_skill_pack(OmhPaths(omh_home=root / "omh", hermes_home=root / "hermes"))
        manifest_path = root / "omh" / "manifest.json"
        recorded = json.loads(manifest_path.read_text())
        recorded["skills"][0]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(recorded))
        return manifest_path

    def _live_update_runner(self, root: Path):
        """Fake venv/pip/import/version; run the pack smoke and re-entry through the real CLI."""
        fake = self._runner()

        def run(command, **kwargs):
            if "update" not in command:
                return fake(command, **kwargs)
            argv = command[command.index("omh.cli") + 1 :]
            with patch.dict(os.environ, dict(kwargs["env"]), clear=True):
                status, stdout, stderr = run_cli(argv, output_json=False)
            return subprocess.CompletedProcess(command, status, stdout, stderr)

        return run

    def test_staged_transaction_completes_when_the_home_manifest_predates_the_candidate(self):
        # The reported 2.0.1 rollback, end to end: the smoke renders the candidate
        # pack into its generation, and the re-entered `omh update` against the
        # real home must accept that pack even though the home manifest still
        # carries the hashes of the pack the active generation was installed from.
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy, args, plan = self._fixture(root, pointer=True)
            manifest_path = self._older_pack_home(root)
            argv = ["omh", "--omh-home", str(root / "omh"), "--hermes-home", str(root / "hermes"), "update", "--no-interactive"]
            with patch.object(sys, "argv", argv):
                result = self._run(root, args, plan, self._live_update_runner(root))
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["post_activation"]["status"], "ok")
            candidate = Path(result["candidate"]["path"])
            self.assertEqual(json.loads(manifest_path.read_text())["skills_dir"], str(candidate / "skills"))
            self._assert_pair(root, candidate)

    def test_every_generation_interpreter_call_ignores_the_working_directory(self):
        # The owner's 2.0.2 -> "2.0.1" update: `omh update` was run from inside
        # a source checkout, whose top-level `omh/` shim sat at `sys.path[0]`
        # for every `python -m omh.cli` the transaction spawned. The stale
        # checkout judged the fresh candidate, refused it, rolled back, and
        # stamped the manifest with its own version. `-P` keeps the smoke and
        # both re-entries on the generation's own package.
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy, args, plan = self._fixture(root, pointer=True)
            plan["release"].version = "1.0.7"
            commands: list[list[str]] = []
            fake = self._runner(version="1.0.7\n")

            def run(command, **kwargs):
                commands.append(list(command))
                return fake(command, **kwargs)

            result = self._run(root, args, plan, run)
            self.assertTrue(result["ok"], result)
            omh_calls = [command for command in commands if "omh.cli" in " ".join(command)]
            self.assertEqual(len(omh_calls), 4, omh_calls)  # import, version, pack smoke, re-entry
            for command in omh_calls:
                self.assertEqual(command[1], "-P", command)
            self.assertEqual(omh_calls[0][1:4], ["-P", "-c", "import omh.cli"])
            self.assertEqual(omh_calls[-1][1:4], ["-P", "-m", "omh.cli"])
            self.assertIn("--command-package-updated", omh_calls[-1])

    def test_dash_p_is_what_keeps_a_checkout_shim_out_of_the_interpreter(self):
        # The mechanism itself, on the real interpreter: a directory holding an
        # `omh/cli.py` wins `python -m omh.cli` when it is the cwd, and loses
        # once `-P` drops the cwd from sys.path.
        with TemporaryDirectory() as temporary:
            shadow = Path(temporary) / "omh"
            shadow.mkdir()
            (shadow / "__init__.py").write_text("")
            (shadow / "cli.py").write_text("print('SHADOWED')\n")
            env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
            env["PYTHONSAFEPATH"] = ""
            plain = subprocess.run(
                [sys.executable, "-m", "omh.cli"], cwd=temporary, env=env, text=True, capture_output=True, check=False
            )
            isolated = subprocess.run(
                [sys.executable, "-P", "-m", "omh.cli"], cwd=temporary, env=env, text=True, capture_output=True, check=False
            )
            self.assertEqual(plain.stdout.strip(), "SHADOWED")
            self.assertNotIn("SHADOWED", isolated.stdout)

    def test_staged_transaction_regression_guard_names_the_refusal_it_prevents(self):
        # Proves the guard above is load-bearing: judging the candidate by the
        # home manifest is exactly the refusal that rolled 2.0.1 updates back.
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy, args, plan = self._fixture(root, pointer=True)
            self._older_pack_home(root)
            previous = (root / "current").resolve()
            argv = ["omh", "--omh-home", str(root / "omh"), "--hermes-home", str(root / "hermes"), "update", "--no-interactive"]
            with (
                patch.object(sys, "argv", argv),
                patch("omh.install.installer._manifest_describes", return_value=True),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                result = self._run(root, args, plan, self._live_update_runner(root))
            self.assertFalse(result["ok"])
            self.assertEqual(result["phase"], "post_activation")
            self.assertIn("local modifications detected", result["post_activation"]["reason"])
            self.assertTrue(result["rollback"]["performed"])
            self._assert_pair(root, previous)


class ManagedWorkflowRegistrationTests(unittest.TestCase):
    @staticmethod
    def _args(root: Path) -> SimpleNamespace:
        return SimpleNamespace(
            omh_home=str(root / "omh"),
            hermes_home=str(root / "hermes"),
            scope="user",
            dry_run=False,
            force=False,
            memory_mode="review-first",
        )

    def test_pre_migration_apply_registers_the_existing_legacy_skills_dir(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy_skills = root / "omh" / "skills"
            legacy_skills.mkdir(parents=True)
            current_skills = root / "current" / "skills"
            args = self._args(root)

            with (
                patch.object(setup_commands, "managed_current_workflow_pack_dir", return_value=current_skills),
                patch.object(setup_commands, "_managed_command_runtime", return_value={"managed": True}),
            ):
                setup_commands._apply_result(args)

            registered = external_dirs((root / "hermes" / "config.yaml").read_text())
            self.assertEqual(registered, [legacy_skills.resolve().as_posix()])
            state = json.loads((root / "omh" / "runtime" / "state.json").read_text())
            self.assertEqual(state["last_applied_skills_dir"], str(legacy_skills.resolve()))
            self.assertNotIn(current_skills.as_posix(), registered)

    def test_migrated_profile_sync_recognizes_the_current_pointer_registration(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            current_skills = PureWindowsPath(r"C:\omh\current\skills")
            profile = root / "hermes" / "profiles" / "bot"
            (profile / "plugins" / "omh").mkdir(parents=True)
            (profile / "config.yaml").write_text(
                f"skills:\n  external_dirs:\n  - {current_skills.as_posix()}\n"
            )
            args = self._args(root)

            with (
                patch.object(setup_commands, "_registered_workflow_dir", return_value=current_skills),
                patch.object(setup_commands, "install_plugin_bundle"),
                patch.object(setup_commands, "install_tui_widget"),
                patch.object(setup_commands, "install_skin"),
                patch.object(setup_commands, "_apply_result", return_value={}) as apply_result,
            ):
                results = setup_commands._sync_hermes_profiles(args)

            self.assertEqual(results, [{"profile": "bot", "status": "refreshed"}])
            apply_result.assert_called_once()

    def test_migrated_uninstall_removes_current_registration_from_primary_and_profiles(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            current_skills = root / "current" / "skills"
            current_skills.mkdir(parents=True)
            profile = root / "hermes" / "profiles" / "bot"
            profile.mkdir(parents=True)
            for home in (root / "hermes", profile):
                (home / "config.yaml").write_text(
                    f"skills:\n  external_dirs:\n  - {current_skills}\n"
                )
            base = [
                "--omh-home",
                str(root / "omh"),
                "--hermes-home",
                str(root / "hermes"),
                "uninstall",
                "--registration-only",
            ]

            with (
                patch.object(setup_commands, "managed_current_workflow_pack_dir", return_value=current_skills),
                patch.object(setup_commands, "_managed_command_runtime", return_value={"managed": True}),
            ):
                status, _stdout, stderr = run_cli(base)

            self.assertEqual((status, stderr), (0, ""))
            for home in (root / "hermes", profile):
                self.assertNotIn(current_skills.as_posix(), external_dirs((home / "config.yaml").read_text()))
