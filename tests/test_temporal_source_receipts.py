"""`temporal_source_receipt/v1` (issue #1403).

Grouped by success criterion: the schema and validator with capture identity,
timestamps, cutoff relation, and failure state; the research lanes and the
router distinguishing live evidence from historical captures; a consumer that
preserves a receipt unchanged; the then-versus-now split; the explicit gap for
missing archive access or exhausted provider authority; and the rejections the
issue names -- a capture after the cutoff, a missing or invalid capture time,
and a live page presented as historical.
"""

from __future__ import annotations

import hashlib
import json
import unittest

from _local_package import load_local_package

load_local_package()
from omh.quality.routing_precision import ROUTING_INTERVENTION_CASES, ROUTING_PRECISION_CASES
from omh.routing.chat import route_chat_message
from omh.skills.catalog import builtin_definitions
from omh.workflows.research_briefing import build_research_briefing, render_research_briefing_markdown
from omh.workflows.temporal_source_receipts import (
    AVAILABILITIES,
    GAP_CLAIM_BOUNDARY,
    GAP_KINDS,
    RECEIPT_CLAIM_BOUNDARY,
    SURFACES_CLAIM_BOUNDARY,
    TEMPORAL_EVIDENCE_SURFACES_SCHEMA_VERSION,
    TEMPORAL_RETRIEVAL_GAP_SCHEMA_VERSION,
    TEMPORAL_SOURCE_RECEIPT_KEYS,
    TEMPORAL_SOURCE_RECEIPT_SCHEMA_VERSION,
    TemporalSourceReceiptError,
    as_of_claim_gap,
    as_of_date,
    as_of_interval,
    build_temporal_evidence_surfaces,
    build_temporal_retrieval_gap,
    build_temporal_source_receipt,
    cutoff_relation_for,
    preserve_temporal_source_receipt,
    receipt_supports_as_of_claim,
    temporal_source_receipt_fingerprint,
    temporal_source_receipt_id,
    validate_temporal_retrieval_gap,
    validate_temporal_source_receipt,
)
from omh.wrapper.contract import build_chat_interaction_payload

_URL = "https://vendor.example/pricing"
_AS_OF = as_of_date("2026-06-01")
_DIGEST = hashlib.sha256(b"Pro plan: $20 per seat").hexdigest()


def _capture(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "as_of": _AS_OF,
        "source_url": _URL,
        "source_class": "upstream_official",
        "evidence_kind": "historical_capture",
        "retrieved_at": "2026-09-08T10:00:00Z",
        "capture_provider": "web_archive",
        "capture_provider_class": "archive_service",
        "captured_at": "2026-05-20T08:15:00Z",
        "capture_ref": "20260520081500",
        "content_digest": _DIGEST,
    }
    kwargs.update(overrides)
    return build_temporal_source_receipt(**kwargs)


def _live(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "as_of": _AS_OF,
        "source_url": _URL,
        "source_class": "upstream_official",
        "evidence_kind": "live_page",
        "retrieved_at": "2026-09-08T10:00:00Z",
        "content_digest": _DIGEST,
    }
    kwargs.update(overrides)
    return build_temporal_source_receipt(**kwargs)


