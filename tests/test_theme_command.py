"""`omh theme` -- the user-facing switch for the shipped TUI palettes.

Themes only matter if a person can pick one without editing YAML, so these
tests pin the command surface end to end: what `list` reports, that `use`
writes `display.skin` and installs the files it selects, that `--dry-run`
writes nothing, that an invalid name fails loudly with the valid ones named,
and that a foreign skin is reported as the user's own rather than overwritten
behind their back.

`repair` is the same contract read backwards: it is the only command that may
overwrite a theme file OMH cannot prove it wrote, so the tests pin that the
bare form never writes, that naming a theme is what makes it destructive, and
that `status`/`list` point at it exactly when something is unmanaged.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _cli_harness import run_cli

from omh.install.config_adapter import display_skin_selection
from omh.skin_pack import MANIFEST_FILENAME, SKIN_FILENAME, skin_colors, skin_payload


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


class ThemeRepairCommandTests(unittest.TestCase):
    """The repair surface: report by default, overwrite only when asked.

    There is no digest that separates a theme file OMH wrote from one a person
    wrote once the manifest has gone stale AND the template has moved on, so
    the command invocation is the consent. Every test here is a statement about
    where that line sits.
    """

    def _homes(self, tmp: str) -> list[str]:
        root = Path(tmp)
        (root / "hermes").mkdir()
        (root / "omh").mkdir()
        return ["--omh-home", str(root / "omh"), "--hermes-home", str(root / "hermes")]

    def _strand_sky(self, tmp: str, homes: list[str]) -> bytes:
        """Reproduce the live drift: an older-release `omh.yaml`, no record for it.

        Exactly the state `omh theme status` reported as `sky omh.yaml -
        unmanaged` on the owner's machine after `omh update` to merged main.
        """
        run_cli([*homes, "theme", "use", "sky"])
        skins = Path(tmp) / "hermes" / "skins"
        stale = skin_payload().replace(skin_colors()["ui_label"].encode(), b"#7FDBFF")
        (skins / SKIN_FILENAME).write_bytes(stale)
        manifest = skins / MANIFEST_FILENAME
        record = json.loads(manifest.read_text(encoding="utf-8"))
        record["files"].pop(SKIN_FILENAME)
        manifest.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        return stale

    def test_the_bare_repair_reports_the_change_and_writes_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            homes = self._homes(tmp)
            stale = self._strand_sky(tmp, homes)
            status, out, _ = run_cli([*homes, "theme", "repair"], output_json=False)
            self.assertEqual(status, 0)
            self.assertIn("unmanaged; NOT adopted", out)
            # The before/after a person needs in order to accept knowingly.
            self.assertIn(f"ui_label: #7FDBFF -> {skin_colors()['ui_label']}", out)
            self.assertIn("Nothing was written.", out)
            self.assertIn("omh theme repair --all", out)
            self.assertEqual((Path(tmp) / "hermes" / "skins" / SKIN_FILENAME).read_bytes(), stale)

    def test_naming_a_theme_adopts_it_and_records_it(self) -> None:
        with TemporaryDirectory() as tmp:
            homes = self._homes(tmp)
            self._strand_sky(tmp, homes)
            status, out, _ = run_cli([*homes, "theme", "repair", "sky"], output_json=False)
            self.assertEqual(status, 0)
            self.assertIn("repaired; replaced with the shipped file", out)
            skins = Path(tmp) / "hermes" / "skins"
            self.assertEqual((skins / SKIN_FILENAME).read_bytes(), skin_payload())
            manifest = json.loads((skins / MANIFEST_FILENAME).read_text(encoding="utf-8"))
            self.assertIn(SKIN_FILENAME, manifest["files"])
            # Idempotent afterwards: a repaired file is simply managed.
            _, again, _ = run_cli([*homes, "theme", "repair"], output_json=False)
            self.assertIn("there is nothing to repair", again)

    def test_a_dry_run_shows_the_same_report_and_writes_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            homes = self._homes(tmp)
            stale = self._strand_sky(tmp, homes)
            status, out, _ = run_cli([*homes, "theme", "repair", "sky", "--dry-run"], output_json=False)
            self.assertEqual(status, 0)
            self.assertIn("WOULD be replaced with the shipped file", out)
            self.assertIn(f"ui_label: #7FDBFF -> {skin_colors()['ui_label']}", out)
            self.assertIn("Dry run: nothing was written.", out)
            self.assertEqual((Path(tmp) / "hermes" / "skins" / SKIN_FILENAME).read_bytes(), stale)

    def test_all_adopts_every_unmanaged_file_at_once(self) -> None:
        with TemporaryDirectory() as tmp:
            homes = self._homes(tmp)
            self._strand_sky(tmp, homes)
            skins = Path(tmp) / "hermes" / "skins"
            (skins / "omh-amber.yaml").write_text("name: omh-amber\n# mine\n", encoding="utf-8")
            payload = json.loads(run_cli([*homes, "theme", "repair", "--all"])[1])
            statuses = {entry["theme"]: entry["status"] for entry in payload["themes"]}
            self.assertEqual(statuses["sky"], "repaired")
            self.assertEqual(statuses["amber"], "repaired")
            self.assertEqual(statuses["mono"], "managed")

    def test_a_hand_written_skin_survives_the_no_argument_form(self) -> None:
        # The negative control. A file a person actually authored looks exactly
        # like a stranded one of ours, so the safety has to come from consent:
        # reporting must never touch it, and `--dry-run` must not either.
        with TemporaryDirectory() as tmp:
            homes = self._homes(tmp)
            run_cli([*homes, "theme", "use", "sky"])
            destination = Path(tmp) / "hermes" / "skins" / SKIN_FILENAME
            mine = b"name: omh\n# entirely my own skin\n"
            destination.write_bytes(mine)

            run_cli([*homes, "theme", "repair"], output_json=False)
            self.assertEqual(destination.read_bytes(), mine)
            run_cli([*homes, "theme", "repair", "sky", "--dry-run"], output_json=False)
            self.assertEqual(destination.read_bytes(), mine)
            run_cli([*homes, "theme", "list"], output_json=False)
            self.assertEqual(destination.read_bytes(), mine)
            run_cli([*homes, "theme", "status"], output_json=False)
            self.assertEqual(destination.read_bytes(), mine)
            # Selecting a theme must not repair either -- `use` installs, and
            # installing has always left an unmanaged file alone.
            run_cli([*homes, "theme", "use", "sky"])
            self.assertEqual(destination.read_bytes(), mine)

            # Only the explicit name takes it, and that is the whole contract.
            run_cli([*homes, "theme", "repair", "sky"], output_json=False)
            self.assertEqual(destination.read_bytes(), skin_payload())

    def test_a_name_and_all_together_is_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            status, out, _ = run_cli([*self._homes(tmp), "theme", "repair", "sky", "--all"], output_json=False)
            self.assertEqual(status, 2)
            self.assertIn("not both", out)

    def test_an_unknown_theme_name_is_refused_with_the_valid_ones(self) -> None:
        with TemporaryDirectory() as tmp:
            status, out, _ = run_cli([*self._homes(tmp), "theme", "repair", "ultraviolet"], output_json=False)
            self.assertEqual(status, 2)
            self.assertIn("unknown theme", out)
            self.assertIn("crimson", out)

    def test_the_json_payload_carries_the_digests_and_the_palette_diff(self) -> None:
        with TemporaryDirectory() as tmp:
            homes = self._homes(tmp)
            self._strand_sky(tmp, homes)
            payload = json.loads(run_cli([*homes, "theme", "repair"])[1])
            self.assertEqual(payload["schema_version"], "omh_theme_repair/v1")
            self.assertEqual(payload["mode"], "report")
            self.assertEqual(payload["status"], "unmanaged")
            self.assertEqual(payload["requested"], "")
            self.assertFalse(payload["all"])
            self.assertFalse(payload["dry_run"])
            self.assertEqual([entry["theme"] for entry in payload["themes"]], ["sky", "amber", "crimson", "mono"])
            sky = payload["themes"][0]
            self.assertEqual(
                sorted(sky),
                [
                    "after_sha256",
                    "before_sha256",
                    "filename",
                    "palette_changes",
                    "path",
                    "skin",
                    "state",
                    "status",
                    "theme",
                ],
            )
            self.assertEqual(sky["state"], "unmanaged")
            self.assertEqual(sky["status"], "unmanaged")
            self.assertNotEqual(sky["before_sha256"], sky["after_sha256"])
            self.assertEqual(
                sky["palette_changes"],
                [{"key": "ui_label", "before": "#7FDBFF", "after": skin_colors()["ui_label"]}],
            )

    def test_status_and_list_point_at_repair_only_while_something_is_unmanaged(self) -> None:
        # Discoverability is the other half of the fix: a frozen skin that
        # nobody can find the cure for stays frozen. An always-on hint would
        # be noise, so it appears exactly when it applies.
        with TemporaryDirectory() as tmp:
            homes = self._homes(tmp)
            run_cli([*homes, "theme", "use", "sky"])
            for surface in ("status", "list"):
                _, out, _ = run_cli([*homes, "theme", surface], output_json=False)
                self.assertNotIn("omh theme repair", out)
                self.assertEqual(json.loads(run_cli([*homes, "theme", surface])[1])["unmanaged_themes"], [])

            self._strand_sky(tmp, homes)
            for surface in ("status", "list"):
                _, out, _ = run_cli([*homes, "theme", surface], output_json=False)
                self.assertIn("Unmanaged: sky", out)
                self.assertIn("omh theme repair", out)
                self.assertEqual(json.loads(run_cli([*homes, "theme", surface])[1])["unmanaged_themes"], ["sky"])
