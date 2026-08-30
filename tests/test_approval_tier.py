"""The approval-tier resolver: one pure DECISION function every dispatch and
install confirmation guard now asks (`P3 -- P. Approval-tier resolution as a
pure function`), plus the enforcement-site integration that replaced their own
scattered "if not confirmed: raise" copies.

Coverage in this module:

- Every `APPROVAL_RULE_TABLE` row is exercised for both its confirmed and
  unconfirmed shape (`ResolveApprovalTierTests`).
- The unknown-defaults-to-most-restrictive and headless-rejects-rather-than-
  hangs rules absorbed from `docs/approval-mode.md` (P3, item P).
- Posture composition through `security_posture.strict_override`
  (`installer_confirmation_override_available`).
- Enforcement-site integration for the two installer guards this resolver now
  drives (`_write_skill`/`install_skill_pack`'s local-modification refusal,
  `_collect_removal`'s unowned-plugin-dir refusal): strict posture disables
  `--force` for both, default posture is unchanged from before this resolver
  existed. The hermes-child-dispatch and fanout-recursion-depth sites'
  default-posture behavior is already pinned by
  `tests/test_security_posture.py::InvariantRowTests`,
  `tests/test_hermes_child_cli.py`, and `tests/test_fanout_dispatch.py`
  (`FanoutSpawnGuardTests`) -- this module does not duplicate those.
"""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()

from omh.core.errors import OmhError  # noqa: E402
from omh.install.installer import _collect_removal, install_skill_pack, uninstall_profile_plugin  # noqa: E402
from omh.manifest import new_manifest, skill_records, write_manifest  # noqa: E402
from omh.paths import resolve_paths  # noqa: E402
from omh.system.approval_tier import (  # noqa: E402
    APPROVAL_RULE_TABLE,
    APPROVAL_TIER_SCHEMA_VERSION,
    APPROVAL_TIERS,
    TIER_AUTO_ALLOWED,
    TIER_NEEDS_CONFIRMATION,
    TIER_REFUSED,
    UNKNOWN_OPERATION_REASON,
    describe_approval_tier_table,
    known_operation_classes,
    resolve_approval_tier,
)
from omh.system.security_posture import DEFAULT_POSTURE, POSTURE_MAPPING, STRICT_POSTURE  # noqa: E402


class RuleTableIntegrityTests(unittest.TestCase):
    def test_every_row_has_a_unique_class_a_valid_tier_and_a_rationale(self) -> None:
        self.assertTrue(APPROVAL_RULE_TABLE)
        classes = [rule.operation_class for rule in APPROVAL_RULE_TABLE]
        self.assertEqual(len(classes), len(set(classes)), "operation_class values must be unique")
        for rule in APPROVAL_RULE_TABLE:
            self.assertIn(rule.base_tier, APPROVAL_TIERS, rule)
            self.assertTrue(rule.rationale.strip(), rule)

    def test_every_posture_key_names_a_real_security_posture_mapping_row(self) -> None:
        posture_keys = {row.key for row in POSTURE_MAPPING}
        for rule in APPROVAL_RULE_TABLE:
            if rule.posture_key is not None:
                self.assertIn(rule.posture_key, posture_keys, rule)

    def test_only_needs_confirmation_rows_carry_a_posture_key(self) -> None:
        # auto_allowed and refused rows are already the most/least permissive
        # tier; nothing for posture to tighten toward.
        for rule in APPROVAL_RULE_TABLE:
            if rule.posture_key is not None:
                self.assertEqual(rule.base_tier, TIER_NEEDS_CONFIRMATION, rule)

    def test_known_operation_classes_matches_the_table_order(self) -> None:
        self.assertEqual(known_operation_classes(), tuple(rule.operation_class for rule in APPROVAL_RULE_TABLE))


