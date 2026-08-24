from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from omh.maintenance.doctor import run_doctor
from omh.maintenance.hermes_tui import (
    REQUIRED_WIDGET_SDK_KEYS,
    hermes_tui_preflight,
    tui_identity_verdict,
    widget_render_blockers,
)
from omh.paths import OmhPaths
from omh.tui_widget_pack import install_tui_widget


_MODERN_SDK_SOURCE = """
import { Box, Text } from '@hermes/ink'
export const widgetSdk = {
  Box,
  Text,
  defineWidgetApp,
  h: React.createElement,
  openWidget,
  updateWidget,
  useShimmerPhase
} as const
"""


def _make_paths(root: Path) -> OmhPaths:
    # macOS TemporaryDirectory lives under the /var -> /private/var symlink,
    # which install_tui_widget rejects by design; resolve before building paths.
    resolved = root.resolve()
    return OmhPaths(resolved / ".omh", resolved / ".hermes")


def _make_hermes_install(
    hermes_home: Path,
    *,
    version: str = "0.20.1",
    sdk_source: str | None = _MODERN_SDK_SOURCE,
) -> Path:
    install = hermes_home / "hermes-agent"
    (install / "hermes_cli").mkdir(parents=True)
    (install / "hermes_cli" / "__init__.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8"
    )
    if sdk_source is not None:
        loader = install / "ui-tui" / "src" / "sdk"
        loader.mkdir(parents=True)
        (loader / "userWidgets.ts").write_text(sdk_source, encoding="utf-8")
    return install


def _write_display_interface(hermes_home: Path, value: str) -> None:
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "config.yaml").write_text(
        f"display:\n  interface: {value}\n", encoding="utf-8"
    )


