"""Contracts for `session_activity_receipt/v1` (issue #1404).

What is asserted here, against the issue's success criteria:

- the schema and validator define identity, boundary, bounded counters,
  availability, and evidence-reference semantics
- a partial (turn) receipt is never reported as a terminal session result
- ingestion is idempotent by receipt identity; stale, cross-profile, and
  identity-conflicting payloads are quarantined, never stored
- a missing metric stays `unavailable`; it is never zero
- exposure and activation are separate, and exposed-but-unused is derived
  only when both were observed
- hostile or oversized arrays and strings are bounded without retaining raw
  material
- six named consumers ingest the same fixture and keep observed, heuristic,
  and unavailable readings apart
- a producer loaded mid-session reports a floor, never an exact total
"""

from __future__ import annotations

import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from _cli_harness import run_cli
from _local_package import load_local_package

load_local_package()
from omh.paths import resolve_paths  # noqa: E402
from omh.skills.catalog import installable_skill_names  # noqa: E402
from omh.skills.render import workflow_reference_markdown  # noqa: E402
from omh.system.append_only_store import RAW_OR_HIDDEN_KEYS  # noqa: E402
from omh.workflows.session_activity_receipts import (  # noqa: E402
    CLAIM_BOUNDARY,
    MAX_EVIDENCE_REFS,
    MAX_SKILL_NAMES,
    METRIC_NAMES,
    QUARANTINE_REASONS,
    RAW_SESSION_KEYS,
    REASON_CROSS_PROFILE,
    REASON_IDENTITY_CONFLICT,
    REASON_STALE_AFTER_FINAL,
    REASON_STALE_AGE,
    REASON_STALE_SEQUENCE,
    SESSION_ACTIVITY_CONSUMERS,
    SESSION_ACTIVITY_RECEIPT_SCHEMA_VERSION,
    SessionActivityReceiptError,
    admit_session_activity_receipt,
    attach_session_activity_evidence,
    ingest_session_activity_receipt,
    latest_session_activity_receipt,
    metric_reading,
    read_session_activity_quarantine,
    read_session_activity_receipts,
    session_activity_evidence,
    skill_exposure_summary,
    validate_session_activity_receipt,
    validate_session_activity_receipt_store,
)


def _reading(value: int, availability: str = "observed", measurement: str = "exact") -> dict[str, Any]:
    return {"value": value, "availability": availability, "measurement": measurement}


def _payload(**overrides: Any) -> dict[str, Any]:
    """A final, full-session receipt as an observer plugin would supply it."""
    payload: dict[str, Any] = {
        "schema_version": SESSION_ACTIVITY_RECEIPT_SCHEMA_VERSION,
        "receipt_id": "sar-20260908-0001",
        "producer": {"kind": "observer_plugin", "ref": "hermes-observer", "version": "0.3.0"},
        "profile_ref": "profile-a1",
        "session_ref": "sess-9f2c4a",
        "boundary": {"kind": "session_end", "final": True, "sequence": 12},
        "observed_interval": {
            "started_at": "2026-09-08T09:00:00Z",
            "ended_at": "2026-09-08T09:42:10Z",
            "coverage": "full_session",
        },
        "metrics": {
            "skills_exposed": _reading(4),
            "skills_activated": _reading(1),
            "tool_calls": _reading(31),
            "tool_errors": _reading(2, availability="heuristic"),
            "compaction_boundaries": _reading(1),
            "tokens_total": _reading(48210),
            "context_peak_tokens": _reading(91000),
            "wall_clock_ms": _reading(2530000),
        },
        "skills": {
            "exposed_names": ["plan", "research", "review", "wiki"],
            "activated_names": ["plan"],
            "exposed_names_truncated": False,
            "activated_names_truncated": False,
            "name_form": "plain",
        },
        "model_refs": ["glm-5.3"],
        "evidence_refs": ["observer:session:sess-9f2c4a:end"],
        "observed_at": "2026-09-08T09:42:11Z",
    }
    payload.update(overrides)
    return payload


