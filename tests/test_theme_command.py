"""`omh theme` -- the user-facing switch for the shipped TUI palettes.

Themes only matter if a person can pick one without editing YAML, so these
tests pin the command surface end to end: what `list` reports, that `use`
writes `display.skin` and installs the files it selects, that `--dry-run`
writes nothing, that an invalid name fails loudly with the valid ones named,
and that a foreign skin is reported as the user's own rather than overwritten
behind their back.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _cli_harness import run_cli

from omh.install.config_adapter import display_skin_selection
from omh.skin_pack import MANIFEST_FILENAME


class ThemeCommandTests(unittest.TestCase):
    def _homes(self, tmp: str) -> list[str]:
        root = Path(tmp)
        (root / "hermes").mkdir()
        (root / "omh").mkdir()
        return ["--omh-home", str(root / "omh"), "--hermes-home", str(root / "hermes")]

    def _config(self, tmp: str) -> str:
        path = Path(tmp) / "hermes" / "config.yaml"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_bare_theme_lists_every_shipped_palette(self) -> None:
        with TemporaryDirectory() as tmp:
            status, out, _ = run_cli([*self._homes(tmp), "theme"], output_json=False)
            self.assertEqual(status, 0)
            for name in ("sky", "amber", "crimson", "mono"):
                self.assertIn(name, out)
            self.assertIn("(default)", out)

    def test_list_json_marks_the_active_theme_and_install_state(self) -> None:
        with TemporaryDirectory() as tmp:
            homes = self._homes(tmp)
            status, out, _ = run_cli([*homes, "theme", "list"])
            self.assertEqual(status, 0)
            payload = json.loads(out)
            self.assertEqual(payload["schema_version"], "omh_theme_list/v1")
            self.assertTrue(payload["active_is_unset"])
            self.assertEqual([theme["name"] for theme in payload["themes"]], ["sky", "amber", "crimson", "mono"])
            self.assertEqual({theme["install_state"] for theme in payload["themes"]}, {"missing"})
            run_cli([*homes, "theme", "use", "amber"])
            payload = json.loads(run_cli([*homes, "theme", "list"])[1])
            active = [theme for theme in payload["themes"] if theme["active"]]
            self.assertEqual([theme["name"] for theme in active], ["amber"])
            self.assertEqual({theme["install_state"] for theme in payload["themes"]}, {"managed"})

    def test_the_group_level_json_flag_survives_the_subcommand(self) -> None:
        # argparse resets a group flag from a subparser default unless the
        # subparser suppresses it; `omh theme --json list` must not silently
        # print the human summary to a wrapper expecting JSON.
        with TemporaryDirectory() as tmp:
            status, out, _ = run_cli([*self._homes(tmp), "theme", "--json", "list"], output_json=False)
            self.assertEqual(status, 0)
            self.assertEqual(json.loads(out)["schema_version"], "omh_theme_list/v1")

    def test_use_writes_the_skin_and_installs_every_theme_file(self) -> None:
        with TemporaryDirectory() as tmp:
            homes = self._homes(tmp)
            status, out, _ = run_cli([*homes, "theme", "use", "crimson"])
            self.assertEqual(status, 0)
            payload = json.loads(out)
            self.assertEqual(payload["schema_version"], "omh_theme_change/v1")
            self.assertEqual(payload["skin"], "omh-crimson")
            self.assertTrue(payload["changed"])
            self.assertEqual(display_skin_selection(self._config(tmp)), "omh-crimson")
            skins = Path(tmp) / "hermes" / "skins"
            # Switching is instant and offline because every palette is on
            # disk already, not fetched when it is selected.
            self.assertTrue((skins / "omh.yaml").is_file())
            self.assertTrue((skins / "omh-crimson.yaml").is_file())
            self.assertTrue((skins / MANIFEST_FILENAME).is_file())

    def test_use_accepts_the_full_skin_name_and_the_default_alias(self) -> None:
        with TemporaryDirectory() as tmp:
            homes = self._homes(tmp)
            self.assertEqual(json.loads(run_cli([*homes, "theme", "use", "omh-mono"])[1])["theme"], "mono")
            self.assertEqual(display_skin_selection(self._config(tmp)), "omh-mono")
            payload = json.loads(run_cli([*homes, "theme", "use", "default"])[1])
            self.assertEqual(payload["skin"], "omh")
            self.assertEqual(payload["previous_skin"], "omh-mono")
            self.assertEqual(payload["reverse_command"], "omh theme use mono")
            self.assertEqual(display_skin_selection(self._config(tmp)), "omh")

    def test_reselecting_the_active_theme_reports_no_change(self) -> None:
        with TemporaryDirectory() as tmp:
            homes = self._homes(tmp)
            run_cli([*homes, "theme", "use", "amber"])
            payload = json.loads(run_cli([*homes, "theme", "use", "amber"])[1])
            self.assertTrue(payload["already_active"])
            self.assertFalse(payload["changed"])

    def test_dry_run_writes_neither_the_config_nor_the_skin_files(self) -> None:
        with TemporaryDirectory() as tmp:
            homes = self._homes(tmp)
            payload = json.loads(run_cli([*homes, "theme", "use", "crimson", "--dry-run"])[1])
            self.assertTrue(payload["dry_run"])
            self.assertFalse(payload["changed"])
            self.assertEqual(self._config(tmp), "")
            self.assertFalse((Path(tmp) / "hermes" / "skins" / "omh-crimson.yaml").exists())

    def test_an_invalid_name_fails_and_names_the_valid_ones(self) -> None:
        with TemporaryDirectory() as tmp:
            status, out, _ = run_cli([*self._homes(tmp), "theme", "use", "ultraviolet"], output_json=False)
            self.assertEqual(status, 2)
            self.assertIn("ultraviolet", out)
            for name in ("sky", "amber", "crimson", "mono"):
                self.assertIn(name, out)
            self.assertEqual(self._config(tmp), "")

    def test_status_reports_an_omh_theme_as_ours(self) -> None:
        with TemporaryDirectory() as tmp:
            homes = self._homes(tmp)
            run_cli([*homes, "theme", "use", "amber"])
            payload = json.loads(run_cli([*homes, "theme", "status"])[1])
            self.assertEqual(payload["schema_version"], "omh_theme_status/v1")
            self.assertTrue(payload["active_is_omh"])
            self.assertEqual(payload["active_theme"], "amber")
            self.assertFalse(payload["active_is_unset"])
            self.assertIn("Restart", payload["restart_note"])
            self.assertEqual({entry["state"] for entry in payload["managed"]}, {"managed"})

    def test_status_reports_a_foreign_skin_as_the_users_own(self) -> None:
        with TemporaryDirectory() as tmp:
            homes = self._homes(tmp)
            (Path(tmp) / "hermes" / "config.yaml").write_text("display:\n  skin: ares\n", encoding="utf-8")
            payload = json.loads(run_cli([*homes, "theme", "status"])[1])
            self.assertFalse(payload["active_is_omh"])
            self.assertEqual(payload["active_skin"], "ares")
            self.assertEqual(payload["active_theme"], "")
            _, out, _ = run_cli([*homes, "theme", "status"], output_json=False)
            self.assertIn("not an OMH theme", out)

    def test_choosing_a_theme_over_a_foreign_skin_is_explicit_consent(self) -> None:
        # `omh theme use` is the operator asking for it, so unlike setup's
        # unset-only default writer it may replace a foreign value.
        with TemporaryDirectory() as tmp:
            homes = self._homes(tmp)
            (Path(tmp) / "hermes" / "config.yaml").write_text("display:\n  skin: ares\n", encoding="utf-8")
            payload = json.loads(run_cli([*homes, "theme", "use", "mono"])[1])
            self.assertEqual(payload["previous_skin"], "ares")
            self.assertTrue(payload["changed"])
            self.assertEqual(display_skin_selection(self._config(tmp)), "omh-mono")
            self.assertEqual(payload["reverse_command"], "omh theme use sky")


if __name__ == "__main__":
    unittest.main()
