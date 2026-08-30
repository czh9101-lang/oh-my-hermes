"""Named strict security posture: the resolver, the mapping table, and the
`OMH_SECURITY` env-var contract (`OMH_LANG`/`normalize_language` idiom).

`P3 -- 15. Named strict security posture`: a single `OMH_SECURITY=strict`
switch bundles the conservative end of OMH's already-existing safety knobs.
These tests pin the resolver in isolation; the per-surface wiring (fanout
concurrency/retries, verification escalation, the loop stop ladder) is
covered where each surface already has its own test module.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()

from omh.coding.hermes_child_dispatch import (  # noqa: E402
    DispatchConfirmationError,
    require_hermes_child_dispatch_boundary,
)
from omh.maintenance.doctor import _security_posture_check  # noqa: E402
from omh.quality.completion_integrity import classify_completion_integrity  # noqa: E402
from omh.system.security_posture import (  # noqa: E402
    DEFAULT_POSTURE,
    POSTURE_MAPPING,
    SECURITY_POSTURE_ENV_VAR,
    SECURITY_POSTURE_SCHEMA_VERSION,
    STRICT_POSTURE,
    VALID_POSTURES,
    describe_security_posture,
    resolve_security_posture,
    security_posture_value,
    strict_override,
)


class ResolvePostureTests(unittest.TestCase):
    def test_env_var_name_and_valid_choices(self) -> None:
        self.assertEqual(SECURITY_POSTURE_ENV_VAR, "OMH_SECURITY")
        self.assertEqual(VALID_POSTURES, (DEFAULT_POSTURE, STRICT_POSTURE))

    def test_unset_reads_as_default(self) -> None:
        self.assertEqual(resolve_security_posture({}), DEFAULT_POSTURE)

    def test_blank_reads_as_default(self) -> None:
        self.assertEqual(resolve_security_posture({"OMH_SECURITY": "  "}), DEFAULT_POSTURE)

    def test_strict_is_recognized_case_and_whitespace_insensitively(self) -> None:
        self.assertEqual(resolve_security_posture({"OMH_SECURITY": "strict"}), STRICT_POSTURE)
        self.assertEqual(resolve_security_posture({"OMH_SECURITY": " STRICT "}), STRICT_POSTURE)

    def test_explicit_default_is_recognized(self) -> None:
        self.assertEqual(resolve_security_posture({"OMH_SECURITY": "default"}), DEFAULT_POSTURE)

    def test_an_unrecognized_value_is_rejected_loudly(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            resolve_security_posture({"OMH_SECURITY": "paranoid"})
        message = str(ctx.exception)
        self.assertIn("OMH_SECURITY", message)
        self.assertIn("paranoid", message)
        self.assertIn("default", message)
        self.assertIn("strict", message)


class MappingTableTests(unittest.TestCase):
    """The mapping table lives in exactly one place, with a rationale per row."""

    def test_every_row_names_a_surface_and_a_non_empty_rationale(self) -> None:
        self.assertTrue(POSTURE_MAPPING)
        keys = [row.key for row in POSTURE_MAPPING]
        self.assertEqual(len(keys), len(set(keys)), "mapping-table keys must be unique")
        for row in POSTURE_MAPPING:
            self.assertTrue(row.surface.startswith("src/"), row)
            self.assertTrue(row.rationale.strip(), row)

    def test_security_posture_value_looks_up_the_strict_value(self) -> None:
        self.assertEqual(security_posture_value("fanout_max_retries"), 0)

    def test_security_posture_value_rejects_an_unknown_key(self) -> None:
        with self.assertRaises(KeyError):
            security_posture_value("not_a_real_key")


class StrictOverrideTests(unittest.TestCase):
    def test_default_posture_returns_the_callers_own_default_unchanged(self) -> None:
        self.assertEqual(strict_override("fanout_max_retries", DEFAULT_POSTURE, 2), 2)
        self.assertEqual(strict_override("loop_no_progress_cap", DEFAULT_POSTURE, 2), 2)

    def test_strict_posture_returns_the_mapped_value_regardless_of_default(self) -> None:
        self.assertEqual(strict_override("fanout_max_retries", STRICT_POSTURE, 2), 0)
        self.assertEqual(strict_override("loop_no_progress_cap", STRICT_POSTURE, 2), 1)


class DescribeSecurityPostureTests(unittest.TestCase):
    def test_schema_and_row_count_match_the_mapping_table(self) -> None:
        report = describe_security_posture({})
        self.assertEqual(report["schema_version"], SECURITY_POSTURE_SCHEMA_VERSION)
        self.assertEqual(report["posture"], DEFAULT_POSTURE)
        self.assertEqual(len(report["rows"]), len(POSTURE_MAPPING))
        self.assertFalse(any(row["active_when_strict"] for row in report["rows"]))

    def test_strict_report_flags_every_row_active(self) -> None:
        report = describe_security_posture({"OMH_SECURITY": "strict"})
        self.assertEqual(report["posture"], STRICT_POSTURE)
        self.assertTrue(all(row["active_when_strict"] for row in report["rows"]))

    def test_an_invalid_env_value_raises_before_building_the_report(self) -> None:
        with self.assertRaises(ValueError):
            describe_security_posture({"OMH_SECURITY": "bogus"})


class InvariantRowTests(unittest.TestCase):
    """Two `POSTURE_MAPPING` rows tighten nothing: `completion_integrity_refusal_overridable`
    and `dispatch_confirmation_required` are already non-overridable/always-required in
    `default` posture, and the row exists to document the invariant rather than change it.
    These are the closest thing to an equivalence proof a no-op row can carry: neither
    consuming function accepts a posture, an override, or a bypass argument at all.
    """

    def test_completion_integrity_has_no_override_or_bypass_argument(self) -> None:
        import inspect

        params = inspect.signature(classify_completion_integrity).parameters
        self.assertNotIn("override", params)
        self.assertNotIn("bypass", params)
        self.assertNotIn("posture", params)

    def test_completion_integrity_refuses_a_placeholder_evidence_entry_regardless_of_posture(self) -> None:
        for env in ({}, {"OMH_SECURITY": "strict"}):
            with patch.dict(os.environ, env, clear=False):
                if not env:
                    os.environ.pop("OMH_SECURITY", None)
                result = classify_completion_integrity(evidence=["TBD"])
            self.assertTrue(result["refused"])
            self.assertIn("empty_evidence", result["categories"])

    def test_dispatch_confirmation_is_required_regardless_of_posture(self) -> None:
        for env in ({}, {"OMH_SECURITY": "strict"}):
            with patch.dict(os.environ, env, clear=False):
                if not env:
                    os.environ.pop("OMH_SECURITY", None)
                with self.assertRaises(DispatchConfirmationError):
                    require_hermes_child_dispatch_boundary(dispatch_policy="prepare_only", confirmed=False)
                with self.assertRaises(DispatchConfirmationError):
                    require_hermes_child_dispatch_boundary(dispatch_policy="ask_before_dispatch", confirmed=False)
                # No exception: explicit confirmation with the required policy passes.
                require_hermes_child_dispatch_boundary(dispatch_policy="ask_before_dispatch", confirmed=True)


class DoctorSecurityPostureCheckTests(unittest.TestCase):
    """`omh doctor` surfaces the active posture (`_security_posture_check`)."""

    def test_default_posture_is_an_ok_informational_check(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OMH_SECURITY", None)
            check = _security_posture_check()
        self.assertTrue(check.ok)
        self.assertEqual(check.severity, "ok")
        self.assertIn("default", check.message)

    def test_strict_posture_is_named_in_the_message(self) -> None:
        with patch.dict(os.environ, {"OMH_SECURITY": "strict"}):
            check = _security_posture_check()
        self.assertTrue(check.ok)
        self.assertIn("strict", check.message)

    def test_an_unrecognized_value_fails_the_check_with_the_valid_choices(self) -> None:
        with patch.dict(os.environ, {"OMH_SECURITY": "paranoid"}):
            check = _security_posture_check()
        self.assertFalse(check.ok)
        self.assertIn("paranoid", check.message)
        self.assertIn("default", check.message)
        self.assertIn("strict", check.message)


if __name__ == "__main__":
    unittest.main()
