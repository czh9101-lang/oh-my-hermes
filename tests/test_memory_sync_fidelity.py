from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from typing import Callable

from _cli_harness import run_cli
from _local_package import load_local_package

load_local_package()
from omh.workflows.memory_provider_posture import build_memory_provider_posture, parse_memory_provider_posture_input
from omh.workflows.memory_sync_fidelity_validation import is_metadata_list, metadata_mapping

decode_json: Callable[[str], object] = json.loads


_BINDING = {
    "provider_mode": "local", "profile_ref": "qa-profile", "session_ref": "session-a",
    "policy_digest": "sha256:" + "a" * 64, "input_digest": "sha256:" + "b" * 64,
}


def fidelity_input(attempts: list[dict[str, object]] | None = None) -> dict[str, object]:
    declared = {"status": "not_observed", "evidence_class": "declared_documentation"}
    return {
        "schema_version": "memory_provider_posture_input/v1", "provider_id": "provider-local",
        "observed_version_boundary": "v1", "storage_boundary": "local_runtime",
        "input_fidelity": {
            "schema_version": "memory_sync_fidelity/v1", "binding": dict(_BINDING),
            "input_surface": {**declared, "roles": {"user": "eligible", "assistant": "eligible", "tool_result": "eligible", "metadata": "eligible"}},
            "selection_policy": {**declared, "mode": "whole_turn"},
            "limit": {**declared, "value": 100, "unit": "bytes", "scope": "per_turn", "origin": "configured"},
            "truncation_policy": {**declared, "mode": "boundary", "splits_structured_input": False},
            "omission_visibility": {**declared, "mode": "counted_without_content"},
            "backpressure_policy": {**declared, "mode": "queue", "max_wait_ms": 1000, "duplicate_prevention": "input_digest"},
            "extraction": {**declared, "mode": "local_visible"}, "attempts": attempts or [],
        },
    }


def receipt(outcome: str = "complete", evidence: str = "observed_trial_receipt") -> dict[str, object]:
    partial = outcome.startswith("truncated_")
    skipped = outcome.startswith("skipped_") or outcome == "rejected"
    position = {"truncated_head": "head", "truncated_tail": "tail", "truncated_boundary": "middle"}.get(outcome, "none" if outcome == "complete" else "unknown")
    failure = {"rejected": "policy_rejection", "skipped_timeout": "timeout", "failed": "provider_error"}.get(outcome, "none" if outcome != "unknown" else "unknown")
    return {
        "schema_version": "memory_sync_receipt/v1", "receipt_id": "receipt-1",
        "provider_id": "provider-local", **_BINDING, "attempt_id": "attempt-1", "outcome": outcome,
        "selected_count": 0 if skipped else 80, "omitted_count": 20 if partial or skipped else 0,
        "unit": "bytes", "omitted_position": position, "failure_category": failure,
        "entered_provider_state": "no" if skipped else "yes",
        "omitted_input_entered_provider_state": "unknown", "evidence_class": evidence,
        "observed_at": "2026-09-10T00:00:00Z",
    }


def posture(payload: object) -> dict[str, object]:
    return build_memory_provider_posture(parse_memory_provider_posture_input(payload))


def at(value: object, path: tuple[str | int, ...]) -> object:
    """Inspect JSON outputs without assuming nested object types or coercing values."""
    current = value
    for key in path:
        if isinstance(key, int):
            assert is_metadata_list(current)
            current = current[key]
        else:
            current = metadata_mapping(current, "test output")[key]
    return current


