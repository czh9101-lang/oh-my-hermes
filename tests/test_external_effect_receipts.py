"""Contracts for `external_effect_receipt/v1` (issue #836).

Grouped by acceptance criterion:

- AC1: status reports tell requested / attempted / succeeded / failed / unknown
  external effects apart.
- AC2: every success claim names an observed receipt and its acting surface.
- AC3: receipt rendering redacts secrets and never exposes raw prompts, private
  payloads, or external URLs.

Plus the invariant the whole store rests on: nothing prepared or requested can
become a receipt.
"""

from __future__ import annotations

import json
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from _cli_harness import run_cli
from _local_package import load_local_package

load_local_package()
from omh.adapter_quality import (
    build_adapter_quality_delivery_card,
    build_adapter_quality_observation,
    prepare_adapter_quality_delivery,
    record_adapter_quality_delivery,
)
from omh.coding_delegation import build_coding_delegation_payload, coding_delegation_record_payload
from omh.conformance.checker import check_runtime_run
from omh.external_effect_receipts import (
    ACTING_SURFACES,
    ACTIONS,
    CLAIM_BOUNDARY,
    EXTERNAL_EFFECT_MINT_RESULT_SCHEMA_VERSION,
    EXTERNAL_EFFECT_RECEIPT_KEYS,
    EXTERNAL_EFFECT_RECEIPT_SCHEMA_VERSION,
    MAX_SUMMARY_CHARS,
    OBSERVED_RESULTS,
    PROJECTED_RESULTS,
    RECEIPT_RESULTS,
    ExternalEffectReceiptError,
    append_external_effect_receipt,
    build_external_effect_receipt,
    compact_external_effect_receipt,
    external_effect_mint_failures_path,
    latest_receipt_in,
    mint_external_effect_receipt,
    mint_external_effect_receipt_at,
    project_external_effects,
    read_external_effect_mint_failures,
    read_external_effect_receipts,
    receipt_contradicts_success_claim,
    receipt_satisfies_success_claim,
    record_external_effect,
    redacted_external_effect_ref,
    select_effect_receipt,
    success_claim_citation,
    validate_external_effect_receipt,
    validate_external_effect_receipt_store,
)

# The guard-class tables the field sweep is asserted against. Private on purpose
# -- they are the module's own classification, and a test that re-derived them
# would assert nothing.
from omh.workflows.external_effect_receipts import (
    _OPTIONAL_RECEIPT_REFS,
    _RECEIPT_STRING_FIELDS,
    _RECEIPT_TEXT_FIELDS,
    _RECEIPT_VOCABULARIES,
    _REQUIRED_RECEIPT_REFS,
)
from omh.paths import OmhPaths, resolve_paths
from omh.runtime.artifacts import external_effect_id, runtime_store_path_for_run_dir
from omh.runtime.records import EXTERNAL_EFFECT_RECEIPT_RECORD_KEYS, OPTIONAL_RUNTIME_STORE_VALIDATORS
from omh.runtime_artifacts import (
    create_prepared_coding_delegation_run,
    export_runtime,
    show_run,
    summarize_delegated_coding_status,
    validate_run_dir,
    validate_runtime,
    write_ci_record,
    write_coding_delegation,
    write_delegation,
    write_merge_record,
    write_review_record,
    write_wrapper_contract,
)


def _prepared_run(paths: OmhPaths) -> str:
    run = create_prepared_coding_delegation_run(paths, {"skill": "coding", "harness": "delegate"})
    run_dir = paths.runtime_runs_dir / run["run_id"]
    message = "implement safe conformance adapter without overclaiming"
    payload = build_coding_delegation_payload(message, source="discord", executor_target="codex")
    write_coding_delegation(run_dir, coding_delegation_record_payload(payload, message))
    return str(run["run_id"])


def _executed_run(paths: OmhPaths) -> str:
    run_id = _prepared_run(paths)
    run_dir = paths.runtime_runs_dir / run_id
    write_wrapper_contract(
        run_dir,
        {
            "prompt_dispatched": True,
            "hermes_response_observed": True,
            "verification_observed": True,
            "completion_status": "completed",
        },
    )
    write_delegation(run_dir, {"requested": True, "observed": True, "result": "completed"})
    write_review_record(run_dir, {"status": "passed", "reviewer": "code-review", "evidence_refs": ["review-comment"]})
    return run_id


def _merge_ready_run(paths: OmhPaths) -> str:
    """A run with every lower rung observed and CI passed with a named provider."""
    run_id = _executed_run(paths)
    run_dir = paths.runtime_runs_dir / run_id
    write_ci_record(run_dir, {"status": "passed", "provider": "github-actions", "checks": ["unit:passed"]})
    return run_id


def _rewrite_record(path: Path, **fields: object) -> None:
    """Hand-edit a run record the way a probe or a stale writer would."""
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(fields)
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")


def _replace_receipts(paths: OmhPaths, edits: dict[str, dict[str, object]]) -> None:
    """Rewrite the stored receipts for named effects, leaving the rest alone."""
    path = paths.runtime_external_effect_receipts_path
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        receipt = json.loads(line)
        override = edits.get(str(receipt.get("effect_id", "")))
        if override:
            receipt = {**receipt, **override}
        lines.append(json.dumps(receipt, sort_keys=True))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_events(run_dir: Path) -> list[dict[str, Any]]:
    """The run's own event log, which records every gate status ever written."""
    path = run_dir / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _append_raw_store_line(paths: OmhPaths, line: str) -> None:
    path = paths.runtime_external_effect_receipts_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _store_parse_count(paths: OmhPaths, call: Callable[[], Any]) -> int:
    """How many times `call` parses the receipt store."""
    import omh.workflows.external_effect_receipts as receipts_module

    real = receipts_module.read_jsonl_objects
    store = paths.runtime_external_effect_receipts_path
    parses = 0

    def counting(path: Path) -> Any:
        nonlocal parses
        if path == store:
            parses += 1
        return real(path)

    with patch.object(receipts_module, "read_jsonl_objects", counting):
        call()
    return parses


def _remove_receipt_store(paths: OmhPaths) -> None:
    """Leave the tree exactly as a version without receipts would have left it."""
    for path in (
        paths.runtime_external_effect_receipts_path,
        external_effect_mint_failures_path(paths.runtime_external_effect_receipts_path),
    ):
        path.unlink(missing_ok=True)
        path.with_name(f".{path.name}.lock").unlink(missing_ok=True)


def _valid_receipt(**overrides: object) -> dict[str, object]:
    record = build_external_effect_receipt(
        effect_id="ci:run-1",
        action="ci_run",
        acting_surface="runtime_ci_record",
        observed_result="succeeded",
        run_id="run-1",
        external_ref="github-actions",
    )
    return {**record, **overrides}


class ExternalEffectVocabularyTests(unittest.TestCase):
    def test_result_vocabulary_covers_the_five_reportable_states(self) -> None:
        self.assertEqual(OBSERVED_RESULTS, ("requested", "attempted", "succeeded", "failed", "unknown"))
        # `requested` is a projected state, never a mintable one.
        self.assertEqual(RECEIPT_RESULTS, ("attempted", "succeeded", "failed", "unknown"))
        self.assertNotIn("requested", RECEIPT_RESULTS)

    def test_every_action_and_surface_is_a_closed_vocabulary(self) -> None:
        self.assertEqual(ACTIONS, ("message_sent", "review_submitted", "ci_run", "merge", "browser_mutation"))
        self.assertEqual(
            ACTING_SURFACES,
            ("adapter_quality_delivery", "runtime_review_record", "runtime_ci_record", "runtime_merge_record",
             "browser_adapter_readback"),
        )
        with self.assertRaises(ExternalEffectReceiptError):
            build_external_effect_receipt(
                effect_id="x:1",
                action="pr_opened",
                acting_surface="runtime_ci_record",
                observed_result="succeeded",
                external_ref="ref-1",
            )
        with self.assertRaises(ExternalEffectReceiptError):
            build_external_effect_receipt(
                effect_id="x:1",
                action="ci_run",
                acting_surface="operator_said_so",
                observed_result="succeeded",
                external_ref="ref-1",
            )

    def test_receipt_store_is_registered_beside_the_other_runtime_records(self) -> None:
        self.assertEqual(EXTERNAL_EFFECT_RECEIPT_RECORD_KEYS, EXTERNAL_EFFECT_RECEIPT_KEYS)
        entries = {entry.store_name: entry for entry in OPTIONAL_RUNTIME_STORE_VALIDATORS}
        self.assertIn("external_effect_receipts.jsonl", entries)
        entry = entries["external_effect_receipts.jsonl"]
        self.assertEqual(entry.record_id_key, "receipt_id")
        self.assertEqual(entry.validator(_valid_receipt()), [])
        self.assertTrue(entry.validator({"schema_version": "wrong"}))

    def test_receipt_record_keys_are_exact(self) -> None:
        record = _valid_receipt()
        self.assertEqual(tuple(sorted(record)), EXTERNAL_EFFECT_RECEIPT_KEYS)
        self.assertEqual(record["schema_version"], EXTERNAL_EFFECT_RECEIPT_SCHEMA_VERSION)
        self.assertEqual(record["claim_boundary"], CLAIM_BOUNDARY)
        self.assertEqual(record["privacy"], "metadata_only")
        self.assertEqual(validate_external_effect_receipt(record), [])


