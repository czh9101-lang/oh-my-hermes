from __future__ import annotations

import hashlib
import json
import subprocess
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from _cli_harness import run_cli
from _local_package import load_local_package

load_local_package()

from omh.browser_workflow_learning import (
    fixture_digest,
    parse_browser_workflow_trace,
    replay_browser_workflow_trace,
    trace_digest,
    validate_browser_workflow_trace,
)
from omh.workflows.browser_workflow_learning_store import approve_browser_workflow_trace, read_browser_workflow_trace, replay_stored_browser_workflow_trace, resolved_browser_workflow_promotion_reference, resolved_browser_workflow_trace_reference, write_browser_workflow_trace
from omh.system.paths import OmhPaths
from omh.workflows.web_visual_qa import build_web_visual_qa_package, save_web_visual_qa_package
from omh.workflows.web_visual_qa_validation import validate_web_visual_qa_package


class BrowserWorkflowLearningTests(unittest.TestCase):
    def test_parse_trace_when_selected_terminal_success_is_bound_and_redacted(self) -> None:
        trace = parse_browser_workflow_trace(_trace("click", {"url": "https://bücher.example/path?token=secret#private", "cookie": "secret", "full_dom": "private markup"}))

        self.assertEqual(trace["origins"], ["https://xn--bcher-kva.example"])
        self.assertEqual(trace["metadata"], {})
        self.assertNotIn("cookie", trace["metadata"])
        self.assertNotIn("full_dom", trace["metadata"])
        self.assertEqual(trace["lifecycle"]["status"], "pending_approval")

    def test_store_trace_when_git_root_is_observed_deduplicates_and_binds_approval(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _git(root)
            trace = write_browser_workflow_trace(_trace("click"), root)
            duplicate = write_browser_workflow_trace(_trace("click"), root)
            approved = approve_browser_workflow_trace(root, str(trace["trace_id"]), str(trace["digest"]))
            reference = resolved_browser_workflow_trace_reference(root, str(trace["trace_id"]))

            self.assertEqual(duplicate["trace_id"], trace["trace_id"])
            self.assertEqual(approved["lifecycle"]["approved_digest"], trace["digest"])
            self.assertEqual(reference["schema_version"], "browser_workflow_trace_reference/v1")
            self.assertEqual(read_browser_workflow_trace(root, str(trace["trace_id"]))["lifecycle"]["status"], "approved")
            self.assertEqual(len([path for path in (root / ".omh" / "web-visual-qa" / "traces").glob("bwt-*.json")]), 1)

    def test_cli_trace_when_project_local_inspects_approves_and_marks_drift(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _git(root)
            trace_input = root / "trace.json"
            trace_input.write_text(json.dumps(_trace("click")), encoding="utf-8")
            status, stdout, stderr = run_cli(["web-qa", "trace", "record", "--project-root", str(root), "--input", str(trace_input)])
            self.assertEqual(status, 0, stderr)
            trace = json.loads(stdout)
            status, stdout, stderr = run_cli(["web-qa", "trace", "approve", "--project-root", str(root), "--trace-id", trace["trace_id"], "--digest", trace["digest"]])
            self.assertEqual(status, 0, stderr)
            observation = root / "observation.json"
            observation.write_text(json.dumps({"fixture_id": "negative"}), encoding="utf-8")
            status, stdout, stderr = run_cli(["web-qa", "trace", "replay", "--project-root", str(root), "--trace-id", trace["trace_id"], "--observation", str(observation)])
            self.assertEqual(status, 0, stderr)
            self.assertEqual(json.loads(stdout)["status"], "stale")
            status, stdout, stderr = run_cli(["web-qa", "trace", "status", "--project-root", str(root), "--trace-id", trace["trace_id"]])
            self.assertEqual(status, 0, stderr)
            self.assertEqual(json.loads(stdout)["lifecycle"]["status"], "stale")

    def test_parser_trace_when_bounds_or_source_proof_are_invalid_fails_closed(self) -> None:
        oversized_steps = _trace("click")
        oversized_steps["steps"] = oversized_steps["steps"] * 65
        oversized_locators = _trace("click")
        oversized_locators["steps"][0]["locators"] *= 9
        missing_success = _trace("click")
        missing_success["source"]["success"]["state"] = "failed"
        wrong_project_binding = _trace("click")
        wrong_project_binding["source"]["binding"]["project_identity"] = "0" * 64

        for candidate in (oversized_steps, oversized_locators, missing_success, wrong_project_binding):
            with self.assertRaises(Exception) as raised:
                parse_browser_workflow_trace(candidate)
            self.assertIn("browser trace", str(raised.exception))

    def test_rehashed_invalid_trace_cannot_become_replay_evidence(self) -> None:
        forged = parse_browser_workflow_trace(_trace("click"))
        forged["schema_version"] = "wrong/v9"
        forged["origins"] = ["https://example.com"]
        forged["steps"] = [{"action": "unapproved_action", "locators": []}]
        forged["raw_response_body"] = "synthetic-private-marker"
        forged["lifecycle"] = {
            "revision": 999,
            "status": "approved",
            "approved_digest": "",
        }
        digest = trace_digest(forged)
        forged["digest"] = digest
        forged["trace_id"] = f"bwt-{digest[:24]}"
        forged["lifecycle"]["approved_digest"] = digest

        errors = validate_browser_workflow_trace(forged)

        self.assertTrue(errors)

    def test_replay_trace_when_drift_or_retry_conditions_change_stops_without_autoheal(self) -> None:
        trace = parse_browser_workflow_trace(_trace("read"))

        zero = replay_browser_workflow_trace(trace, {"fixture_id": "negative"})
        mutating = parse_browser_workflow_trace(_trace("submit"))
        ambiguous = replay_browser_workflow_trace(mutating, {"fixture_id": "negative"})
        retry = replay_browser_workflow_trace(trace, {"fixture_id": "transient"})
        no_retry = replay_browser_workflow_trace(mutating, {"fixture_id": "transient"})

        self.assertEqual((zero["status"], ambiguous["status"]), ("stale", "quarantined"))
        self.assertEqual((retry["status"], no_retry["status"]), ("retry_once", "stale"))
        self.assertEqual(
            replay_browser_workflow_trace(trace, {"locator_matches": [1], "schema_valid": True})["status"],
            "not_suitable",
        )

    def test_stored_replay_consumes_the_only_safe_retry_and_quarantines_repeated_drift(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _git(root)
            trace = write_browser_workflow_trace(_trace("read"), root)

            retry = replay_stored_browser_workflow_trace(root, str(trace["trace_id"]), {"fixture_id": "transient"})
            exhausted = replay_stored_browser_workflow_trace(root, str(trace["trace_id"]), {"fixture_id": "transient"})
            self.assertEqual((retry["status"], exhausted["status"]), ("retry_once", "stale"))
            self.assertTrue(read_browser_workflow_trace(root, str(trace["trace_id"]))["lifecycle"]["retry_used"])

            drift = write_browser_workflow_trace(_trace("click"), root)
            trace_id = str(drift["trace_id"])
            approve_browser_workflow_trace(root, trace_id, str(drift["digest"]))
            stale = replay_stored_browser_workflow_trace(root, trace_id, {"fixture_id": "negative"})
            before_replay = read_browser_workflow_trace(root, trace_id)
            duplicate = replay_stored_browser_workflow_trace(root, trace_id, {"fixture_id": "negative"})
            self.assertEqual(before_replay, read_browser_workflow_trace(root, trace_id))
            quarantined = replay_stored_browser_workflow_trace(root, trace_id, {"fixture_id": "negative_fresh"})

            self.assertEqual((stale["status"], duplicate["status"], quarantined["status"]), ("stale", "stale", "quarantined"))
            self.assertEqual(read_browser_workflow_trace(root, trace_id)["lifecycle"]["status"], "quarantined")
            self.assertEqual(
                replay_stored_browser_workflow_trace(root, trace_id, {"fixture_id": "positive"})["status"],
                "quarantined",
            )

    def test_web_qa_trace_when_project_resolver_mints_a_reference_is_admitted(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _git(root)
            trace = write_browser_workflow_trace(_trace("click"), root)
            reference = resolved_browser_workflow_trace_reference(
                root,
                str(approve_browser_workflow_trace(root, str(trace["trace_id"]), str(trace["digest"]))["trace_id"]),
            )
            package = build_web_visual_qa_package(
                package_id="resolved-trace",
                target="Checkout",
                criteria=[{"criterion_id": "layout", "label": "Layout", "pass_rule": "No overlap", "severity": "blocking"}],
                interaction_traces=[reference],
            )

            self.assertFalse(any(error.startswith("interaction_traces") for error in validate_web_visual_qa_package(package)))
            package["interaction_traces"] = [{**reference, "digest": "0" * 64}]
            with self.assertRaisesRegex(ValueError, "does not resolve to the project-bound trace"):
                save_web_visual_qa_package(
                    OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes", omh_home_named=False),
                    package,
                )

    def test_web_qa_trace_when_arbitrary_dictionary_is_supplied_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "resolved browser workflow trace reference"):
            build_web_visual_qa_package(
                package_id="typed-trace",
                target="Checkout",
                criteria=[{"criterion_id": "layout", "label": "Layout", "pass_rule": "No overlap", "severity": "blocking"}],
                interaction_traces=[{"trace_id": "untrusted"}],
            )

    def test_replay_trace_when_locator_is_ambiguous_quarantines_mutating_step(self) -> None:
        trace = parse_browser_workflow_trace(_trace("submit"))

        result = replay_browser_workflow_trace(
            trace,
            {"fixture_id": "negative"},
        )

        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(result["reason"], "ambiguous_locator")
        self.assertFalse(result["executed"])

    def test_click_ambiguity_quarantines_and_missing_submission_is_stale(self) -> None:
        raw = _trace("click")
        fixture = {
            "fixture_id": "ambiguous",
            "kind": "negative",
            "origin": "https://xn--bcher-kva.example",
            "nodes": [{"role": "button", "name": "Continue"}] * 2,
            "output_fields": ["confirmation"],
        }
        fixture["digest"] = fixture_digest(fixture)
        raw["fixtures"].insert(0, fixture)
        click = parse_browser_workflow_trace(raw)
        submit = parse_browser_workflow_trace(_trace("submit"))

        self.assertEqual(replay_browser_workflow_trace(click, {"fixture_id": "ambiguous"})["status"], "quarantined")
        missing = replay_browser_workflow_trace(submit, {"fixture_id": "negative_fresh"})
        self.assertEqual((missing["status"], missing["reason"]), ("stale", "zero_locator"))

    def test_missing_primary_locator_cannot_fall_back_to_another_target(self) -> None:
        raw = _trace("read")
        raw["steps"][0]["locators"].append({"kind": "test_id", "name": "stable-target"})
        for fixture in raw["fixtures"]:
            for node in fixture["nodes"]:
                node["test_id"] = "stable-target"
            fixture["digest"] = fixture_digest(fixture)

        trace = parse_browser_workflow_trace(raw)

        self.assertEqual(replay_browser_workflow_trace(trace, {"fixture_id": "positive"})["status"], "replayed")
        drift = replay_browser_workflow_trace(trace, {"fixture_id": "negative_fresh"})
        self.assertEqual((drift["status"], drift["reason"]), ("stale", "zero_locator"))

    def test_promotion_reference_requires_approved_fixture_proof_and_binds_every_digest(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = _git(Path(temporary_directory))
            trace = write_browser_workflow_trace(_trace("click"), root)
            with self.assertRaisesRegex(ValueError, "approved"):
                resolved_browser_workflow_promotion_reference(root, str(trace["trace_id"]))
            approve_browser_workflow_trace(root, str(trace["trace_id"]), str(trace["digest"]))
            reference = resolved_browser_workflow_promotion_reference(root, str(trace["trace_id"]))
            self.assertEqual(reference["schema_version"], "browser_workflow_promotion_reference/v1")
            self.assertEqual(reference["trace_digest"], trace["digest"])
            self.assertEqual(reference["replay_status"], "passed")
            self.assertEqual(set(reference["fixture_digests"]), {"negative", "negative_fresh", "positive", "transient"})
            self.assertRegex(str(reference["replay_digest"]), r"^[a-f0-9]{64}$")
            replay_stored_browser_workflow_trace(root, str(trace["trace_id"]), {"fixture_id": "negative"})
            with self.assertRaisesRegex(ValueError, "approved"):
                resolved_browser_workflow_promotion_reference(root, str(trace["trace_id"]))

    def test_promotion_reference_accepts_expected_safe_read_retry_fixtures(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = _git(Path(temporary_directory))
            trace = write_browser_workflow_trace(_trace("read"), root)
            approve_browser_workflow_trace(root, str(trace["trace_id"]), str(trace["digest"]))

            reference = resolved_browser_workflow_promotion_reference(root, str(trace["trace_id"]))

            self.assertEqual(reference["replay_status"], "passed")
            self.assertEqual(set(reference["fixture_digests"]), {"negative", "negative_fresh", "positive", "transient"})

    def test_approval_rechecks_drift_after_acquiring_the_record_lock(self) -> None:
        from omh.workflows import browser_workflow_learning_store as store

        with TemporaryDirectory() as temporary_directory:
            root = _git(Path(temporary_directory))
            trace = write_browser_workflow_trace(_trace("click"), root)
            trace_id = str(trace["trace_id"])
            original_lock = store.file_lock
            interleaved = False

            @contextmanager
            def drift_before_lock(path, **kwargs):
                nonlocal interleaved
                if not interleaved:
                    interleaved = True
                    replay_stored_browser_workflow_trace(root, trace_id, {"fixture_id": "negative"})
                with original_lock(path, **kwargs):
                    yield

            with patch.object(store, "file_lock", drift_before_lock):
                with self.assertRaisesRegex(ValueError, "pending"):
                    approve_browser_workflow_trace(root, trace_id, str(trace["digest"]))
            self.assertEqual(read_browser_workflow_trace(root, trace_id)["lifecycle"]["status"], "stale")

    def test_interleaved_fresh_mismatches_preserve_both_observations(self) -> None:
        from omh.workflows import browser_workflow_learning_store as store

        with TemporaryDirectory() as temporary_directory:
            root = _git(Path(temporary_directory))
            trace = write_browser_workflow_trace(_trace("click"), root)
            trace_id = str(trace["trace_id"])
            original_lock = store.file_lock
            interleaved = False

            @contextmanager
            def mismatch_before_lock(path, **kwargs):
                nonlocal interleaved
                if not interleaved:
                    interleaved = True
                    replay_stored_browser_workflow_trace(root, trace_id, {"fixture_id": "negative"})
                with original_lock(path, **kwargs):
                    yield

            with patch.object(store, "file_lock", mismatch_before_lock):
                result = replay_stored_browser_workflow_trace(root, trace_id, {"fixture_id": "negative_fresh"})

            self.assertEqual(result["status"], "quarantined")
            current = read_browser_workflow_trace(root, trace_id)
            self.assertEqual(len(current["lifecycle"]["mismatch_fixture_digests"]), 2)


def _trace(action: str, metadata: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "schema_version": "browser_workflow_trace/v1",
        "project": {"identity": hashlib.sha256(b"/workspace/project").hexdigest()},
        "origins": ["https://bücher.example"],
        "adapter_version": "adapter/v1",
        "parser_version": "parser/v1",
        "source": {
            "run_ref": "run-1",
            "evidence_ref": "evidence-1",
            "selected": True,
            "success": {"state": "success", "evidence_digest": "e" * 64},
            "binding": {
                "project_identity": hashlib.sha256(b"/workspace/project").hexdigest(),
                "origin": "https://xn--bcher-kva.example",
                "adapter_version": "adapter/v1",
            },
            "lineage": {"source_digest": "c" * 64, "environment_digest": "d" * 64, "adapter_digest": "e" * 64},
        },
        "steps": [{"action": action, "locators": [{"kind": "role", "role": "button", "name": "Continue"}]}],
        "output_schema": {"kind": "object", "fields": ["confirmation"]},
        "fixtures": _fixtures(action),
        "metadata": metadata or {},
    }


def _git(root: Path) -> Path:
    subprocess.run(["git", "init", "--quiet", str(root)], check=True, capture_output=True, text=True)
    return root


def _fixtures(action: str) -> list[dict[str, object]]:
    nodes = [{"role": "button", "name": "Continue"}]
    negative_nodes: list[dict[str, str]] = [] if action != "submit" else [*nodes, *nodes]
    result: list[dict[str, object]] = []
    for fixture_id, kind, fixture_nodes in (
        ("negative", "negative", negative_nodes),
        ("negative_fresh", "negative", [{"role": "button", "name": "Different target"}]),
        ("positive", "positive", nodes),
        ("transient", "transient", nodes),
    ):
        fixture: dict[str, object] = {"fixture_id": fixture_id, "kind": kind, "origin": "https://xn--bcher-kva.example", "nodes": fixture_nodes, "output_fields": ["confirmation"]}
        fixture["digest"] = fixture_digest(fixture)
        result.append(fixture)
    return result


if __name__ == "__main__":
    unittest.main()
