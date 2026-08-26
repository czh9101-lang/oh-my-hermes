"""Contract tests for the deterministic trigger loss + collision ownership report.

The report exists so a maintainer can see which catalog-defined triggers never
reached picker frontmatter and why, and can review which normalized trigger
overlaps are intentionally shared. A collision is not asserted to be a defect
here; only the review surface and its fail-closed declaration gate are.
"""

from __future__ import annotations

import json
import unittest

from _cli_harness import run_cli
from _local_package import load_local_package

load_local_package()

from omh.routing.localization import normalized_phrase
from omh.skills.catalog import builtin_definitions
from omh.skills.catalog_types import SkillDefinition
from omh.skills.trigger_review import (
    COLLISION_STATUS_VALUES,
    OMISSION_REASONS,
    TRIGGER_REVIEW_SCHEMA_VERSION,
    CollisionDeclaration,
    collision_groups,
    skill_trigger_review_payload,
    trigger_omissions,
    validate_collision_declarations,
)


def _definition(name: str, triggers: tuple[str, ...], aliases: tuple[str, ...] = ()) -> SkillDefinition:
    return SkillDefinition(
        name=name,
        description=f"Fixture skill {name} used only by the trigger-review contract tests.",
        triggers=triggers,
        use_when=f"Fixture {name} routing surface.",
        aliases=aliases,
    )


class TriggerOmissionReportTests(unittest.TestCase):
    def test_payload_carries_the_versioned_schema(self) -> None:
        payload = skill_trigger_review_payload()
        self.assertEqual(payload["schema_version"], TRIGGER_REVIEW_SCHEMA_VERSION)
        self.assertEqual(TRIGGER_REVIEW_SCHEMA_VERSION, "skill_trigger_review/v1")

    def test_every_omission_carries_a_reason_from_the_closed_set(self) -> None:
        payload = skill_trigger_review_payload()
        omissions = payload["omissions"]
        self.assertIsInstance(omissions, list)
        self.assertTrue(omissions, "the shipped catalog omits triggers; the report must show them")
        for entry in omissions:
            self.assertIn(entry["reason"], OMISSION_REASONS)
            self.assertIn("skill", entry)
            self.assertIn("trigger", entry)

    def test_budget_overflow_and_unsafe_and_alias_duplicates_are_distinguished(self) -> None:
        definitions = [
            _definition(
                "fixture-overflow",
                triggers=tuple(f"phrase {index}" for index in range(12)) + ("/sigil", "ulf"),
                aliases=("ulf",),
            )
        ]
        omissions = trigger_omissions(definitions)
        by_trigger = {entry["trigger"]: entry["reason"] for entry in omissions}
        self.assertEqual(by_trigger["/sigil"], "unsafe_for_frontmatter")
        self.assertEqual(by_trigger["ulf"], "duplicate_of_alias")
        self.assertEqual(by_trigger["phrase 11"], "budget_overflow")
        self.assertNotIn("phrase 0", by_trigger)

    def test_emitted_triggers_match_the_rendered_frontmatter_description(self) -> None:
        from omh.skills.render import frontmatter_description

        payload = skill_trigger_review_payload()
        emitted = {entry["skill"]: entry["emitted_triggers"] for entry in payload["skills"]}
        for definition in builtin_definitions():
            description = frontmatter_description(definition)
            for trigger in emitted[definition.name]:
                self.assertIn(trigger, description)

    def test_report_grades_nothing(self) -> None:
        """No scoring vocabulary in the report's own structure.

        Catalog trigger phrases are echoed verbatim and some legitimately
        contain words like `badges`, so the assertion walks the payload's keys
        and generated values rather than grepping the serialized blob.
        """
        banned = ("score", "grade", "badge", "rank", "percent", "severity")
        payload = skill_trigger_review_payload()

        def walk_keys(node: object) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    for word in banned:
                        self.assertNotIn(word, key.casefold())
                    walk_keys(value)
            elif isinstance(node, list):
                for item in node:
                    walk_keys(item)

        walk_keys(payload)
        generated = " ".join(
            [str(payload["review_boundary"]), *[str(reason) for reason in payload["omission_reasons"]]]
        ).casefold()
        for word in banned:
            self.assertNotIn(word, generated)


