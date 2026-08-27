from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from omh.quality.paired_run_decision import (
    build_paired_run_decision,
    parse_paired_run_decision,
    validate_paired_run_decision,
)
from omh.quality.paired_run_model import PairedRunValidationError
from test_paired_run_decision_contract import _request


class PairedRunContractBoundaryTests(unittest.TestCase):
    def test_collecting_validator_reports_boundary_errors(self) -> None:
        with TemporaryDirectory() as raw:
            home = (Path(raw) / ".omh").resolve()
            valid = build_paired_run_decision(_request(home), home).to_json()
            invalid = json.loads(valid)
            invalid["recorded_at"] = "2026-08-27T00:01:00"
            self.assertEqual(validate_paired_run_decision(valid, home), ())
            self.assertIn(
                "recorded_at must be an exact UTC-Z timestamp",
                validate_paired_run_decision(json.dumps(invalid), home),
            )

    def test_public_parse_and_validate_reject_untrusted_evidence_rows(self) -> None:
        with TemporaryDirectory(prefix="omh-untrusted-receipt-") as raw:
            home = (Path(raw) / ".omh").resolve()
            valid = json.loads(
                build_paired_run_decision(
                    _request(home),
                    home,
                ).to_json()
            )
        valid["results"][0]["receipt_ref"] = (
            f"hermes-child:{valid['results'][0]['receipt_run_id']}:{'0' * 64}"
        )
        document = json.dumps(valid)
        with self.assertRaises(PairedRunValidationError):
            parse_paired_run_decision(document)
        self.assertTrue(validate_paired_run_decision(document))

    def test_rejects_closed_key_digest_budget_shape_and_raw_field_faults(
        self,
    ) -> None:
        with TemporaryDirectory() as raw:
            home = (Path(raw) / ".omh").resolve()
            valid = json.loads(
                build_paired_run_decision(_request(home), home).to_json()
            )
            mutations = (
                lambda item: item.update({"score": 1}),
                lambda item: item.update({"hidden_reasoning": "x"}),
                lambda item: item.update({"task_set_digest": "0" * 64}),
                lambda item: item.update({"max_total_runs": 0}),
                lambda item: item["results"].pop(),
                lambda item: item["baseline"].update(
                    {"exposure_digest": "f" * 64}
                ),
                lambda item: item["tasks"][0].pop(
                    "acceptance_criteria_ref"
                ),
            )
            for mutate in mutations:
                with self.subTest(mutate=mutate):
                    candidate = json.loads(json.dumps(valid))
                    mutate(candidate)
                    with self.assertRaises(PairedRunValidationError):
                        parse_paired_run_decision(
                            json.dumps(candidate),
                            home,
                        )


if __name__ == "__main__":
    unittest.main()