def _turn_payload(sequence: int, receipt_id: str) -> dict[str, Any]:
    payload = _payload(receipt_id=receipt_id)
    payload["boundary"] = {"kind": "turn", "final": False, "sequence": sequence}
    payload["observed_interval"]["ended_at"] = "2026-09-08T09:10:00Z"
    return payload


class TheSchemaDefinesIdentityBoundaryCountersAndAvailability(unittest.TestCase):
    def test_a_supplied_final_receipt_validates_and_keeps_its_readings(self) -> None:
        admission = admit_session_activity_receipt(_payload())

        self.assertEqual(admission["outcome"], "accepted", admission["errors"])
        receipt = admission["receipt"]
        self.assertEqual(validate_session_activity_receipt(receipt), [])
        self.assertEqual(receipt["privacy"], "metadata_only")
        self.assertEqual(receipt["claim_boundary"], CLAIM_BOUNDARY)
        self.assertEqual(metric_reading(receipt, "tool_calls"), _reading(31))
        self.assertEqual(metric_reading(receipt, "tool_errors")["availability"], "heuristic")

    def test_every_string_is_a_closed_value_or_an_opaque_reference(self) -> None:
        for field, value in (
            ("receipt_id", "https://host/receipt?1"),
            ("profile_ref", "sk-live-abcdefghijklmnop"),
            ("session_ref", "/Users/someone/.hermes/sessions/1"),
            ("observed_at", "2026-09-08 09:42"),
        ):
            with self.subTest(field=field):
                admission = admit_session_activity_receipt(_payload(**{field: value}))
                self.assertEqual(admission["outcome"], "rejected")
                self.assertTrue(any(field in error for error in admission["errors"]), admission["errors"])
                self.assertNotIn(value, json.dumps(admission))

    def test_counters_are_bounded_integers_and_unknown_metric_names_are_refused(self) -> None:
        oversized = _payload()
        oversized["metrics"]["tool_calls"] = _reading(10**13)
        negative = _payload()
        negative["metrics"]["tool_calls"] = _reading(-1)
        boolean = _payload()
        boolean["metrics"]["tool_calls"] = _reading(True)
        unknown = _payload()
        unknown["metrics"]["raw_prompt_tokens"] = _reading(1)
        for name, payload in (("oversized", oversized), ("negative", negative), ("boolean", boolean), ("unknown", unknown)):
            with self.subTest(payload=name):
                self.assertEqual(admit_session_activity_receipt(payload)["outcome"], "rejected")

    def test_the_producer_and_boundary_vocabularies_are_closed(self) -> None:
        producer = _payload(producer={"kind": "crawler", "ref": "x", "version": ""})
        boundary = _payload()
        boundary["boundary"]["kind"] = "guess"
        coverage = _payload()
        coverage["observed_interval"]["coverage"] = "most_of_it"
        for name, payload in (("producer", producer), ("boundary", boundary), ("coverage", coverage)):
            with self.subTest(payload=name):
                admission = admit_session_activity_receipt(payload)
                self.assertEqual(admission["outcome"], "rejected")
                self.assertTrue(any("unsupported" in error for error in admission["errors"]), admission["errors"])

    def test_an_interval_that_ends_before_it_starts_is_refused(self) -> None:
        payload = _payload()
        payload["observed_interval"]["ended_at"] = "2026-09-08T08:59:59Z"
        admission = admit_session_activity_receipt(payload)
        self.assertEqual(admission["outcome"], "rejected")
        self.assertTrue(any("ended_at must not precede" in error for error in admission["errors"]))


