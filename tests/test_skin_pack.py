"""The managed OMH identity skins: install discipline and activation rules.

The skins are the owner-directed identity default — installing OMH opts into
the OH-MY-HERMES look the way installing oh-my-zsh restyles the shell — so
these tests pin the edges that keep that honest. Every shipped theme file is
only ever overwritten when a manifest record proves OMH wrote THAT file, and
`display.skin` is only ever defaulted in the unset case. An explicit user skin
wins forever, in both places, and so does an explicitly chosen OMH theme.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from _platform_support import requires_symlinks

from omh.install.config_adapter import display_skin_selection, ensure_omh_skin
from omh.skin_pack import (
    MANIFEST_FILENAME,
    SKIN_FILENAME,
    SKIN_NAME,
    SKIN_THEMES,
    SkinInstallError,
    available_skins,
    hex_to_rgb,
    install_skin,
    installed_skin_report,
    is_omh_skin_name,
    repair_skins,
    skin_colors,
    skin_payload,
    theme_for_name,
    theme_for_skin_name,
    theme_names,
    uninstall_skin,
)

_SKINS_DIR = Path(__file__).resolve().parents[1] / "src" / "omh" / "skins"


def _skin_key_paths(text: str) -> list[str]:
    """Top-level and one-level-nested mapping keys, in document order.

    A deliberately tiny reader rather than a YAML dependency: OMH ships with
    zero runtime dependencies and the skin documents are ours, with a fixed
    two-level block-mapping shape. It exists to compare STRUCTURE across
    themes, which is all the parity guard needs.
    """
    paths: list[str] = []
    section = ""
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("  ") and not line.startswith("    "):
            key, separator, _ = line.strip().partition(":")
            if separator and not key.startswith("- "):
                paths.append(f"{section}.{key}")
        elif not line.startswith(" "):
            key, separator, _ = line.partition(":")
            if separator:
                section = key
                paths.append(key)
    return paths


def _logo_rows(text: str) -> list[str]:
    # No strip: the O and the closing S rows legitimately begin with a
    # space, and eating it is exactly the misalignment this catches. Only the
    # rich markup and the uniform YAML block indent go.
    return [
        re.sub(r"\[[^\]]*\]", "", line).removeprefix("  ")
        for line in text.splitlines()
        if "██" in line or "╚" in line
    ]


class SkinRegistryTests(unittest.TestCase):
    def test_the_registry_matches_the_packaged_skin_files(self) -> None:
        # The registry is the ordered source of truth for the command surface;
        # a YAML added to the package without a registry row would ship a file
        # nobody can select, and a row without a file would crash on install.
        on_disk = {path.name for path in _SKINS_DIR.glob("*.yaml")}
        self.assertEqual({theme.filename for theme in SKIN_THEMES}, on_disk)

    def test_the_shipped_set_is_the_four_named_themes(self) -> None:
        self.assertEqual(available_skins(), ("omh", "omh-amber", "omh-crimson", "omh-mono"))
        self.assertEqual(theme_names(), ("sky", "amber", "crimson", "mono"))
        self.assertEqual(SKIN_THEMES[0].skin_name, SKIN_NAME)

    def test_short_names_aliases_and_full_names_all_resolve(self) -> None:
        for value in ("sky", "default", "omh", "SKY", " amber "):
            with self.subTest(value=value):
                self.assertIsNotNone(theme_for_name(value))
        self.assertEqual(theme_for_name("crimson").skin_name, "omh-crimson")
        self.assertEqual(theme_for_name("omh-mono").short_name, "mono")
        self.assertIsNone(theme_for_name("ares"))
        self.assertIsNone(theme_for_name(""))

    def test_a_config_value_resolves_only_by_skin_name(self) -> None:
        # `display.skin: amber` is somebody else's skin, not our amber theme.
        self.assertIsNone(theme_for_skin_name("amber"))
        self.assertEqual(theme_for_skin_name("omh-amber").short_name, "amber")

    def test_ownership_covers_every_shipped_skin_and_nothing_else(self) -> None:
        for name in available_skins():
            with self.subTest(name=name):
                self.assertTrue(is_omh_skin_name(name))
        for name in ("ares", "midnight", "default", "sky", "amber", ""):
            with self.subTest(name=name):
                self.assertFalse(is_omh_skin_name(name))

    def test_every_theme_has_a_payload(self) -> None:
        for theme in SKIN_THEMES:
            with self.subTest(theme=theme.short_name):
                text = skin_payload(theme.skin_name).decode("utf-8")
                self.assertIn(f"name: {theme.skin_name}", text)
        self.assertEqual(skin_payload(), skin_payload(SKIN_NAME))

    def test_an_unknown_payload_name_is_refused(self) -> None:
        with self.assertRaises(SkinInstallError):
            skin_payload("ares")


class SkinPayloadTests(unittest.TestCase):
    def test_the_shipped_skin_carries_the_identity_contract(self) -> None:
        text = skin_payload().decode("utf-8")
        self.assertIn("name: omh", text)
        # The rename is the point: Hermes' banner title and welcome line read
        # OH-MY-HERMES through skin branding alone, never through a patch.
        self.assertIn('agent_name: "OH-MY-HERMES"', text)
        self.assertIn("banner_logo:", text)
        # The palette anchors on the README badge turquoise.
        self.assertIn('"#00CED1"', text)
        # Diff underlays are dark-palette overrides: the engine's light-pastel
        # defaults render as harsh full-brightness bands on a dark terminal.
        self.assertIn('diff_removed: "#3A2027"', text)
        self.assertIn('diff_added: "#12362D"', text)
        self.assertIn("diff_removed_word:", text)
        self.assertIn("diff_added_word:", text)

    def test_every_theme_keeps_the_default_key_structure(self) -> None:
        # The parity guard. A new theme that forgets `status_bar_bg` or a diff
        # underlay renders half-stock chrome, which reads as a broken install
        # rather than a theme; comparing key paths catches it at commit time.
        expected = _skin_key_paths(skin_payload(SKIN_NAME).decode("utf-8"))
        for theme in SKIN_THEMES:
            with self.subTest(theme=theme.short_name):
                self.assertEqual(_skin_key_paths(skin_payload(theme.skin_name).decode("utf-8")), expected)

    def test_every_theme_keeps_the_identity_branding(self) -> None:
        for theme in SKIN_THEMES:
            with self.subTest(theme=theme.short_name):
                text = skin_payload(theme.skin_name).decode("utf-8")
                self.assertIn('agent_name: "OH-MY-HERMES"', text)
                self.assertIn("diff_removed_word:", text)
                self.assertIn("diff_added_word:", text)

    def test_no_theme_declares_a_background_token(self) -> None:
        # Any `background` value that differs from the terminal's own paints
        # every banner text run as a lighter selected-looking box. Omitting it
        # is the invariant, not an oversight, so it holds for every theme.
        for theme in SKIN_THEMES:
            with self.subTest(theme=theme.short_name):
                text = skin_payload(theme.skin_name).decode("utf-8")
                self.assertNotIn("\n  background:", text)

    def test_user_prompt_rows_never_share_the_banner_title_colour(self) -> None:
        # `ui_label` paints the user's own prompt rows in the transcript. When
        # it equals `banner_title` those rows read as more label-coloured
        # chrome and the eye loses the one line it scans for, so every theme
        # keeps the two apart and keeps the label clearly above the dims.
        for theme in SKIN_THEMES:
            with self.subTest(theme=theme.short_name):
                colors = skin_colors(theme.skin_name)
                label = colors["ui_label"]
                self.assertNotEqual(label, colors["banner_title"])
                for dim_token in ("banner_dim", "ui_thinking", "status_bar_dim"):
                    self.assertGreater(
                        sum(hex_to_rgb(label)),
                        sum(hex_to_rgb(colors[dim_token])),
                        f"{theme.short_name}: ui_label must outshine {dim_token}",
                    )

    def test_every_colour_token_is_a_readable_hex(self) -> None:
        for theme in SKIN_THEMES:
            for token, value in skin_colors(theme.skin_name).items():
                with self.subTest(theme=theme.short_name, token=token):
                    self.assertIsNotNone(hex_to_rgb(value))
        self.assertIsNone(hex_to_rgb("#fff"))
        self.assertIsNone(hex_to_rgb("#GGGGGG"))

    def test_the_logo_rows_are_equally_wide_in_every_theme(self) -> None:
        # A block logo with ragged rows renders as visible corruption in the
        # banner; alignment is a contract, not cosmetics. Recolouring the logo
        # by hand is exactly how a row loses a character, so every theme is
        # checked, and every theme against the same width.
        widths: set[int] = set()
        for theme in SKIN_THEMES:
            with self.subTest(theme=theme.short_name):
                rows = _logo_rows(skin_payload(theme.skin_name).decode("utf-8"))
                self.assertEqual(len(rows), 6)
                self.assertEqual(len({len(row) for row in rows}), 1)
                widths.add(len(rows[0]))
        self.assertEqual(len(widths), 1)


class SkinInstallTests(unittest.TestCase):
    def test_install_writes_every_theme_and_a_per_file_manifest(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            result = install_skin(home)
            self.assertEqual(result["status"], "installed")
            for theme in SKIN_THEMES:
                destination = home / "skins" / theme.filename
                self.assertEqual(destination.read_bytes(), skin_payload(theme.skin_name))
            manifest = json.loads((home / "skins" / MANIFEST_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], "omh_skin_manifest/v2")
            self.assertEqual(sorted(manifest["files"]), sorted(theme.filename for theme in SKIN_THEMES))
            self.assertIn("sha256", manifest["files"][SKIN_FILENAME])

    def test_reinstall_is_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            install_skin(home)
            self.assertEqual(install_skin(home)["status"], "unchanged")

    def test_dry_run_writes_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            self.assertEqual(install_skin(home, dry_run=True)["status"], "would_install")
            self.assertFalse((home / "skins" / SKIN_FILENAME).exists())
            self.assertFalse((home / "skins" / MANIFEST_FILENAME).exists())

    def test_a_user_authored_theme_file_is_never_replaced(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            destination = home / "skins" / SKIN_FILENAME
            destination.parent.mkdir(parents=True)
            destination.write_text("name: omh\n# mine\n", encoding="utf-8")
            entries = {entry["filename"]: entry["status"] for entry in install_skin(home)["skins"]}
            self.assertEqual(entries[SKIN_FILENAME], "kept_unmanaged")
            self.assertEqual(destination.read_text(encoding="utf-8"), "name: omh\n# mine\n")

    def test_one_user_authored_theme_does_not_block_the_others(self) -> None:
        # Per-file isolation is the whole reason the manifest went per-file: a
        # hand-edited omh-mono.yaml used to make the shared record mismatch, so
        # `omh update` stopped refreshing the default skin too.
        with TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            mine = home / "skins" / "omh-mono.yaml"
            mine.parent.mkdir(parents=True)
            mine.write_text("name: omh-mono\n# mine\n", encoding="utf-8")
            entries = {entry["filename"]: entry["status"] for entry in install_skin(home)["skins"]}
            self.assertEqual(entries["omh-mono.yaml"], "kept_unmanaged")
            self.assertEqual(entries[SKIN_FILENAME], "installed")
            self.assertEqual(mine.read_text(encoding="utf-8"), "name: omh-mono\n# mine\n")
            manifest = json.loads((home / "skins" / MANIFEST_FILENAME).read_text(encoding="utf-8"))
            self.assertNotIn("omh-mono.yaml", manifest["files"])

    def test_a_v1_manifest_still_proves_ownership_of_the_default_skin(self) -> None:
        # Every machine installed before themes existed carries a v1 manifest.
        # Failing to read it would classify OMH's own omh.yaml as user-authored
        # and freeze it at the version it shipped with.
        with TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            skins = home / "skins"
            skins.mkdir(parents=True)
            legacy_payload = b"name: omh\n# an older shipped build\n"
            (skins / SKIN_FILENAME).write_bytes(legacy_payload)
            (skins / MANIFEST_FILENAME).write_text(
                json.dumps(
                    {
                        "schema_version": "omh_skin_manifest/v1",
                        "filename": SKIN_FILENAME,
                        "sha256": hashlib.sha256(legacy_payload).hexdigest(),
                    }
                ),
                encoding="utf-8",
            )
            entries = {entry["filename"]: entry["status"] for entry in install_skin(home)["skins"]}
            self.assertEqual(entries[SKIN_FILENAME], "installed")
            self.assertEqual((skins / SKIN_FILENAME).read_bytes(), skin_payload())
            manifest = json.loads((skins / MANIFEST_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], "omh_skin_manifest/v2")

    def test_a_stale_manifest_over_our_own_bytes_self_heals(self) -> None:
        # Observed on the owner's machine while smoke-testing this branch: the
        # installed omh.yaml was byte-identical to the shipped template, but
        # the v1 manifest still recorded an older sha, because some past update
        # refreshed the file without refreshing the record. Record-only
        # ownership read that as user-authored, so the skin would have been
        # kept_unmanaged forever and no future template change -- including the
        # ui_label brightening in this very commit -- would ever have reached
        # it. Matching the current template is the second ownership proof.
        with TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            skins = home / "skins"
            skins.mkdir(parents=True)
            (skins / SKIN_FILENAME).write_bytes(skin_payload())
            (skins / MANIFEST_FILENAME).write_text(
                json.dumps(
                    {
                        "schema_version": "omh_skin_manifest/v1",
                        "filename": SKIN_FILENAME,
                        "sha256": "1420e408" + "0" * 56,
                    }
                ),
                encoding="utf-8",
            )
            # Reported as ours before anything is written, so `omh theme
            # status` on such a machine stops saying `unmanaged`.
            states = {entry["skin"]: entry["state"] for entry in installed_skin_report(home)}
            self.assertEqual(states["omh"], "managed")

            entries = {entry["filename"]: entry["status"] for entry in install_skin(home)["skins"]}
            self.assertEqual(entries[SKIN_FILENAME], "unchanged")
            manifest = json.loads((skins / MANIFEST_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], "omh_skin_manifest/v2")
            self.assertEqual(
                manifest["files"][SKIN_FILENAME]["sha256"],
                hashlib.sha256(skin_payload()).hexdigest(),
            )

    def test_a_healed_machine_receives_the_next_template_change(self) -> None:
        # The point of healing the record is not the record, it is that the
        # NEXT release reaches the machine. Ship a changed template and assert
        # the file actually moves.
        with TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            skins = home / "skins"
            skins.mkdir(parents=True)
            (skins / SKIN_FILENAME).write_bytes(skin_payload())
            (skins / MANIFEST_FILENAME).write_text(
                json.dumps(
                    {
                        "schema_version": "omh_skin_manifest/v1",
                        "filename": SKIN_FILENAME,
                        "sha256": "1420e408" + "0" * 56,
                    }
                ),
                encoding="utf-8",
            )
            install_skin(home)

            shipped = skin_payload
            next_release = skin_payload(SKIN_NAME) + b"# a later release\n"

            def _next_template(name: str = SKIN_NAME) -> bytes:
                return next_release if name == SKIN_NAME else shipped(name)

            with patch("omh.skin_pack.skin_payload", _next_template):
                entries = {entry["filename"]: entry["status"] for entry in install_skin(home)["skins"]}
            self.assertEqual(entries[SKIN_FILENAME], "installed")
            self.assertEqual((skins / SKIN_FILENAME).read_bytes(), next_release)

    def test_a_genuinely_edited_file_is_still_never_adopted(self) -> None:
        # The negative half of the self-heal: matching neither the manifest
        # record nor the current template is what user-authored means, and that
        # file keeps winning forever.
        with TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            skins = home / "skins"
            skins.mkdir(parents=True)
            mine = skin_payload().replace(b"name: omh\n", b"name: omh\n# my own tweak\n")
            (skins / SKIN_FILENAME).write_bytes(mine)
            (skins / MANIFEST_FILENAME).write_text(
                json.dumps(
                    {
                        "schema_version": "omh_skin_manifest/v1",
                        "filename": SKIN_FILENAME,
                        "sha256": "1420e408" + "0" * 56,
                    }
                ),
                encoding="utf-8",
            )
            states = {entry["skin"]: entry["state"] for entry in installed_skin_report(home)}
            self.assertEqual(states["omh"], "unmanaged")

            entries = {entry["filename"]: entry["status"] for entry in install_skin(home)["skins"]}
            self.assertEqual(entries[SKIN_FILENAME], "kept_unmanaged")
            self.assertEqual((skins / SKIN_FILENAME).read_bytes(), mine)
            manifest = json.loads((skins / MANIFEST_FILENAME).read_text(encoding="utf-8"))
            self.assertNotIn(SKIN_FILENAME, manifest["files"])

    def test_uninstall_takes_back_a_file_it_would_have_adopted(self) -> None:
        # Symmetry: if install adopts our own bytes despite a stale record,
        # uninstall must remove them too, or removal leaves orphans behind.
        with TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            skins = home / "skins"
            skins.mkdir(parents=True)
            (skins / SKIN_FILENAME).write_bytes(skin_payload())
            (skins / MANIFEST_FILENAME).write_text(
                json.dumps(
                    {
                        "schema_version": "omh_skin_manifest/v1",
                        "filename": SKIN_FILENAME,
                        "sha256": "1420e408" + "0" * 56,
                    }
                ),
                encoding="utf-8",
            )
            entries = {entry["filename"]: entry["status"] for entry in uninstall_skin(home)["skins"]}
            self.assertEqual(entries[SKIN_FILENAME], "removed")
            self.assertFalse((skins / SKIN_FILENAME).exists())

    def test_uninstall_removes_only_the_managed_files(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            install_skin(home)
            self.assertEqual(uninstall_skin(home)["status"], "removed")
            for theme in SKIN_THEMES:
                self.assertFalse((home / "skins" / theme.filename).exists())
            self.assertFalse((home / "skins" / MANIFEST_FILENAME).exists())

    def test_uninstall_keeps_a_user_authored_file_and_the_records_of_the_rest(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            install_skin(home)
            mine = home / "skins" / "omh-amber.yaml"
            mine.write_text("name: omh-amber\n# mine\n", encoding="utf-8")
            entries = {entry["filename"]: entry["status"] for entry in uninstall_skin(home)["skins"]}
            self.assertEqual(entries["omh-amber.yaml"], "kept_unmanaged")
            self.assertEqual(entries[SKIN_FILENAME], "removed")
            self.assertTrue(mine.exists())
            manifest = json.loads((home / "skins" / MANIFEST_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(list(manifest["files"]), ["omh-amber.yaml"])

    def test_uninstall_on_a_clean_home_is_absent(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            self.assertEqual(uninstall_skin(home)["status"], "absent")

    def test_installed_report_names_missing_managed_and_unmanaged_files(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            self.assertEqual({entry["state"] for entry in installed_skin_report(home)}, {"missing"})
            install_skin(home)
            (home / "skins" / "omh-mono.yaml").write_text("name: omh-mono\n", encoding="utf-8")
            states = {entry["skin"]: entry["state"] for entry in installed_skin_report(home)}
            self.assertEqual(states["omh"], "managed")
            self.assertEqual(states["omh-mono"], "unmanaged")


class SkinRepairTests(unittest.TestCase):
    """`repair_skins` -- the consent-based way out of a permanently frozen skin.

    `_is_managed_file`'s self-heal covers a stale manifest over an UNCHANGED
    template. The gap this closes is the one seen on the owner's machine after
    that fix shipped: the manifest went stale at some earlier point AND the
    template has since moved on, so the installed file matches neither proof.
    It is ours in origin, indistinguishable from a hand-written one on disk,
    and frozen forever. Consent is therefore the only sound gate, and the
    command invocation is the consent -- which is exactly why the no-argument
    form must never write.
    """

    def _older_template(self, skin_name: str = SKIN_NAME) -> bytes:
        """A believable previous release of one theme: same document, older palette.

        Shaped after the observed drift, where the installed `omh.yaml` still
        carried the pre-brightening `#7FDBFF` `ui_label` while the shipped
        template had moved on to `#9FE8FF`. Derived from the current template
        rather than pinned to literal bytes so it keeps meaning "one release
        behind" as the palette keeps evolving.
        """
        payload = skin_payload(skin_name).replace(skin_colors(skin_name)["ui_label"].encode(), b"#7FDBFF")
        self.assertNotEqual(payload, skin_payload(skin_name))
        return payload

    def _stranded_home(self, tmp: str) -> tuple[Path, bytes]:
        """The exact live shape: an older-template file, no record naming it.

        A v2 manifest that records the three other themes and not `omh.yaml`,
        which is what `omh theme status` was reporting as `sky omh.yaml -
        unmanaged` on the machine that motivated this.
        """
        home = Path(tmp).resolve()
        install_skin(home)
        skins = home / "skins"
        stale = self._older_template()
        (skins / SKIN_FILENAME).write_bytes(stale)
        manifest = skins / MANIFEST_FILENAME
        record = json.loads(manifest.read_text(encoding="utf-8"))
        record["files"].pop(SKIN_FILENAME)
        manifest.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        states = {entry["skin"]: entry["state"] for entry in installed_skin_report(home)}
        self.assertEqual(states[SKIN_NAME], "unmanaged")
        return home, stale

    def test_repair_adopts_a_file_stranded_by_a_stale_record_and_a_moved_template(self) -> None:
        with TemporaryDirectory() as tmp:
            home, stale = self._stranded_home(tmp)
            result = repair_skins(home, adopt=frozenset({SKIN_FILENAME}))
            entries = {entry["filename"]: entry for entry in result["skins"]}
            self.assertEqual(result["status"], "repaired")
            self.assertEqual(entries[SKIN_FILENAME]["state"], "unmanaged")
            self.assertEqual(entries[SKIN_FILENAME]["status"], "repaired")
            self.assertEqual(entries[SKIN_FILENAME]["before_sha256"], hashlib.sha256(stale).hexdigest())
            self.assertEqual(
                entries[SKIN_FILENAME]["after_sha256"],
                hashlib.sha256(skin_payload()).hexdigest(),
            )
            self.assertEqual((home / "skins" / SKIN_FILENAME).read_bytes(), skin_payload())

            manifest = json.loads((home / "skins" / MANIFEST_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], "omh_skin_manifest/v2")
            self.assertEqual(
                manifest["files"][SKIN_FILENAME]["sha256"],
                hashlib.sha256(skin_payload()).hexdigest(),
            )
            # The whole point: the machine is back on the update path.
            states = {entry["skin"]: entry["state"] for entry in installed_skin_report(home)}
            self.assertEqual(states[SKIN_NAME], "managed")

    def test_a_repaired_machine_receives_the_next_release(self) -> None:
        # Recording the file is not the win; the win is that the NEXT template
        # change actually lands on it. Ship one and watch the bytes move.
        with TemporaryDirectory() as tmp:
            home, _ = self._stranded_home(tmp)
            repair_skins(home, adopt=frozenset({SKIN_FILENAME}))

            shipped = skin_payload
            next_release = skin_payload(SKIN_NAME) + b"# a later release\n"

            def _next_template(name: str = SKIN_NAME) -> bytes:
                return next_release if name == SKIN_NAME else shipped(name)

            with patch("omh.skin_pack.skin_payload", _next_template):
                entries = {entry["filename"]: entry["status"] for entry in install_skin(home)["skins"]}
            self.assertEqual(entries[SKIN_FILENAME], "installed")
            self.assertEqual((home / "skins" / SKIN_FILENAME).read_bytes(), next_release)

    def test_repair_names_the_palette_tokens_that_would_change(self) -> None:
        # A person accepting an overwrite of a file we cannot prove we wrote
        # gets to see what they are trading away first.
        with TemporaryDirectory() as tmp:
            home, _ = self._stranded_home(tmp)
            entries = {entry["filename"]: entry for entry in repair_skins(home)["skins"]}
            self.assertEqual(
                entries[SKIN_FILENAME]["palette_changes"],
                [{"key": "ui_label", "before": "#7FDBFF", "after": skin_colors()["ui_label"]}],
            )

    def test_the_bare_repair_reports_and_never_writes(self) -> None:
        # The safety contract: with nothing named there is no consent, so the
        # reporting form must be safe and idempotent to run.
        with TemporaryDirectory() as tmp:
            home, stale = self._stranded_home(tmp)
            manifest = home / "skins" / MANIFEST_FILENAME
            before_manifest = manifest.read_bytes()
            result = repair_skins(home)
            entries = {entry["filename"]: entry["status"] for entry in result["skins"]}
            self.assertEqual(result["status"], "unmanaged")
            self.assertEqual(entries[SKIN_FILENAME], "unmanaged")
            self.assertEqual((home / "skins" / SKIN_FILENAME).read_bytes(), stale)
            self.assertEqual(manifest.read_bytes(), before_manifest)
            self.assertEqual(repair_skins(home)["status"], "unmanaged")
            self.assertEqual((home / "skins" / SKIN_FILENAME).read_bytes(), stale)

    def test_a_dry_run_shows_the_repair_without_performing_it(self) -> None:
        with TemporaryDirectory() as tmp:
            home, stale = self._stranded_home(tmp)
            manifest = home / "skins" / MANIFEST_FILENAME
            before_manifest = manifest.read_bytes()
            result = repair_skins(home, adopt=frozenset({SKIN_FILENAME}), dry_run=True)
            entries = {entry["filename"]: entry for entry in result["skins"]}
            self.assertEqual(result["status"], "would_repair")
            self.assertEqual(entries[SKIN_FILENAME]["status"], "would_repair")
            self.assertEqual(
                entries[SKIN_FILENAME]["after_sha256"],
                hashlib.sha256(skin_payload()).hexdigest(),
            )
            self.assertEqual((home / "skins" / SKIN_FILENAME).read_bytes(), stale)
            self.assertEqual(manifest.read_bytes(), before_manifest)

    def test_a_genuinely_user_authored_file_is_overwritten_only_when_named(self) -> None:
        # The negative case that defines the consent contract. Nothing on disk
        # separates this file from a stranded one of ours, so repair CAN take
        # it -- but only because a person typed its name. Bare repair and
        # --dry-run must both leave it exactly as written.
        with TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            install_skin(home)
            mine = skin_payload().replace(b"name: omh\n", b"name: omh\n# my own tweak\n")
            destination = home / "skins" / SKIN_FILENAME
            destination.write_bytes(mine)

            self.assertEqual(
                {entry["filename"]: entry["status"] for entry in repair_skins(home)["skins"]}[SKIN_FILENAME],
                "unmanaged",
            )
            self.assertEqual(destination.read_bytes(), mine)

            repair_skins(home, adopt=frozenset({SKIN_FILENAME}), dry_run=True)
            self.assertEqual(destination.read_bytes(), mine)

            adopted = repair_skins(home, adopt=frozenset({SKIN_FILENAME}))
            entries = {entry["filename"]: entry["status"] for entry in adopted["skins"]}
            self.assertEqual(entries[SKIN_FILENAME], "repaired")
            self.assertEqual(destination.read_bytes(), skin_payload())

    def test_repair_leaves_already_managed_files_untouched(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            install_skin(home)
            manifest = home / "skins" / MANIFEST_FILENAME
            before_manifest = manifest.read_bytes()
            result = repair_skins(home, adopt=frozenset(theme.filename for theme in SKIN_THEMES))
            self.assertEqual(result["status"], "managed")
            self.assertEqual({entry["status"] for entry in result["skins"]}, {"managed"})
            self.assertEqual(manifest.read_bytes(), before_manifest)

    def test_repair_installs_a_missing_file_only_when_named(self) -> None:
        # Consistent with `install_skin`, which installs a file that is not
        # there; without a name it is reported and left alone.
        with TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            install_skin(home)
            destination = home / "skins" / "omh-mono.yaml"
            destination.unlink()

            entries = {entry["filename"]: entry["status"] for entry in repair_skins(home)["skins"]}
            self.assertEqual(entries["omh-mono.yaml"], "missing")
            self.assertFalse(destination.exists())

            entries = {
                entry["filename"]: entry["status"]
                for entry in repair_skins(home, adopt=frozenset({"omh-mono.yaml"}))["skins"]
            }
            self.assertEqual(entries["omh-mono.yaml"], "installed")
            self.assertEqual(destination.read_bytes(), skin_payload("omh-mono"))

    def test_repair_all_adopts_every_file_that_needs_it_in_one_pass(self) -> None:
        with TemporaryDirectory() as tmp:
            home, _ = self._stranded_home(tmp)
            (home / "skins" / "omh-amber.yaml").write_text("name: omh-amber\n# mine\n", encoding="utf-8")
            adopted = repair_skins(home, adopt=frozenset(theme.filename for theme in SKIN_THEMES))
            entries = {entry["filename"]: entry["status"] for entry in adopted["skins"]}
            self.assertEqual(entries[SKIN_FILENAME], "repaired")
            self.assertEqual(entries["omh-amber.yaml"], "repaired")
            self.assertEqual(entries["omh-mono.yaml"], "managed")
            manifest = json.loads((home / "skins" / MANIFEST_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(sorted(manifest["files"]), sorted(theme.filename for theme in SKIN_THEMES))

    def test_repair_migrates_a_legacy_v1_manifest_forward(self) -> None:
        # The v1 -> v2 migration is an invariant of every write path, not just
        # `install_skin`: a machine old enough to be stranded is exactly the
        # machine likeliest to still hold a v1 record.
        with TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            skins = home / "skins"
            skins.mkdir(parents=True)
            stale = self._older_template()
            (skins / SKIN_FILENAME).write_bytes(stale)
            (skins / MANIFEST_FILENAME).write_text(
                json.dumps(
                    {
                        "schema_version": "omh_skin_manifest/v1",
                        "filename": SKIN_FILENAME,
                        "sha256": "1420e408" + "0" * 56,
                    }
                ),
                encoding="utf-8",
            )
            adopted = repair_skins(home, adopt=frozenset({SKIN_FILENAME}))
            entries = {entry["filename"]: entry["status"] for entry in adopted["skins"]}
            self.assertEqual(entries[SKIN_FILENAME], "repaired")
            manifest = json.loads((skins / MANIFEST_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], "omh_skin_manifest/v2")
            self.assertEqual(
                manifest["files"][SKIN_FILENAME]["sha256"],
                hashlib.sha256(skin_payload()).hexdigest(),
            )

    def test_uninstall_takes_back_a_repaired_file(self) -> None:
        # Symmetry: a file repair adopted is a file uninstall must remove, or
        # removal leaves an orphan behind on exactly the machines we just fixed.
        with TemporaryDirectory() as tmp:
            home, _ = self._stranded_home(tmp)
            repair_skins(home, adopt=frozenset({SKIN_FILENAME}))
            entries = {entry["filename"]: entry["status"] for entry in uninstall_skin(home)["skins"]}
            self.assertEqual(entries[SKIN_FILENAME], "removed")
            self.assertFalse((home / "skins" / SKIN_FILENAME).exists())

    @requires_symlinks
    def test_repair_refuses_a_symlinked_destination_and_parent(self) -> None:
        # Repair is a write path, so it inherits the whole symlink invariant:
        # naming a theme must never become a way to write through a link.
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            home = root / ".hermes"
            skins = home / "skins"
            skins.mkdir(parents=True)
            victim = root / "victim.yaml"
            victim_bytes = b"do not overwrite\n"
            victim.write_bytes(victim_bytes)
            (skins / SKIN_FILENAME).symlink_to(victim)

            with self.assertRaises(SkinInstallError):
                repair_skins(home, adopt=frozenset({SKIN_FILENAME}))
            with self.assertRaises(SkinInstallError):
                repair_skins(home)
            self.assertEqual(victim.read_bytes(), victim_bytes)

            (skins / SKIN_FILENAME).unlink()
            skins.rmdir()
            external = root / "external-skins"
            external.mkdir()
            skins.symlink_to(external, target_is_directory=True)
            with self.assertRaises(SkinInstallError):
                repair_skins(home, adopt=frozenset({SKIN_FILENAME}))
            self.assertEqual(list(external.iterdir()), [])


class EnsureOmhSkinTests(unittest.TestCase):
    def test_unset_skin_defaults_to_omh(self) -> None:
        change = ensure_omh_skin("display:\n  compact: true\n", SKIN_NAME)
        self.assertTrue(change.changed)
        self.assertEqual(display_skin_selection(change.text), SKIN_NAME)
        self.assertIn("  compact: true", change.text)

    def test_the_default_name_is_the_shipped_default_skin(self) -> None:
        self.assertEqual(display_skin_selection(ensure_omh_skin("display:\n  compact: true\n").text), SKIN_NAME)

    def test_missing_display_section_is_appended(self) -> None:
        change = ensure_omh_skin("plugins:\n  enabled:\n    - omh\n", SKIN_NAME)
        self.assertTrue(change.changed)
        self.assertEqual(display_skin_selection(change.text), SKIN_NAME)

    def test_an_explicit_skin_choice_is_never_rewritten(self) -> None:
        # `hermes skin use ares` after setup must stick across every future
        # update; rewriting it would repeat the display.interface mistake.
        text = "display:\n  skin: ares\n"
        change = ensure_omh_skin(text, SKIN_NAME)
        self.assertFalse(change.changed)
        self.assertEqual(change.text, text)
        self.assertIn("user preference", change.message)

    def test_a_chosen_omh_theme_survives_the_default_writer(self) -> None:
        # The theme is an explicit choice too, so setup/update must leave it —
        # and must say WHICH kind of choice it is, because "user preference"
        # reads as a foreign skin in a support transcript.
        text = "display:\n  skin: omh-amber\n"
        change = ensure_omh_skin(text, SKIN_NAME)
        self.assertFalse(change.changed)
        self.assertEqual(change.text, text)
        self.assertIn("OMH theme omh-amber", change.message)

    def test_already_omh_is_unchanged(self) -> None:
        change = ensure_omh_skin("display:\n  skin: omh\n", SKIN_NAME)
        self.assertFalse(change.changed)

    def test_dotted_key_is_user_owned(self) -> None:
        change = ensure_omh_skin("display.skin: ares\n", SKIN_NAME)
        self.assertFalse(change.changed)

    def test_duplicate_display_sections_are_left_alone(self) -> None:
        change = ensure_omh_skin("display:\n  compact: true\ndisplay:\n  streaming: true\n", SKIN_NAME)
        self.assertFalse(change.changed)


if __name__ == "__main__":
    unittest.main()
