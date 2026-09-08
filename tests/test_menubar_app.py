from __future__ import annotations

import base64
import json
import platform
import plistlib
import shutil
import struct
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from _cli_harness import run_cli
from _local_package import load_local_package

load_local_package()
import omh.menubar_app as menubar_app_module
from omh.menubar_app import MENUBAR_APP_SCHEMA_VERSION, setup_menubar_app
from omh.paths import resolve_paths


class MenubarAppTests(unittest.TestCase):
    def test_embedded_character_icon_is_a_non_empty_36_pixel_png(self) -> None:
        icon_bytes = base64.b64decode(menubar_app_module.MENUBAR_ICON_BASE64, validate=True)

        self.assertGreater(len(icon_bytes), 24)
        self.assertEqual(icon_bytes[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(icon_bytes[12:16], b"IHDR")
        self.assertEqual(struct.unpack(">II", icon_bytes[16:24]), (36, 36))

    def test_install_materializes_exact_icon_bytes_and_passes_icon_path(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            expected_icon = base64.b64decode(menubar_app_module.MENUBAR_ICON_BASE64, validate=True)

            with (
                patch.object(menubar_app_module.Path, "home", return_value=root),
                patch(
                    "omh.menubar_app.shutil.which",
                    side_effect=lambda name: {
                        "swiftc": "/usr/bin/swiftc",
                        "node": "/Users/example/.nvm/versions/node/v24/bin/node",
                    }.get(name),
                ),
                patch(
                    "sys.executable",
                    "/Users/example/.pyenv/versions/3.12/bin/python3",
                ),
                patch("omh.menubar_app._compile_swift_helper"),
            ):
                payload = setup_menubar_app(
                    paths,
                    platform_name="Darwin",
                    start=False,
                    command_path="/Users/example/.npm-global/bin/omh",
                )
                managed = menubar_app_module.is_managed_menubar_install(paths)

            icon_path = paths.omh_home / "menubar" / "omh-character-mask.png"
            self.assertEqual(payload["icon"], str(icon_path))
            self.assertEqual(icon_path.read_bytes(), expected_icon)
            self.assertGreater(len(icon_path.read_bytes()), 0)
            launch_agent = root / "Library" / "LaunchAgents" / "com.rlaope.omh.menubar.plist"
            launch_agent_payload = plistlib.loads(launch_agent.read_bytes())
            arguments = launch_agent_payload["ProgramArguments"]
            icon_argument = arguments.index("--icon")
            self.assertEqual(arguments[icon_argument + 1], str(icon_path))
            self.assertTrue(managed)
            launch_path = launch_agent_payload["EnvironmentVariables"]["PATH"].split(":")
            self.assertEqual(
                launch_path[:3],
                [
                    "/Users/example/.nvm/versions/node/v24/bin",
                    "/Users/example/.pyenv/versions/3.12/bin",
                    "/Users/example/.npm-global/bin",
                ],
            )
            self.assertIn("/usr/local/bin", launch_path)
            self.assertIn("/opt/homebrew/bin", launch_path)
            self.assertIn("/usr/bin", launch_path)
            self.assertIn("/bin", launch_path)
            self.assertIn("/usr/sbin", launch_path)
            self.assertIn("/sbin", launch_path)

    def test_valid_launch_agent_repairs_a_missing_managed_copy(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            with patch.object(menubar_app_module.Path, "home", return_value=root):
                app_paths = menubar_app_module.menubar_app_paths(paths)
                app_paths["launch_agent"].parent.mkdir(parents=True)
                app_paths["launch_agent"].write_bytes(
                    plistlib.dumps(
                        {
                            "Label": menubar_app_module.MENUBAR_LABEL,
                            "ProgramArguments": [
                                str(app_paths["executable"]),
                                "--omh-command",
                                "/usr/local/bin/omh",
                                "--omh-home",
                                str(paths.omh_home),
                                "--hermes-home",
                                str(paths.hermes_home),
                            ],
                        }
                    )
                )

                managed = menubar_app_module.is_managed_menubar_install(paths)

        self.assertTrue(managed)

    def test_operator_owned_directory_without_launch_agent_is_not_managed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            with patch.object(menubar_app_module.Path, "home", return_value=root):
                app_paths = menubar_app_module.menubar_app_paths(paths)
                app_paths["app_dir"].mkdir(parents=True)
                (app_paths["app_dir"] / "README.txt").write_text("operator owned", encoding="utf-8")

                managed = menubar_app_module.is_managed_menubar_install(paths)

        self.assertFalse(managed)

    @unittest.skipIf(sys.platform == "win32", "symlink privileges vary on Windows")
    def test_symlinked_managed_paths_are_not_trusted(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            with patch.object(menubar_app_module.Path, "home", return_value=root):
                app_paths = menubar_app_module.menubar_app_paths(paths)
                paths.omh_home.mkdir(parents=True)
                target_dir = root / "attacker-owned"
                target_dir.mkdir()
                app_paths["app_dir"].symlink_to(target_dir, target_is_directory=True)
                app_paths["launch_agent"].parent.mkdir(parents=True)
                app_paths["launch_agent"].write_bytes(
                    plistlib.dumps(
                        {
                            "Label": menubar_app_module.MENUBAR_LABEL,
                            "ProgramArguments": [
                                str(app_paths["executable"]),
                                "--omh-command",
                                "/usr/local/bin/omh",
                                "--omh-home",
                                str(paths.omh_home),
                                "--hermes-home",
                                str(paths.hermes_home),
                            ],
                        }
                    )
                )

                managed = menubar_app_module.is_managed_menubar_install(paths)

        self.assertFalse(managed)

    @unittest.skipIf(sys.platform == "win32", "symlink privileges vary on Windows")
    def test_broken_launch_agent_symlink_is_not_managed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            with patch.object(menubar_app_module.Path, "home", return_value=root):
                app_paths = menubar_app_module.menubar_app_paths(paths)
                app_paths["launch_agent"].parent.mkdir(parents=True)
                app_paths["launch_agent"].symlink_to(root / "missing.plist")

                managed = menubar_app_module.is_managed_menubar_install(paths)

        self.assertFalse(managed)

    def test_global_launch_agent_must_match_target_homes(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            with patch.object(menubar_app_module.Path, "home", return_value=root):
                app_paths = menubar_app_module.menubar_app_paths(paths)
                app_paths["app_dir"].mkdir(parents=True)
                app_paths["executable"].touch()
                app_paths["launch_agent"].parent.mkdir(parents=True)
                app_paths["launch_agent"].write_bytes(
                    plistlib.dumps(
                        {
                            "Label": menubar_app_module.MENUBAR_LABEL,
                            "ProgramArguments": [
                                str(app_paths["executable"]),
                                "--omh-command",
                                "/usr/local/bin/omh",
                                "--omh-home",
                                str(paths.omh_home),
                                "--hermes-home",
                                str(root / "another-hermes-home"),
                            ],
                        }
                    )
                )

                managed = menubar_app_module.is_managed_menubar_install(paths)

        self.assertFalse(managed)

    @unittest.skipUnless(platform.system() == "Darwin", "symlink resolution targets launchd")
    def test_launch_agent_path_resolves_node_shim_and_deduplicates_real_bin(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_bin = root / "runtime" / "bin"
            shim_bin = root / "shims"
            real_bin.mkdir(parents=True)
            shim_bin.mkdir()
            real_node = real_bin / "node"
            real_node.write_text("", encoding="utf-8")
            shim_node = shim_bin / "node"
            shim_node.symlink_to(real_node)

            with (
                patch("omh.menubar_app.shutil.which", return_value=str(shim_node)),
                patch("sys.executable", str(real_bin / "python3")),
            ):
                launch_path = menubar_app_module._launch_agent_path(str(real_bin / "omh"))

        path_entries = launch_path.split(":")
        self.assertEqual(path_entries.count(str(real_bin.resolve())), 1)
        self.assertNotIn(str(shim_bin), path_entries)

    def test_setup_menubar_app_skips_unsupported_platform(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")

            payload = setup_menubar_app(paths, platform_name="Linux", dry_run=True)

            self.assertEqual(payload["schema_version"], MENUBAR_APP_SCHEMA_VERSION)
            self.assertEqual(payload["status"], "skipped")
            self.assertFalse(payload["supported"])
            self.assertFalse((root / ".omh" / "menubar").exists())

    def test_setup_menubar_app_darwin_dry_run_reports_install_plan(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")

            with patch("omh.menubar_app.shutil.which", return_value="/usr/bin/swiftc"):
                payload = setup_menubar_app(
                    paths,
                    platform_name="Darwin",
                    dry_run=True,
                    command_path="/usr/local/bin/omh",
                )

            self.assertEqual(payload["schema_version"], MENUBAR_APP_SCHEMA_VERSION)
            self.assertEqual(payload["status"], "dry_run")
            self.assertTrue(payload["supported"])
            self.assertFalse(payload["installed"])
            self.assertEqual(payload["swiftc"], "/usr/bin/swiftc")
            self.assertEqual(payload["omh_command"], "/usr/local/bin/omh")
            self.assertFalse((root / ".omh" / "menubar").exists())

    def test_menubar_install_cli_dry_run_uses_contract_payload(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch("omh.menubar_app.platform.system", return_value="Darwin"),
                patch("omh.menubar_app.shutil.which", return_value="/usr/bin/swiftc"),
                patch("omh.menubar_app._resolved_omh_command", return_value="/usr/local/bin/omh"),
            ):
                status, stdout, stderr = run_cli(
                    [
                        "--omh-home",
                        str(root / ".omh"),
                        "--hermes-home",
                        str(root / ".hermes"),
                        "menubar",
                        "install",
                        "--dry-run",
                    ]
                )

            self.assertEqual(stderr, "")
            self.assertEqual(status, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["schema_version"], MENUBAR_APP_SCHEMA_VERSION)
            self.assertEqual(payload["status"], "dry_run")
            self.assertEqual(payload["omh_command"], "/usr/local/bin/omh")
            self.assertFalse((root / ".omh" / "menubar").exists())

    def test_native_helper_requests_json_status_payload(self) -> None:
        self.assertIn(
            '["menubar", "status", "--observe-local-processes", "--json"]',
            menubar_app_module._SWIFT_SOURCE,
        )

    def test_native_helper_renders_v2_table_rows_and_count_title(self) -> None:
        source = menubar_app_module._SWIFT_SOURCE

        self.assertIn('== "table_row"', source)
        self.assertIn('fixedWidth((row["left"] as? String) ?? "", 18)', source)
        self.assertIn('(row["right"] as? String) ?? ""', source)
        self.assertIn('rows.prefix(6)', source)
        self.assertIn('display?["menu_bar_title"]', source)
        self.assertNotIn('agent_status', source)

    def test_native_helper_refreshes_off_the_main_thread_and_updates_the_open_menu(self) -> None:
        # The owner's report: the menu looked stuck on its first reading.
        # Three causes, each pinned here. The status subprocess ran on the
        # main thread (a ~0.5s freeze every tick); the timer lived in the
        # default run-loop mode, silent for as long as the menu stayed open;
        # and every refresh replaced `statusItem.menu`, which AppKit only
        # shows on the NEXT open. One long-lived NSMenu mutated in place,
        # a `.common`-mode timer, a background queue, and a refresh on
        # `menuWillOpen` are the fix; the footer stamps the reading's time.
        source = menubar_app_module._SWIFT_SOURCE

        self.assertIn("NSMenuDelegate", source)
        self.assertIn("func menuWillOpen(_ menu: NSMenu)", source)
        self.assertIn("RunLoop.main.add(timer, forMode: .common)", source)
        self.assertIn("refreshQueue.async { [weak self] in", source)
        self.assertIn("DispatchQueue.main.async {", source)
        self.assertIn("menu.removeAllItems()", source)
        self.assertEqual(source.count("statusItem.menu = menu"), 1)
        self.assertNotIn("let menu = NSMenu()", source.split("private let menu = NSMenu()", 1)[1])
        self.assertIn('let stamp = "Updated \\(clock.string(from: refreshedAt))"', source)
        # A single failed read keeps the last good cards instead of flashing
        # the attention mark; the mark returns once the failure persists.
        self.assertIn("if lastPayload != nil && consecutiveFailures < 3 {", source)
        # Rows are information, not commands: enabled so they render in the
        # normal text color rather than the disabled gray that reads as
        # "still loading".
        self.assertIn("menu.autoenablesItems = false", source)
        self.assertIn("item.isEnabled = true", source)
        self.assertNotIn("item.isEnabled = false", source)
        # Drain stdout before waiting so a large payload cannot deadlock.
        self.assertLess(source.index("readDataToEndOfFile()"), source.index("process.waitUntilExit()"))

    def test_native_helper_loads_template_icon_at_18_points_with_accessible_label(self) -> None:
        source = menubar_app_module._SWIFT_SOURCE

        self.assertIn('case "--icon":', source)
        self.assertIn('NSImage(contentsOfFile: iconPath)', source)
        self.assertIn('image.size = NSSize(width: 18, height: 18)', source)
        self.assertIn('image.isTemplate = true', source)
        self.assertIn('button.imagePosition = .imageLeading', source)
        self.assertIn('button.setAccessibilityLabel(', source)
        self.assertIn(r'"OMH — \(headline) — \(summary)"', source)
        self.assertNotIn('statusItem.button?.title = "omh !"', source)
        self.assertNotIn(r'? "\(title) \(mark)" : menuBarTitle', source)

    def test_native_helper_keeps_sessions_table_header_visible(self) -> None:
        source = menubar_app_module._SWIFT_SOURCE

        self.assertIn('let line = tableHeaderTitle(columns)', source)
        self.assertIn('return fixedWidth(value, 18)', source)
        self.assertNotIn('fixedWidth(value, 12)', source)

    def test_native_helper_bounds_table_values_without_truncating_tooltips(self) -> None:
        source = menubar_app_module._SWIFT_SOURCE

        self.assertIn('fixedWidth((row["right"] as? String) ?? "", 24)', source)
        self.assertIn('item.toolTip = rowToolTip(row)', source)
        self.assertIn('return fixedWidth(value, 24)', source)
        tooltip_start = source.index("    private func rowToolTip")
        tooltip_end = source.index("\n    private func menuCards", tooltip_start)
        tooltip_source = source[tooltip_start:tooltip_end]
        self.assertIn('let right = (row["right"] as? String) ?? ""', tooltip_source)
        self.assertIn('return "\\(left): \\(right)"', tooltip_source)
        self.assertNotIn("fixedWidth", tooltip_source)

    def test_native_helper_fixed_width_truncates_a_long_value_to_24_characters(self) -> None:
        swiftc = shutil.which("swiftc")
        if swiftc is None:
            self.skipTest("Native fixed-width behavior coverage requires swiftc")

        source = menubar_app_module._SWIFT_SOURCE
        function_start = source.index("    private func fixedWidth")
        function_end = source.index("\n    private func rowTitle", function_start)
        fixed_width_source = source[function_start:function_end].replace(
            "    private func fixedWidth",
            "func fixedWidth",
            1,
        )
        long_value = "abcdefghijklmnopqrstuvwxyz0123456789"
        expected = long_value[:23] + "…"

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            harness = root / "main.swift"
            executable = root / "fixed-width-test"
            harness.write_text(
                f'import Foundation\n{fixed_width_source}\nprint(fixedWidth("{long_value}", 24))\n',
                encoding="utf-8",
            )
            compile_result = subprocess.run(
                [swiftc, str(harness), "-o", str(executable)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr or compile_result.stdout)
            run_result = subprocess.run(
                [str(executable)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
            )

        self.assertEqual(run_result.returncode, 0, run_result.stderr or run_result.stdout)
        self.assertEqual(run_result.stdout.rstrip("\n"), expected)
        self.assertEqual(len(expected), 24)

    def test_native_helper_swift_source_compiles_on_darwin(self) -> None:
        swiftc = shutil.which("swiftc")
        if platform.system() != "Darwin" or swiftc is None:
            self.skipTest("Swift/AppKit compile coverage requires swiftc on Darwin")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "OMHMenuBar.swift"
            executable = root / "omh-menubar"
            source.write_text(menubar_app_module._SWIFT_SOURCE, encoding="utf-8")
            result = subprocess.run(
                [swiftc, "-framework", "AppKit", str(source), "-o", str(executable)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_menubar_start_defaults_to_human_readable_output(self) -> None:
        payload = {
            "schema_version": MENUBAR_APP_SCHEMA_VERSION,
            "operation": "start",
            "platform": "Darwin",
            "label": "com.rlaope.omh.menubar",
            "launch_agent": "/tmp/com.rlaope.omh.menubar.plist",
            "started": True,
            "status": "running",
            "message": "OMH menu bar helper started.",
        }
        with patch("omh.commands.menubar.start_menubar_app", return_value=payload):
            status, stdout, stderr = run_cli(["menubar", "start"], output_json=False)

        self.assertEqual(stderr, "")
        self.assertEqual(status, 0)
        self.assertIn("OMH menu bar start", stdout)
        self.assertIn("Status: running", stdout)
        self.assertIn("Result: helper started", stdout)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(stdout)

        with patch("omh.commands.menubar.start_menubar_app", return_value=payload):
            status, stdout, stderr = run_cli(["menubar", "start", "--json"], output_json=False)

        self.assertEqual(stderr, "")
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(stdout)["schema_version"], MENUBAR_APP_SCHEMA_VERSION)

    def test_menubar_stop_failure_is_readable_without_json(self) -> None:
        payload = {
            "schema_version": MENUBAR_APP_SCHEMA_VERSION,
            "operation": "stop",
            "platform": "Darwin",
            "label": "com.rlaope.omh.menubar",
            "launch_agent": "/tmp/com.rlaope.omh.menubar.plist",
            "stopped": False,
            "status": "failed",
            "message": "Boot-out failed: 5: Input/output error\nTry re-running the command as root for richer errors.",
        }
        with patch("omh.commands.menubar.stop_menubar_app", return_value=payload):
            status, stdout, stderr = run_cli(["menubar", "stop"], output_json=False)

        self.assertEqual(stderr, "")
        self.assertEqual(status, 0)
        self.assertIn("OMH menu bar stop", stdout)
        self.assertIn("Status: failed", stdout)
        self.assertIn("Boot-out failed: 5: Input/output error", stdout)
        self.assertIn("Run `omh menubar status`", stdout)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(stdout)

    def test_custom_path_uninstall_does_not_touch_user_launch_agent(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            omh_home = root / ".omh"
            hermes_home = root / ".hermes"
            self.assertEqual(run_cli(["--omh-home", str(omh_home), "--hermes-home", str(hermes_home), "setup"])[0], 0)

            with patch(
                "omh.commands.setup.uninstall_menubar_app",
                side_effect=AssertionError("custom path uninstall must not touch the user LaunchAgent"),
            ):
                status, stdout, stderr = run_cli(
                    ["--omh-home", str(omh_home), "--hermes-home", str(hermes_home), "uninstall"]
                )

            self.assertEqual(stderr, "")
            self.assertEqual(status, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["menubar_app"]["status"], "not_requested")


if __name__ == "__main__":
    unittest.main()