class APartialReceiptIsNeverATerminalResult(unittest.TestCase):
    def test_a_turn_boundary_cannot_claim_final(self) -> None:
        payload = _payload()
        payload["boundary"] = {"kind": "turn", "final": True, "sequence": 3}
        admission = admit_session_activity_receipt(payload)
        self.assertEqual(admission["outcome"], "rejected")
        self.assertTrue(any("only allowed on a session_end boundary" in error for error in admission["errors"]))

    def test_a_process_exit_is_the_producers_boundary_not_the_sessions(self) -> None:
        payload = _payload()
        payload["boundary"] = {"kind": "process_exit", "final": True, "sequence": 3}
        self.assertEqual(admit_session_activity_receipt(payload)["outcome"], "rejected")

    def test_a_partial_receipt_projects_as_partial_for_every_consumer(self) -> None:
        receipt = admit_session_activity_receipt(_turn_payload(3, "sar-turn-3"))["receipt"]
        for consumer in SESSION_ACTIVITY_CONSUMERS:
            with self.subTest(consumer=consumer):
                evidence = session_activity_evidence(receipt, consumer)
                self.assertFalse(evidence["terminal"])
                self.assertEqual(evidence["session_outcome"], "partial")
                self.assertEqual(evidence["boundary_kind"], "turn")

    def test_a_final_receipt_projects_as_final(self) -> None:
        receipt = admit_session_activity_receipt(_payload())["receipt"]
        evidence = session_activity_evidence(receipt, "agent-ops-review")
        self.assertTrue(evidence["terminal"])
        self.assertEqual(evidence["session_outcome"], "final")


class AProducerLoadedMidSessionReportsAFloor(unittest.TestCase):
    def test_an_exact_counter_under_partial_coverage_is_refused(self) -> None:
        payload = _payload()
        payload["observed_interval"]["coverage"] = "from_producer_load"
        admission = admit_session_activity_receipt(payload)
        self.assertEqual(admission["outcome"], "rejected")
        self.assertTrue(any("reports a floor" in error for error in admission["errors"]), admission["errors"])

    def test_floor_counters_under_partial_coverage_are_accepted_and_flagged(self) -> None:
        payload = _payload()
        payload["observed_interval"]["coverage"] = "from_producer_load"
        for reading in payload["metrics"].values():
            reading["measurement"] = "floor"
        admission = admit_session_activity_receipt(payload)
        self.assertEqual(admission["outcome"], "accepted", admission["errors"])
        evidence = session_activity_evidence(admission["receipt"], "run-efficiency")
        self.assertTrue(evidence["observation_floor"])
        self.assertEqual(evidence["coverage"], "from_producer_load")
        self.assertEqual(evidence["metrics"]["tool_calls"]["measurement"], "floor")
        exposure = skill_exposure_summary(admission["receipt"])
        self.assertEqual(exposure["derivation"], "names")

    def test_a_restarted_producer_that_cannot_say_reports_unknown_coverage_as_a_floor(self) -> None:
        payload = _payload()
        payload["observed_interval"]["coverage"] = "unknown"
        payload["metrics"] = {"tool_calls": _reading(5, measurement="floor")}
        admission = admit_session_activity_receipt(payload)
        self.assertEqual(admission["outcome"], "accepted", admission["errors"])
        self.assertTrue(session_activity_evidence(admission["receipt"], "run-efficiency")["observation_floor"])


class AMissingMetricStaysUnavailable(unittest.TestCase):
    def test_an_absent_metric_reads_as_unavailable_with_a_null_value(self) -> None:
        receipt = admit_session_activity_receipt(_payload())["receipt"]
        self.assertEqual(metric_reading(receipt, "subagent_spawns"), {"value": None, "availability": "unavailable", "measurement": ""})
        evidence = session_activity_evidence(receipt, "agent-ops-review")
        self.assertIn("subagent_spawns", evidence["unavailable_metrics"])
        self.assertIsNone(evidence["metrics"]["subagent_spawns"]["value"])
        self.assertNotEqual(evidence["metrics"]["subagent_spawns"]["value"], 0)

    def test_an_explicit_unavailable_reading_must_carry_a_null_value(self) -> None:
        payload = _payload()
        payload["metrics"]["subagent_spawns"] = {"value": 0, "availability": "unavailable", "measurement": ""}
        admission = admit_session_activity_receipt(payload)
        self.assertEqual(admission["outcome"], "rejected")
        self.assertTrue(any("must carry a null value" in error for error in admission["errors"]))

    def test_a_heuristic_counter_is_kept_apart_from_observed_ones(self) -> None:
        receipt = admit_session_activity_receipt(_payload())["receipt"]
        evidence = session_activity_evidence(receipt, "agent-ops-review")
        self.assertEqual(evidence["heuristic_metrics"], ["tool_errors"])
        self.assertNotIn("tool_errors", evidence["observed_metrics"])
        self.assertIn("tool_calls", evidence["observed_metrics"])
        self.assertEqual(evidence["metrics"]["tool_errors"]["availability"], "heuristic")


