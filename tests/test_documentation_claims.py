from __future__ import annotations

import importlib
import importlib.util
import json
import multiprocessing
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def advisory_adapter(request):
    return {
        "state": "stale", "provider": "fixture", "model": "fixture-v1",
        "run_id": "run-1", "adapter": "unittest", "claim_id": request.claim_id,
        "input_digest": request.input_digest,
        "cost_status": "reported", "cost_usd": 0.0,
        "prompt": "secret-must-not-escape", "transcript": "private",
    }


def unavailable_adapter(request):
    raise RuntimeError("sk-private-must-not-escape")


def invalid_binding_adapter(request):
    return {**advisory_adapter(request), "input_digest": "0" * 64}


def invalid_cost_adapter(request):
    return {**advisory_adapter(request), "cost_usd": float("nan")}


def supported_adapter(request):
    return {**advisory_adapter(request), "state": "supported", "cost_status": "unknown", "cost_usd": None}


def evaluating_adapter(request):
    actual = json.loads(request.evidence)["prepared_is_observed"]
    return {**advisory_adapter(request), "state": "supported" if actual == request.expected_fact else "stale"}


class BlockingAdapter:
    def __init__(self, ready, release):
        self.ready, self.release = ready, release

    def __call__(self, request):
        self.ready.set()
        self.release.wait()