class HermesTuiPreflightTests(unittest.TestCase):
    def test_healthy_modern_install_reports_no_blockers(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _make_paths(Path(tmp))
            _make_hermes_install(paths.hermes_home)
            _write_display_interface(paths.hermes_home, "tui")
            install_tui_widget(paths.hermes_home)

            preflight = hermes_tui_preflight(paths)

            self.assertTrue(preflight["install"]["found"])
            self.assertEqual(preflight["install"]["version"], "0.20.1")
            self.assertEqual(preflight["widget_loader"]["marker"], "ui-tui-source")
            self.assertTrue(preflight["sdk_surface"]["parsed"])
            self.assertEqual(preflight["sdk_surface"]["missing"], [])
            self.assertEqual(preflight["display_interface"]["value"], "tui")
            self.assertTrue(preflight["widget"]["installed"])
            self.assertTrue(preflight["widget"]["managed"])
            self.assertEqual(preflight["widget"]["interpreter"], os.path.realpath(sys.executable))
            self.assertTrue(preflight["widget"]["interpreter_ok"])
            self.assertTrue(preflight["widget"]["themed_panel"])
            self.assertEqual(preflight["display_skin"], {"value": "default", "explicit": False})
            self.assertEqual(widget_render_blockers(preflight), [])

    def test_preflight_names_an_explicit_skin_and_a_stale_widget(self) -> None:
        # A widget from an older OMH loads fine, so it is a degraded look and
        # never a render blocker. Staleness is simulated by stripping the
        # current design's marker (the derived state label).
        with TemporaryDirectory() as tmp:
            paths = _make_paths(Path(tmp))
            _make_hermes_install(paths.hermes_home)
            (paths.hermes_home / "config.yaml").write_text(
                "display:\n  interface: tui\n  skin: ares\n", encoding="utf-8"
            )
            install_tui_widget(paths.hermes_home)
            widget = paths.hermes_home / "tui-widgets" / "omh-status.mjs"
            widget.write_text(
                widget.read_text(encoding="utf-8").replace("hudStateLabel", "oldStateLabel"),
                encoding="utf-8",
            )

            preflight = hermes_tui_preflight(paths)

            self.assertFalse(preflight["widget"]["themed_panel"])
            self.assertEqual(preflight["display_skin"], {"value": "ares", "explicit": True})
            self.assertEqual(widget_render_blockers(preflight), [])

    def test_preflight_rejects_the_retired_bordered_card_as_stale(self) -> None:
        # The bordered card was the interim design; its border marker now
        # identifies a widget that predates the text-line HUD.
        with TemporaryDirectory() as tmp:
            paths = _make_paths(Path(tmp))
            _make_hermes_install(paths.hermes_home)
            _write_display_interface(paths.hermes_home, "tui")
            install_tui_widget(paths.hermes_home)
            widget = paths.hermes_home / "tui-widgets" / "omh-status.mjs"
            widget.write_text(
                widget.read_text(encoding="utf-8").replace(
                    "{ flexDirection: 'column', width: '100%' }",
                    "{ borderStyle: 'round', flexDirection: 'column', width: '100%' }",
                    1,
                ),
                encoding="utf-8",
            )

            self.assertFalse(hermes_tui_preflight(paths)["widget"]["themed_panel"])

    def test_verdict_is_ready_on_a_healthy_modern_install(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _make_paths(Path(tmp))
            _make_hermes_install(paths.hermes_home)
            _write_display_interface(paths.hermes_home, "tui")
            install_tui_widget(paths.hermes_home)

            verdict = tui_identity_verdict(paths)

            self.assertEqual(verdict["status"], "ready")
            self.assertEqual(verdict["blockers"], [])
            self.assertEqual(verdict["next_commands"], [])

    def test_verdict_on_old_hermes_is_blocked_with_hermes_update_command(self) -> None:
        # The reported journey: setup/update succeed, the terminal still shows
        # stock Hermes. The verdict must name `hermes update` as the fix, not
        # bury it mid-stream.
        with TemporaryDirectory() as tmp:
            paths = _make_paths(Path(tmp))
            _make_hermes_install(paths.hermes_home, version="0.8.0", sdk_source=None)
            _write_display_interface(paths.hermes_home, "tui")
            install_tui_widget(paths.hermes_home)

            verdict = tui_identity_verdict(paths)

            self.assertEqual(verdict["status"], "blocked")
            self.assertEqual(verdict["next_commands"], ["hermes update"])
            self.assertEqual(verdict["hermes_version"], "0.8.0")

    def test_verdict_without_a_hermes_install_is_unknown(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _make_paths(Path(tmp))
            verdict = tui_identity_verdict(paths)
            self.assertEqual(verdict["status"], "unknown")
            self.assertEqual(verdict["next_commands"], [])

    def test_verdict_notes_an_explicit_foreign_skin_as_user_choice(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _make_paths(Path(tmp))
            _make_hermes_install(paths.hermes_home)
            paths.hermes_home.mkdir(parents=True, exist_ok=True)
            (paths.hermes_home / "config.yaml").write_text(
                "display:\n  interface: tui\n  skin: midnight\n", encoding="utf-8"
            )
            install_tui_widget(paths.hermes_home)

            verdict = tui_identity_verdict(paths)

            self.assertEqual(verdict["status"], "ready")
            self.assertTrue(any("midnight" in note for note in verdict["notes"]))

    def test_old_hermes_without_widget_loader_names_hermes_update(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _make_paths(Path(tmp))
            _make_hermes_install(paths.hermes_home, version="0.8.0", sdk_source=None)
            _write_display_interface(paths.hermes_home, "tui")
            install_tui_widget(paths.hermes_home)

            preflight = hermes_tui_preflight(paths)
            blockers = widget_render_blockers(preflight)

            self.assertFalse(preflight["widget_loader"]["present"])
            self.assertFalse(preflight["sdk_surface"]["checked"])
            self.assertEqual(len(blockers), 1)
            self.assertIn("0.8.0", blockers[0])
            self.assertIn("hermes update", blockers[0])

    def test_stripped_sdk_surface_reports_each_missing_key(self) -> None:
        # useShimmerPhase is stripped too, but its absence is NOT a finding:
        # the widget no longer subscribes to the shimmer clock (animation
        # repaints cleared drag-selections over the dock), so only the keys
        # the widget actually destructures may block.
        stripped = _MODERN_SDK_SOURCE.replace("  useShimmerPhase\n", "").replace(
            "  updateWidget,\n", ""
        )
        with TemporaryDirectory() as tmp:
            paths = _make_paths(Path(tmp))
            _make_hermes_install(paths.hermes_home, sdk_source=stripped)
            _write_display_interface(paths.hermes_home, "tui")
            install_tui_widget(paths.hermes_home)

            preflight = hermes_tui_preflight(paths)
            blockers = widget_render_blockers(preflight)

            self.assertEqual(preflight["sdk_surface"]["missing"], ["updateWidget"])
            self.assertEqual(len(blockers), 1)
            self.assertIn("updateWidget", blockers[0])
            self.assertNotIn("useShimmerPhase", blockers[0])

    def test_unset_display_interface_is_a_blocker_and_explicit_cli_is_user_owned(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _make_paths(Path(tmp))
            _make_hermes_install(paths.hermes_home)
            paths.hermes_home.mkdir(parents=True, exist_ok=True)
            (paths.hermes_home / "config.yaml").write_text("model: something\n", encoding="utf-8")
            install_tui_widget(paths.hermes_home)

            unset = hermes_tui_preflight(paths)
            self.assertFalse(unset["display_interface"]["explicit"])
            self.assertTrue(unset["display_interface"]["settable"])
            unset_blockers = widget_render_blockers(unset)
            self.assertEqual(len(unset_blockers), 1)
            self.assertIn("omh setup", unset_blockers[0])
            self.assertIn("hermes --tui", unset_blockers[0])
            self.assertNotIn("styled TUI with `omh`", unset_blockers[0])

            _write_display_interface(paths.hermes_home, "cli")
            explicit = hermes_tui_preflight(paths)
            blockers = widget_render_blockers(explicit)
            self.assertTrue(explicit["display_interface"]["explicit"])
            self.assertEqual(len(blockers), 1)
            self.assertIn("hermes --tui", blockers[0])

    def test_stale_widget_interpreter_is_a_blocker(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _make_paths(Path(tmp))
            _make_hermes_install(paths.hermes_home)
            _write_display_interface(paths.hermes_home, "tui")
            install_tui_widget(paths.hermes_home)
            widget = paths.hermes_home / "tui-widgets" / "omh-status.mjs"
            gone = str(Path(tmp).resolve() / "missing-python")
            # The installer embeds the path via json.dumps, which escapes
            # Windows backslashes — replace the encoded form, not the raw one.
            widget.write_text(
                widget.read_text(encoding="utf-8").replace(
                    json.dumps(os.path.realpath(sys.executable)), json.dumps(gone)
                ),
                encoding="utf-8",
            )

            preflight = hermes_tui_preflight(paths)
            blockers = widget_render_blockers(preflight)

            self.assertEqual(preflight["widget"]["interpreter"], gone)
            self.assertFalse(preflight["widget"]["interpreter_ok"])
            self.assertEqual(len(blockers), 1)
            self.assertIn("omh setup", blockers[0])

    def test_missing_install_reports_single_unknowable_blocker(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _make_paths(Path(tmp))
            preflight = hermes_tui_preflight(paths)
            blockers = widget_render_blockers(preflight)
            self.assertFalse(preflight["install"]["found"])
            self.assertEqual(len(blockers), 1)
            self.assertIn("unknowable", blockers[0])

    def test_required_sdk_keys_match_the_installed_widget_destructure(self) -> None:
        from importlib import resources

        widget_source = (
            resources.files("omh.tui_widgets").joinpath("omh-status.mjs").read_text(encoding="utf-8")
        )
        destructure = widget_source.split("= sdk", 1)[0]
        for key in REQUIRED_WIDGET_SDK_KEYS:
            self.assertIn(key, destructure, f"widget no longer destructures {key}")


    def test_noncanonical_display_config_is_not_reported_as_a_blocker(self) -> None:
        # ensure_tui_interface refuses to touch an inline user-owned display
        # block, so calling it "unset — run omh setup" would loop forever.
        with TemporaryDirectory() as tmp:
            paths = _make_paths(Path(tmp))
            _make_hermes_install(paths.hermes_home)
            paths.hermes_home.mkdir(parents=True, exist_ok=True)
            (paths.hermes_home / "config.yaml").write_text(
                "display: {interface: tui}\n", encoding="utf-8"
            )
            install_tui_widget(paths.hermes_home)

            preflight = hermes_tui_preflight(paths)

            self.assertFalse(preflight["display_interface"]["explicit"])
            self.assertFalse(preflight["display_interface"]["settable"])
            self.assertEqual(
                [b for b in widget_render_blockers(preflight) if "omh setup" in b], []
            )

    def test_unrecognizable_sdk_export_reports_unparsed_not_all_missing(self) -> None:
        # Without the closing marker the block would be the whole file and a
        # stripped SDK could never be detected; unparseable must say so.
        reshaped = _MODERN_SDK_SOURCE.replace("} as const", "} satisfies WidgetSdk")
        with TemporaryDirectory() as tmp:
            paths = _make_paths(Path(tmp))
            _make_hermes_install(paths.hermes_home, sdk_source=reshaped)
            _write_display_interface(paths.hermes_home, "tui")
            install_tui_widget(paths.hermes_home)

            preflight = hermes_tui_preflight(paths)
            blockers = widget_render_blockers(preflight)

            self.assertTrue(preflight["sdk_surface"]["checked"])
            self.assertFalse(preflight["sdk_surface"]["parsed"])
            self.assertEqual(preflight["sdk_surface"]["missing"], [])
            self.assertEqual(len(blockers), 1)
            self.assertIn("cannot be verified", blockers[0])


class DoctorHermesTuiChecksTests(unittest.TestCase):
    def _checks_by_name(self, paths: OmhPaths) -> dict[str, object]:
        return {check.name: check for check in run_doctor(paths)}

    def test_doctor_reports_all_five_checks_ok_on_modern_install(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _make_paths(Path(tmp))
            _make_hermes_install(paths.hermes_home)
            _write_display_interface(paths.hermes_home, "tui")
            install_tui_widget(paths.hermes_home)

            checks = self._checks_by_name(paths)

            for name in (
                "hermes_tui_support",
                "hermes_tui_sdk_surface",
                "hermes_tui_interface_default",
                "hermes_tui_widget_state",
                "hermes_tui_widget_chrome",
            ):
                self.assertIn(name, checks)
                self.assertTrue(checks[name].ok, name)
                self.assertEqual(checks[name].severity, "ok", name)

    def test_doctor_warns_on_a_stale_widget_without_flipping_the_exit_code(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _make_paths(Path(tmp))
            _make_hermes_install(paths.hermes_home)
            _write_display_interface(paths.hermes_home, "tui")
            install_tui_widget(paths.hermes_home)
            widget = paths.hermes_home / "tui-widgets" / "omh-status.mjs"
            widget.write_text(
                widget.read_text(encoding="utf-8").replace("hudStateLabel", "oldStateLabel"),
                encoding="utf-8",
            )

            checks = self._checks_by_name(paths)
            chrome = checks["hermes_tui_widget_chrome"]

            self.assertTrue(chrome.ok)
            self.assertEqual(chrome.severity, "warning")
            self.assertIn("predates the current text HUD", chrome.message)
            self.assertIn("omh setup", chrome.next_action)
            # The widget itself is still installed and loadable; only its look
            # is stale, so the sibling state check stays clean.
            self.assertEqual(checks["hermes_tui_widget_state"].severity, "ok")

    def test_doctor_omits_the_chrome_check_when_no_widget_is_installed(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _make_paths(Path(tmp))
            _make_hermes_install(paths.hermes_home)
            _write_display_interface(paths.hermes_home, "tui")

            checks = self._checks_by_name(paths)

            self.assertNotIn("hermes_tui_widget_chrome", checks)
            self.assertEqual(checks["hermes_tui_widget_state"].severity, "warning")

    def test_doctor_warns_with_hermes_update_next_action_on_old_hermes(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _make_paths(Path(tmp))
            _make_hermes_install(paths.hermes_home, version="0.8.0", sdk_source=None)
            _write_display_interface(paths.hermes_home, "tui")

            checks = self._checks_by_name(paths)
            support = checks["hermes_tui_support"]

            # Degraded optional surfaces never flip the doctor exit code:
            # ok stays True, the warning severity and next action carry it.
            self.assertTrue(support.ok)
            self.assertEqual(support.severity, "warning")
            self.assertIn("hermes update", support.next_action)
            self.assertNotIn("hermes_tui_sdk_surface", checks)

    def test_doctor_skips_quietly_when_hermes_install_is_absent(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _make_paths(Path(tmp))
            checks = self._checks_by_name(paths)
            support = checks["hermes_tui_support"]
            self.assertTrue(support.ok)
            self.assertFalse(support.observed)


if __name__ == "__main__":
    unittest.main()