class PreparedNeverMintsAReceiptTests(unittest.TestCase):
    def test_requested_result_is_refused_by_build_and_by_validation(self) -> None:
        with self.assertRaises(ExternalEffectReceiptError) as raised:
            build_external_effect_receipt(
                effect_id="ci:run-1",
                action="ci_run",
                acting_surface="runtime_ci_record",
                observed_result="requested",
                external_ref="github-actions",
            )
        self.assertIn("requested external effect is not an observed receipt", str(raised.exception))
        errors = validate_external_effect_receipt(_valid_receipt(observed_result="requested"))
        self.assertTrue(any("must not be requested" in error for error in errors), errors)

    def test_appending_a_requested_record_is_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            with self.assertRaises(ExternalEffectReceiptError):
                append_external_effect_receipt(paths, _valid_receipt(observed_result="requested"))
            self.assertEqual(read_external_effect_receipts(paths), [])

    def test_succeeded_requires_naming_the_external_effect(self) -> None:
        with self.assertRaises(ExternalEffectReceiptError) as raised:
            build_external_effect_receipt(
                effect_id="merge:run-1",
                action="merge",
                acting_surface="runtime_merge_record",
                observed_result="succeeded",
            )
        self.assertIn("succeeded requires an external_ref", str(raised.exception))

    def test_preparing_an_adapter_delivery_mints_nothing_and_observing_it_mints_one(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            observation = build_adapter_quality_observation(
                observation_id="obs-1",
                subject_id="subject-1",
                surface_kind="web",
                adapter_id="slack-adapter",
                source_revision="rev-1",
                checks=[],
                layout_checks=[],
                metrics=[],
            )
            card = build_adapter_quality_delivery_card(observation, renderer_target="slack")
            preparation = prepare_adapter_quality_delivery(paths, session_id="ws-quality", card=card)

            self.assertEqual(preparation["status"], "prepared_not_observed")
            self.assertEqual(read_external_effect_receipts(paths), [], "a preparation is not an observation")

            record_adapter_quality_delivery(
                paths,
                preparation=preparation,
                adapter="slack-adapter",
                delivery_result="delivered",
                external_message_ref="slack:message-1",
            )

            receipts = read_external_effect_receipts(paths)
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0]["action"], "message_sent")
            self.assertEqual(receipts[0]["acting_surface"], "adapter_quality_delivery")
            self.assertEqual(receipts[0]["observed_result"], "succeeded")
            self.assertEqual(receipts[0]["external_ref"], "slack:message-1")

    def test_unobserved_and_not_required_gates_mint_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run_id = _prepared_run(paths)
            run_dir = paths.runtime_runs_dir / run_id

            write_review_record(run_dir, {"status": "not_observed", "required": True, "observed": False})
            write_ci_record(run_dir, {"status": "not_required", "required": False, "observed": True})
            write_merge_record(run_dir, {"status": "not_ready"})

            self.assertEqual(read_external_effect_receipts(paths, run_id=run_id), [])

    def test_an_unobserved_pending_record_mints_nothing_and_is_projected_attempted(self) -> None:
        """DEFECT 1: `--status pending` writes `observed: false`; nothing was observed."""
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run_id = _prepared_run(paths)
            run_dir = paths.runtime_runs_dir / run_id

            record = write_ci_record(run_dir, {"status": "pending", "provider": "github-actions"})

            self.assertFalse(record["observed"])
            self.assertEqual(read_external_effect_receipts(paths, run_id=run_id), [])

            effects = summarize_delegated_coding_status(paths, run_id)["external_effects"]
            attempted = [row for row in effects["attempted"] if row["effect_id"] == external_effect_id("ci", run_id)]

            # Still reportable as attempted -- projected from the run's own
            # record, with no receipt id and no acting surface behind it.
            self.assertEqual(len(attempted), 1)
            self.assertEqual(attempted[0]["receipt_id"], "")
            self.assertEqual(attempted[0]["acting_surface"], "")
            self.assertEqual(effects["receipt_count"], 0)

    def test_an_observed_pending_record_is_the_only_way_to_mint_attempted(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run_id = _prepared_run(paths)
            run_dir = paths.runtime_runs_dir / run_id

            write_ci_record(run_dir, {"status": "pending", "observed": True, "provider": "github-actions"})

            receipts = read_external_effect_receipts(paths, run_id=run_id)
            self.assertEqual([receipt["observed_result"] for receipt in receipts], ["attempted"])
            self.assertEqual(receipts[0]["acting_surface"], "runtime_ci_record")

    def test_projected_states_are_exactly_the_two_that_need_no_receipt(self) -> None:
        self.assertEqual(PROJECTED_RESULTS, ("requested", "attempted"))
        for state in PROJECTED_RESULTS:
            with self.subTest(state=state):
                self.assertIn(state, OBSERVED_RESULTS)
        self.assertNotIn("requested", RECEIPT_RESULTS)


class ExternalEffectStateReportingTests(unittest.TestCase):
    """AC1: five reportable states, each reachable, each distinct."""

    def test_status_report_separates_all_five_external_effect_states(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run_id = _prepared_run(paths)
            run_dir = paths.runtime_runs_dir / run_id

            # review is required for this run and no surface reported it.
            write_ci_record(run_dir, {"status": "pending", "provider": "github-actions"})
            write_merge_record(run_dir, {"status": "blocked", "target_branch": "main"})
            # The two message deliveries are minted the way the adapter surface
            # mints them: one named, one that reached a terminal state nobody
            # could name.
            record_external_effect(
                paths,
                effect_id="delivery:prep-named",
                action="message_sent",
                acting_surface="adapter_quality_delivery",
                observed_result="succeeded",
                run_id=run_id,
                external_ref="slack:message-1",
            )
            record_external_effect(
                paths,
                effect_id="delivery:prep-unnamed",
                action="message_sent",
                acting_surface="adapter_quality_delivery",
                observed_result="unknown",
                run_id=run_id,
            )

            status = summarize_delegated_coding_status(paths, run_id)
            effects = status["external_effects"]

            self.assertEqual([row["effect_id"] for row in effects["requested"]], [external_effect_id("review", run_id)])
            self.assertEqual([row["effect_id"] for row in effects["attempted"]], [external_effect_id("ci", run_id)])
            self.assertEqual([row["effect_id"] for row in effects["succeeded"]], ["delivery:prep-named"])
            self.assertEqual([row["effect_id"] for row in effects["failed"]], [external_effect_id("merge", run_id)])
            self.assertEqual([row["effect_id"] for row in effects["unknown"]], ["delivery:prep-unnamed"])
            self.assertEqual(effects["effect_count"], 5)
            # Five states, five disjoint effect sets.
            seen = [row["effect_id"] for state in OBSERVED_RESULTS for row in effects[state]]
            self.assertEqual(len(seen), len(set(seen)))

    def test_requested_effect_carries_no_receipt_and_no_acting_surface(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run_id = _prepared_run(paths)

            status = summarize_delegated_coding_status(paths, run_id)
            requested = status["external_effects"]["requested"]

            self.assertTrue(requested)
            for row in requested:
                self.assertEqual(row["receipt_id"], "")
                self.assertEqual(row["acting_surface"], "")
                self.assertEqual(row["observed_result"], "requested")

    def test_unknown_is_reached_by_an_observed_success_nobody_can_name(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run_id = _prepared_run(paths)
            run_dir = paths.runtime_runs_dir / run_id

            write_review_record(run_dir, {"status": "passed"})

            receipts = read_external_effect_receipts(paths, run_id=run_id)
            self.assertEqual(len(receipts), 1)
            self.assertEqual(receipts[0]["observed_result"], "unknown")
            self.assertEqual(receipts[0]["external_ref"], "")

    def test_show_run_reports_the_run_receipts_and_their_bounds(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run_id = _prepared_run(paths)
            run_dir = paths.runtime_runs_dir / run_id
            write_ci_record(run_dir, {"status": "failed", "provider": "github-actions", "checks": ["unit:failed"]})

            shown = show_run(paths, run_id)

            self.assertEqual(len(shown["external_effect_receipts"]), 1)
            self.assertEqual(shown["external_effect_receipts"][0]["observed_result"], "failed")
            self.assertEqual(
                shown["history"]["external_effect_receipts"],
                {"total": 1, "shown": 1, "omitted": 0},
            )
            self.assertIn(
                "external_effect_receipts.jsonl",
                shown["history"]["full_history_artifacts"]["external_effect_receipts"],
            )


class SuccessClaimsCiteReceiptsTests(unittest.TestCase):
    """AC2: a success claim names the receipt and the surface that acted."""

    def test_ci_passed_without_a_named_receipt_blocks_the_ci_claim(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run_id = _executed_run(paths)
            run_dir = paths.runtime_runs_dir / run_id
            # Passed, but with nothing naming the CI system that ran it.
            write_ci_record(run_dir, {"status": "passed", "checks": ["unit:passed"]})

            report = check_runtime_run(paths, run_id)
            blocked = {item["claim"]: item["reason"] for item in report["blocked_claims"]}

            self.assertTrue(report["ok"], report["violations"])
            self.assertEqual(report["claim_state"], "review_observed")
            self.assertIn("ci_observed", blocked)
            self.assertIn("external effect receipt", blocked["ci_observed"])

    def test_ci_passed_with_a_named_receipt_allows_the_ci_claim(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run_id = _executed_run(paths)
            run_dir = paths.runtime_runs_dir / run_id
            write_ci_record(run_dir, {"status": "passed", "provider": "github-actions", "checks": ["unit:passed"]})

            status = summarize_delegated_coding_status(paths, run_id)
            report = check_runtime_run(paths, run_id)

            self.assertIn("ci_observed", report["allowed_claims"])
            self.assertEqual(status["ci"]["receipt"]["observed_result"], "succeeded")
            self.assertEqual(status["ci"]["receipt"]["acting_surface"], "runtime_ci_record")
            self.assertTrue(status["ci"]["receipt"]["receipt_id"])

    def test_merged_claim_names_the_receipt_id_and_acting_surface(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run_id = _executed_run(paths)
            run_dir = paths.runtime_runs_dir / run_id
            write_ci_record(run_dir, {"status": "passed", "provider": "github-actions", "checks": ["unit:passed"]})
            write_merge_record(run_dir, {"status": "merged", "target_branch": "main", "merge_commit": "abc123"})

            status = summarize_delegated_coding_status(paths, run_id)
            report = check_runtime_run(paths, run_id)
            receipt = status["merge"]["receipt"]

            self.assertEqual(report["claim_state"], "merged")
            self.assertEqual(receipt["observed_result"], "succeeded")
            self.assertEqual(receipt["acting_surface"], "runtime_merge_record")
            self.assertIn(receipt["receipt_id"], status["safe_summary"])
            self.assertIn(receipt["acting_surface"], status["safe_summary"])
            self.assertIn("merge evidence is observed", status["safe_summary"])

    def test_a_failed_receipt_does_not_satisfy_a_passed_or_merged_claim(self) -> None:
        """The gate checks the receipt's result, not merely that one exists."""
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run_id = _executed_run(paths)
            run_dir = paths.runtime_runs_dir / run_id
            write_ci_record(run_dir, {"status": "failed", "provider": "github-actions", "checks": ["unit:failed"]})
            # The probe: rewrite the record to claim success while the only
            # receipt for the effect says the CI run failed.
            _rewrite_record(run_dir / "ci.json", status="passed", checks=[{"name": "unit", "status": "passed"}])

            result = validate_runtime(paths, run_id)
            report = check_runtime_run(paths, run_id)
            errors = "\n".join(result["runs"][0]["errors"])

            self.assertFalse(result["ok"])
            self.assertIn("ci passed contradicts external effect receipt", errors)
            self.assertIn("which observed failed", errors)
            self.assertNotIn("ci_observed", report["allowed_claims"])

    def test_an_attempted_receipt_does_not_satisfy_a_merged_claim(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run_id = _merge_ready_run(paths)
            run_dir = paths.runtime_runs_dir / run_id
            write_merge_record(run_dir, {"status": "merged", "target_branch": "main", "merge_commit": "abc123"})
            # Replace the succeeded merge receipt with an attempted one; the
            # record is untouched, so only the receipt's result differs.
            _replace_receipts(
                paths,
                {external_effect_id("merge", run_id): {"observed_result": "attempted", "external_ref": ""}},
            )

            status = summarize_delegated_coding_status(paths, run_id)
            report = check_runtime_run(paths, run_id)

            self.assertTrue(report["ok"], report["violations"])
            self.assertEqual(status["merge"]["receipt"]["observed_result"], "attempted")
            self.assertNotIn("merged", report["allowed_claims"])
            self.assertIn("merged", {item["claim"] for item in report["blocked_claims"]})

    def test_a_receipt_for_another_effect_or_surface_satisfies_nothing(self) -> None:
        run_id = "run-1"
        succeeded = build_external_effect_receipt(
            effect_id=external_effect_id("ci", run_id),
            action="ci_run",
            acting_surface="runtime_ci_record",
            observed_result="succeeded",
            run_id=run_id,
            external_ref="github-actions",
        )

        self.assertTrue(receipt_satisfies_success_claim(succeeded, kind="ci", run_id=run_id))
        # Right receipt, wrong gate.
        self.assertFalse(receipt_satisfies_success_claim(succeeded, kind="merge", run_id=run_id))
        # Right gate, wrong run.
        self.assertFalse(receipt_satisfies_success_claim(succeeded, kind="ci", run_id="run-2"))
        # Right effect, wrong acting surface.
        self.assertFalse(
            receipt_satisfies_success_claim(
                {**succeeded, "acting_surface": "adapter_quality_delivery"}, kind="ci", run_id=run_id
            )
        )
        for result in ("attempted", "failed", "unknown"):
            with self.subTest(result=result):
                self.assertFalse(
                    receipt_satisfies_success_claim({**succeeded, "observed_result": result}, kind="ci", run_id=run_id)
                )
        self.assertFalse(receipt_satisfies_success_claim({}, kind="ci", run_id=run_id))

    def test_validation_and_the_claim_ladder_share_one_predicate(self) -> None:
        """`contradicts` is defined against `satisfies`, so they cannot disagree."""
        run_id = "run-1"
        base = build_external_effect_receipt(
            effect_id=external_effect_id("ci", run_id),
            action="ci_run",
            acting_surface="runtime_ci_record",
            observed_result="succeeded",
            run_id=run_id,
            external_ref="github-actions",
        )
        for result in ("succeeded", "attempted", "failed", "unknown"):
            receipt = {**base, "observed_result": result}
            with self.subTest(result=result):
                satisfies = receipt_satisfies_success_claim(receipt, kind="ci", run_id=run_id)
                contradicts = receipt_contradicts_success_claim(receipt, kind="ci", run_id=run_id)
                self.assertFalse(satisfies and contradicts)
                self.assertEqual(satisfies, result == "succeeded")
                # Only an observed non-happening contradicts; observing less
                # than a classified success withholds the claim instead.
                self.assertEqual(contradicts, result == "failed")


class LegacyStoreCompatibilityTests(unittest.TestCase):
    """DEFECT 4: a run recorded before receipts existed stays valid.

    Validation describes whether the records on disk are internally consistent.
    A store with no receipt file was written by a version that had none, so it
    is consistent; refusing the CI and merge *claims* is where the receipt
    requirement belongs, because a refused claim can be recovered from and an
    invalid store cannot -- there is deliberately no mint command.
    """

    def test_a_receiptless_store_validates_clean_and_keeps_its_lower_rungs(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run_id = _merge_ready_run(paths)
            run_dir = paths.runtime_runs_dir / run_id
            write_merge_record(run_dir, {"status": "merged", "target_branch": "main", "merge_commit": "abc123"})
            # Exactly what a store written before this change looks like: the
            # records are all there, the receipt store was never created.
            _remove_receipt_store(paths)

            result = validate_runtime(paths, run_id)
            report = check_runtime_run(paths, run_id)
            blocked = {item["claim"]: item["reason"] for item in report["blocked_claims"]}

            self.assertTrue(result["ok"], result)
            self.assertTrue(report["ok"], report["violations"])
            self.assertEqual(report["violations"], [])
            self.assertEqual(
                report["allowed_claims"],
                [
                    "metadata_available",
                    "handoff_prepared",
                    "executor_dispatched",
                    "execution_observed",
                    "verification_observed",
                ],
            )
            self.assertNotEqual(report["claim_state"], "metadata_available")
            # #844 made review the third receipt-gated rung, so a legacy store
            # now keeps its lower rungs through verification rather than review.
            self.assertIn("external effect receipt", blocked["review_observed"])
            self.assertIn("before external effect receipts existed", blocked["review_observed"])
            self.assertIn("external effect receipt", blocked["ci_observed"])
            self.assertIn("before external effect receipts existed", blocked["ci_observed"])
            self.assertIn("external effect receipt", blocked["merged"])

    def test_a_legacy_run_becomes_citable_by_recording_the_gate_again(self) -> None:
        """The documented recovery: re-record the gate and the receipt is minted."""
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run_id = _merge_ready_run(paths)
            run_dir = paths.runtime_runs_dir / run_id
            write_merge_record(run_dir, {"status": "merged", "target_branch": "main", "merge_commit": "abc123"})
            _remove_receipt_store(paths)

            # Three gates since #844, in ladder order: each re-record mints the
            # receipt its own rung now requires.
            write_review_record(run_dir, {"status": "passed", "reviewer": "code-review"})
            write_ci_record(run_dir, {"status": "passed", "provider": "github-actions", "checks": ["unit:passed"]})
            write_merge_record(run_dir, {"status": "merged", "target_branch": "main", "merge_commit": "abc123"})

            report = check_runtime_run(paths, run_id)

            self.assertTrue(report["ok"], report["violations"])
            self.assertEqual(report["claim_state"], "merged")

    def test_a_legacy_merged_run_recovers_through_supported_commands_only(self) -> None:
        """The recovery an operator can actually perform, and the only one.

        `write_ci_record` and `write_merge_record` are library calls. An
        operator has the CLI, and the CLI refused every merge status for a run
        that already said `merge merged`, because its `next_action` was
        `report_merged`. That left the run permanently uncitable: nothing else
        mints a receipt for a past effect.

        This is the documented upgrade path end to end -- two commands, in
        order, each re-recording what the run's own records already say -- and
        it asserts what makes it honest: neither command ever writes a status
        the operator did not observe.
        """
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            base = ["--omh-home", str(paths.omh_home), "--hermes-home", str(paths.hermes_home)]
            run_id = _merge_ready_run(paths)
            run_dir = paths.runtime_runs_dir / run_id
            write_merge_record(run_dir, {"status": "merged", "target_branch": "main", "merge_commit": "abc123"})
            _remove_receipt_store(paths)
            recovery_starts_at = len(_run_events(run_dir))

            before = summarize_delegated_coding_status(paths, run_id)
            self.assertEqual(before["next_action"], "report_merged")
            self.assertEqual(check_runtime_run(paths, run_id)["claim_state"], "verification_observed")

            for command in (
                ["runtime", "review", "--run", run_id, "--status", "passed", "--reviewer", "code-review"],
                ["runtime", "ci", "--run", run_id, "--status", "passed", "--provider", "github-actions", "--check", "unit:passed"],
                ["runtime", "merge", "--run", run_id, "--merged", "--target-branch", "main", "--merge-commit", "abc123"],
            ):
                with self.subTest(command=command[1]):
                    status, _, stderr = run_cli(base + command)
                    self.assertEqual(stderr, "")
                    self.assertEqual(status, 0)

            report = check_runtime_run(paths, run_id)
            after = summarize_delegated_coding_status(paths, run_id)

            self.assertTrue(report["ok"], report["violations"])
            self.assertEqual(report["claim_state"], "merged")
            self.assertIn("merged", report["allowed_claims"])
            # The claim is cited: a receipt id and the surface that observed it.
            self.assertTrue(after["merge"]["receipt"]["observed"])
            self.assertTrue(after["merge"]["receipt"]["receipt_id"])
            self.assertEqual(after["merge"]["receipt"]["acting_surface"], "runtime_merge_record")
            self.assertEqual(after["ci"]["receipt"]["acting_surface"], "runtime_ci_record")
            self.assertEqual(after["review"]["receipt"]["acting_surface"], "runtime_review_record")
            self.assertIn(after["merge"]["receipt"]["receipt_id"], after["safe_summary"])
            # No false intermediate record. Every gate status written during the
            # recovery is one the operator observed; the `not_observed` CI record
            # the old sequence forced never appears.
            recorded = [
                str(event.get("data", {}).get("status", ""))
                for event in _run_events(run_dir)[recovery_starts_at:]
                if event.get("event") in {"review_recorded", "ci_recorded", "merge_recorded"}
            ]
            self.assertEqual(recorded, ["passed", "passed", "merged"])
            self.assertEqual(
                sorted(
                    (receipt["effect_id"], receipt["observed_result"])
                    for receipt in read_external_effect_receipts(paths, run_id=run_id)
                ),
                [
                    (external_effect_id("ci", run_id), "succeeded"),
                    (external_effect_id("merge", run_id), "succeeded"),
                    (external_effect_id("review", run_id), "succeeded"),
                ],
            )

    def test_recording_a_gate_the_run_has_not_reached_is_still_refused(self) -> None:
        """The re-record path admits a restatement, never a contradiction."""
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            base = ["--omh-home", str(paths.omh_home), "--hermes-home", str(paths.hermes_home)]
            run_id = _prepared_run(paths)

            self.assertEqual(summarize_delegated_coding_status(paths, run_id)["next_action"], "dispatch_to_executor")
            for command, refusal in (
                (["runtime", "review", "--run", run_id, "--status", "passed", "--reviewer", "code-review"], "cannot record review passed"),
                (["runtime", "ci", "--run", run_id, "--status", "passed", "--provider", "github-actions"], "cannot record passed CI"),
                (["runtime", "merge", "--run", run_id, "--merged", "--merge-commit", "abc123"], "cannot record merge merged"),
            ):
                with self.subTest(command=command[1]):
                    status, _, stderr = run_cli(base + command)
                    self.assertEqual(status, 2)
                    self.assertIn(refusal, stderr)
                    self.assertIn("dispatch_to_executor", stderr)
            self.assertEqual(read_external_effect_receipts(paths, run_id=run_id), [])


class ReceiptFieldGuardTests(unittest.TestCase):
    """AC3, applied to the field class rather than to the two fields a review named.

    `receipt_id` is what every AC2 citation is built from, and it was the field
    with no guard at build, none in validation, and none at render: a store line
    whose `receipt_id` carried `\\x1b[2K\\r` validated clean and printed straight
    into a rendered success claim.
    """

    def test_the_guard_classes_cover_every_string_field_on_a_receipt(self) -> None:
        classified = (
            set(_REQUIRED_RECEIPT_REFS)
            | set(_OPTIONAL_RECEIPT_REFS)
            | {name for name, _ in _RECEIPT_VOCABULARIES}
            | set(_RECEIPT_TEXT_FIELDS)
        )

        self.assertEqual(classified, set(_RECEIPT_STRING_FIELDS))
        # And the string fields are exactly the receipt keys that are neither a
        # container nor a fixed constant, so a new field cannot be added to the
        # record without landing in a class.
        self.assertEqual(
            set(_RECEIPT_STRING_FIELDS),
            set(EXTERNAL_EFFECT_RECEIPT_KEYS) - {"evidence_refs", "schema_version", "privacy", "claim_boundary"},
        )

    def test_a_control_character_receipt_id_is_refused_flagged_and_never_rendered(self) -> None:
        tampered = "receipt-\x1b[2K\rEVERYTHING IS FINE"
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            base = ["--omh-home", str(paths.omh_home), "--hermes-home", str(paths.hermes_home)]
            run_id = _merge_ready_run(paths)
            _replace_receipts(paths, {external_effect_id("ci", run_id): {"receipt_id": tampered}})
            stored = read_external_effect_receipts(paths, effect_id=external_effect_id("ci", run_id))[-1]

            # Refused on the way in: nothing can append it.
            with self.assertRaises(ExternalEffectReceiptError) as raised:
                append_external_effect_receipt(paths, stored)
            self.assertIn("receipt_id", str(raised.exception))
            # And the build path cannot mint one, because the value the id is
            # derived from goes through the same guard.
            with self.assertRaises(ExternalEffectReceiptError):
                build_external_effect_receipt(
                    effect_id="ci:run-1",
                    action="ci_run",
                    acting_surface="runtime_ci_record",
                    observed_result="succeeded",
                    run_id="run-1",
                    external_ref="github-actions",
                    observed_at="2026-01-01T00:00:00Z\x1b[2K\r",
                )

            # Flagged by validation, at record, store, and runtime level.
            self.assertTrue(any("receipt_id" in error for error in validate_external_effect_receipt(stored)))
            store = validate_external_effect_receipt_store(paths.runtime_external_effect_receipts_path)
            self.assertFalse(store["ok"])
            self.assertFalse(validate_runtime(paths, run_id)["ok"])

            # And never rendered: the citation carries a bounded handle instead.
            rendered = compact_external_effect_receipt(stored)
            self.assertNotIn("\x1b", rendered["receipt_id"])
            self.assertTrue(rendered["receipt_id"].startswith("ref-"))
            citation = success_claim_citation(project_external_effects(paths, run_id))
            self.assertNotIn("\x1b", citation)
            self.assertNotIn("EVERYTHING IS FINE", citation)
            self.assertNotIn("\x1b", summarize_delegated_coding_status(paths, run_id)["safe_summary"])
            status, stdout, _ = run_cli(base + ["runtime", "receipts", "--run", run_id], output_json=False)
            self.assertEqual(status, 0)
            self.assertNotIn("\x1b", stdout)
            self.assertNotIn("EVERYTHING IS FINE", stdout)

    def test_every_identifier_field_refuses_a_control_character(self) -> None:
        for field in (*_REQUIRED_RECEIPT_REFS, *_OPTIONAL_RECEIPT_REFS):
            with self.subTest(field=field):
                record = _valid_receipt(**{field: "handle-\n\x1b[2K"})
                errors = validate_external_effect_receipt(record)
                self.assertTrue(any(field in error for error in errors), errors)

    def test_every_identifier_field_refuses_a_link_and_a_secret(self) -> None:
        for field in (*_REQUIRED_RECEIPT_REFS, *_OPTIONAL_RECEIPT_REFS):
            for value in ("https://example.invalid/run/1", "ghp_0123456789abcdef0123456789abcdef"):
                with self.subTest(field=field, value=value):
                    errors = validate_external_effect_receipt(_valid_receipt(**{field: value}))
                    self.assertTrue(any(field in error for error in errors), errors)

    def test_every_closed_vocabulary_field_renders_only_its_vocabulary(self) -> None:
        for field, vocabulary in _RECEIPT_VOCABULARIES:
            with self.subTest(field=field):
                rendered = compact_external_effect_receipt(_valid_receipt(**{field: "\x1b[2Kmerged"}))
                self.assertEqual(rendered[field], "")
                self.assertIn(compact_external_effect_receipt(_valid_receipt())[field], vocabulary)

    def test_every_free_text_field_renders_redacted_when_it_carries_raw_text(self) -> None:
        for field in _RECEIPT_TEXT_FIELDS:
            with self.subTest(field=field):
                rendered = compact_external_effect_receipt(_valid_receipt(**{field: "line one\nline two"}))
                self.assertEqual(rendered[field], "[redacted]")

    def test_a_result_outside_the_vocabulary_projects_as_unknown_not_as_itself(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run_id = _merge_ready_run(paths)
            _replace_receipts(paths, {external_effect_id("ci", run_id): {"observed_result": "succeeded\x1b[2K"}})

            projection = project_external_effects(paths, run_id)

            self.assertNotIn(
                external_effect_id("ci", run_id),
                [row["effect_id"] for row in projection["succeeded"]],
            )
            self.assertEqual(
                [row["effect_id"] for row in projection["unknown"]],
                [external_effect_id("ci", run_id)],
            )
            self.assertEqual(projection["unknown"][0]["observed_result"], "")


class GateReceiptSelectionTests(unittest.TestCase):
    """One effect, one receipt, whichever surface is asking.

    The shared predicate cannot disagree with itself, but the two *call sites*
    could still hand it different receipts: runtime validation resolved a run's
    identity from `run.json` while the status projection used the directory the
    run was looked up under. When the two differ, one side judged a receipt the
    other never saw.
    """

    def test_both_gate_call_sites_select_the_same_receipt(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run_id = _merge_ready_run(paths)
            run_dir = paths.runtime_runs_dir / run_id
            write_merge_record(run_dir, {"status": "merged", "target_branch": "main", "merge_commit": "abc123"})
            # The run's recorded identity stops matching its directory name.
            renamed = f"{run_id}-relabelled"
            _rewrite_record(run_dir / "run.json", run_id=renamed)
            _append_raw_store_line(
                paths,
                json.dumps(
                    build_external_effect_receipt(
                        effect_id=external_effect_id("merge", renamed),
                        action="merge",
                        acting_surface="runtime_merge_record",
                        observed_result="failed",
                        run_id=renamed,
                        external_ref="abc123",
                    ),
                    sort_keys=True,
                ),
            )

            status = summarize_delegated_coding_status(paths, run_id)
            validation = validate_runtime(paths, run_id)
            claims_side = status["merge"]["receipt"]
            validate_side = select_effect_receipt(
                read_external_effect_receipts(paths, run_id=renamed),
                kind="merge",
                run_id=renamed,
            )

            self.assertEqual(claims_side["receipt_id"], validate_side["receipt_id"])
            self.assertEqual(claims_side["observed_result"], validate_side["observed_result"])
            self.assertEqual(claims_side["observed_result"], "failed")
            # Neither surface claims a merge the receipt says did not happen.
            self.assertFalse(validation["ok"])
            self.assertNotIn("Observed external effect receipts", status["safe_summary"])
            self.assertFalse(receipt_satisfies_success_claim(claims_side, kind="merge", run_id=renamed))

    def test_the_selection_rule_is_latest_in_store_order_for_every_caller(self) -> None:
        receipts = [
            _valid_receipt(receipt_id="receipt-first", observed_result="failed", effect_id="ci:run-1"),
            _valid_receipt(receipt_id="receipt-second", observed_result="succeeded", effect_id="ci:run-1"),
            _valid_receipt(receipt_id="receipt-other", observed_result="succeeded", effect_id="merge:run-1"),
        ]

        self.assertEqual(select_effect_receipt(receipts, kind="ci", run_id="run-1")["receipt_id"], "receipt-second")
        self.assertEqual(
            select_effect_receipt(receipts, kind="ci", run_id="run-1"),
            latest_receipt_in(receipts, external_effect_id("ci", "run-1")),
        )
        self.assertEqual(select_effect_receipt(receipts, kind="merge", run_id="run-2"), {})


class ReceiptHistoryTests(unittest.TestCase):
    def test_a_retry_links_to_the_prior_receipt_and_never_rewrites_it(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run_id = _prepared_run(paths)
            run_dir = paths.runtime_runs_dir / run_id

            write_ci_record(run_dir, {"status": "failed", "provider": "github-actions", "checks": ["unit:failed"]})
            first_lines = paths.runtime_external_effect_receipts_path.read_text(encoding="utf-8").splitlines()
            write_ci_record(run_dir, {"status": "passed", "provider": "github-actions", "checks": ["unit:passed"]})
            second_lines = paths.runtime_external_effect_receipts_path.read_text(encoding="utf-8").splitlines()

            receipts = read_external_effect_receipts(paths, effect_id=external_effect_id("ci", run_id))
            self.assertEqual(len(receipts), 2)
            self.assertEqual(receipts[0]["observed_result"], "failed")
            self.assertEqual(receipts[1]["observed_result"], "succeeded")
            self.assertEqual(receipts[1]["supersedes_receipt_ref"], receipts[0]["receipt_id"])
            self.assertEqual(receipts[0]["supersedes_receipt_ref"], "")
            # Append-only: the earlier line is byte-identical after the retry.
            self.assertEqual(second_lines[: len(first_lines)], first_lines)

    def test_projection_reports_the_latest_state_and_the_full_receipt_count(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run_id = _prepared_run(paths)
            run_dir = paths.runtime_runs_dir / run_id

            write_merge_record(run_dir, {"status": "blocked", "target_branch": "main"})
            write_merge_record(run_dir, {"status": "merged", "target_branch": "main", "merge_commit": "abc123"})

            projection = project_external_effects(paths, run_id)

            self.assertEqual([row["effect_id"] for row in projection["failed"]], [])
            self.assertEqual([row["effect_id"] for row in projection["succeeded"]], [external_effect_id("merge", run_id)])
            self.assertEqual(projection["succeeded"][0]["receipt_count"], 2)
            self.assertEqual(projection["receipt_count"], 2)

    def test_store_validation_rejects_a_supersede_link_to_no_earlier_receipt(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            append_external_effect_receipt(paths, _valid_receipt(supersedes_receipt_ref="receipt-nothing"))

            result = validate_external_effect_receipt_store(paths.runtime_external_effect_receipts_path)

            self.assertFalse(result["ok"])
            self.assertTrue(any("does not name an earlier receipt" in error for error in result["errors"]))

    def test_concurrent_appends_do_not_interleave(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            writers = 12
            barrier = threading.Barrier(writers)
            failures: list[Exception] = []

            def append(index: int) -> None:
                record = build_external_effect_receipt(
                    effect_id=f"ci:run-{index}",
                    action="ci_run",
                    acting_surface="runtime_ci_record",
                    observed_result="succeeded",
                    run_id=f"run-{index}",
                    external_ref=f"github-actions-{index}",
                    summary="x" * 120,
                )
                barrier.wait()
                try:
                    append_external_effect_receipt(paths, record)
                except Exception as exc:  # reported on the main thread, never silently dropped
                    failures.append(exc)

            threads = [threading.Thread(target=append, args=(index,)) for index in range(writers)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(failures, [])
            lines = paths.runtime_external_effect_receipts_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), writers)
            for line in lines:
                self.assertEqual(validate_external_effect_receipt(json.loads(line)), [])
            self.assertEqual(
                sorted(json.loads(line)["run_id"] for line in lines),
                sorted(f"run-{index}" for index in range(writers)),
            )

    def test_concurrent_retries_of_one_effect_keep_an_unbroken_supersede_chain(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            writers = 8
            barrier = threading.Barrier(writers)
            failures: list[Exception] = []

            def retry(index: int) -> None:
                barrier.wait()
                try:
                    record_external_effect(
                        paths,
                        effect_id="ci:run-shared",
                        action="ci_run",
                        acting_surface="runtime_ci_record",
                        observed_result="failed",
                        run_id="run-shared",
                        external_ref=f"github-actions-{index}",
                    )
                except Exception as exc:  # reported on the main thread, never silently dropped
                    failures.append(exc)

            threads = [threading.Thread(target=retry, args=(index,)) for index in range(writers)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(failures, [])
            result = validate_external_effect_receipt_store(paths.runtime_external_effect_receipts_path)
            receipts = read_external_effect_receipts(paths, effect_id="ci:run-shared")

            self.assertTrue(result["ok"], result["errors"])
            self.assertEqual(len(receipts), writers)
            # Every retry but the first links to exactly one distinct predecessor.
            links = [receipt["supersedes_receipt_ref"] for receipt in receipts[1:]]
            self.assertEqual(links, [receipt["receipt_id"] for receipt in receipts[:-1]])
            self.assertEqual(receipts[0]["supersedes_receipt_ref"], "")


class ReceiptRedactionTests(unittest.TestCase):
    """AC3: no secret, prompt, payload, or URL survives into a receipt."""

    def test_a_url_reference_is_refused(self) -> None:
        with self.assertRaises(ExternalEffectReceiptError) as raised:
            build_external_effect_receipt(
                effect_id="ci:run-1",
                action="ci_run",
                acting_surface="runtime_ci_record",
                observed_result="succeeded",
                external_ref="https://github.example/checks/dco",
            )
        self.assertIn("opaque identifier, not a URL", str(raised.exception))

    def test_a_secret_shaped_reference_is_refused(self) -> None:
        for candidate in ("slack-token-abcdefgh", "ghp_abcdefghijklmnopqrstuvwxyz012345", "my-api_key-1"):
            with self.subTest(candidate=candidate), self.assertRaises(ExternalEffectReceiptError):
                build_external_effect_receipt(
                    effect_id="ci:run-1",
                    action="ci_run",
                    acting_surface="runtime_ci_record",
                    observed_result="succeeded",
                    external_ref=candidate,
                )

    def test_free_text_is_bounded_and_secrets_are_redacted(self) -> None:
        long_summary = build_external_effect_receipt(
            effect_id="ci:run-1",
            action="ci_run",
            acting_surface="runtime_ci_record",
            observed_result="failed",
            summary="a" * (MAX_SUMMARY_CHARS * 4),
        )
        secret_summary = build_external_effect_receipt(
            effect_id="ci:run-2",
            action="ci_run",
            acting_surface="runtime_ci_record",
            observed_result="failed",
            summary="failed with authorization bearer abcdef",
        )

        self.assertEqual(len(long_summary["summary"]), MAX_SUMMARY_CHARS)
        self.assertEqual(secret_summary["summary"], "[redacted]")
        self.assertEqual(validate_external_effect_receipt(long_summary), [])
        self.assertEqual(validate_external_effect_receipt(secret_summary), [])

    def test_summary_refuses_links_paths_secrets_and_raw_text(self) -> None:
        """DEFECT 3: `summary` was the one free-text field with no guard."""
        unsafe = {
            "url": "ci run finished, console at https://ci.internal.example.com/job/42/console",
            "absolute_path": "log written to /Users/someone/.hermes/runtime/run.log",
            "windows_path": "log written to C:\\Users\\someone\\hermes\\run.log",
            "secret": "authorization bearer abcdefghijklmnop",
            "prompt": "You are a helpful assistant.\nImplement the change and report back.",
            "query": "checks?run=42",
        }
        for label, text in unsafe.items():
            with self.subTest(label=label):
                built = build_external_effect_receipt(
                    effect_id="ci:run-1",
                    action="ci_run",
                    acting_surface="runtime_ci_record",
                    observed_result="failed",
                    summary=text,
                )
                # Redacted on the way in ...
                self.assertEqual(built["summary"], "[redacted]")
                self.assertEqual(validate_external_effect_receipt(built), [])
                # ... refused on the way back, so a hand-written store line
                # carrying the same text is a violation rather than a render.
                errors = validate_external_effect_receipt(_valid_receipt(summary=text))
                self.assertTrue(any("summary must not carry" in error for error in errors), errors)
                # ... and never rendered verbatim.
                self.assertEqual(
                    compact_external_effect_receipt(_valid_receipt(summary=text))["summary"], "[redacted]"
                )

    def test_a_safe_summary_survives_bounded_and_intact(self) -> None:
        built = build_external_effect_receipt(
            effect_id="ci:run-1",
            action="ci_run",
            acting_surface="runtime_ci_record",
            observed_result="failed",
            summary="  unit suite failed on 2 of 40 checks  ",
        )
        self.assertEqual(built["summary"], "unit suite failed on 2 of 40 checks")
        self.assertEqual(validate_external_effect_receipt(built), [])

    def test_supersedes_receipt_ref_goes_through_the_reference_guard(self) -> None:
        """DEFECT 3: the one reference field that bypassed every guard."""
        for unsafe in (
            "https://ci.internal.example.com/job/42/console",
            "slack-token-abcdefgh",
            "receipt with spaces",
            "a" * 400,
        ):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(ExternalEffectReceiptError):
                    build_external_effect_receipt(
                        effect_id="ci:run-1",
                        action="ci_run",
                        acting_surface="runtime_ci_record",
                        observed_result="failed",
                        supersedes_receipt_ref=unsafe,
                    )
                errors = validate_external_effect_receipt(_valid_receipt(supersedes_receipt_ref=unsafe))
                self.assertTrue(any("supersedes_receipt_ref" in error for error in errors), errors)

    def test_no_raw_or_hidden_key_can_serialize_into_a_receipt(self) -> None:
        for key in (
            "prompt",
            "raw_prompt",
            "payload",
            "raw_payload",
            "message",
            "transcript",
            "reasoning",
            "stdout",
            "url",
        ):
            with self.subTest(key=key):
                errors = validate_external_effect_receipt(_valid_receipt(**{key: "private user request"}))
                self.assertTrue(any("raw or hidden keys" in error for error in errors), errors)

    def test_unsupported_keys_are_rejected_outright(self) -> None:
        errors = validate_external_effect_receipt(_valid_receipt(operator_note="looks merged to me"))
        self.assertTrue(any("unsupported keys" in error for error in errors), errors)

    def test_redacted_reference_folds_links_and_secrets_but_keeps_opaque_handles(self) -> None:
        self.assertEqual(redacted_external_effect_ref("slack:message-1"), "slack:message-1")
        self.assertEqual(redacted_external_effect_ref(""), "")
        for unsafe in ("https://github.example/checks/dco", "slack-token-abcdefgh", "a" * 400):
            with self.subTest(unsafe=unsafe):
                folded = redacted_external_effect_ref(unsafe)
                self.assertTrue(folded.startswith("ref-"))
                self.assertNotIn(unsafe, folded)
                # Stable, so rows keyed by the reference still deduplicate.
                self.assertEqual(folded, redacted_external_effect_ref(unsafe))

    def test_rendering_a_receipt_redacts_its_reference(self) -> None:
        compact = compact_external_effect_receipt(
            {**_valid_receipt(), "external_ref": "github-actions", "summary": "ci passed"}
        )
        self.assertEqual(compact["external_ref"], "github-actions")
        self.assertNotIn("claim_boundary", compact)
        self.assertNotIn("privacy", compact)

    def test_export_runtime_redacts_the_external_reference(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run_id = _prepared_run(paths)
            run_dir = paths.runtime_runs_dir / run_id
            write_ci_record(run_dir, {"status": "failed", "provider": "github-actions", "checks": ["unit:failed"]})

            exported = export_runtime(paths, redacted=True, run_id=run_id)
            unredacted = export_runtime(paths, redacted=False, run_id=run_id)

            receipt = exported["runs"][0]["external_effect_receipts"][0]
            self.assertNotIn("github-actions", json.dumps(receipt))
            self.assertIn("github-actions", json.dumps(unredacted["runs"][0]["external_effect_receipts"][0]))
            self.assertEqual(receipt["external_ref"], "[redacted]")
            # The citation survives redaction: it names who acted, not what was sent.
            self.assertEqual(receipt["acting_surface"], "runtime_ci_record")
            self.assertTrue(receipt["receipt_id"])


class ReceiptStoreScopeTests(unittest.TestCase):
    """DEFECT 5: the store is runtime-wide, so its faults are the store's."""

    def test_one_corrupt_line_faults_the_store_once_and_no_unrelated_run(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run_ids = [_prepared_run(paths) for _ in range(3)]
            _append_raw_store_line(paths, '{"schema_version": "external_effect_re')

            everything = validate_runtime(paths)
            store = everything["external_effect_receipts"]

            self.assertEqual(store["schema_version"], "external_effect_receipt_store_validation/v1")
            self.assertEqual(len(store["errors"]), 1, store["errors"])
            self.assertIn("external_effect_receipts.jsonl:1", store["errors"][0])
            self.assertFalse(everything["ok"])
            # Not one of the three runs is faulted by a line none of them wrote.
            for run in everything["runs"]:
                self.assertTrue(run["ok"], run["errors"])
            for run_id in run_ids:
                with self.subTest(run_id=run_id):
                    scoped = validate_runtime(paths, run_id)
                    report = check_runtime_run(paths, run_id)
                    self.assertTrue(scoped["ok"], scoped)
                    self.assertTrue(report["ok"], report["violations"])
                    self.assertEqual(report["claim_state"], "handoff_prepared")

    def test_a_broken_receipt_faults_only_the_run_that_owns_it(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            owner = _prepared_run(paths)
            bystander = _prepared_run(paths)
            write_ci_record(
                paths.runtime_runs_dir / owner,
                {"status": "failed", "provider": "github-actions", "checks": ["unit:failed"]},
            )
            _replace_receipts(paths, {external_effect_id("ci", owner): {"acting_surface": "operator_said_so"}})

            everything = validate_runtime(paths)
            by_run = {run["run_id"]: run for run in everything["runs"]}

            self.assertFalse(by_run[owner]["ok"])
            self.assertTrue(any("acting_surface is unsupported" in error for error in by_run[owner]["errors"]))
            self.assertTrue(by_run[bystander]["ok"], by_run[bystander]["errors"])
            # A run-owned receipt fault reaches the conformance report, so an
            # invalid run is never reported as invalid with an empty reason.
            owner_report = check_runtime_run(paths, owner)
            self.assertFalse(owner_report["ok"])
            self.assertTrue(any("acting_surface is unsupported" in v for v in owner_report["violations"]))
            self.assertTrue(check_runtime_run(paths, bystander)["ok"])

    def test_validating_and_exporting_parse_the_store_once_whatever_the_run_count(self) -> None:
        """DEFECT 5: O(runs x receipts) is what made a full store unusable."""
        reads: list[int] = []
        for run_count in (2, 6):
            with self.subTest(run_count=run_count), TemporaryDirectory() as tmp:
                paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
                for _ in range(run_count):
                    run_id = _prepared_run(paths)
                    write_ci_record(
                        paths.runtime_runs_dir / run_id,
                        {"status": "failed", "provider": "github-actions", "checks": ["unit:failed"]},
                    )
                reads.append(
                    (
                        _store_parse_count(paths, lambda: validate_runtime(paths)),
                        _store_parse_count(paths, lambda: export_runtime(paths, redacted=False)),
                    )
                )
        self.assertEqual(reads[0], reads[1], "store parses must not grow with the number of runs")

    def test_validating_a_run_never_reaches_outside_the_home_it_lives_in(self) -> None:
        """DEFECT 8: every path is derived from the run directory being validated."""
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run_id = _prepared_run(paths)
            run_dir = paths.runtime_runs_dir / run_id

            self.assertEqual(
                runtime_store_path_for_run_dir(run_dir, "external_effect_receipts.jsonl"),
                paths.runtime_external_effect_receipts_path,
            )
            self.assertIsNone(runtime_store_path_for_run_dir(Path(tmp) / "loose-run", "external_effect_receipts.jsonl"))

            def refuse(_self: Path) -> Path:
                raise AssertionError("receipt paths must never be expanded from the developer's home")

            with patch.object(Path, "expanduser", refuse):
                write_ci_record(run_dir, {"status": "failed", "provider": "github-actions", "checks": ["unit:failed"]})
                self.assertTrue(validate_run_dir(run_dir)["ok"])
            self.assertEqual(len(read_external_effect_receipts(paths, run_id=run_id)), 1)


class ReceiptStoreDurabilityTests(unittest.TestCase):
    """DEFECT 6: a torn write must not take the next record with it."""

    def test_a_torn_last_line_cannot_swallow_the_next_append(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            record_external_effect(
                paths,
                effect_id="ci:run-1",
                action="ci_run",
                acting_surface="runtime_ci_record",
                observed_result="failed",
                run_id="run-1",
                external_ref="github-actions",
            )
            path = paths.runtime_external_effect_receipts_path
            # A short write: the process died mid-line, so the tail has no
            # newline terminating it.
            with path.open("a", encoding="utf-8") as handle:
                handle.write('{"schema_version": "external_effect_re')

            record_external_effect(
                paths,
                effect_id="ci:run-2",
                action="ci_run",
                acting_surface="runtime_ci_record",
                observed_result="succeeded",
                run_id="run-2",
                external_ref="github-actions",
            )

            lines = path.read_text(encoding="utf-8").splitlines()
            store = validate_external_effect_receipt_store(path)

            self.assertEqual(len(lines), 3)
            self.assertEqual(json.loads(lines[0])["run_id"], "run-1")
            self.assertEqual(lines[1], '{"schema_version": "external_effect_re')
            self.assertEqual(json.loads(lines[2])["run_id"], "run-2")
            # The torn line is the only casualty, and the store says so once.
            self.assertEqual(len(store["errors"]), 1, store["errors"])
            self.assertIn(":2:", store["errors"][0])
            self.assertEqual([receipt["run_id"] for receipt in read_external_effect_receipts(paths)], ["run-1", "run-2"])

    def test_a_mint_failure_neither_raises_nor_loses_the_receipt(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            # The store path is unwritable: a directory stands where the file
            # belongs, which is what an OS-level failure looks like from here.
            store_path = paths.runtime_external_effect_receipts_path
            store_path.parent.mkdir(parents=True, exist_ok=True)
            store_path.mkdir()

            failed = mint_external_effect_receipt(
                paths,
                effect_id="delivery:prep-1",
                action="message_sent",
                acting_surface="adapter_quality_delivery",
                observed_result="succeeded",
                external_ref="slack:message-1",
            )

            self.assertEqual(failed["schema_version"], EXTERNAL_EFFECT_MINT_RESULT_SCHEMA_VERSION)
            self.assertFalse(failed["minted"])
            self.assertEqual(failed["outcome"], "not_written")
            self.assertTrue(failed["error"])
            self.assertEqual(failed["receipt_id"], "")
            # The failure is on record, not just returned.
            logged = read_external_effect_mint_failures(store_path)
            self.assertEqual(len(logged), 1)
            self.assertEqual(logged[0]["effect_id"], "delivery:prep-1")
            self.assertEqual(logged[0]["outcome"], "not_written")

            store_path.rmdir()
            recovered = mint_external_effect_receipt(
                paths,
                effect_id="delivery:prep-1",
                action="message_sent",
                acting_surface="adapter_quality_delivery",
                observed_result="succeeded",
                external_ref="slack:message-1",
            )

            self.assertTrue(recovered["minted"])
            self.assertEqual(recovered["outcome"], "recorded")
            self.assertEqual(len(read_external_effect_receipts(paths, effect_id="delivery:prep-1")), 1)
            self.assertEqual(
                validate_external_effect_receipt_store(store_path)["mint_failure_count"],
                1,
            )

    def test_a_refused_mint_is_returned_and_logged_instead_of_raised(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")

            refused = mint_external_effect_receipt(
                paths,
                effect_id="ci:run-1",
                action="ci_run",
                acting_surface="runtime_ci_record",
                observed_result="succeeded",
                run_id="run-1",
            )

            self.assertFalse(refused["minted"])
            self.assertEqual(refused["outcome"], "refused")
            self.assertIn("succeeded requires an external_ref", refused["error"])
            self.assertEqual(read_external_effect_receipts(paths), [])
            self.assertEqual(
                [failure["outcome"] for failure in read_external_effect_mint_failures(
                    paths.runtime_external_effect_receipts_path
                )],
                ["refused"],
            )

    def test_an_unwritable_store_does_not_fail_the_record_that_produced_it(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run_id = _prepared_run(paths)
            run_dir = paths.runtime_runs_dir / run_id
            store_path = paths.runtime_external_effect_receipts_path
            store_path.parent.mkdir(parents=True, exist_ok=True)
            store_path.mkdir()

            record = write_ci_record(run_dir, {"status": "failed", "provider": "github-actions", "checks": ["unit:failed"]})

            self.assertEqual(record["status"], "failed")
            events = [event for event in show_run(paths, run_id)["events"] if isinstance(event, dict)]
            unreceipted = [event for event in events if event.get("event") == "external_effect_receipt_not_recorded"]
            self.assertEqual(len(unreceipted), 1)
            self.assertEqual(unreceipted[0]["level"], "warning")
            self.assertEqual(unreceipted[0]["data"]["outcome"], "not_written")

    def test_concurrent_mint_failures_do_not_interleave(self) -> None:
        """The failure log took no lock, unlike the two receipt-store appends.

        It is the record that has to survive when things are already going
        wrong. Unlocked, two producers failing at once compute the same end
        offset and one write lands on top of the other, so the log loses exactly
        the lines it exists to keep.
        """
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            store_path = paths.runtime_external_effect_receipts_path
            store_path.parent.mkdir(parents=True, exist_ok=True)
            writers = 16
            barrier = threading.Barrier(writers)
            raised: list[Exception] = []

            def failing_mint(index: int) -> None:
                barrier.wait()
                try:
                    # Refused, never written: `succeeded` with nothing to name.
                    mint_external_effect_receipt_at(
                        store_path,
                        effect_id=f"ci:run-{index}",
                        action="ci_run",
                        acting_surface="runtime_ci_record",
                        observed_result="succeeded",
                        run_id=f"run-{index}",
                    )
                except Exception as exc:  # reported on the main thread, never silently dropped
                    raised.append(exc)

            threads = [threading.Thread(target=failing_mint, args=(index,)) for index in range(writers)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(raised, [])
            failures = read_external_effect_mint_failures(store_path)
            self.assertEqual(len(failures), writers)
            self.assertEqual(
                sorted(failure["run_id"] for failure in failures),
                sorted(f"run-{index}" for index in range(writers)),
            )
            self.assertEqual({failure["outcome"] for failure in failures}, {"refused"})
            # Every line parses: none was written on top of another.
            lines = external_effect_mint_failures_path(store_path).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), writers)
            for line in lines:
                json.loads(line)


class ReceiptIdempotencyTests(unittest.TestCase):
    """DEFECT 7: one observation is one receipt, and a chain is a line."""

    def test_repeated_identical_writes_mint_exactly_one_receipt(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run_id = _prepared_run(paths)
            run_dir = paths.runtime_runs_dir / run_id
            ci = {"status": "failed", "provider": "github-actions", "checks": ["unit:failed"]}

            for _ in range(3):
                write_ci_record(run_dir, ci)

            receipts = read_external_effect_receipts(paths, effect_id=external_effect_id("ci", run_id))
            projection = project_external_effects(paths, run_id)

            self.assertEqual(len(receipts), 1)
            self.assertEqual(projection["receipt_count"], 1)
            self.assertEqual(projection["failed"][0]["receipt_count"], 1)

    def test_a_changed_observation_still_mints_a_linked_receipt(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            run_id = _prepared_run(paths)
            run_dir = paths.runtime_runs_dir / run_id

            write_ci_record(run_dir, {"status": "failed", "provider": "github-actions", "checks": ["unit:failed"]})
            write_ci_record(run_dir, {"status": "failed", "provider": "github-actions", "checks": ["unit:failed"]})
            write_ci_record(run_dir, {"status": "passed", "provider": "github-actions", "checks": ["unit:passed"]})

            receipts = read_external_effect_receipts(paths, effect_id=external_effect_id("ci", run_id))

            self.assertEqual([receipt["observed_result"] for receipt in receipts], ["failed", "succeeded"])
            self.assertEqual(receipts[1]["supersedes_receipt_ref"], receipts[0]["receipt_id"])

    def test_mint_reports_a_replay_as_already_recorded(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            first = mint_external_effect_receipt(
                paths,
                effect_id="delivery:prep-1",
                action="message_sent",
                acting_surface="adapter_quality_delivery",
                observed_result="succeeded",
                external_ref="slack:message-1",
            )
            replay = mint_external_effect_receipt(
                paths,
                effect_id="delivery:prep-1",
                action="message_sent",
                acting_surface="adapter_quality_delivery",
                observed_result="succeeded",
                external_ref="slack:message-1",
            )

            self.assertTrue(first["minted"])
            self.assertEqual(first["outcome"], "recorded")
            self.assertFalse(replay["minted"])
            self.assertEqual(replay["outcome"], "already_recorded")
            self.assertEqual(replay["receipt_id"], first["receipt_id"])
            self.assertEqual(len(read_external_effect_receipts(paths)), 1)

    def test_store_validation_rejects_a_self_superseding_receipt(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            receipt = _valid_receipt()
            _append_raw_store_line(
                paths, json.dumps({**receipt, "supersedes_receipt_ref": receipt["receipt_id"]}, sort_keys=True)
            )

            result = validate_external_effect_receipt_store(paths.runtime_external_effect_receipts_path)

            self.assertFalse(result["ok"])
            self.assertTrue(any("must not name itself" in error for error in result["errors"]), result["errors"])

    def test_a_run_scoped_report_never_invents_a_broken_chain(self) -> None:
        """Scoping the report must not turn an out-of-scope predecessor into a gap."""
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            first = record_external_effect(
                paths,
                effect_id="delivery:prep-1",
                action="message_sent",
                acting_surface="adapter_quality_delivery",
                observed_result="failed",
                external_ref="slack:message-1",
            )
            second = record_external_effect(
                paths,
                effect_id="delivery:prep-1",
                action="message_sent",
                acting_surface="adapter_quality_delivery",
                observed_result="succeeded",
                run_id="run-1",
                external_ref="slack:message-2",
            )

            self.assertEqual(second["supersedes_receipt_ref"], first["receipt_id"])
            scoped = validate_external_effect_receipt_store(
                paths.runtime_external_effect_receipts_path, run_id="run-1"
            )
            self.assertTrue(scoped["ok"], scoped["errors"])
            self.assertEqual(scoped["receipt_count"], 1)

    def test_store_validation_rejects_a_forked_supersede_chain(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            root = _valid_receipt()
            append_external_effect_receipt(paths, root)
            for suffix in ("aaa", "bbb"):
                _append_raw_store_line(
                    paths,
                    json.dumps(
                        {
                            **root,
                            "receipt_id": f"{root['receipt_id']}-{suffix}",
                            "supersedes_receipt_ref": root["receipt_id"],
                        },
                        sort_keys=True,
                    ),
                )

            result = validate_external_effect_receipt_store(paths.runtime_external_effect_receipts_path)

            self.assertFalse(result["ok"])
            self.assertTrue(any("forks the chain" in error for error in result["errors"]), result["errors"])


class ReceiptCliTests(unittest.TestCase):
    def test_receipts_view_is_read_only_plain_text_by_default(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            base = ["--omh-home", str(paths.omh_home), "--hermes-home", str(paths.hermes_home)]
            run_id = _prepared_run(paths)
            run_dir = paths.runtime_runs_dir / run_id
            write_ci_record(run_dir, {"status": "failed", "provider": "github-actions", "checks": ["unit:failed"]})

            status, stdout, stderr = run_cli(base + ["runtime", "receipts", "--run", run_id], output_json=False)

            self.assertEqual(stderr, "")
            self.assertEqual(status, 0)
            self.assertNotIn("{", stdout)
            self.assertIn("External effect receipts (1 shown)", stdout)
            self.assertIn("ci_run — failed — runtime_ci_record", stdout)
            self.assertIn("failed 1", stdout)

            status, stdout, stderr = run_cli(base + ["runtime", "receipts", "--run", run_id, "--json"])

            self.assertEqual(stderr, "")
            self.assertEqual(status, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["schema_version"], "runtime_external_effect_receipts_view/v1")
            self.assertTrue(payload["store_ok"])
            self.assertEqual(payload["receipts"][0]["acting_surface"], "runtime_ci_record")
            self.assertEqual(payload["projection"]["failed"][0]["effect_id"], external_effect_id("ci", run_id))

    def test_the_receipts_view_and_delegation_status_report_the_same_effects(self) -> None:
        """Two surfaces reading one run at one instant must not contradict.

        `requested` and `attempted` are projected from what the run's own
        records ask for. A projection built from the receipt store alone can
        never reach either, so this view printed `requested 0` while
        `omh runtime delegation-status` printed `requested 1` for the same run.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            base = ["--omh-home", str(paths.omh_home), "--hermes-home", str(paths.hermes_home)]
            run_id = _executed_run(paths)

            delegation = summarize_delegated_coding_status(paths, run_id)["external_effects"]
            status, stdout, stderr = run_cli(base + ["runtime", "receipts", "--run", run_id, "--json"])

            self.assertEqual(stderr, "")
            self.assertEqual(status, 0)
            self.assertEqual(json.loads(stdout)["projection"], delegation)
            # And the run genuinely has both classes, so the agreement is not
            # two empty projections agreeing with each other.
            self.assertEqual(len(delegation["requested"]), 1)
            self.assertEqual(len(delegation["succeeded"]), 1)

            _, text, _ = run_cli(base + ["runtime", "receipts", "--run", run_id], output_json=False)
            self.assertIn("Effects: requested 1, attempted 0, succeeded 1, failed 0, unknown 0", text)

    def test_the_receipts_view_still_answers_for_a_run_with_no_records(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            base = ["--omh-home", str(paths.omh_home), "--hermes-home", str(paths.hermes_home)]

            status, stdout, stderr = run_cli(base + ["runtime", "receipts", "--run", "run-that-never-existed", "--json"])

            self.assertEqual(stderr, "")
            self.assertEqual(status, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["projection"]["effect_count"], 0)
            self.assertEqual(payload["receipts"], [])

    def test_no_cli_surface_can_mint_a_receipt(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = resolve_paths(root / ".omh", root / ".hermes")
            base = ["--omh-home", str(paths.omh_home), "--hermes-home", str(paths.hermes_home)]

            for attempt in (
                ["runtime", "receipts", "--record", "--result", "succeeded"],
                ["runtime", "receipt", "--run", "run-1"],
                ["runtime", "receipts", "--acting-surface", "adapter_quality_delivery"],
            ):
                with self.subTest(attempt=attempt), self.assertRaises(SystemExit):
                    run_cli(base + attempt, output_json=False)
            self.assertEqual(read_external_effect_receipts(paths), [])


if __name__ == "__main__":
    unittest.main()
