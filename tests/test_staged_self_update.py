import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from omh.commands.setup import _run_command_package_self_update
from omh.core.errors import OmhError
from omh.install import self_update
from omh.install.self_update import run_installer_self_update, switch_current
from omh.install.self_update_platform import SelfUpdatePlatform, _remove_directory_link
from omh.install.self_update_state import STATE_SCHEMA_VERSION, collect_garbage, pointer_target
from omh.system.local_store import atomic_write_json, file_lock


class StagedSelfUpdateTests(unittest.TestCase):
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
        switch_current(root, bootstrap)
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
            if command[1:3] == ["-c", "import omh.cli"]:
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
            return run_installer_self_update(args, plan, runner=runner)

    def _assert_pair(self, root: Path, expected: Path | None = None) -> None:
        current = (root / "current").resolve()
        config = (root / "hermes" / "config.yaml").read_text()
        self.assertIn((root / "current" / "skills").as_posix(), config)
        launcher = root / "bin" / "omh"
        if launcher.exists() or launcher.is_symlink():
            self.assertIn(str(root / "current"), os.readlink(launcher))
        if expected is not None:
            self.assertEqual(current, expected)

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
            with patch.object(self_update, "_retarget_launcher", side_effect=OSError("locked")):
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

            def reject_candidate(transaction_root, target):
                if target.name != "bootstrap-legacy":
                    raise OmhError("replace failed")
                original(transaction_root, target)

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
                self.assertTrue(result["rollback"]["performed"])
                self.assertEqual(sum("--command-package-updated" in call and "update" not in call for call in calls), 2)
                self._assert_pair(root, previous)

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
                    switch_current(root, candidate)
                state = json.loads((root / "self-update.json").read_text())
                state["activation_in_progress"] = {
                    "candidate": str(candidate), "previous": str(previous), "phase": "pre_switch"
                }
                atomic_write_json(root / "self-update.json", state, private=True)
                result = self._run(root, args, plan, self._runner(failure="pip"))
                expected = "completed_interrupted_activation" if at_candidate else "discarded_unswitched_candidate"
                self.assertEqual(result["recovery"]["action"], expected)
                self.assertEqual((root / "current").resolve(), (candidate if at_candidate else previous).resolve())
                self._assert_pair(root)

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
            self.assertIn((root / "current" / "skills").as_posix(), (root / "hermes" / "config.yaml").read_text())
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "generations" / "candidate"
            target.mkdir(parents=True)
            with patch.object(self_update.os, "symlink", side_effect=OSError("no junction")):
                with self.assertRaisesRegex(OmhError, "cannot atomically"):
                    switch_current(root, target)
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
            self.assertEqual(pointer_target(root, platform=platform), previous)
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
                self.assertTrue(self_update._retarget_launcher(root, platform))
            expected = str(root).replace("/", "\\") + "\\current\\venv\\Scripts\\omh.exe"
            self.assertIn(expected, shim.read_text())
            switch_current(root, candidate, platform=platform)
            switch_current(root, previous, platform=platform)
            self.assertEqual(pointer_target(root, platform=platform), previous)
            self.assertTrue(all(call[0][:5] == ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command"] for call in calls))
            self.assertTrue(all(call[1]["shell"] is False for call in calls))
            self.assertTrue(all(call[1]["timeout"] > 0 for call in calls))

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
            self.assertEqual(pointer_target(root, platform=platform), previous)
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
                self.assertTrue(self_update._retarget_launcher(root, platform))
                result = run_installer_self_update(args, plan, runner=self._runner(failure="post"), platform=platform)
            self.assertEqual(result["activation"]["status"], "ok")
            self.assertTrue(result["rollback"]["performed"])
            self.assertEqual(pointer_target(root, platform=platform), previous)
            self.assertIn("\\current\\venv\\Scripts\\omh.exe", shim.read_text())
            self.assertIn((root / "current" / "skills").as_posix(), (root / "hermes" / "config.yaml").read_text())
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
        failed = {"ok": False, "phase": "post_activation"}
        with (
            patch("omh.install.self_update.run_installer_self_update", return_value=failed),
            contextlib.redirect_stdout(output),
        ):
            status = _run_command_package_self_update(SimpleNamespace(json=False), {"method": "installer"})
        self.assertEqual(status, 1)
        self.assertIn("stopped during post_activation", output.getvalue())
        self.assertNotIn("update complete", output.getvalue().lower())