class CollisionOwnershipTests(unittest.TestCase):
    def test_groups_key_on_the_shared_normalization_helper(self) -> None:
        """Case and compatibility folding are the shared helper's, not a local copy."""
        definitions = [
            _definition("fixture-a", triggers=("Deploy Review",)),
            _definition("fixture-b", triggers=("ｄeploy review",)),
        ]
        groups = collision_groups(definitions)
        identities = {group["identity"] for group in groups}
        self.assertEqual(identities, {normalized_phrase("Deploy Review")})

    def test_identity_is_exactly_the_shared_helper_output(self) -> None:
        """The report never re-normalizes; a phrase the helper keeps distinct stays distinct."""
        definitions = [
            _definition("fixture-a", triggers=("deploy  review",)),
            _definition("fixture-b", triggers=("deploy review",)),
        ]
        self.assertEqual(collision_groups(definitions), [])

    def test_aliases_participate_in_collision_ownership(self) -> None:
        definitions = [
            _definition("fixture-alias-owner", triggers=("standalone",), aliases=("shared-id",)),
            _definition("fixture-trigger-owner", triggers=("shared-id",)),
        ]
        groups = collision_groups(definitions)
        group = next(group for group in groups if group["identity"] == normalized_phrase("shared-id"))
        sources = {(owner["skill"], owner["source"]) for owner in group["owners"]}
        self.assertIn(("fixture-alias-owner", "alias"), sources)
        self.assertIn(("fixture-trigger-owner", "trigger"), sources)

    def test_single_owner_phrases_are_not_collisions(self) -> None:
        definitions = [_definition("fixture-solo", triggers=("only here", "only here"))]
        self.assertEqual(collision_groups(definitions), [])

    def test_every_group_carries_a_declaration_status(self) -> None:
        payload = skill_trigger_review_payload()
        self.assertTrue(payload["collisions"])
        for group in payload["collisions"]:
            self.assertIn(group["status"], COLLISION_STATUS_VALUES)

    def test_shipped_catalog_collisions_are_all_declared(self) -> None:
        result = validate_collision_declarations()
        self.assertEqual(result["errors"], [])
        self.assertTrue(result["ok"])


