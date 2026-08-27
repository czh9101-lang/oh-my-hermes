from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _local_package import load_local_package

load_local_package()

from omh.coding import skill_load_observation as observation  # noqa: E402


class SkillLoadWindowsResolutionTests(unittest.TestCase):
    def test_supplied_path_and_pathext_are_case_insensitive(self) -> None:
        # Given: an isolated Windows-style environment absent from the parent env.
        with TemporaryDirectory(prefix="omh-windows-resolution-") as temporary:
            root = Path(temporary)
            executable = root / "HERMES.PY"
            executable.write_text("raise SystemExit(2)\n", encoding="utf-8")
            env = {"Path": str(root), "PathExt": ".EXE;.Py;.CMD"}

            # When: a bare command is resolved using Windows semantics.
            selected = observation._resolve_executable("hermes", env, windows=True)

            # Then: caller PATH/PATHEXT and filesystem casing select the script.
            self.assertEqual(selected, executable)

    def test_windows_resolution_does_not_consult_parent_path(self) -> None:
        # Given: an explicitly empty child PATH and a parent-resolvable command.
        env = {"PATH": "", "PATHEXT": ".EXE;.PY"}

        # When/Then: resolution does not silently fall back to os.environ.
        self.assertIsNone(
            observation._resolve_executable("python", env, windows=True)
        )

    def test_windows_python_snapshot_uses_current_interpreter(self) -> None:
        # Given: a mixed-case Python snapshot suffix.
        executable = str(Path("C:/private/HERMES.Py"))

        # When: protocol argv is assembled for Windows.
        argv = observation._inventory_argv(executable, ("--protocol", "v1"), windows=True)

        # Then: Python fixtures retain explicit interpreter semantics.
        self.assertEqual(argv[1:], (executable, "--protocol", "v1"))
        self.assertNotEqual(argv[0], executable)


if __name__ == "__main__":
    unittest.main()
