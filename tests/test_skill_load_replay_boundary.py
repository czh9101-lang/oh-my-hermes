from __future__ import annotations

import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _cli_harness import run_cli


class SkillLoadReplayBoundaryTests(unittest.TestCase):
    def test_authenticated_observation_cannot_move_to_another_run(self) -> None:
        # Given: one authenticated unsupported observation in its original run.
        with TemporaryDirectory(prefix="omh-skill-replay-") as temporary:
            root = Path(temporary)
            home = root / ".omh"
            hermes = root / "unsupported.py"
            hermes.write_text("#!/usr/bin/env python3\nraise SystemExit(2)\n", encoding="utf-8")
            hermes.chmod(0o755)
            base = ["--omh-home", str(home), "coding", "hermes-child"]
            probe = [
                *base, "skill-load-probe", "--confirm-dispatch", "--run-id", "run-a",
                "--hermes", str(hermes),
            ]
            self.assertEqual(run_cli(probe)[0], 0)
            runs = home / "coding" / "hermes-child"
            shutil.copytree(runs / "run-a", runs / "run-b")

            # When: another run asks status for the byte-identical copied record.
            status, stdout, stderr = run_cli(
                [*base, "skill-load-status", "--run-id", "run-b"]
            )

            # Then: persisted authentication remains bound to the original run.
            self.assertEqual((status, stdout), (2, ""))
            self.assertIn("invalid", stderr)
            original = run_cli([*base, "skill-load-status", "--run-id", "run-a"])
            self.assertEqual(original[0], 0, original[2])


if __name__ == "__main__":
    unittest.main()