class ReceiptSchemaTests(unittest.TestCase):
    def test_valid_capture_at_or_before_the_cutoff_is_eligible(self) -> None:
        receipt = _capture()

        self.assertEqual(receipt["schema_version"], TEMPORAL_SOURCE_RECEIPT_SCHEMA_VERSION)
        self.assertEqual(sorted(receipt), sorted(TEMPORAL_SOURCE_RECEIPT_KEYS))
        self.assertEqual(receipt["cutoff_relation"], "at_or_before")
        self.assertEqual(receipt["captured_at_attribution"], "provider_reported")
        self.assertEqual(receipt["availability"], "available")
        self.assertEqual(receipt["claim_boundary"], RECEIPT_CLAIM_BOUNDARY)
        self.assertTrue(receipt["receipt_id"].startswith("tsr_"))
        self.assertEqual(validate_temporal_source_receipt(receipt), [])
        self.assertTrue(receipt_supports_as_of_claim(receipt))
        self.assertIsNone(as_of_claim_gap(receipt))

    def test_capture_on_the_cutoff_day_counts_as_at_or_before(self) -> None:
        # A calendar date means the whole day: a capture at 23:00 UTC on the
        # cutoff date is still at or before it.
        receipt = _capture(captured_at="2026-06-01T23:00:00Z")
        self.assertEqual(receipt["cutoff_relation"], "at_or_before")
        self.assertTrue(receipt_supports_as_of_claim(receipt))
        self.assertEqual(cutoff_relation_for("2026-06-02T00:00:01Z", _AS_OF), "after")

    def test_interval_cutoff_is_the_interval_end(self) -> None:
        window = as_of_interval("2026-05-01", "2026-06-01")
        self.assertEqual(cutoff_relation_for("2026-05-30", window), "at_or_before")
        self.assertEqual(cutoff_relation_for("2026-06-02", window), "after")
        with self.assertRaises(TemporalSourceReceiptError):
            _capture(as_of=as_of_interval("2026-06-01", "2026-05-01"))

    def test_receipt_id_is_stable_across_retrieval_time(self) -> None:
        first = _capture()
        second = _capture(retrieved_at="2026-09-09T10:00:00Z")
        self.assertEqual(first["receipt_id"], second["receipt_id"])
        self.assertNotEqual(first["receipt_id"], _capture(capture_ref="20260521000000")["receipt_id"])

    def test_capture_after_the_cutoff_is_rejected_for_the_claim(self) -> None:
        receipt = _capture(captured_at="2026-06-03T00:00:00Z")

        self.assertEqual(receipt["cutoff_relation"], "after")
        self.assertFalse(receipt_supports_as_of_claim(receipt))
        gap = as_of_claim_gap(receipt)
        self.assertIsNotNone(gap)
        self.assertEqual(gap["gap_kind"], "capture_after_cutoff")
        self.assertEqual(gap["receipt_ref"], receipt["receipt_id"])
        # A stored receipt cannot relabel the relation to sneak past the cutoff.
        forged = {**receipt, "cutoff_relation": "at_or_before"}
        self.assertIn(
            "temporal_source_receipt cutoff_relation must be after for this captured_at and as_of",
            validate_temporal_source_receipt(forged),
        )

    def test_missing_or_invalid_capture_timestamp_is_rejected(self) -> None:
        with self.assertRaises(TemporalSourceReceiptError) as invalid:
            _capture(captured_at="yesterday")
        self.assertIn("captured_at must be an ISO-8601 timestamp", str(invalid.exception))
        with self.assertRaises(TemporalSourceReceiptError):
            _capture(captured_at="2026-13-40")

        # A missing capture time cannot claim a cutoff relation.
        missing = {**_capture(), "captured_at": "", "captured_at_attribution": "unknown"}
        missing["receipt_id"] = temporal_source_receipt_id(missing)
        errors = validate_temporal_source_receipt(missing)
        self.assertTrue(any("cutoff_relation at_or_before requires a captured_at" in error for error in errors))
        # A capture time is never anything but the provider's word.
        errors = validate_temporal_source_receipt({**_capture(), "captured_at_attribution": "unknown"})
        self.assertTrue(any("always provider_reported" in error for error in errors))

    def test_live_page_presented_as_historical_is_rejected(self) -> None:
        with self.assertRaises(TemporalSourceReceiptError) as presented:
            _live(captured_at="2026-05-20T08:15:00Z", capture_ref="20260520081500")
        self.assertIn("not historical evidence", str(presented.exception))
        with self.assertRaises(TemporalSourceReceiptError):
            _live(capture_provider="web_archive", capture_provider_class="archive_service")
        relabeled = {**_live(), "cutoff_relation": "at_or_before"}
        self.assertTrue(
            any("never historical" in error for error in validate_temporal_source_receipt(relabeled))
        )
        # A capture that names no provider is a live page wearing a label.
        with self.assertRaises(TemporalSourceReceiptError) as unnamed:
            _capture(capture_provider="", capture_provider_class="none")
        self.assertIn("names the provider that captured it", str(unnamed.exception))

    def test_unknown_fields_are_preserved_without_inventing_values(self) -> None:
        receipt = _capture(
            captured_at="",
            capture_ref="",
            content_digest="",
            availability="unavailable",
            failure_reason="provider returned no capture for this URL",
            retrieved_at="",
        )

        self.assertEqual(validate_temporal_source_receipt(receipt), [])
        self.assertEqual(receipt["captured_at"], "")
        self.assertEqual(receipt["captured_at_attribution"], "unknown")
        self.assertEqual(receipt["cutoff_relation"], "unknown")
        self.assertEqual(receipt["retrieved_at"], "")
        self.assertFalse(receipt_supports_as_of_claim(receipt))
        self.assertEqual(as_of_claim_gap(receipt)["gap_kind"], "capture_unavailable")

        # Available but with no reported capture time: still a valid record,
        # still not an eligible one.
        undated = _capture(captured_at="")
        self.assertEqual(undated["cutoff_relation"], "unknown")
        self.assertEqual(as_of_claim_gap(undated)["gap_kind"], "capture_time_unknown")
        unverifiable = _capture(capture_ref="", content_digest="")
        self.assertEqual(as_of_claim_gap(unverifiable)["gap_kind"], "capture_unverifiable")

    def test_builder_refuses_what_it_would_otherwise_have_to_invent(self) -> None:
        with self.assertRaises(TemporalSourceReceiptError) as no_stamp:
            _capture(retrieved_at="")
        self.assertIn("records when it was retrieved", str(no_stamp.exception))
        with self.assertRaises(TemporalSourceReceiptError):
            _capture(availability="access_denied")
        with self.assertRaises(TemporalSourceReceiptError):
            _capture(source_url="ftp://vendor.example/pricing")
        with self.assertRaises(TemporalSourceReceiptError):
            _capture(source_url="https://user:pass@vendor.example/pricing")
        with self.assertRaises(TemporalSourceReceiptError):
            _capture(content_digest="not-a-digest")
        with self.assertRaises(TemporalSourceReceiptError):
            _capture(source_class="blog")
        self.assertEqual(validate_temporal_source_receipt({**_capture(), "extra": 1})[0][:45], "temporal_source_receipt has unsupported keys:")


