from __future__ import annotations

import unittest

from omh.quality.verification_tiering import (
    SENSITIVE_PATH_ESCALATION_SCHEMA_VERSION,
    sensitive_path_escalation,
)


class SensitivePathEscalationTests(unittest.TestCase):
    def test_no_paths_never_escalates(self) -> None:
        self.assertIsNone(sensitive_path_escalation([]))

    def test_ordinary_paths_never_escalate(self) -> None:
        for path in ("README.md", "src/foo.py", "src/quality/routing_precision.py", "tests/test_cli.py"):
            with self.subTest(path=path):
                self.assertIsNone(sensitive_path_escalation([path]))

    def test_author_dot_py_is_not_auth(self) -> None:
        """A filename merely containing the letters `auth` must never false-positive."""
        self.assertIsNone(sensitive_path_escalation(["src/blog/author.py"]))
        self.assertIsNone(sensitive_path_escalation(["authoring/notes.py"]))

    def test_environment_dot_md_is_not_env(self) -> None:
        """A filename merely containing the letters `env` must never false-positive."""
        self.assertIsNone(sensitive_path_escalation(["docs/environment.md"]))
        self.assertIsNone(sensitive_path_escalation(["src/envelope.py"]))

    def test_auth_directory_escalates(self) -> None:
        result = sensitive_path_escalation(["src/auth/login.py"])
        self.assertIsNotNone(result)
        self.assertEqual(result["category"], "auth")
        self.assertEqual(result["matched_path"], "src/auth/login.py")
        self.assertEqual(result["schema_version"], SENSITIVE_PATH_ESCALATION_SCHEMA_VERSION)
        self.assertIn("thorough", result["reason"])

    def test_jwt_filename_escalates_as_auth(self) -> None:
        result = sensitive_path_escalation(["lib/jwt_utils.py"])
        self.assertIsNotNone(result)
        self.assertEqual(result["category"], "auth")

    def test_dotenv_escalates(self) -> None:
        for path in (".env", ".env.local", ".env.production"):
            with self.subTest(path=path):
                result = sensitive_path_escalation([path])
                self.assertIsNotNone(result)
                self.assertEqual(result["category"], "secrets_config")

    def test_credential_filename_escalates(self) -> None:
        result = sensitive_path_escalation(["config/credentials.json"])
        self.assertIsNotNone(result)
        self.assertEqual(result["category"], "secrets_config")

    def test_migrations_directory_escalates(self) -> None:
        result = sensitive_path_escalation(["db/migrations/0001_init.sql"])
        self.assertIsNotNone(result)
        self.assertEqual(result["category"], "schema_migrations")

    def test_schema_filename_escalates(self) -> None:
        result = sensitive_path_escalation(["schema.sql"])
        self.assertIsNotNone(result)
        self.assertEqual(result["category"], "schema_migrations")

    def test_payment_directory_escalates(self) -> None:
        result = sensitive_path_escalation(["src/payments/charge.py"])
        self.assertIsNotNone(result)
        self.assertEqual(result["category"], "payment_crypto")

    def test_wallet_filename_escalates(self) -> None:
        result = sensitive_path_escalation(["wallet_signer.py"])
        self.assertIsNotNone(result)
        self.assertEqual(result["category"], "payment_crypto")

    def test_first_rule_and_first_path_win_deterministically(self) -> None:
        result = sensitive_path_escalation(["README.md", "src/foo.py", "src/auth/login.py", ".env"])
        self.assertIsNotNone(result)
        self.assertEqual(result["category"], "auth")
        self.assertEqual(result["matched_path"], "src/auth/login.py")

    def test_non_string_and_blank_entries_are_skipped(self) -> None:
        self.assertIsNone(sensitive_path_escalation([None, "", "   ", 42]))  # type: ignore[list-item]

    def test_matching_is_case_insensitive_but_still_component_scoped(self) -> None:
        result = sensitive_path_escalation(["src/Auth/Login.py"])
        self.assertIsNotNone(result)
        self.assertEqual(result["category"], "auth")
        self.assertIsNone(sensitive_path_escalation(["src/Author/Notes.py"]))


if __name__ == "__main__":
    unittest.main()
