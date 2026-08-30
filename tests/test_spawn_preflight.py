"""Positive/negative coverage for backlog G's shared spawn preflight checks.

Each pure check gets one passing case and one failing case, and the failing
case always names a remedy. `run_spawn_preflight` gets its own contract test
for the combined verdict shape. Boundary integration (spawn never attempted on
a failed preflight) lives in `tests/test_hermes_child_dispatch.py`, next to
the boundary it protects.
"""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _local_package import load_local_package

load_local_package()

from omh.coding.spawn_preflight import (  # noqa: E402
    CHECK_CREDENTIAL_FILE_PRESENCE,
    CHECK_ENV_PREREQUISITE,
    CHECK_EXECUTABLE_PRESENCE,
    CHECK_WORKING_DIRECTORY,
    SPAWN_PREFLIGHT_SCHEMA_VERSION,
    check_credential_file_present,
    check_env_any_present,
    check_executable_present,
    check_working_directory,
    run_spawn_preflight,
)


class ExecutablePresenceTests(unittest.TestCase):
    def test_absolute_path_that_exists_and_is_executable_passes(self) -> None:
        with TemporaryDirectory() as tmp:
            script = Path(tmp) / "tool"
            script.write_text("#!/bin/sh\n", encoding="utf-8")
            script.chmod(0o755)
            result = check_executable_present(str(script))
        self.assertEqual(result["check"], CHECK_EXECUTABLE_PRESENCE)
        self.assertTrue(result["passed"])
        self.assertEqual(result["remedy"], "")

    def test_absolute_path_that_does_not_exist_fails_with_remedy(self) -> None:
        result = check_executable_present("/no/such/path/tool-xyz")
        self.assertFalse(result["passed"])
        self.assertIn("does not exist", result["detail"])
        self.assertTrue(result["remedy"])

    def test_absolute_path_that_is_not_executable_fails_with_remedy(self) -> None:
        if os.name == "nt":
            self.skipTest("execute-bit check is POSIX-only")
        with TemporaryDirectory() as tmp:
            script = Path(tmp) / "tool"
            script.write_text("not executable\n", encoding="utf-8")
            script.chmod(0o644)
            result = check_executable_present(str(script))
        self.assertFalse(result["passed"])
        self.assertIn("not executable", result["detail"])
        self.assertTrue(result["remedy"])

    def test_bare_name_resolves_through_injected_which(self) -> None:
        result = check_executable_present("tool", which=lambda name: "/usr/bin/tool")
        self.assertTrue(result["passed"])
        self.assertIn("/usr/bin/tool", result["detail"])

    def test_bare_name_not_on_path_fails_with_remedy(self) -> None:
        result = check_executable_present("tool", which=lambda name: None)
        self.assertFalse(result["passed"])
        self.assertIn("not found on PATH", result["detail"])
        self.assertTrue(result["remedy"])

    def test_empty_command_fails(self) -> None:
        result = check_executable_present("")
        self.assertFalse(result["passed"])


class WorkingDirectoryTests(unittest.TestCase):
    def test_none_passes(self) -> None:
        result = check_working_directory(None)
        self.assertEqual(result["check"], CHECK_WORKING_DIRECTORY)
        self.assertTrue(result["passed"])

    def test_existing_directory_passes(self) -> None:
        with TemporaryDirectory() as tmp:
            result = check_working_directory(tmp)
        self.assertTrue(result["passed"])

    def test_missing_directory_fails_with_remedy(self) -> None:
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist"
            result = check_working_directory(missing)
        self.assertFalse(result["passed"])
        self.assertIn("does not exist", result["detail"])
        self.assertTrue(result["remedy"])

    def test_path_that_is_a_file_not_a_directory_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "not-a-dir"
            file_path.write_text("x", encoding="utf-8")
            result = check_working_directory(file_path)
        self.assertFalse(result["passed"])
        self.assertIn("not a directory", result["detail"])


class EnvPrerequisiteTests(unittest.TestCase):
    def test_no_names_declared_passes(self) -> None:
        result = check_env_any_present((), env={})
        self.assertEqual(result["check"], CHECK_ENV_PREREQUISITE)
        self.assertTrue(result["passed"])

    def test_one_of_several_present_passes(self) -> None:
        result = check_env_any_present(
            ("FIRST_VAR", "SECOND_VAR"), env={"SECOND_VAR": "value"}
        )
        self.assertTrue(result["passed"])

    def test_none_present_fails_with_remedy_naming_every_candidate(self) -> None:
        result = check_env_any_present(("FIRST_VAR", "SECOND_VAR"), env={})
        self.assertFalse(result["passed"])
        self.assertIn("FIRST_VAR", result["detail"])
        self.assertIn("SECOND_VAR", result["detail"])
        self.assertIn("FIRST_VAR", result["remedy"])

    def test_present_but_empty_value_does_not_count(self) -> None:
        result = check_env_any_present(("FIRST_VAR",), env={"FIRST_VAR": ""})
        self.assertFalse(result["passed"])


class CredentialFilePresenceTests(unittest.TestCase):
    def test_no_path_declared_passes(self) -> None:
        result = check_credential_file_present(None)
        self.assertEqual(result["check"], CHECK_CREDENTIAL_FILE_PRESENCE)
        self.assertTrue(result["passed"])

    def test_empty_path_declared_passes(self) -> None:
        result = check_credential_file_present("")
        self.assertTrue(result["passed"])

    def test_existing_file_passes_without_reading_content(self) -> None:
        with TemporaryDirectory() as tmp:
            credential = Path(tmp) / "service-account.json"
            credential.write_text("{ this is not valid json ", encoding="utf-8")
            result = check_credential_file_present(credential)
        self.assertTrue(result["passed"])
        self.assertNotIn("this is not valid json", str(result))

    def test_missing_file_fails_with_remedy(self) -> None:
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / "service-account.json"
            result = check_credential_file_present(missing)
        self.assertFalse(result["passed"])
        self.assertIn("does not exist", result["detail"])
        self.assertTrue(result["remedy"])


class RunSpawnPreflightTests(unittest.TestCase):
    def test_all_checks_passing_yields_ready_verdict(self) -> None:
        verdict = run_spawn_preflight(
            [check_working_directory(None), check_credential_file_present(None)]
        )
        self.assertEqual(verdict["schema_version"], SPAWN_PREFLIGHT_SCHEMA_VERSION)
        self.assertEqual(verdict["status"], "ready")
        self.assertTrue(verdict["ready"])
        self.assertEqual(verdict["failed_checks"], [])
        self.assertEqual(len(verdict["checks"]), 2)
        self.assertTrue(verdict["claim_boundary"])

    def test_one_failing_check_yields_blocked_verdict_naming_it(self) -> None:
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / "gone"
            verdict = run_spawn_preflight(
                [check_working_directory(None), check_working_directory(missing)]
            )
        self.assertEqual(verdict["status"], "blocked")
        self.assertFalse(verdict["ready"])
        self.assertEqual(len(verdict["checks"]), 2)
        self.assertEqual(len(verdict["failed_checks"]), 1)
        self.assertEqual(verdict["failed_checks"][0]["check"], CHECK_WORKING_DIRECTORY)
        self.assertTrue(verdict["failed_checks"][0]["remedy"])

    def test_empty_checks_yields_ready_verdict(self) -> None:
        verdict = run_spawn_preflight([])
        self.assertTrue(verdict["ready"])
        self.assertEqual(verdict["checks"], [])


if __name__ == "__main__":
    unittest.main()
