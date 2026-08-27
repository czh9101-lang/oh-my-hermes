from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import unittest

from _skill_load_observation_support import SkillLoadObservationTestCase
from omh.coding.skill_load_observation import (
    InventoryNonceLedger,
    expected_skills_digest,
    probe_skill_load,
    skill_load_observation_is_fresh,
    validate_skill_load_observation,
)
from omh.evidence.labels import SKILL_LOAD_PROBE_LABELS, SKILL_LOAD_STATE_LABELS
from omh.quality.harness_quality import skill_load_evidence_state


class SkillLoadObservationTests(SkillLoadObservationTestCase):
    def test_fake_protocol_all_partial_none_unexpected_and_not_applicable(self) -> None:
        cases = (
            ("all", ("alpha", "beta"), "all_loaded", (), ()),
            ("partial", ("alpha", "beta"), "partially_loaded", ("beta",), ()),
            ("none", ("alpha", "beta"), "none_loaded", ("alpha", "beta"), ()),
            ("unexpected", ("alpha", "beta"), "all_loaded", (), ("gamma",)),
            ("unexpected-only", ("alpha", "beta"), "none_loaded", ("alpha", "beta"), ("gamma",)),
            ("not-applicable", (), "not_applicable", (), ()),
        )
        for mode, expected, state, missing, unexpected in cases:
            with self.subTest(mode=mode):
                payload = probe_skill_load(self.request(mode, expected), confirmed=True)
                self.assertEqual(payload["probe_status"], "observed")
                self.assertEqual(payload["load_state"], state)
                self.assertEqual(payload["expected_skills"], list(expected))
                self.assertEqual(payload["missing_skills"], list(missing))
                self.assertEqual(payload["unexpected_skills"], list(unexpected))
                self.assertEqual(validate_skill_load_observation(payload), [])

    def test_unsupported_and_process_error_are_inventory_free_and_redacted(self) -> None:
        for mode, status, reason in (
            ("unsupported", "unsupported", "inventory_protocol_unavailable"),
            ("error", "probe_error", "inventory_process_error"),
        ):
            with self.subTest(mode=mode):
                payload = probe_skill_load(self.request(mode), confirmed=True)
                self.assertEqual((payload["probe_status"], payload["reason_code"]), (status, reason))
                for key in ("load_state", "expected_skills", "observed_skills", "missing_skills", "unexpected_skills", "inventory_digest"):
                    self.assertNotIn(key, payload)
                self.assertNotIn("SECRET", json.dumps(payload))

    def test_malformed_binding_duplicate_expiry_and_forbidden_fields_fail_closed(self) -> None:
        reasons = {
            "malformed": "inventory_response_malformed",
            "duplicate": "inventory_response_malformed",
            "nonce-mismatch": "inventory_nonce_mismatch",
            "digest-mismatch": "inventory_expected_digest_mismatch",
            "inventory-digest-mismatch": "inventory_digest_mismatch",
            "tool-mismatch": "inventory_response_malformed",
            "expired": "inventory_response_expired",
            "stale": "inventory_response_stale",
            "forbidden": "inventory_response_malformed",
        }
        for mode, reason in reasons.items():
            with self.subTest(mode=mode):
                payload = probe_skill_load(self.request(mode), confirmed=True)
                self.assertEqual(payload["probe_status"], "probe_error")
                self.assertEqual(payload["reason_code"], reason)
                self.assertNotIn("observed_skills", payload)
                self.assertNotIn("SECRET", json.dumps(payload))

    def test_replay_is_rejected_by_bounded_nonce_ledger(self) -> None:
        ledger = InventoryNonceLedger(capacity=2)
        fixed_nonce = "a" * 64
        first = probe_skill_load(self.request("all", nonce=fixed_nonce), confirmed=True, nonce_ledger=ledger)
        second = probe_skill_load(self.request("all", nonce=fixed_nonce), confirmed=True, nonce_ledger=ledger)
        self.assertEqual(first["probe_status"], "observed")
        self.assertEqual((second["probe_status"], second["reason_code"]), ("probe_error", "inventory_nonce_replayed"))

    def test_observation_expiry_and_schema_forbidden_keys(self) -> None:
        payload = probe_skill_load(self.request("all"), confirmed=True)
        self.assertTrue(skill_load_observation_is_fresh(payload))
        future = datetime.now(timezone.utc) + timedelta(days=1)
        self.assertFalse(skill_load_observation_is_fresh(payload, now=future))
        for forbidden in ("prompt", "raw_log", "credential", "cpu", "ram", "resource_score"):
            errors = validate_skill_load_observation({**payload, forbidden: "x"})
            self.assertTrue(errors, forbidden)

    def test_expected_digest_is_sorted_and_stable(self) -> None:
        self.assertEqual(expected_skills_digest(("beta", "alpha")), expected_skills_digest(("alpha", "beta")))
        self.assertEqual(len(expected_skills_digest(("alpha",))), 64)

    def test_labels_and_harness_consumer_preserve_probe_axes(self) -> None:
        observed = probe_skill_load(self.request("partial"), confirmed=True)
        unsupported = probe_skill_load(self.request("unsupported"), confirmed=True)
        self.assertEqual(skill_load_evidence_state(observed), "partially_loaded")
        self.assertEqual(skill_load_evidence_state(unsupported), "unsupported")
        self.assertEqual(set(SKILL_LOAD_PROBE_LABELS), {"observed", "unsupported", "probe_error"})
        self.assertEqual(set(SKILL_LOAD_STATE_LABELS), {"all_loaded", "partially_loaded", "none_loaded", "not_applicable"})


if __name__ == "__main__":
    unittest.main()
