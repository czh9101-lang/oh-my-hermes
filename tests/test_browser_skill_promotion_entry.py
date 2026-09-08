from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests._cli_harness import run_cli


class BrowserSkillPromotionEntryTests(unittest.TestCase):
    def test_public_status_is_project_local_and_does_not_activate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True)

            status, stdout, stderr = run_cli([
                "web-qa", "promotion", "status", "--project-root", str(root),
                "--skill-name", "checkout-confirmation",
            ])

            self.assertEqual(status, 0, stderr)
            result = json.loads(stdout)
            self.assertEqual(result["schema_version"], "browser_skill_promotion/v1")
            self.assertEqual(result["status"], "inactive")
            self.assertFalse((root / ".hermes").exists())
            self.assertFalse((root / ".omh").exists())

    def test_public_status_rejects_traversal_without_state_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True)

            status, _, _ = run_cli([
                "web-qa", "promotion", "status", "--project-root", str(root),
                "--skill-name", "../../escape",
            ])

            self.assertNotEqual(status, 0)
            self.assertFalse((root / ".omh").exists())
