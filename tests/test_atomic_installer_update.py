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
from unittest.mock import patch

from omh.commands.setup import _run_command_package_self_update


class AtomicInstallerUpdateContractTests(unittest.TestCase):
    def test_installer_update_stages_outside_the_active_interpreter_and_reports_phases(self) -> None:
        captured: list[list[str]] = []

        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            captured.append(command)
            if command[1:3] == ["-m", "venv"]:
                candidate = Path(command[-1]).parent
                (candidate / "venv" / "bin").mkdir(parents=True, exist_ok=True)
                (candidate / "venv" / "bin" / "python").touch()
            if "update" in command:
                generation = Path(str(dict(kwargs["env"])["OMH_SELF_UPDATE_GENERATION"]))
                (generation / "skills").mkdir(parents=True, exist_ok=True)
                (generation / "skills" / "SKILL.md").write_text("candidate")
            return subprocess.CompletedProcess(command, 0, "", "")

        output = io.StringIO()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "venv").mkdir()
            with (
                patch(f"{_run_command_package_self_update.__module__}.subprocess.run", side_effect=fake_run),
                patch(f"{_run_command_package_self_update.__module__}.sys.argv", ["omh", "update", "--json"]),
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
                self.assertEqual(
                    _run_command_package_self_update(
                        Namespace(json=True),
                        {
                            "method": "installer",
                            "release": SimpleNamespace(package_url="https://example.invalid/omh.whl"),
                            "python": sys.executable,
                            "venv_dir": str(root / "venv"),
                        },
                    ),
                    0,
                )

        pip_command = next(command for command in captured if command[1:4] == ["-m", "pip", "install"])
        payload = json.loads(output.getvalue() or "{}")
        with self.subTest("active interpreter is never the pip target"):
            self.assertNotEqual(pip_command[0], sys.executable)
        with self.subTest("machine output records activation or rollback"):
            self.assertTrue(payload.get("activation") or payload.get("rollback"))