class ExposureAndActivationAreSeparate(unittest.TestCase):
    def test_exposed_but_unused_is_derived_from_complete_name_lists(self) -> None:
        receipt = admit_session_activity_receipt(_payload())["receipt"]
        exposure = skill_exposure_summary(receipt)
        self.assertEqual(exposure["derivation"], "names")
        self.assertEqual(exposure["exposed_unused_count"], 3)
        self.assertEqual(exposure["exposed_unused_names"], ["research", "review", "wiki"])

    def test_exposed_but_unused_falls_back_to_counters_when_a_list_is_truncated(self) -> None:
        payload = _payload()
        payload["skills"]["exposed_names_truncated"] = True
        receipt = admit_session_activity_receipt(payload)["receipt"]
        exposure = skill_exposure_summary(receipt)
        self.assertEqual(exposure["derivation"], "counters")
        self.assertEqual(exposure["exposed_unused_count"], 3)
        self.assertEqual(exposure["exposed_unused_names"], [])

    def test_exposed_but_unused_is_unavailable_when_exposure_was_not_observed(self) -> None:
        payload = _payload()
        del payload["metrics"]["skills_exposed"]
        payload["skills"]["exposed_names"] = []
        receipt = admit_session_activity_receipt(payload)["receipt"]
        exposure = skill_exposure_summary(receipt)
        self.assertEqual(exposure["derivation"], "unavailable")
        self.assertIsNone(exposure["exposed_unused_count"])
        self.assertIn("both be observed", exposure["reason"])
        self.assertEqual(exposure["skills_activated"]["value"], 1)

    def test_exposed_but_unused_is_unavailable_when_activation_was_not_observed(self) -> None:
        payload = _payload()
        del payload["metrics"]["skills_activated"]
        payload["skills"]["activated_names"] = []
        exposure = skill_exposure_summary(admit_session_activity_receipt(payload)["receipt"])
        self.assertEqual(exposure["derivation"], "unavailable")

    def test_disagreeing_counters_do_not_produce_a_negative_finding(self) -> None:
        payload = _payload()
        payload["metrics"]["skills_activated"] = _reading(9)
        payload["skills"]["exposed_names_truncated"] = True
        exposure = skill_exposure_summary(admit_session_activity_receipt(payload)["receipt"])
        self.assertEqual(exposure["derivation"], "unavailable")
        self.assertIn("disagree", exposure["reason"])


