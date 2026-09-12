from __future__ import annotations

import unittest

from _handoff_risk_fixture import scan


class HandoffRiskRulesTests(unittest.TestCase):
    def test_S3_danger_ids_when_actionable_commands_supplied(self):
        # Given
        cases = (
            ("Run git reset --hard HEAD", "destructive_git"),
            ("git clean -fdx", "destructive_git"),
            ("git branch -D obsolete", "destructive_git"),
            ('Run `rm -rf "private path"`', "destructive_filesystem"),
            ('rm -rf "escaped\\\"name"', "destructive_filesystem"),
            ("sudo rm --recursive --force ./data", "destructive_filesystem"),
            ("DROP TABLE customers;", "destructive_database"),
            ("TRUNCATE TABLE customers;", "destructive_database"),
            ("DELETE FROM customers;", "destructive_database"),
            ('psql -c "DROP DATABASE fixture;"', "destructive_database"),
            ("Disable the tests to finish", "test_weakening"),
            ("Delete failing tests", "test_weakening"),
            ("Bypass test verification", "test_weakening"),
            ("pytest --ignore=tests", "test_weakening"),
            ("git push origin HEAD:main", "protected_branch_write"),
            ("git push --force-with-lease origin main", "protected_branch_write"),
            ("git push origin +HEAD:refs/heads/master", "protected_branch_write"),
            ("git push --delete origin main", "protected_branch_write"),
            ("git push origin main:main", "protected_branch_write"),
            ("Do not run git reset --hard; run rm -rf ./data", "destructive_filesystem"),
            ("git status && git reset --hard", "destructive_git"),
            ("git status\nrm -rf ./data", "destructive_filesystem"),
            ("git reset \\\n --hard", "destructive_git"),
            ("Example: git status\nRun rm -rf ./data", "destructive_filesystem"),
            ("Examples:\n```sh\ngit status\n```\nRun rm -rf ./data", "destructive_filesystem"),
            ("Do not run git status && run rm -rf ./data", "destructive_filesystem"),
            ("Run `git reset --hard`; run `rm -rf ./data`", "destructive_git"),
            ("Run `git reset --hard`; run `rm -rf ./data`", "destructive_filesystem"),
        )
        for brief, expected in cases:
            with self.subTest(brief=brief):
                # When
                report = scan(brief)
                # Then
                self.assertIn(expected, {f["id"] for f in report.get("findings", [])})

    def test_S3_clear_when_negated_or_illustrative_or_safe(self):
        # Given
        cases = (
            "Do not run git reset --hard.",
            "Never run rm -rf ./data",
            "Avoid DROP TABLE customers;",
            "> git reset --hard\n> rm -rf ./data",
            "Example: `git reset --hard` destroys local changes.",
            "For example, `rm -rf ./data` is dangerous.",
            "Explanation: git reset --hard discards changes.",
            "Examples:\n```sh\nrm -rf ./data\n```",
            'The command "git reset --hard" is destructive.',
            "Do not disable tests.",
            "Don't run git reset --hard.",
            "Do not run:\n```sh\nrm -rf ./data\n```",
            "Review the fixture's correctness.",
            "Don't disable tests; inspect status.",
            "git push origin feature", "git push origin main:feature",
            "git clean -nd", "git reset --soft HEAD~1", "git status",
            "DELETE FROM customers WHERE id = 3;",
            "echo 'rm -rf ./data'", "printf '%s' 'git reset --hard'",
            "ｒｍ -rf ./data", "git reset --hаrd",  # Non-runnable lookalikes, not normalized to commands.
        )
        for brief in cases:
            with self.subTest(brief=brief):
                # When
                report = scan(brief)
                # Then
                self.assertEqual(report.get("verdict"), "clear")

    def test_S3_scope_when_negation_followed_by_action(self):
        # Given / When
        report = scan("Do not run git reset --hard; run rm -rf ./data")
        # Then
        self.assertEqual({f["id"] for f in report.get("findings", [])}, {"destructive_filesystem"})

    def test_S3_override_when_protected_branch_replaced(self):
        # Given / When
        report = scan("git push origin main", ["--protected-branch", "production"])
        # Then
        self.assertEqual(report.get("verdict"), "clear")

    def test_S3_exact_branch_when_override_matches(self):
        # Given / When
        report = scan("git push origin HEAD:production", ["--protected-branch", "production"])
        # Then
        self.assertEqual(report.get("verdict"), "high_risk")