class GapTests(unittest.TestCase):
    def test_missing_archive_access_and_exhausted_authority_are_explicit_gaps(self) -> None:
        denied = _capture(
            availability="access_denied",
            failure_reason="archive connector is not configured on this machine",
            captured_at="",
            capture_ref="",
            content_digest="",
        )
        exhausted = _capture(
            availability="provider_authority_exhausted",
            failure_reason="paid capture provider quota is spent; cost authority not granted",
            captured_at="",
            capture_ref="",
            content_digest="",
        )
        untried = _capture(
            availability="not_attempted",
            failure_reason="no archive provider is installed",
            captured_at="",
            capture_ref="",
            content_digest="",
            retrieved_at="",
        )

        for receipt, kind in (
            (denied, "archive_access_unavailable"),
            (exhausted, "provider_authority_exhausted"),
            (untried, "archive_access_unavailable"),
        ):
            with self.subTest(kind=kind):
                gap = as_of_claim_gap(receipt)
                self.assertEqual(gap["schema_version"], TEMPORAL_RETRIEVAL_GAP_SCHEMA_VERSION)
                self.assertEqual(gap["gap_kind"], kind)
                self.assertEqual(gap["network_action"], "none")
                self.assertEqual(gap["resolution"], "abstain")
                self.assertEqual(gap["detail"], receipt["failure_reason"])
                self.assertEqual(gap["claim_boundary"], GAP_CLAIM_BOUNDARY)
                self.assertEqual(validate_temporal_retrieval_gap(gap), [])
        self.assertEqual(set(AVAILABILITIES) - {"available"}, {"unavailable", "access_denied", "provider_authority_exhausted", "not_attempted"})

    def test_gap_is_closed_vocabulary_and_never_a_retrieval(self) -> None:
        with self.assertRaises(TemporalSourceReceiptError):
            build_temporal_retrieval_gap(as_of=_AS_OF, source_url=_URL, gap_kind="try_again_later")
        gap = build_temporal_retrieval_gap(as_of=_AS_OF, source_url=_URL, gap_kind="no_eligible_capture")
        self.assertIn("no_eligible_capture", GAP_KINDS)
        self.assertIn(
            "network_action must be none",
            validate_temporal_retrieval_gap({**gap, "network_action": "fetched"})[0],
        )