class HostileInputIsBoundedWithoutRetainingRawMaterial(unittest.TestCase):
    def test_raw_session_material_keys_are_refused_by_name(self) -> None:
        for key in sorted(RAW_OR_HIDDEN_KEYS | RAW_SESSION_KEYS):
            with self.subTest(key=key):
                admission = admit_session_activity_receipt(_payload(**{key: "do not store"}))
                self.assertEqual(admission["outcome"], "rejected")
                self.assertTrue(any("raw session material" in error for error in admission["errors"]), admission["errors"])
                self.assertNotIn("do not store", json.dumps(admission))

    def test_nested_raw_keys_are_refused_too(self) -> None:
        payload = _payload()
        payload["skills"]["prompt"] = "the user said"
        admission = admit_session_activity_receipt(payload)
        self.assertEqual(admission["outcome"], "rejected")
        self.assertNotIn("the user said", json.dumps(admission))

    def test_path_shaped_and_secret_shaped_skill_names_become_digests(self) -> None:
        payload = _payload()
        payload["skills"]["exposed_names"] = ["plan", "/Users/someone/.hermes/skills/private", "sk-live-abcdefghijklmnop", "with space"]
        payload["skills"]["activated_names"] = ["plan"]
        admission = admit_session_activity_receipt(payload)
        self.assertEqual(admission["outcome"], "accepted", admission["errors"])
        names = admission["receipt"]["skills"]["exposed_names"]
        self.assertEqual(names[0], "plan")
        for name in names[1:]:
            self.assertTrue(name.startswith("ref-"), name)
        serialized = json.dumps(admission)
        self.assertNotIn("/Users/", serialized)
        self.assertNotIn("sk-live", serialized)
        self.assertNotIn("with space", serialized)

    def test_redact_skill_names_folds_every_name_to_a_digest(self) -> None:
        admission = admit_session_activity_receipt(_payload(), redact_skill_names=True)
        receipt = admission["receipt"]
        self.assertEqual(receipt["skills"]["name_form"], "digest")
        self.assertTrue(all(name.startswith("ref-") for name in receipt["skills"]["exposed_names"]))
        self.assertNotIn("research", json.dumps(receipt))
        # Digests are stable, so exposed-but-unused still derives across them.
        self.assertEqual(skill_exposure_summary(receipt)["exposed_unused_count"], 3)

    def test_oversized_name_lists_are_truncated_and_marked(self) -> None:
        payload = _payload()
        payload["skills"]["exposed_names"] = [f"skill-{index}" for index in range(MAX_SKILL_NAMES + 20)]
        payload["metrics"]["skills_exposed"] = _reading(MAX_SKILL_NAMES + 20)
        admission = admit_session_activity_receipt(payload)
        self.assertEqual(admission["outcome"], "accepted", admission["errors"])
        skills = admission["receipt"]["skills"]
        self.assertEqual(len(skills["exposed_names"]), MAX_SKILL_NAMES)
        self.assertTrue(skills["exposed_names_truncated"])
        # A truncated list is not a complete set, so the derivation falls back
        # to the counters instead of naming an incomplete difference.
        self.assertEqual(skill_exposure_summary(admission["receipt"])["derivation"], "counters")

    def test_oversized_evidence_refs_are_capped_and_links_become_digests(self) -> None:
        payload = _payload(evidence_refs=[f"observer:event:{index}" for index in range(MAX_EVIDENCE_REFS + 5)] + ["https://host/log?x=1"])
        receipt = admit_session_activity_receipt(payload)["receipt"]
        self.assertEqual(len(receipt["evidence_refs"]), MAX_EVIDENCE_REFS)
        linked = admit_session_activity_receipt(_payload(evidence_refs=["https://host/log?x=1"]))["receipt"]
        self.assertTrue(linked["evidence_refs"][0].startswith("ref-"))
        self.assertNotIn("host/log", json.dumps(linked))

    def test_a_control_character_in_a_model_ref_is_not_stored_as_typed(self) -> None:
        receipt = admit_session_activity_receipt(_payload(model_refs=["glm-5.3\x1b[2K"]))["receipt"]
        self.assertTrue(receipt["model_refs"][0].startswith("ref-"))
        self.assertNotIn("\x1b", json.dumps(receipt))

    def test_a_non_object_payload_is_rejected_by_name(self) -> None:
        admission = admit_session_activity_receipt("not a receipt")  # type: ignore[arg-type]
        self.assertEqual(admission["outcome"], "rejected")
        self.assertIsNone(admission["receipt"])


