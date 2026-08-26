"""Per-rule fixtures for the deterministic offline skill structure lint.

Every stable rule owns one passing fixture and one failing fixture, and the
failing fixture must raise exactly its own rule. That isolation is the whole
point: a lint that reports three rules for one defect cannot tell a maintainer
where the defect belongs.

The lint answers "is this skill structurally valid" only. It never scores,
grades, ranks, compares third-party skills, reads natural-language wording, or
opens a socket, so the tests below assert structure and silence rather than
quality.
"""

from __future__ import annotations

import dataclasses
import io
import socket
import unittest
from contextlib import contextmanager, redirect_stdout
from unittest import mock

from omh.commands.main import main
from omh.skills.catalog import builtin_definitions
from omh.skills.validation import (
    SKILL_STRUCTURE_LINT_SCHEMA_VERSION,
    STRUCTURE_LINT_RULE_IDS,
    skill_structure_lint_payload,
)


@contextmanager
def _blocked_sockets():
    """Fail loudly if the lint reaches for the network.

    Patching `connect` rather than the whole module keeps local-only socket
    construction legal while making an actual dial an immediate error.
    """
    with mock.patch.object(
        socket.socket,
        "connect",
        side_effect=AssertionError("skill structure lint attempted a network connection"),
    ):
        yield


def _definition(name: str):
    for definition in builtin_definitions():
        if definition.name == name:
            return definition
    raise AssertionError(f"missing catalog fixture skill: {name}")


def _violated_rules(payload: dict[str, object]) -> set[str]:
    violations = payload["violations"]
    assert isinstance(violations, list)
    return {str(violation["rule"]) for violation in violations}


class SkillStructureLintPassTests(unittest.TestCase):
    def test_tracked_tree_passes_with_zero_violations(self) -> None:
        with _blocked_sockets():
            payload = skill_structure_lint_payload()

        self.assertEqual(payload["schema_version"], SKILL_STRUCTURE_LINT_SCHEMA_VERSION)
        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["violations"], [])

    def test_every_stable_rule_reports_a_pass_verdict_on_the_tracked_tree(self) -> None:
        """The passing fixture for every rule is the shipped tree itself.

        Listing the evaluated rules in the payload is what makes that claim
        checkable; otherwise a rule that silently stopped running would still
        look like a pass.
        """
        with _blocked_sockets():
            payload = skill_structure_lint_payload()

        self.assertEqual(sorted(payload["rules"]), sorted(STRUCTURE_LINT_RULE_IDS))

    def test_two_runs_are_byte_identical(self) -> None:
        with _blocked_sockets():
            first = skill_structure_lint_payload()
            second = skill_structure_lint_payload()

        self.assertEqual(first, second)

    def test_report_carries_no_score_grade_or_third_party_comparison(self) -> None:
        with _blocked_sockets():
            payload = skill_structure_lint_payload()

        banned = {
            "score",
            "scores",
            "grade",
            "grades",
            "rank",
            "ranking",
            "rating",
            "badge",
            "badges",
            "third_party",
            "comparison",
        }
        self.assertEqual(banned & set(payload), set())

    def test_verdict_states_that_structure_is_not_host_loading_proof(self) -> None:
        """C012: passing structure never implies the host actually loaded a skill."""
        with _blocked_sockets():
            payload = skill_structure_lint_payload()

        self.assertEqual(payload["checks"], "structure_only")
        self.assertEqual(payload["proves_host_loading"], False)