class EvidenceSurfaceTests(unittest.TestCase):
    def test_then_versus_now_yields_two_typed_surfaces(self) -> None:
        then = _capture()
        now = _live()
        late = _capture(captured_at="2026-07-01T00:00:00Z", capture_ref="20260701000000")

        surfaces = build_temporal_evidence_surfaces(as_of=_AS_OF, receipts=(then, now, late))

        self.assertEqual(surfaces["schema_version"], TEMPORAL_EVIDENCE_SURFACES_SCHEMA_VERSION)
        self.assertEqual(surfaces["as_of"], _AS_OF)
        self.assertEqual([r["receipt_id"] for r in surfaces["historical_captures"]], [then["receipt_id"]])
        self.assertEqual([r["receipt_id"] for r in surfaces["live_evidence"]], [now["receipt_id"]])
        self.assertEqual(
            [gap["gap_kind"] for gap in surfaces["unresolved_annex"]],
            ["live_page_only", "capture_after_cutoff"],
        )
        self.assertEqual(surfaces["claim_boundary"], SURFACES_CLAIM_BOUNDARY)
        # The live page is current evidence and never crosses into history.
        self.assertNotIn(now["receipt_id"], [r["receipt_id"] for r in surfaces["historical_captures"]])

    def test_surfaces_refuse_a_receipt_for_another_cutoff(self) -> None:
        other = _capture(as_of=as_of_date("2026-01-01"), captured_at="2025-12-01T00:00:00Z")
        with self.assertRaises(TemporalSourceReceiptError) as mismatch:
            build_temporal_evidence_surfaces(as_of=_AS_OF, receipts=(other,))
        self.assertIn("different as_of cutoff", str(mismatch.exception))


class ConsumerPreservationTests(unittest.TestCase):
    def test_research_briefing_carries_the_receipt_unchanged(self) -> None:
        receipt = _capture()
        before = temporal_source_receipt_fingerprint(receipt)
        briefing = build_research_briefing(
            audience="coding_agent_handoff",
            question="What did the vendor pricing page say as of 2026-06-01?",
            as_of="2026-06-01",
            sections=(
                {
                    "role": "case",
                    "title": "Pro plan seat price before the June change",
                    "paragraphs": ("The archived capture lists the Pro plan at $20 per seat.",),
                },
            ),
            sources=(
                {
                    "title": "Vendor pricing page",
                    "url": _URL,
                    "source_class": "upstream_official",
                    "retrieved_on": "2026-09-08",
                    "temporal_source_receipt": receipt,
                },
            ),
        )

        stored = briefing["sources"][0]["temporal_source_receipt"]
        self.assertEqual(temporal_source_receipt_fingerprint(stored), before)
        self.assertEqual(json.dumps(stored, sort_keys=True), json.dumps(receipt, sort_keys=True))
        self.assertEqual(preserve_temporal_source_receipt(stored), receipt)
        markdown = render_research_briefing_markdown(briefing)
        self.assertIn("retrieved 2026-09-08, historical capture 2026-05-20T08:15:00Z via web_archive", markdown)

    def test_research_briefing_refuses_a_citation_that_contradicts_its_receipt(self) -> None:
        receipt = _capture()
        base = {
            "audience": "coding_agent_handoff",
            "question": "q",
            "as_of": "2026-06-01",
            "sections": ({"role": "case", "title": "Seat price", "paragraphs": ("p",)},),
        }
        with self.assertRaises(ValueError) as mismatch:
            build_research_briefing(
                **base,
                sources=(
                    {
                        "title": "x",
                        "source_class": "practitioner",
                        "retrieved_on": "2026-09-08",
                        "temporal_source_receipt": receipt,
                    },
                ),
            )
        self.assertIn("source_class must match the temporal_source_receipt", str(mismatch.exception))
        with self.assertRaises(ValueError):
            build_research_briefing(
                **base,
                sources=(
                    {
                        "title": "x",
                        "source_class": "upstream_official",
                        "retrieved_on": "2026-09-08",
                        "temporal_source_receipt": {**receipt, "cutoff_relation": "after"},
                    },
                ),
            )


