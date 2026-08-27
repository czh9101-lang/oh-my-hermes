"""Declaration-removal and CLI coverage for the offline skill structure lint."""

from __future__ import annotations

import dataclasses
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from omh.commands.main import main
from omh.skills.catalog import builtin_definitions
from omh.skills.validation import SKILL_STRUCTURE_LINT_SCHEMA_VERSION
from skill_structure_lint_test_support import blocked_sockets, definition, violated_rules


class SkillHealthDeclarationRemovalTests(unittest.TestCase):
    """The four producer-less machine declarations are removed, not produced."""

    REMOVED = (
        "skill_portfolio_health_dashboard/v1",
        "skill_failure_pattern_clusters/v1",
        "pending_skill_amendment_review/v1",
        "skill_health_action_plan/v1",
    )

    def test_removed_from_catalog_declarations(self) -> None:
        skill_definition = definition("skill-health")
        declared = " ".join((*skill_definition.expected_outputs, *skill_definition.artifact_expectations))
        for schema in self.REMOVED:
            self.assertNotIn(schema, declared)

    def test_removed_from_generated_skill_projection(self) -> None:
        from omh.skills.packaging import builtin_skill_templates

        content = next(
            template.content for template in builtin_skill_templates() if template.name == "skill-health"
        )
        for schema in self.REMOVED:
            self.assertNotIn(schema, content)

    def test_removed_from_harness_declarations(self) -> None:
        from omh.skills.catalog import builtin_harnesses

        harness = next(harness for harness in builtin_harnesses() if harness.name == "skill-health")
        for schema in self.REMOVED:
            self.assertNotIn(schema, " ".join(harness.expected_outputs))

    def test_skill_health_still_declares_a_supported_output(self) -> None:
        """Removal must not leave the workflow with an empty machine contract."""
        skill_definition = definition("skill-health")
        self.assertTrue(skill_definition.expected_outputs)
        self.assertTrue(skill_definition.artifact_expectations)


class SkillStructureLintCliTests(unittest.TestCase):
    def _run(self, argv: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        with blocked_sockets(), redirect_stdout(buffer):
            code = main(argv)
        return code, buffer.getvalue()

    def test_pass_exits_zero_with_json_payload(self) -> None:
        import json

        code, stdout = self._run(["docs", "skill-lint", "--format", "json"])

        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["schema_version"], SKILL_STRUCTURE_LINT_SCHEMA_VERSION)
        self.assertEqual(payload["ok"], True)

    def test_two_cli_runs_are_byte_identical(self) -> None:
        first_code, first = self._run(["docs", "skill-lint", "--format", "json"])
        second_code, second = self._run(["docs", "skill-lint", "--format", "json"])

        self.assertEqual((first_code, second_code), (0, 0))
        self.assertEqual(first, second)

    def test_unsupported_format_is_an_invocation_error(self) -> None:
        buffer = io.StringIO()
        with self.assertRaises(SystemExit) as raised, mock.patch("sys.stderr", buffer):
            main(["docs", "skill-lint", "--format", "yaml"])

        self.assertEqual(raised.exception.code, 2)

    def test_violations_exit_one(self) -> None:
        import json

        broken = list(builtin_definitions())
        for index, skill_definition in enumerate(broken):
            if skill_definition.name == "skill-health":
                broken[index] = dataclasses.replace(skill_definition, why_this_exists="  ")
        buffer = io.StringIO()
        with (
            blocked_sockets(),
            redirect_stdout(buffer),
            mock.patch("omh.skills.validation.builtin_definitions", return_value=broken),
        ):
            code = main(["docs", "skill-lint", "--format", "json"])

        self.assertEqual(code, 1)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["ok"], False)
        self.assertEqual(violated_rules(payload), {"SKILL_CATALOG_CONTRACT"})

    def test_unexpected_internal_error_exits_two_and_stays_classified(self) -> None:
        """An unexpected failure is an internal error (2), never a violation (1).

        Exit 1 means "this tree has structural violations". A crash inside the
        lint is not that answer, and letting the exception escape `main()` would
        both collide with the violation code and print a traceback carrying
        whatever the message happened to hold.
        """
        import json

        secret = "tok_D0NOTLEAK_9f3a"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            blocked_sockets(),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            mock.patch(
                "omh.quality.skill_governance.skill_structure_lint_report",
                side_effect=RuntimeError(f"catalog exploded {secret}"),
            ),
        ):
            code = main(["docs", "skill-lint", "--format", "json"])

        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        report = json.loads(stderr.getvalue())
        self.assertEqual(report["schema_version"], SKILL_STRUCTURE_LINT_SCHEMA_VERSION)
        self.assertEqual(report["status"], "internal_error")
        self.assertEqual(report["ok"], False)
        self.assertEqual(report["error_type"], "RuntimeError")
        # An internal error is not a verdict about the tree, so it must not
        # carry the field a caller reads to enumerate structural violations.
        self.assertNotIn("violations", report)

    def test_internal_error_leaks_neither_message_payload_nor_traceback(self) -> None:
        secret = "tok_D0NOTLEAK_9f3a"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            blocked_sockets(),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            mock.patch(
                "omh.quality.skill_governance.skill_structure_lint_report",
                side_effect=RuntimeError(f"catalog exploded {secret}"),
            ),
        ):
            main(["docs", "skill-lint", "--format", "json"])

        combined = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(secret, combined)
        self.assertNotIn("Traceback (most recent call last)", combined)

    def test_internal_error_classification_is_deterministic(self) -> None:
        def _run() -> tuple[int, str]:
            stderr = io.StringIO()
            with (
                blocked_sockets(),
                redirect_stdout(io.StringIO()),
                redirect_stderr(stderr),
                mock.patch(
                    "omh.quality.skill_governance.skill_structure_lint_report",
                    side_effect=RuntimeError("catalog exploded"),
                ),
            ):
                return main(["docs", "skill-lint", "--format", "json"]), stderr.getvalue()

        first_code, first = _run()
        second_code, second = _run()

        self.assertEqual((first_code, second_code), (2, 2))
        self.assertEqual(first, second)

    def test_internal_error_does_not_suppress_a_real_violation_verdict(self) -> None:
        """The new boundary must not turn violations into internal errors."""
        import json

        broken = list(builtin_definitions())
        for index, skill_definition in enumerate(broken):
            if skill_definition.name == "skill-health":
                broken[index] = dataclasses.replace(skill_definition, why_this_exists="  ")
        stdout = io.StringIO()
        with (
            blocked_sockets(),
            redirect_stdout(stdout),
            mock.patch("omh.skills.validation.builtin_definitions", return_value=broken),
        ):
            code = main(["docs", "skill-lint", "--format", "json"])

        self.assertEqual(code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertNotIn("status", payload)
        self.assertEqual(violated_rules(payload), {"SKILL_CATALOG_CONTRACT"})

    def test_help_describes_the_structure_only_boundary(self) -> None:
        from omh.commands.main import build_parser

        buffer = io.StringIO()
        with redirect_stdout(buffer), self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(["docs", "skill-lint", "--help"])

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("structure", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