class IngestionIsIdempotentAndQuarantinesWhatItMustNotKeep(unittest.TestCase):
    def test_the_same_receipt_sent_three_times_is_one_line(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            outcomes = [ingest_session_activity_receipt(paths, _payload())["outcome"] for _ in range(3)]
            self.assertEqual(outcomes, ["recorded", "already_recorded", "already_recorded"])
            self.assertEqual(len(read_session_activity_receipts(paths)), 1)
            self.assertEqual(read_session_activity_quarantine(paths), [])

    def test_a_resend_without_observed_at_is_still_the_same_receipt(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            ingest_session_activity_receipt(paths, _payload())
            resend = _payload()
            del resend["observed_at"]
            self.assertEqual(ingest_session_activity_receipt(paths, resend)["outcome"], "already_recorded")

    def test_a_reused_identity_with_a_different_observation_is_quarantined(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            ingest_session_activity_receipt(paths, _payload())
            conflicting = _payload()
            conflicting["metrics"]["tool_calls"] = _reading(99)
            result = ingest_session_activity_receipt(paths, conflicting)
            self.assertEqual(result["outcome"], "quarantined")
            self.assertEqual(result["reason"], REASON_IDENTITY_CONFLICT)
            self.assertEqual(len(read_session_activity_receipts(paths)), 1)
            self.assertEqual(read_session_activity_receipts(paths)[0]["metrics"]["tool_calls"]["value"], 31)
            quarantine = read_session_activity_quarantine(paths)
            self.assertEqual(len(quarantine), 1)
            self.assertEqual(quarantine[0]["reason"], REASON_IDENTITY_CONFLICT)

    def test_a_cross_profile_receipt_is_quarantined_not_stored(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            result = ingest_session_activity_receipt(paths, _payload(profile_ref="profile-b2"), expected_profile_ref="profile-a1")
            self.assertEqual(result["outcome"], "quarantined")
            self.assertEqual(result["reason"], REASON_CROSS_PROFILE)
            self.assertEqual(read_session_activity_receipts(paths), [])
            quarantine = read_session_activity_quarantine(paths)
            self.assertEqual(quarantine[0]["profile_ref"], "profile-b2")
            self.assertEqual(set(quarantine[0]), {
                "schema_version", "quarantined_at", "reason", "errors", "receipt_id", "session_ref", "profile_ref", "claim_boundary",
            })

    def test_a_receipt_after_the_sessions_final_receipt_is_stale(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            ingest_session_activity_receipt(paths, _payload())
            result = ingest_session_activity_receipt(paths, _turn_payload(13, "sar-late-turn"))
            self.assertEqual(result["outcome"], "quarantined")
            self.assertEqual(result["reason"], REASON_STALE_AFTER_FINAL)

    def test_an_earlier_boundary_arriving_after_a_later_one_is_stale(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            self.assertEqual(ingest_session_activity_receipt(paths, _turn_payload(5, "sar-turn-5"))["outcome"], "recorded")
            result = ingest_session_activity_receipt(paths, _turn_payload(4, "sar-turn-4"))
            self.assertEqual(result["outcome"], "quarantined")
            self.assertEqual(result["reason"], REASON_STALE_SEQUENCE)
            self.assertEqual(ingest_session_activity_receipt(paths, _turn_payload(6, "sar-turn-6"))["outcome"], "recorded")
            latest = latest_session_activity_receipt(read_session_activity_receipts(paths), "sess-9f2c4a")
            self.assertEqual(latest["receipt_id"], "sar-turn-6")

    def test_an_interval_older_than_the_freshness_window_is_stale(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            result = ingest_session_activity_receipt(paths, _payload(), now="2026-09-09T09:42:10Z", max_age_seconds=3600)
            self.assertEqual(result["outcome"], "quarantined")
            self.assertEqual(result["reason"], REASON_STALE_AGE)
            fresh = ingest_session_activity_receipt(paths, _payload(), now="2026-09-08T10:00:00Z", max_age_seconds=3600)
            self.assertEqual(fresh["outcome"], "recorded")

    def test_a_rejected_payload_reaches_nothing_on_disk(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            result = ingest_session_activity_receipt(paths, _payload(prompt="never"))
            self.assertEqual(result["outcome"], "rejected")
            self.assertFalse(paths.runtime_session_activity_receipts_path.exists())
            self.assertEqual(read_session_activity_quarantine(paths), [])

    def test_the_store_validator_reports_a_hand_edited_record_and_a_reused_identity(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            ingest_session_activity_receipt(paths, _payload())
            self.assertTrue(validate_session_activity_receipt_store(paths.runtime_session_activity_receipts_path)["ok"])
            edited = deepcopy(read_session_activity_receipts(paths)[0])
            edited["metrics"]["tool_calls"]["value"] = "many"
            with paths.runtime_session_activity_receipts_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(edited, sort_keys=True) + "\n")
            report = validate_session_activity_receipt_store(paths.runtime_session_activity_receipts_path)
            self.assertFalse(report["ok"])
            self.assertEqual(report["receipt_count"], 2)
            self.assertTrue(any("different observation" in error for error in report["errors"]))


class SixNamedConsumersReadOneFixture(unittest.TestCase):
    def test_every_consumer_reads_only_its_metrics_and_keeps_the_boundaries(self) -> None:
        receipt = admit_session_activity_receipt(_payload())["receipt"]
        self.assertGreaterEqual(len(SESSION_ACTIVITY_CONSUMERS), 3)
        for consumer, names in SESSION_ACTIVITY_CONSUMERS.items():
            with self.subTest(consumer=consumer):
                evidence = session_activity_evidence(receipt, consumer)
                self.assertEqual(evidence["schema_version"], "session_activity_evidence/v1")
                self.assertEqual(evidence["consumer"], consumer)
                self.assertEqual(tuple(evidence["metrics"]), names)
                self.assertTrue(set(names) <= set(METRIC_NAMES))
                self.assertEqual(
                    sorted(evidence["observed_metrics"] + evidence["heuristic_metrics"] + evidence["unavailable_metrics"]),
                    sorted(names),
                )
                self.assertEqual(evidence["claim_boundary"], CLAIM_BOUNDARY)
                self.assertFalse(evidence["observation_floor"])
                for name in evidence["unavailable_metrics"]:
                    self.assertIsNone(evidence["metrics"][name]["value"])

    def test_skill_facing_consumers_carry_the_exposure_summary_and_others_do_not(self) -> None:
        receipt = admit_session_activity_receipt(_payload())["receipt"]
        for consumer, names in SESSION_ACTIVITY_CONSUMERS.items():
            with self.subTest(consumer=consumer):
                evidence = session_activity_evidence(receipt, consumer)
                self.assertEqual("skill_exposure" in evidence, "skills_exposed" in names)
        self.assertEqual(session_activity_evidence(receipt, "skill-health")["skill_exposure"]["exposed_unused_count"], 3)

    def test_every_consumer_is_an_installable_skill_whose_guidance_names_the_receipt(self) -> None:
        installable = set(installable_skill_names())
        reference = workflow_reference_markdown()
        for consumer in SESSION_ACTIVITY_CONSUMERS:
            with self.subTest(consumer=consumer):
                self.assertIn(consumer, installable)
                # A workflow's reference sections are the `### <name>` blocks up
                # to the next heading; at least one must name the receipt.
                sections = [part.split("\n#", 1)[0] for part in reference.split(f"\n### {consumer}\n")[1:]]
                self.assertTrue(sections)
                self.assertTrue(any("`session_activity_receipt/v1`" in section for section in sections))

    def test_quarantine_reasons_stay_inside_the_closed_vocabulary(self) -> None:
        for reason in (REASON_CROSS_PROFILE, REASON_STALE_AFTER_FINAL, REASON_STALE_SEQUENCE, REASON_STALE_AGE, REASON_IDENTITY_CONFLICT):
            self.assertIn(reason, QUARANTINE_REASONS)

    def test_an_unknown_consumer_and_an_invalid_receipt_are_refused(self) -> None:
        receipt = admit_session_activity_receipt(_payload())["receipt"]
        with self.assertRaises(SessionActivityReceiptError):
            session_activity_evidence(receipt, "transcript-scraper")
        with self.assertRaises(SessionActivityReceiptError):
            session_activity_evidence({"schema_version": "nonsense"}, "achievements")

    def test_attaching_no_receipt_records_that_none_was_supplied(self) -> None:
        payload = attach_session_activity_evidence({"summary": {}}, None, "achievements")
        self.assertIsNone(payload["session_activity"])
        receipt = admit_session_activity_receipt(_payload())["receipt"]
        attached = attach_session_activity_evidence({"summary": {}}, receipt, "achievements")
        self.assertEqual(attached["session_activity"]["consumer"], "achievements")


class TheCommandLineValidatesWhatItIsHandedAndCollectsNothing(unittest.TestCase):
    def test_ingest_records_once_projects_consumers_and_lists(self) -> None:
        with TemporaryDirectory() as tmp:
            omh_home = Path(tmp) / ".omh"
            hermes_home = Path(tmp) / ".hermes"
            input_path = Path(tmp) / "receipt.json"
            input_path.write_text(json.dumps(_payload()), encoding="utf-8")
            base = ["--omh-home", str(omh_home), "--hermes-home", str(hermes_home), "runtime", "session-receipt"]

            code, out, _ = run_cli(base + ["ingest", "--input", str(input_path), "--consumer", "workflow-learning", "--consumer", "context-budget-review"])
            self.assertEqual(code, 0, out)
            result = json.loads(out)
            self.assertEqual(result["outcome"], "recorded")
            self.assertEqual(result["evidence"]["workflow-learning"]["skill_exposure"]["exposed_unused_count"], 3)
            self.assertEqual(result["evidence"]["context-budget-review"]["metrics"]["context_peak_tokens"]["value"], 91000)
            self.assertIn("tokens_input", result["evidence"]["context-budget-review"]["unavailable_metrics"])

            code, out, _ = run_cli(base + ["ingest", "--input", str(input_path)])
            self.assertEqual(code, 0, out)
            self.assertEqual(json.loads(out)["outcome"], "already_recorded")

            code, out, _ = run_cli(base + ["list", "--session", "sess-9f2c4a"])
            self.assertEqual(code, 0, out)
            listing = json.loads(out)
            self.assertEqual(len(listing["receipts"]), 1)
            self.assertEqual(listing["quarantined"], [])
            self.assertEqual(listing["claim_boundary"], CLAIM_BOUNDARY)

    def test_dry_run_admits_without_writing_and_cross_profile_is_reported(self) -> None:
        with TemporaryDirectory() as tmp:
            omh_home = Path(tmp) / ".omh"
            hermes_home = Path(tmp) / ".hermes"
            input_path = Path(tmp) / "receipt.json"
            input_path.write_text(json.dumps(_payload()), encoding="utf-8")
            base = ["--omh-home", str(omh_home), "--hermes-home", str(hermes_home), "runtime", "session-receipt"]

            code, out, _ = run_cli(base + ["ingest", "--input", str(input_path), "--dry-run", "--consumer", "skill-health"])
            self.assertEqual(code, 0, out)
            result = json.loads(out)
            self.assertEqual(result["outcome"], "accepted")
            self.assertFalse(result["written"])
            self.assertEqual(result["evidence"]["skill-health"]["consumer"], "skill-health")
            self.assertFalse((omh_home / "runtime" / "session_activity_receipts.jsonl").exists())

            code, out, _ = run_cli(base + ["ingest", "--input", str(input_path), "--expected-profile", "profile-zz", "--consumer", "achievements"])
            self.assertEqual(code, 0, out)
            result = json.loads(out)
            self.assertEqual(result["outcome"], "quarantined")
            self.assertEqual(result["reason"], REASON_CROSS_PROFILE)
            self.assertIsNone(result["evidence"]["achievements"])

    def test_an_invalid_payload_is_rejected_with_named_errors(self) -> None:
        with TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "receipt.json"
            input_path.write_text(json.dumps(_payload(transcript="user: hello")), encoding="utf-8")
            code, out, _ = run_cli(
                ["--omh-home", str(Path(tmp) / ".omh"), "--hermes-home", str(Path(tmp) / ".hermes"), "runtime", "session-receipt", "ingest", "--input", str(input_path)]
            )
            self.assertEqual(code, 0, out)
            result = json.loads(out)
            self.assertEqual(result["outcome"], "rejected")
            self.assertTrue(result["errors"])
            self.assertNotIn("user: hello", out)


if __name__ == "__main__":
    unittest.main()
