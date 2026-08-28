from __future__ import annotations

import unittest
from unittest import mock

from _local_package import load_local_package

load_local_package()

from omh.commands import setup as _setup_module  # noqa: E402
from omh.commands.language import LANGUAGE_CODES, MESSAGES, tr  # noqa: E402
from omh.maintenance.doctor import Check, _structural_search_check, doctor_ok  # noqa: E402
from omh.maintenance.probe import _structural_search_capability  # noqa: E402
from omh.maintenance.structural_search import (  # noqa: E402
    STRUCTURAL_SEARCH_CLAIM_BOUNDARY,
    inspect_structural_search,
)

_FAKE_PATH = "/opt/fake/bin/ast-grep"
_PACKAGE_MANAGER_TOKENS = ("brew", "cargo", "npm", "apt", "install")


def _which_present(name: str) -> str | None:
    return _FAKE_PATH if name == "ast-grep" else None


def _which_absent(name: str) -> str | None:
    return None


class StructuralSearchDetectionTests(unittest.TestCase):
    def test_present_branch(self) -> None:
        inspection = inspect_structural_search(which=_which_present)
        self.assertEqual(inspection["tool"], "ast-grep")
        self.assertTrue(inspection["found"])
        self.assertEqual(inspection["path"], _FAKE_PATH)
        self.assertTrue(inspection["observed"])
        self.assertTrue(inspection["read_only"])
        self.assertEqual(inspection["reason_code"], "ast_grep_present_not_executed")
        self.assertEqual(inspection["claim_boundary"], STRUCTURAL_SEARCH_CLAIM_BOUNDARY)
        self.assertIn("does not execute", STRUCTURAL_SEARCH_CLAIM_BOUNDARY)

        check = _structural_search_check(which=_which_present)
        self.assertEqual(check.name, "structural_search_tooling")
        self.assertTrue(check.ok)
        self.assertEqual(check.severity, "ok")
        self.assertIn(_FAKE_PATH, check.message)
        self.assertIn("not executed", check.message)
        self.assertEqual(check.next_action, "")

        capability = _structural_search_capability(which=_which_present)
        self.assertEqual(capability.name, "structural_code_search")
        self.assertEqual(capability.status, "unverified")
        self.assertEqual(capability.evidence, "ast_grep_present_not_executed")

    def test_absent_branch_is_not_a_warning(self) -> None:
        inspection = inspect_structural_search(which=_which_absent)
        self.assertFalse(inspection["found"])
        self.assertIsNone(inspection["path"])
        self.assertEqual(inspection["reason_code"], "ast_grep_missing")

        check = _structural_search_check(which=_which_absent)
        self.assertTrue(check.ok)
        self.assertEqual(check.severity, "ok")
        self.assertEqual(check.next_action, "")
        self.assertEqual(check.remediation, "")
        self.assertIn("grep/ripgrep", check.message)
        for token in _PACKAGE_MANAGER_TOKENS:
            self.assertNotIn(token, check.message.lower())
            self.assertNotIn(token, check.next_action.lower())

        capability = _structural_search_capability(which=_which_absent)
        self.assertEqual(capability.status, "missing")
        self.assertEqual(capability.evidence, "ast_grep_missing")

    def test_absence_does_not_move_warnings_or_flip_the_optional_group(self) -> None:
        baseline = [
            Check("command_path", True, "omh resolved"),
            Check("team_profile_packs", True, "optional OMH team profile packs are not installed"),
        ]
        without_structural = _setup_module._doctor_operator_summary(list(baseline))
        with_structural = _setup_module._doctor_operator_summary(
            [*baseline, _structural_search_check(which=_which_absent)]
        )

        self.assertEqual(with_structural["warnings"], without_structural["warnings"])
        self.assertTrue(doctor_ok([*baseline, _structural_search_check(which=_which_absent)]))
        optional = next(
            group for group in with_structural["groups"] if group["name"] == "optional_surfaces"
        )
        self.assertNotEqual(optional["status"], "warning")
        self.assertEqual(optional["status"], "ok")

    def test_check_groups_into_optional_surfaces_and_nothing_else(self) -> None:
        summary = _setup_module._doctor_operator_summary([_structural_search_check(which=_which_absent)])
        groups = {group["name"]: group for group in summary["groups"]}
        self.assertEqual(groups["optional_surfaces"]["total"], 1)
        self.assertEqual(groups["optional_surfaces"]["passing"], 1)
        for name, group in groups.items():
            if name == "optional_surfaces":
                continue
            self.assertEqual(group["total"], 0, name)

    def test_no_execution(self) -> None:
        def _forbid(*args: object, **kwargs: object) -> None:
            raise AssertionError("structural search detection must never execute a binary")

        with (
            mock.patch("subprocess.Popen", side_effect=_forbid),
            mock.patch("os.system", side_effect=_forbid),
            mock.patch("os.execv", side_effect=_forbid),
            mock.patch("os.execvp", side_effect=_forbid),
        ):
            inspect_structural_search()
            inspect_structural_search(which=_which_present)
            inspect_structural_search(which=_which_absent)
            _structural_search_check()
            _structural_search_check(which=_which_present)
            _structural_search_capability()
            _structural_search_capability(which=_which_absent)

    def _boundary_lines(self, checks: list[dict[str, object]]) -> list[str]:
        with mock.patch.object(_setup_module, "tr", side_effect=lambda language, key: key):
            return _setup_module._doctor_observation_boundary_lines(checks, language="en")

    def test_boundary_line_present_branch(self) -> None:
        # Serialize the real Check through the doctor path: the branch is
        # message-substring-coupled, so a hand-written fixture dict would not
        # prove the shipped message and the printer agree.
        check = _structural_search_check(which=_which_present)
        lines = self._boundary_lines([dict(check.__dict__)])
        self.assertEqual(lines, ["doctor_structural_search_present"])

    def test_boundary_line_absent_branch(self) -> None:
        check = _structural_search_check(which=_which_absent)
        lines = self._boundary_lines([dict(check.__dict__)])
        self.assertEqual(lines, ["doctor_structural_search_absent"])

    def test_plugin_only_fixture_output_is_unchanged(self) -> None:
        # The exact-equality contract from tests/test_plugin_distribution.py:
        # the new block must not fire when its check is absent.
        checks = [
            {"name": "plugin_register_smoke", "ok": True},
            {
                "name": "plugin_loader_observed",
                "ok": True,
                "observed": True,
                "severity": "ok",
            },
        ]
        lines = self._boundary_lines(checks)
        self.assertEqual(lines, ["doctor_plugin_bridge_ready", "doctor_plugin_loader_observed"])

    def test_locales_carry_both_boundary_keys(self) -> None:
        for code in LANGUAGE_CODES:
            for key in ("doctor_structural_search_present", "doctor_structural_search_absent"):
                with self.subTest(locale=code, key=key):
                    self.assertIn(key, MESSAGES[code])
                    self.assertTrue(tr(code, key).strip())


if __name__ == "__main__":
    unittest.main()