class DocumentationClaimsTests(unittest.TestCase):
    def adapter(self, evaluate=advisory_adapter):
        from omh.maintenance.documentation_claims_worker import ModelAdapter

        return ModelAdapter(evaluate, "fixture", "fixture-v1", "run-1", "unittest")

    def evaluator(self):
        name = "omh.maintenance.documentation_claims"
        self.assertIsNotNone(importlib.util.find_spec(name), "bounded claim evaluator is unavailable")
        return importlib.import_module(name)

    def test_changed_command_fact_is_stale_without_poisoning_other_rows(self):
        audit = self.evaluator()
        from omh.catalogs.documentation_claims import documentation_claims

        claims = documentation_claims()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for claim in claims:
                for relative in (*claim.pages, *(anchor.path for anchor in claim.anchors)):
                    target = root / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes((ROOT / relative).read_bytes())
            path = root / "src/maintenance/release.py"
            text = path.read_text()
            old = '"observed": False,\n        "version": release_version,'
            self.assertEqual(text.count(old), 1)
            path.write_text(text.replace(old, '"observed": True,\n        "version": release_version,'))
            report = audit.documentation_claims_report(root=root)

        rows = {row["id"]: row for row in report["claims"]}
        self.assertEqual(rows["release.checklist-prepared"]["state"], "stale")
        self.assertEqual(rows["release.checklist-prepared"]["observed_fact"], True)
        self.assertEqual(rows["release.checklist-prepared"]["expected_fact"], False)
        self.assertTrue(all(row["state"] == "supported" for key, row in rows.items()
                            if key != "release.checklist-prepared" and not row["advisory"]))

    def test_missing_anchor_is_unresolved(self):
        audit = self.evaluator()
        with tempfile.TemporaryDirectory() as directory:
            report = audit.documentation_claims_report(root=Path(directory), claim_ids=("release.checklist-prepared",))
        row = next(row for row in report["claims"] if row["id"] == "release.checklist-prepared")
        self.assertEqual(row["state"], "unresolved")
        self.assertFalse(report["ok"])

    def test_baseline_cli_is_stable_and_offline(self):
        command = [sys.executable, "-m", "omh.cli", "docs", "claims", "--check", "--json"]
        first = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(first.returncode, 0, first.stderr)
        payload = json.loads(first.stdout)
        second = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(payload["schema_version"], "documentation_claim_audit/v1")
        self.assertTrue(all(row["state"] == "supported" for row in payload["claims"] if not row["advisory"]))
        self.assertEqual(payload["model_evaluation"]["runs"], 0)
        self.assertEqual(payload["generated_artifact_drift"]["evidence_class"], "generated_artifact_drift")

    def test_release_checklist_audit_is_prepared_not_observed(self):
        from omh.maintenance.release import release_readiness_checklist

        report = release_readiness_checklist()
        rows = {row["id"]: row for row in report["items"]}
        self.assertIn("documentation_claims", rows)
        self.assertFalse(rows["documentation_claims"]["observed"])
        self.assertEqual(rows["documentation_claims"]["command"], "uv run python -m omh.cli docs claims --check --json")

    def test_all_reviewed_modes_have_complete_catalog_metadata(self):
        self.evaluator()
        from omh.catalogs.documentation_claims import documentation_claims

        claims = documentation_claims()
        self.assertEqual({claim.mode for claim in claims}, {
            "cli_probe", "schema_assertion", "render_equality", "symbol_check", "fixture_behavior", "model_assisted",
        })
        self.assertEqual(len({claim.claim_id for claim in claims}), len(claims))
        for claim in claims:
            self.assertTrue(all((claim.question, claim.invariant, claim.pages, claim.anchors, claim.owner, claim.risk)))
            self.assertTrue(all(anchor.path.startswith("src/") for anchor in claim.anchors))

    def test_release_readiness_carries_observed_audit_separate_from_prepared_work(self):
        from omh.maintenance.release import product_readiness_report
        from omh.system.paths import OmhPaths

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = product_readiness_report(paths=OmhPaths(omh_home=root / "omh", hermes_home=root / "hermes"))
        self.assertIn("documentation_claims", report)
        self.assertTrue(report["documentation_claims"]["observed"])
        self.assertEqual(report["documentation_claims"]["model_evaluation"]["runs"], 0)
        gate = next(row for row in report["gates"] if row["id"] == "documentation_claims")
        self.assertEqual(gate["status"], "passed")

    def test_unknown_selection_is_rejected(self):
        audit = self.evaluator()
        with self.assertRaises(ValueError):
            audit.documentation_claims_report(claim_ids=("arbitrary-shell",))

    def test_unsupported_probe_never_passes(self):
        audit = self.evaluator()
        from omh.catalogs.documentation_claims import documentation_claims

        claim = replace(documentation_claims()[0], probe="arbitrary-shell")
        with patch.object(audit, "documentation_claims", return_value=(claim,)):
            report = audit.documentation_claims_report(root=ROOT)
        self.assertEqual(report["claims"][0]["state"], "unresolved")

    def test_advisory_is_disabled_by_default(self):
        audit = self.evaluator()
        report = audit.documentation_claims_report(root=ROOT, claim_ids=("docs.evidence-language",), model_adapter=self.adapter())
        self.assertEqual(report["claims"][0]["state"], "not_run")
        self.assertEqual(report["model_evaluation"]["runs"], 0)

    def test_advisory_records_provenance_but_never_blocks(self):
        audit = self.evaluator()
        report = audit.documentation_claims_report(
            root=ROOT, claim_ids=("docs.evidence-language",), enable_model=True, model_adapter=self.adapter(),
        )
        row = report["claims"][0]
        self.assertEqual(row["state"], "stale")
        self.assertTrue(report["ok"])
        self.assertTrue(row["advisory"])
        self.assertEqual(row["model_run"]["provider"], "fixture")
        self.assertEqual(row["model_run"]["cost_status"], "reported")
        self.assertEqual(row["model_run"]["cost_usd"], 0.0)
        self.assertEqual(len(row["model_run"]["input_digest"]), 64)
        self.assertNotIn("secret-must-not-escape", json.dumps(report))
        self.assertNotIn("transcript", json.dumps(report))

    def test_unavailable_model_is_unresolved_and_sanitized(self):
        audit = self.evaluator()
        report = audit.documentation_claims_report(
            root=ROOT, claim_ids=("docs.evidence-language",), enable_model=True, model_adapter=self.adapter(unavailable_adapter),
        )
        self.assertEqual(report["claims"][0]["state"], "unresolved")
        self.assertTrue(report["ok"])
        self.assertNotIn("sk-private", json.dumps(report))
        self.assertIsNotNone(report["claims"][0]["model_run"])
        self.assertEqual(report["claims"][0]["model_run"]["cost_status"], "unknown")

    def test_no_adapter_is_not_run(self):
        audit = self.evaluator()
        report = audit.documentation_claims_report(root=ROOT, claim_ids=("docs.evidence-language",), enable_model=True)
        self.assertEqual(report["claims"][0]["state"], "not_run")
        self.assertEqual(report["model_evaluation"]["runs"], 0)

    def test_model_cap_is_enforced_before_invocation(self):
        audit = self.evaluator()
        report = audit.documentation_claims_report(
            root=ROOT, claim_ids=("docs.evidence-language",), enable_model=True,
            model_adapter=self.adapter(), model_run_cap=0,
        )
        self.assertEqual(report["claims"][0]["state"], "not_run")
        self.assertEqual(report["model_evaluation"]["runs"], 0)
        with self.assertRaises(ValueError):
            audit.documentation_claims_report(enable_model=True, model_run_cap=100)

    def test_model_deadline_terminates_blocked_adapter(self):
        audit = self.evaluator()
        context = multiprocessing.get_context("spawn")
        ready, release = context.Event(), context.Event()
        report = audit.documentation_claims_report(
            root=ROOT, claim_ids=("docs.evidence-language",), enable_model=True,
            model_adapter=self.adapter(BlockingAdapter(ready, release)), timeout=2,
        )
        self.assertTrue(ready.is_set())
        self.assertEqual(report["claims"][0]["state"], "unresolved")
        self.assertEqual(report["claims"][0]["reason"], "timeout")
        self.assertIsNotNone(report["claims"][0]["model_run"])
        self.assertEqual(report["claims"][0]["model_run"]["provider"], "fixture")
        self.assertEqual(report["claims"][0]["model_run"]["cost_status"], "unknown")
        self.assertTrue(report["ok"])
        self.assertFalse(multiprocessing.active_children())

    def test_generated_drift_does_not_become_semantic_evidence(self):
        audit = self.evaluator()
        report = audit.documentation_claims_report(root=ROOT, claim_ids=("release.checklist-symbol",))
        self.assertEqual(report["claims"][0]["evidence_class"], "documentation_claim")
        self.assertEqual(report["generated_artifact_drift"]["state"], "not_run")
        self.assertFalse(report["generated_artifact_drift"]["observed"])

    def test_captured_output_cap_counts_utf8_bytes(self):
        from omh.maintenance.documentation_claims_worker import _BoundedOutput, OUTPUT_BYTE_CAP

        with _BoundedOutput() as output:
            output.write("\u00e9" * (OUTPUT_BYTE_CAP // 4))
            output.write("\u00e9" * (OUTPUT_BYTE_CAP // 4))
            with self.assertRaises(ValueError):
                output.write("x")

    def test_model_supported_result_is_advisory_with_unknown_cost(self):
        report = self.evaluator().documentation_claims_report(
            root=ROOT, claim_ids=("docs.evidence-language",), enable_model=True,
            model_adapter=self.adapter(supported_adapter),
        )
        row = report["claims"][0]
        self.assertEqual(row["state"], "supported")
        self.assertTrue(row["advisory"])
        self.assertIsNone(row["model_run"]["cost_usd"])
        self.assertEqual(row["model_run"]["cost_status"], "unknown")

    def test_model_invalid_binding_and_cost_are_unresolved(self):
        for evaluate in (invalid_binding_adapter, invalid_cost_adapter):
            with self.subTest(adapter=evaluate.__name__):
                report = self.evaluator().documentation_claims_report(
                    root=ROOT, claim_ids=("docs.evidence-language",), enable_model=True,
                    model_adapter=self.adapter(evaluate),
                )
                self.assertEqual(report["claims"][0]["state"], "unresolved")
                self.assertTrue(report["ok"])
                self.assertEqual(report["model_evaluation"]["runs"], 1)

    def test_hard_model_cap_limits_multiple_selected_claims(self):
        audit = self.evaluator()
        from omh.catalogs.documentation_claims import documentation_claims

        base = next(claim for claim in documentation_claims() if claim.mode == "model_assisted")
        claims = tuple(replace(base, claim_id=f"docs.fixture-{i}") for i in range(4))
        with patch.object(audit, "documentation_claims", return_value=claims):
            report = audit.documentation_claims_report(
                root=ROOT, claim_ids=tuple(claim.claim_id for claim in claims), enable_model=True,
                model_adapter=self.adapter(), model_run_cap=3,
            )
        self.assertEqual(report["model_evaluation"]["runs"], 3)
        self.assertEqual([row["state"] for row in report["claims"]], ["stale", "stale", "stale", "not_run"])
        self.assertTrue(report["ok"])

    def test_model_enablement_requires_explicit_selection(self):
        with self.assertRaises(ValueError):
            self.evaluator().documentation_claims_report(root=ROOT, enable_model=True, model_adapter=self.adapter())

    def test_timeout_bounds_reject_nonfinite_and_excessive_values(self):
        for timeout in (0, -1, 31, float("nan"), float("inf"), True):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.evaluator().documentation_claims_report(timeout=timeout)

    def test_model_metadata_credentials_never_enter_failed_run_report(self):
        credential = "ghp_123456789abcdef"
        report = self.evaluator().documentation_claims_report(
            root=ROOT, claim_ids=("docs.evidence-language",), enable_model=True,
            model_adapter=replace(self.adapter(), provider=credential),
        )
        self.assertEqual(report["claims"][0]["state"], "unresolved")
        self.assertNotIn(credential, json.dumps(report))
        self.assertEqual(report["model_evaluation"]["runs"], 0)

    def test_injected_evaluator_compares_evidence_to_reviewed_fact(self):
        audit = self.evaluator()
        from omh.catalogs.documentation_claims import documentation_claims

        base = next(claim for claim in documentation_claims() if claim.mode == "model_assisted")
        claim = replace(base, expected_fact=False, pages=("docs/advisory.json",))
        for actual, expected in ((False, "supported"), (True, "stale")):
            with self.subTest(actual=actual), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                page = root / claim.pages[0]
                page.parent.mkdir(parents=True)
                page.write_text(json.dumps({"prepared_is_observed": actual}))
                for anchor in claim.anchors:
                    target = root / anchor.path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes((ROOT / anchor.path).read_bytes())
                with patch.object(audit, "documentation_claims", return_value=(claim,)):
                    report = audit.documentation_claims_report(
                        root=root, claim_ids=(claim.claim_id,), enable_model=True,
                        model_adapter=self.adapter(evaluating_adapter),
                    )
                self.assertEqual(report["claims"][0]["state"], expected)
                self.assertTrue(report["ok"])


if __name__ == "__main__":
    unittest.main()