class MemorySyncFidelityTests(unittest.TestCase):
    def test_provider_neutral_fidelity_schema(self) -> None:
        # Given selected user/assistant/tool metadata, a byte cap and bounded queue policy.
        payload = fidelity_input([receipt()])
        # When the existing posture boundary prepares that input.
        result = posture(payload)["input_fidelity"]
        # Then policy dimensions survive and unsupplied roles remain unknown.
        for key in ("selection_policy", "limit", "truncation_policy", "omission_visibility", "backpressure_policy", "extraction"):
            self.assertEqual(at(result, (key,)), at(payload, ("input_fidelity", key)))
        for role in ("user", "assistant", "tool_result", "metadata"):
            self.assertEqual(at(result, ("input_surface", "roles", role)), "eligible")
        self.assertEqual(at(result, ("input_surface", "roles", "attachment")), "unknown")
        self.assertEqual(at(result, ("attempts", 0, "outcome")), "complete")
        for key in ("mem0_batch", "raw_prompt"):
            invalid = {**metadata_mapping(payload["input_fidelity"], "fixture"), key: 1}
            with self.assertRaises(ValueError):
                _ = posture({**payload, "input_fidelity": invalid})

    def test_complete_and_partial_outcome_matrix(self) -> None:
        # Given all nine input outcome classes, including pre-submission skips.
        expected = {"complete": "complete_observed", "truncated_head": "partial_observed", "truncated_tail": "partial_observed", "truncated_boundary": "partial_observed", "rejected": "rejected_observed", "skipped_queue": "skipped_observed", "skipped_timeout": "skipped_observed", "failed": "failed_observed", "unknown": "unknown"}
        for outcome, readiness in expected.items():
            with self.subTest(outcome=outcome):
                # When preparing one independently supplied receipt.
                result = posture(fidelity_input([receipt(outcome)]))["input_fidelity"]
                # Then omissions/skips stay visible without becoming complete.
                self.assertEqual(at(result, ("synchronization_readiness",)), readiness)
                self.assertEqual(at(result, ("attempts", 0, "outcome")), outcome)

    def test_partial_receipt_not_complete_readiness(self) -> None:
        # Given equal availability but partial input or a foreign binding.
        for key in ("provider_id", "provider_mode", "profile_ref", "session_ref", "policy_digest", "input_digest", None):
            row = receipt("truncated_head" if key is None else "complete")
            if key:
                row[key] = "sha256:" + "c" * 64 if key.endswith("digest") else "foreign"
            # When readiness is derived for the active scope and intended input.
            result = posture(fidelity_input([row]))["input_fidelity"]
            # Then only eligible scoped receipts can settle completeness.
            self.assertEqual(at(result, ("synchronization_readiness",)), "partial_observed" if key is None else "unknown")
        for clock, expected in (("2026-09-10T00:01:00Z", "partial_observed"), ("2026-09-10T00:00:00Z", "unknown")):
            latest = {**receipt("truncated_tail"), "receipt_id": "receipt-2", "attempt_id": "attempt-2", "observed_at": clock}
            result = posture(fidelity_input([latest, receipt()]))["input_fidelity"]
            self.assertEqual(at(result, ("synchronization_readiness",)), expected)

    def test_separate_evidence_classes(self) -> None:
        # Given an identical complete claim from each independent evidence class.
        for evidence in ("declared_documentation", "observed_local_runtime", "observed_trial_receipt"):
            # When preparing supplied metadata, without contacting a provider.
            result = posture(fidelity_input([receipt(evidence=evidence)]))["input_fidelity"]
            # Then local/documentary evidence never becomes a provider receipt.
            self.assertEqual(at(result, ("attempts", 0, "evidence_class")), evidence)
            self.assertEqual(at(result, ("limit", "evidence_class")), "declared_documentation")
            self.assertEqual(at(result, ("synchronization_readiness",)), "complete_observed" if evidence == "observed_trial_receipt" else "unknown")

    def test_bounded_redacted_receipts(self) -> None:
        # Given bounded metadata and hostile inputs at the intake boundary.
        payload = fidelity_input([receipt()])
        # When normalizing a valid receipt.
        result = posture(payload)["input_fidelity"]
        # Then counts, digests and clocks survive but bodies/contradictions cannot enter.
        self.assertEqual(at(result, ("attempts", 0)), receipt())
        for field, value in (
            ("receipt_id", "raw prompt\nbody"), ("profile_ref", "raw memory content"),
            ("session_ref", "raw transcript text"), ("attempt_id", "raw tool result"),
            ("provider_mode", "raw attachment content"), ("receipt_id", "sk-live-123456789"),
            ("input_digest", "raw-input"), ("selected_count", True), ("omitted_count", -1),
            ("observed_at", "yesterday"), ("observed_at", "2026-09-10Z"),
            ("omitted_count", 1), ("raw_prompt", "sentinel"),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                _ = posture(fidelity_input([{**receipt(), field: value}]))
        with self.assertRaises(ValueError):
            _ = posture(fidelity_input([receipt()] * 25))

    def test_parser_error_redacts_invalid_outcome(self) -> None:
        # Given raw content masquerading as a closed outcome variant.
        sentinel = "raw-outcome-body-sentinel"
        # When the parser refuses it.
        with self.assertRaises(ValueError) as error:
            _ = posture(fidelity_input([{**receipt(), "outcome": sentinel}]))
        # Then the CLI-facing error cannot echo the untrusted body.
        self.assertNotIn(sentinel, str(error.exception))

    def test_backpressure_idempotency(self) -> None:
        from omh.workflows.memory_sync_reservations import SyncScope, claim_sync, report_sync_outcome

        active = SyncScope(provider_id="provider-local", **{key: value for key, value in _BINDING.items() if key != "input_digest"})
        first_input = _BINDING["input_digest"]
        second_input = "sha256:" + "c" * 64
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "reservations.json"
            writes: list[tuple[str, ...]] = []
            decisions: list[str] = []
            for attempt, inputs in (
                ("attempt-1", (first_input, second_input)),
                ("attempt-1", (first_input, second_input)),
                ("attempt-2", (first_input,)),
                ("attempt-3", (second_input,)),
                ("attempt-1", ("sha256:" + "d" * 64,)),
            ):
                result = claim_sync(path, active, attempt, inputs)
                decisions.append(result.decision)
                self.assertFalse(result.authorizes_provider_write)
                if result.decision == "claimed":
                    # Independent fixture host approval; no provider callback in OMH.
                    writes.append(inputs)
            self.assertEqual(len(writes), 1)
            self.assertEqual(decisions, ["claimed", "held", "held", "held", "held"])
            _ = report_sync_outcome(path, active, "attempt-1", (first_input, second_input), "written")
            skipped = claim_sync(path, active, "attempt-4", ("sha256:" + "e" * 64,), skip_reason="skipped_timeout")
            self.assertEqual(skipped.decision, "skipped")
            complete_row = receipt(evidence="observed_local_runtime")
            coalesced = {**receipt("skipped_queue", "observed_local_runtime"),
                         "receipt_id": "receipt-2", "attempt_id": "attempt-2", "coalesced_into": "receipt-1"}
            result = posture(fidelity_input([complete_row, coalesced]))["input_fidelity"]
            self.assertEqual(at(result, ("attempts",)), [complete_row, coalesced])
            self.assertEqual(at(result, ("synchronization_readiness",)), "unknown")
            timeout = {**receipt("skipped_timeout", "observed_local_runtime"),
                       "receipt_id": "receipt-3", "attempt_id": "attempt-4",
                       "input_digest": "sha256:" + "e" * 64}
            payload = fidelity_input([timeout])
            fidelity = metadata_mapping(payload["input_fidelity"], "fixture")
            fidelity["binding"] = {**_BINDING, "input_digest": timeout["input_digest"]}
            payload["input_fidelity"] = fidelity
            result = posture(payload)["input_fidelity"]
            self.assertEqual(at(result, ("attempts",)), [timeout])
            self.assertEqual(at(result, ("synchronization_readiness",)), "unknown")

    def test_unknown_caps_and_portability(self) -> None:
        # Given no cap evidence, opaque extraction and a partial receipt.
        payload = fidelity_input([receipt("truncated_head")])
        fidelity = metadata_mapping(payload["input_fidelity"], "fixture")
        del fidelity["limit"]
        fidelity["extraction"] = {"mode": "hosted_opaque"}
        # When the supplied posture is prepared.
        result = posture({**payload, "input_fidelity": fidelity})["input_fidelity"]
        # Then selected input's submission proves nothing about omitted state/export.
        self.assertIsNone(at(result, ("limit", "value")))
        self.assertEqual(at(result, ("limit", "unit")), "unknown")
        self.assertEqual(at(result, ("extraction", "mode")), "hosted_opaque")
        self.assertEqual(at(result, ("deletion_and_portability_effect",)), {"omitted_input_entered_provider_state": "unknown", "export": "unknown", "provider_side_deletion": "unknown"})
        unknown = at(result, ("unknown_field_count",))
        self.assertIsInstance(unknown, int)
        self.assertNotEqual(unknown, 0)

    def test_unreviewed_sync_handoff(self) -> None:
        # Given isolated homes and a complete synthetic receipt.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory, native = root / "qa-profile" / "memory", root / "qa-hermes" / "memories"
            memory.mkdir(parents=True)
            native.mkdir(parents=True)
            _ = (memory / "sentinel.json").write_text('{"fixture":true}', encoding="utf-8")
            _ = (native / "MEMORY.md").write_text("native fixture", encoding="utf-8")
            before = {str(p.relative_to(root)): p.read_bytes() for d in (memory, native) for p in d.rglob("*") if p.is_file()}
            input_path = root / "input.json"
            _ = input_path.write_text(json.dumps(fidelity_input([receipt()])), encoding="utf-8")
            # When the public CLI writes its local operations artifact.
            status, stdout, stderr = run_cli(["--omh-home", str(root / "qa-profile"), "--hermes-home", str(root / "qa-hermes"), "ops", "memory-provider-posture", "--input", str(input_path), "--write"])
            # Then the handoff stays unreviewed and both memory stores stay unchanged.
            self.assertEqual(status, 0, stderr)
            result = decode_json(stdout)
            handoff = at(result, ("memory_sync_handoff",))
            self.assertEqual(at(handoff, ("input_fidelity_summary", "readiness")), "complete_observed")
            self.assertEqual(at(handoff, ("review_status",)), "not_omh_reviewed")
            self.assertIs(at(handoff, ("authorizes_native_memory_mutation",)), False)
            self.assertIs(at(handoff, ("imports_provider_records",)), False)
            prohibited = at(result, ("prohibited_actions",))
            assert is_metadata_list(prohibited)
            self.assertIn("mutate_memory_store", prohibited)
            self.assertEqual(before, {str(p.relative_to(root)): p.read_bytes() for d in (memory, native) for p in d.rglob("*") if p.is_file()})
            path = at(result, ("artifact", "path"))
            assert isinstance(path, str)
            persisted = decode_json(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(at(persisted, ("input_fidelity",)), at(result, ("input_fidelity",)))


if __name__ == "__main__":
    _ = unittest.main()