class ResolveApprovalTierTests(unittest.TestCase):
    """Every table row, exercised for both its confirmed and unconfirmed shape."""

    def test_unknown_operation_class_defaults_to_refused(self) -> None:
        decision = resolve_approval_tier("some_operation_nobody_declared")
        self.assertEqual(decision.tier, TIER_REFUSED)
        self.assertEqual(decision.reason_code, UNKNOWN_OPERATION_REASON)

    def test_auto_allowed_rows_are_allowed_regardless_of_confirmation(self) -> None:
        for rule in APPROVAL_RULE_TABLE:
            if rule.base_tier != TIER_AUTO_ALLOWED:
                continue
            for confirmed in (True, False):
                decision = resolve_approval_tier(rule.operation_class, confirmed=confirmed)
                self.assertEqual(decision.tier, TIER_AUTO_ALLOWED, rule)

    def test_structural_refusal_rows_stay_refused_regardless_of_confirmation(self) -> None:
        for rule in APPROVAL_RULE_TABLE:
            if rule.base_tier != TIER_REFUSED:
                continue
            for confirmed in (True, False):
                decision = resolve_approval_tier(rule.operation_class, confirmed=confirmed)
                self.assertEqual(decision.tier, TIER_REFUSED, rule)
                self.assertEqual(decision.reason_code, "structural_refusal")

    def test_needs_confirmation_rows_allow_only_when_confirmed_in_default_posture(self) -> None:
        for rule in APPROVAL_RULE_TABLE:
            if rule.base_tier != TIER_NEEDS_CONFIRMATION:
                continue
            allowed = resolve_approval_tier(rule.operation_class, confirmed=True, posture=DEFAULT_POSTURE)
            self.assertEqual(allowed.tier, TIER_AUTO_ALLOWED, rule)
            self.assertEqual(allowed.reason_code, "confirmed")

            refused = resolve_approval_tier(rule.operation_class, confirmed=False, posture=DEFAULT_POSTURE)
            self.assertEqual(refused.tier, TIER_REFUSED, rule)
            self.assertEqual(refused.reason_code, "unconfirmed")

    def test_headless_rejects_rather_than_hangs_unconfirmed_never_returns_needs_confirmation(self) -> None:
        # The resolver never hands a call site a pending state to wait on:
        # every decision is a final tier.
        for rule in APPROVAL_RULE_TABLE:
            for confirmed in (True, False):
                for posture in (DEFAULT_POSTURE, STRICT_POSTURE):
                    decision = resolve_approval_tier(rule.operation_class, confirmed=confirmed, posture=posture)
                    self.assertNotEqual(decision.tier, TIER_NEEDS_CONFIRMATION, rule)

    def test_decision_carries_the_operation_class_and_posture_back(self) -> None:
        decision = resolve_approval_tier("hermes_child_dispatch", confirmed=True, posture=STRICT_POSTURE)
        self.assertEqual(decision.operation_class, "hermes_child_dispatch")
        self.assertEqual(decision.posture, STRICT_POSTURE)


class PostureCompositionTests(unittest.TestCase):
    """Strict posture tightens exactly the rows with a `posture_key`, through
    `security_posture.strict_override` -- the same mapping-table pattern every
    other tightened surface (#1196) already uses."""

    def test_strict_posture_disables_the_installer_confirmation_override(self) -> None:
        for operation_class in ("installer_overwrite_local_modification", "installer_remove_unowned_plugin_dir"):
            decision = resolve_approval_tier(operation_class, confirmed=True, posture=STRICT_POSTURE)
            self.assertEqual(decision.tier, TIER_REFUSED, operation_class)
            self.assertEqual(decision.reason_code, "strict_posture_override_disabled", operation_class)

    def test_default_posture_still_honors_the_installer_confirmation_override(self) -> None:
        for operation_class in ("installer_overwrite_local_modification", "installer_remove_unowned_plugin_dir"):
            decision = resolve_approval_tier(operation_class, confirmed=True, posture=DEFAULT_POSTURE)
            self.assertEqual(decision.tier, TIER_AUTO_ALLOWED, operation_class)

    def test_rows_with_no_posture_key_are_posture_invariant(self) -> None:
        # hermes_child_dispatch, hermes_child_recursion_depth, and
        # fanout_recursion_depth all document (in security_posture.py's own
        # `dispatch_confirmation_required` row, and by construction here) that
        # posture never changes their tier.
        for rule in APPROVAL_RULE_TABLE:
            if rule.posture_key is not None:
                continue
            for confirmed in (True, False):
                default_decision = resolve_approval_tier(
                    rule.operation_class, confirmed=confirmed, posture=DEFAULT_POSTURE
                )
                strict_decision = resolve_approval_tier(
                    rule.operation_class, confirmed=confirmed, posture=STRICT_POSTURE
                )
                self.assertEqual(default_decision.tier, strict_decision.tier, rule)

    def test_installer_confirmation_override_row_exists_and_defaults_true(self) -> None:
        row = next(row for row in POSTURE_MAPPING if row.key == "installer_confirmation_override_available")
        self.assertEqual(row.strict_value, False)
        self.assertTrue(row.rationale.strip())