class SkillStructureLintRuleIsolationTests(unittest.TestCase):
    """One mutation per rule; exactly that rule must fail."""

    def _lint_with(self, definitions) -> dict[str, object]:
        with _blocked_sockets():
            return skill_structure_lint_payload(definitions=definitions)

    def _mutated(self, name: str, **changes):
        definitions = list(builtin_definitions())
        for index, definition in enumerate(definitions):
            if definition.name == name:
                definitions[index] = dataclasses.replace(definition, **changes)
                return definitions
        raise AssertionError(f"missing catalog fixture skill: {name}")

    def assertOnlyRule(self, payload: dict[str, object], rule: str, skill: str) -> None:
        self.assertEqual(payload["ok"], False)
        self.assertEqual(_violated_rules(payload), {rule})
        violations = payload["violations"]
        assert isinstance(violations, list)
        for violation in violations:
            self.assertEqual(violation["skill"], skill)
            self.assertTrue(str(violation["detail"]).strip())

    def test_catalog_contract_rule_fails_alone(self) -> None:
        payload = self._lint_with(self._mutated("skill-health", why_this_exists="  "))
        self.assertOnlyRule(payload, "SKILL_CATALOG_CONTRACT", "skill-health")

    def test_frontmatter_rule_fails_alone(self) -> None:
        """An alias that cannot survive unquoted YAML makes the description unrenderable."""
        payload = self._lint_with(self._mutated("skill-health", aliases=("bad/alias",)))
        self.assertOnlyRule(payload, "SKILL_FRONTMATTER_FIELDS", "skill-health")

    def test_harness_resolution_rule_fails_alone(self) -> None:
        from omh.skills import validation as validation_module

        harnesses = [
            harness
            for harness in validation_module.builtin_harnesses()
            if harness.name != "skill-health"
        ]
        with mock.patch.object(validation_module, "builtin_harnesses", return_value=harnesses):
            payload = self._lint_with(list(builtin_definitions()))

        self.assertOnlyRule(payload, "SKILL_HARNESS_RESOLVES", "skill-health")

    def test_generated_parity_rule_fails_alone(self) -> None:
        payload = self._lint_with(self._mutated("skill-health", phase="drifted-phase"))
        self.assertOnlyRule(payload, "SKILL_GENERATED_PARITY", "skill-health")

    def test_trigger_format_rule_fails_alone(self) -> None:
        """Sigil-only triggers are well-formed but never reach a non-router picker."""
        payload = self._lint_with(self._mutated("skill-health", triggers=("/omh", "./omh"), aliases=()))
        self.assertOnlyRule(payload, "SKILL_TRIGGER_FORMAT", "skill-health")

    def test_executable_consumer_rule_fails_alone(self) -> None:
        """Two consumers naming one skill must dispatch the same action.

        The mutation is applied to the routing policy rather than the catalog
        entry, because that is the shape of the real defect: the catalog looks
        fine while the wrapper card and the router disagree about what running
        the skill does.
        """
        from omh.routing import recommend as recommend_module

        policy = recommend_module._SKILL_POLICIES["skill-health"]
        drifted = dict(recommend_module._SKILL_POLICIES)
        drifted["skill-health"] = dataclasses.replace(policy, next_action="prepare_something_else")
        with mock.patch.object(recommend_module, "_SKILL_POLICIES", drifted):
            payload = self._lint_with(list(builtin_definitions()))

        self.assertOnlyRule(payload, "SKILL_EXECUTABLE_CONSUMER", "skill-health")

    def test_context_budget_rule_fails_alone(self) -> None:
        payload = self._lint_with(self._mutated("skill-health", use_when="x" * 200_000))
        self.assertOnlyRule(payload, "SKILL_CONTEXT_BUDGET", "skill-health")


class SkillHealthDeclarationRemovalTests(unittest.TestCase):
    """The four producer-less machine declarations are removed, not produced."""

    REMOVED = (
        "skill_portfolio_health_dashboard/v1",
        "skill_failure_pattern_clusters/v1",
        "pending_skill_amendment_review/v1",
        "skill_health_action_plan/v1",
    )

    def test_removed_from_catalog_declarations(self) -> None:
        definition = _definition("skill-health")
        declared = " ".join((*definition.expected_outputs, *definition.artifact_expectations))
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
        definition = _definition("skill-health")
        self.assertTrue(definition.expected_outputs)
        self.assertTrue(definition.artifact_expectations)


class SkillStructureLintCliTests(unittest.TestCase):
    def _run(self, argv: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        with _blocked_sockets(), redirect_stdout(buffer):
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
        for index, definition in enumerate(broken):
            if definition.name == "skill-health":
                broken[index] = dataclasses.replace(definition, why_this_exists="  ")
        buffer = io.StringIO()
        with (
            _blocked_sockets(),
            redirect_stdout(buffer),
            mock.patch("omh.skills.validation.builtin_definitions", return_value=broken),
        ):
            code = main(["docs", "skill-lint", "--format", "json"])

        self.assertEqual(code, 1)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["ok"], False)
        self.assertEqual(_violated_rules(payload), {"SKILL_CATALOG_CONTRACT"})

    def test_help_describes_the_structure_only_boundary(self) -> None:
        from omh.commands.main import build_parser

        buffer = io.StringIO()
        with redirect_stdout(buffer), self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(["docs", "skill-lint", "--help"])

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("structure", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
