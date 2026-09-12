"""The public installer self-update contract starts red against the old seam."""

import contextlib
import io
import json
import subprocess
import sys
import unittest
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Literal
from unittest.mock import patch

from omh.commands.setup import _run_command_package_self_update
from omh.install.self_update_platform import SelfUpdatePlatform


def _linking_platform(*, is_windows: bool) -> SelfUpdatePlatform:
    """Keep the host pointer strategy, but model the junction call with a link.

    `SelfUpdatePlatform` carries its own runner, bound to the real
    `subprocess.run` when the dataclass field default is evaluated, so the
    runner this contract injects into `_run_command_package_self_update` never
    reaches it. On POSIX that is invisible -- `create_directory_link` calls
    `os.symlink` and starts nothing -- but on Windows it made this test spawn
    four real `powershell.exe` junction processes it never asked for: two for
    the legacy migration's links, one for the migration pointer, one for the
    activation switch. Each carries a 15s timeout, and a spawn that times out
    or is refused surfaces only as the transaction's exit code, which is how a
    Windows runner read `AssertionError: 1 != 0` here while both POSIX lanes
    stayed green. `StagedSelfUpdateTests._platform` binds the same seam for
    the same reason; the junction command itself is covered by the dedicated
    Windows adapter tests, which inject their runner too.
    """

    def junction_runner(
        command: list[str],
        *,
        shell: bool,
        cwd: str,
        env: dict[str, str],
        text: Literal[True],
        stdout: int,
        stderr: int,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        _ = shell, text, stdout, stderr, timeout
        link = Path(env["OMH_JUNCTION_LINK"])
        link.symlink_to(Path(cwd) / env["OMH_JUNCTION_TARGET"], target_is_directory=True)
        return subprocess.CompletedProcess(command, 0, "", "")

    return SelfUpdatePlatform(is_windows=is_windows, runner=junction_runner)


class AtomicInstallerUpdateContractTests(unittest.TestCase):
    def _run_update(self, platform: SelfUpdatePlatform) -> tuple[int, list[list[str]], dict[str, object]]:
        captured: list[list[str]] = []

        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            captured.append(command)
            if command[1:3] == ["-m", "venv"]:
                candidate = Path(command[-1]).parent
                # The candidate venv the installer probes is `Scripts/` on
                # Windows and `bin/` on POSIX; build the one this run means.
                scripts = platform.scripts_dir(candidate / "venv")
                scripts.mkdir(parents=True, exist_ok=True)
                (scripts / ("python.exe" if platform.is_windows else "python")).touch()
            if "update" in command:
                generation = Path(str(dict(kwargs["env"])["OMH_SELF_UPDATE_GENERATION"]))
                (generation / "skills").mkdir(parents=True, exist_ok=True)
                (generation / "skills" / "SKILL.md").write_text("candidate")
            return subprocess.CompletedProcess(command, 0, "", "")

        output = io.StringIO()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "venv").mkdir()
            (root / "omh" / "skills").mkdir(parents=True)
            with (
                patch(f"{_run_command_package_self_update.__module__}.subprocess.run", side_effect=fake_run),
                patch(f"{_run_command_package_self_update.__module__}.sys.argv", ["omh", "update", "--json"]),
                patch.object(SelfUpdatePlatform, "host", return_value=platform),
                contextlib.redirect_stdout(output),
                patch.dict(
                    "os.environ",
                    {
                        "OMH_HOME": str(root / "omh"),
                        "HERMES_HOME": str(root / "hermes"),
                        "OMH_VENV_DIR": str(root / "venv"),
                        "OMH_BIN_DIR": str(root / "bin"),
                    },
                ),
            ):
                exit_code = _run_command_package_self_update(
                    Namespace(json=True),
                    {
                        "method": "installer",
                        "release": SimpleNamespace(package_url="https://example.invalid/omh.whl"),
                        "python": sys.executable,
                        "venv_dir": str(root / "venv"),
                    },
                )
        return exit_code, captured, json.loads(output.getvalue() or "{}")

    def _assert_contract(self, exit_code: int, captured: list[list[str]], payload: dict[str, object]) -> None:
        # The exit code alone reported `1 != 0` and nothing else; the phase and
        # its recorded reason are the only things that say which step stopped.
        self.assertEqual(exit_code, 0, f"update stopped in phase {payload.get('phase')!r}: {payload}")
        pip_command = next(command for command in captured if command[1:4] == ["-m", "pip", "install"])
        with self.subTest("active interpreter is never the pip target"):
            self.assertNotEqual(pip_command[0], sys.executable)
        with self.subTest("machine output records activation or rollback"):
            self.assertTrue(payload.get("activation") or payload.get("rollback"))

    def test_installer_update_stages_outside_the_active_interpreter_and_reports_phases(self) -> None:
        platform = _linking_platform(is_windows=SelfUpdatePlatform.host().is_windows)

        self._assert_contract(*self._run_update(platform))

    def test_installer_update_holds_on_the_windows_pointer_strategy_without_starting_processes(self) -> None:
        with patch.object(
            subprocess, "Popen", side_effect=AssertionError("unexpected self-update subprocess")
        ) as spawn:
            results = self._run_update(_linking_platform(is_windows=True))

        self._assert_contract(*results)
        spawn.assert_not_called()