class DescribeApprovalTierTableTests(unittest.TestCase):
    def test_schema_and_row_count_match_the_table(self) -> None:
        report = describe_approval_tier_table()
        self.assertEqual(report["schema_version"], APPROVAL_TIER_SCHEMA_VERSION)
        self.assertEqual(report["tiers"], list(APPROVAL_TIERS))
        self.assertEqual(len(report["rows"]), len(APPROVAL_RULE_TABLE))
        self.assertEqual(
            {row["operation_class"] for row in report["rows"]},
            {rule.operation_class for rule in APPROVAL_RULE_TABLE},
        )


def _strict_env():
    return patch.dict(os.environ, {"OMH_SECURITY": "strict"}, clear=False)


class InstallerEnforcementSiteIntegrationTests(unittest.TestCase):
    """`install/installer.py`'s overwrite and removal guards now ask the
    resolver. Default posture reproduces the pre-resolver behavior exactly
    (golden); strict posture is new: `--force` no longer overrides either
    guard."""

    def test_default_posture_force_still_overrides_a_locally_edited_skill(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            install_skill_pack(paths, profile="core")
            # Edit one installed SKILL.md, then re-freeze the manifest so the
            # edit predates it -- the same shape as the existing installer
            # golden test (`test_a_locally_edited_flat_directory_blocks_the_install`).
            skill_md = next(paths.skills_dir.glob("**/SKILL.md"))
            write_manifest(
                paths.manifest_path,
                new_manifest("builtin", paths.skills_dir, skill_records(paths.skills_dir, "builtin")),
            )
            skill_md.write_text("edited after the manifest froze\n", encoding="utf-8")

            with self.assertRaises(OmhError):
                install_skill_pack(paths, profile="core")

            # Golden: force still overrides in default posture, exactly as it
            # did before this resolver existed.
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("OMH_SECURITY", None)
                install_skill_pack(paths, profile="core", force=True)
            self.assertNotEqual(skill_md.read_text(encoding="utf-8"), "edited after the manifest froze\n")

    def test_strict_posture_refuses_the_same_edit_even_with_force(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            install_skill_pack(paths, profile="core")
            skill_md = next(paths.skills_dir.glob("**/SKILL.md"))
            write_manifest(
                paths.manifest_path,
                new_manifest("builtin", paths.skills_dir, skill_records(paths.skills_dir, "builtin")),
            )
            skill_md.write_text("edited after the manifest froze\n", encoding="utf-8")

            with _strict_env():
                with self.assertRaises(OmhError):
                    install_skill_pack(paths, profile="core", force=True)
            self.assertEqual(skill_md.read_text(encoding="utf-8"), "edited after the manifest froze\n")

    def test_default_posture_force_still_removes_an_unowned_plugin_dir(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            paths.hermes_plugin_dir.mkdir(parents=True, exist_ok=True)
            (paths.hermes_plugin_dir / "unrelated.txt").write_text("not an OMH manifest\n", encoding="utf-8")

            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("OMH_SECURITY", None)
                kept_without_force = uninstall_profile_plugin(paths, force=False)
                self.assertTrue(kept_without_force["kept_paths"])
                self.assertTrue(paths.hermes_plugin_dir.exists())

                removed_with_force = uninstall_profile_plugin(paths, force=True)
                self.assertIn(str(paths.hermes_plugin_dir), removed_with_force["removed_paths"])
            self.assertFalse(paths.hermes_plugin_dir.exists())

    def test_strict_posture_keeps_the_unowned_plugin_dir_even_with_force(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            paths.hermes_plugin_dir.mkdir(parents=True, exist_ok=True)
            (paths.hermes_plugin_dir / "unrelated.txt").write_text("not an OMH manifest\n", encoding="utf-8")

            with _strict_env():
                result = uninstall_profile_plugin(paths, force=True)
            self.assertTrue(result["kept_paths"])
            self.assertTrue(paths.hermes_plugin_dir.exists())

    def test_collect_removal_directly_traces_to_the_resolver_for_a_managed_plugin(self) -> None:
        # A dir that DOES carry the manifest is never gated by this rule at
        # all -- unaffected by force or posture.
        with TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp) / "plugin"
            plugin_dir.mkdir()
            (plugin_dir / ".omh-plugin-manifest.json").write_text("{}\n", encoding="utf-8")
            removed: list[str] = []
            would_remove: list[str] = []
            kept: list[dict[str, str]] = []
            with _strict_env():
                _collect_removal(
                    plugin_dir,
                    removed=removed,
                    would_remove=would_remove,
                    kept=kept,
                    dry_run=False,
                    force=False,
                    managed_plugin=True,
                )
            self.assertEqual(kept, [])
            self.assertIn(str(plugin_dir), removed)
            self.assertFalse(plugin_dir.exists())


if __name__ == "__main__":
    unittest.main()