class CollisionGateTests(unittest.TestCase):
    """The gate fails closed unless declared owners equal observed owners exactly."""

    def _fixture_definitions(self) -> list[SkillDefinition]:
        return [
            _definition("fixture-one", triggers=("shared phrase",)),
            _definition("fixture-two", triggers=("Shared Phrase",)),
        ]

    def _declaration(self, owners: tuple[str, ...]) -> CollisionDeclaration:
        return CollisionDeclaration(
            identity=normalized_phrase("shared phrase"),
            owners=owners,
            rationale_id="R-FIXTURE",
        )

    def test_undeclared_collision_fails_with_paste_ready_instructions(self) -> None:
        result = validate_collision_declarations(self._fixture_definitions(), declarations=())
        self.assertFalse(result["ok"])
        message = "\n".join(result["errors"])
        self.assertIn(normalized_phrase("shared phrase"), message)
        self.assertIn("CollisionDeclaration", message)
        self.assertIn("rationale_id", message)

    def test_declared_collision_passes(self) -> None:
        result = validate_collision_declarations(
            self._fixture_definitions(),
            declarations=(self._declaration(("fixture-one", "fixture-two")),),
        )
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["errors"], [])

    def test_stale_declaration_fails(self) -> None:
        result = validate_collision_declarations(
            [_definition("fixture-one", triggers=("shared phrase",))],
            declarations=(self._declaration(("fixture-one", "fixture-two")),),
        )
        self.assertFalse(result["ok"])
        self.assertIn("stale", "\n".join(result["errors"]))

    def test_owner_expanded_collision_fails(self) -> None:
        definitions = self._fixture_definitions()
        definitions.append(_definition("fixture-three", triggers=("shared phrase",)))
        result = validate_collision_declarations(
            definitions,
            declarations=(self._declaration(("fixture-one", "fixture-two")),),
        )
        self.assertFalse(result["ok"])
        message = "\n".join(result["errors"])
        self.assertIn("fixture-three", message)

    def test_declaration_naming_an_unobserved_owner_fails(self) -> None:
        """Given a declaration approving an owner the catalog does not share,
        When the gate runs, Then it fails and names the phantom owner.

        A superset declaration is a pre-approval: it signs off on sharing that
        no maintainer has actually reviewed, because the group it describes has
        never existed.
        """
        result = validate_collision_declarations(
            self._fixture_definitions(),
            declarations=(self._declaration(("fixture-one", "fixture-two", "fixture-future")),),
        )
        self.assertFalse(result["ok"])
        message = "\n".join(result["errors"])
        self.assertIn("fixture-future", message)
        self.assertIn(normalized_phrase("shared phrase"), message)
        self.assertIn("INTENTIONAL_COLLISIONS", message)

    def test_phantom_owner_cannot_pre_approve_a_future_expansion(self) -> None:
        """Given one superset declaration, When the catalog is evaluated before and
        after the phantom owner really joins, Then the gate's verdict differs.

        This is the whole fail-closed claim. A gate that only compares observed
        against declared in one direction accepts the superset in BOTH states,
        so the expansion lands already approved and no review is ever triggered.
        """
        pre_expansion = self._fixture_definitions()
        expanded = self._fixture_definitions()
        expanded.append(_definition("fixture-future", triggers=("shared phrase",)))
        superset = self._declaration(("fixture-one", "fixture-two", "fixture-future"))

        before = validate_collision_declarations(pre_expansion, declarations=(superset,))
        after = validate_collision_declarations(expanded, declarations=(superset,))

        self.assertFalse(before["ok"], "a superset declaration must be rejected while its owner is unobserved")
        self.assertTrue(after["ok"], after["errors"])
        self.assertNotEqual(
            before["ok"],
            after["ok"],
            "the gate must distinguish the two catalog states; one accepting both pre-approves the expansion",
        )

    def test_duplicate_declaration_identities_are_rejected(self) -> None:
        """Given two declarations for one identity, When the gate runs, Then it fails.

        Silently collapsing duplicates lets a second entry with different owners
        or a different rationale shadow the reviewed one.
        """
        result = validate_collision_declarations(
            self._fixture_definitions(),
            declarations=(
                self._declaration(("fixture-one", "fixture-two")),
                self._declaration(("fixture-one", "fixture-two")),
            ),
        )
        self.assertFalse(result["ok"])
        message = "\n".join(result["errors"])
        self.assertIn("duplicate", message.lower())
        self.assertIn(normalized_phrase("shared phrase"), message)

    def test_declaration_stores_only_identity_owners_and_rationale(self) -> None:
        fields = set(CollisionDeclaration.__dataclass_fields__)
        self.assertEqual(fields, {"identity", "owners", "rationale_id"})

    def test_catalog_contract_fails_closed_on_undeclared_collisions(self) -> None:
        from omh.skills import validation

        report = validation.validate_catalog_contract()
        self.assertTrue(report["ok"], report["errors"])


class TriggerReviewCliTests(unittest.TestCase):
    def test_command_emits_the_schema(self) -> None:
        status, stdout, _ = run_cli(["docs", "skill-trigger-report", "--format", "json"])
        self.assertEqual(status, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["schema_version"], TRIGGER_REVIEW_SCHEMA_VERSION)

    def test_two_runs_are_byte_identical(self) -> None:
        _, first, _ = run_cli(["docs", "skill-trigger-report", "--format", "json"])
        _, second, _ = run_cli(["docs", "skill-trigger-report", "--format", "json"])
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