class ResearchLaneContractTests(unittest.TestCase):
    def test_both_research_lanes_require_receipts_for_as_of_claims(self) -> None:
        definitions = {definition.name: definition for definition in builtin_definitions()}
        for name in ("web-research", "research"):
            with self.subTest(skill=name):
                definition = definitions[name]
                self.assertTrue(any("temporal_source_receipt/v1" in rule for rule in definition.safety_rules))
                self.assertTrue(any("temporal_evidence_surfaces/v1" in item for item in definition.expected_outputs))
                self.assertTrue(any("as-of date or interval" in item for item in definition.required_inputs))
                self.assertTrue(
                    any("capture time, publication time, and retrieval time" in item for item in definition.quality_bar)
                )
                self.assertTrue(any("temporal retrieval gap" in note for note in definition.recovery_notes))

    def test_point_in_time_request_routes_to_web_research_with_temporal_state(self) -> None:
        message = "what did the vendor pricing page say as of 2026-06-01? use archived captures and cite them"
        decision = route_chat_message(message, source="discord")
        self.assertEqual(decision["selected_skill"], "web-research")
        self.assertIn(
            "guard:point_in_time_web",
            decision["recommendations"][0]["matched"],
        )

        payload = build_chat_interaction_payload(message, source="discord")
        state = payload["chat_response"]["state"]
        self.assertEqual(state["research_scope"], "point_in_time_web_evidence")
        self.assertEqual(state["temporal_evidence"]["receipt_schema"], TEMPORAL_SOURCE_RECEIPT_SCHEMA_VERSION)
        self.assertEqual(state["temporal_evidence"]["surfaces_schema"], TEMPORAL_EVIDENCE_SURFACES_SCHEMA_VERSION)
        self.assertEqual(state["temporal_evidence"]["gap_schema"], TEMPORAL_RETRIEVAL_GAP_SCHEMA_VERSION)
        self.assertTrue(state["temporal_evidence"]["historical_claims_require_receipt"])
        self.assertFalse(state["temporal_evidence"]["live_page_is_historical_evidence"])
        self.assertEqual(state["temporal_evidence"]["network_action"], "none")
        self.assertEqual(state["evidence_not_observed"][0], "archive capture retrieval")

    def test_current_facts_request_keeps_the_plain_research_card(self) -> None:
        payload = build_chat_interaction_payload(
            "web search the current rate limits and cite the sources", source="discord"
        )
        state = payload["chat_response"]["state"]
        self.assertEqual(state["research_scope"], "source_backed_current_evidence")
        self.assertNotIn("temporal_evidence", state)

    def test_point_in_time_corpora_cover_both_directions(self) -> None:
        negatives = {case.id for case in ROUTING_PRECISION_CASES}
        positives = {case.id for case in ROUTING_INTERVENTION_CASES}
        self.assertLessEqual(
            {
                "as-of-status-report-stays-out-of-web-research",
                "as-of-concept-question-stays-direct",
                "jest-snapshot-failure-stays-out-of-web-research",
                "archived-logs-stay-out-of-web-research",
            },
            negatives,
        )
        self.assertLessEqual(
            {
                "as-of-pricing-page-reaches-web-research",
                "then-versus-now-snapshot-reaches-web-research",
                "page-as-it-was-reaches-web-research",
                "korean-archive-capture-reaches-web-research",
            },
            positives,
        )


if __name__ == "__main__":
    unittest.main()
