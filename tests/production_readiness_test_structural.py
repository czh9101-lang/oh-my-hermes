from __future__ import annotations

# ruff: noqa: F405
from production_readiness_test_support import *  # noqa: F403


class ReadinessMatrixTestsMixin:
    def test_local_observations_are_row_bound_and_globally_unique(self) -> None:
            matrix = build_readiness_matrix(
                scope="release-1119", rows=complete_rows(), created_at=CREATED, evidence_fresh_after=FRESH_AFTER
            )
            replayed = copy.deepcopy(matrix)
            replayed["rows"][1]["evidence"] = copy.deepcopy(replayed["rows"][0]["evidence"])

            errors = validate_readiness_matrix(replayed, trusted_context=TRUST_CONTEXT)
            rendered = "; ".join(errors)

            self.assertIn("observed_check_result category must match row authority", rendered)
            self.assertIn("duplicate observed_check_result check_id", rendered)
            self.assertIn("duplicate observed_check_result evidence_ref", rendered)
            self.assertIn("verdict must match derived verdict HOLD", rendered)

    def test_forged_observed_state_is_rejected(self) -> None:
            matrix = build_readiness_matrix(
                scope="release-1119", rows=complete_rows(), created_at=CREATED, evidence_fresh_after=FRESH_AFTER
            )
            matrix["rows"][2]["requires_external_observation"] = True
            self.assertIn("external observed success requires", "; ".join(validate_readiness_matrix(matrix)))
            self.assertIn("verdict must match derived verdict", "; ".join(validate_readiness_matrix(matrix)))

    def test_receipt_without_postcondition_does_not_observe(self) -> None:
            evidence = external_evidence("ci")
            evidence.pop("postcondition")
            rows = complete_rows()
            rows[2] = external_row("ci", evidence)
            with self.assertRaisesRegex(ValueError, "requires observed_postcondition/v1"):
                build_readiness_matrix(
                    scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
                )

    def test_receipt_postcondition_mismatch_does_not_observe(self) -> None:
            rows = complete_rows()
            rows[2] = external_row("ci", external_evidence("ci", post_effect_id="readiness:other"))
            with self.assertRaisesRegex(ValueError, "effect_id must match"):
                build_readiness_matrix(
                    scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
                )

    def test_stale_evidence_holds_and_future_evidence_is_rejected(self) -> None:
            stale_rows = complete_rows()
            stale_rows[0]["evidence"] = [observed_check("build", observed_at="2026-08-26T10:59:59Z")]
            matrix = build_readiness_matrix(
                scope="release-1119", rows=stale_rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
            )
            self.assertEqual(matrix["rows"][0]["evidence_state"], "missing")
            self.assertEqual(matrix["verdict"], "HOLD")

            future_rows = complete_rows()
            future_rows[0]["evidence"] = [observed_check("build", observed_at="2026-08-26T12:00:01Z")]
            with self.assertRaisesRegex(ValueError, "must not be after matrix created_at"):
                build_readiness_matrix(
                    scope="release-1119", rows=future_rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
                )

    def test_stale_receipt_or_postcondition_does_not_observe(self) -> None:
            for evidence in (
                external_evidence("ci", receipt_observed_at="2026-08-26T10:59:59Z"),
                external_evidence(
                    "ci",
                    receipt_observed_at="2026-08-26T10:59:59Z",
                    postcondition_observed_at="2026-08-26T10:59:59Z",
                ),
            ):
                rows = complete_rows()
                rows[2] = external_row("ci", evidence)
                matrix = build_readiness_matrix(
                    scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
                )
                self.assertEqual(matrix["rows"][2]["evidence_state"], "missing")
                self.assertEqual(matrix["verdict"], "HOLD")

    def test_plans_exits_artifacts_and_self_reports_are_not_observations(self) -> None:
            for schema in (
                "production_audit_plan/v1",
                "dispatch_exit/v1",
                "generated_artifact/v1",
                "self_report/v1",
            ):
                with self.subTest(schema=schema):
                    rows = complete_rows()
                    rows[0]["evidence"] = [{"schema_version": schema, "result": "passed", "observed_at": CREATED}]
                    with self.assertRaisesRegex(ValueError, "unsupported readiness evidence schema"):
                        build_readiness_matrix(
                            scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
                        )

    def test_external_binding_is_exact_across_matrix_row_receipt_and_postcondition(self) -> None:
            rows = complete_rows()
            rows[2] = external_row("ci", external_evidence("ci"))
            matrix = build_readiness_matrix(
                scope="release-1119",
                task_id=TASK_ID,
                revision=REVISION,
                rows=rows,
                created_at=CREATED,
                evidence_fresh_after=FRESH_AFTER,
            )
            self.assertEqual(matrix["verdict"], "GO")
            self.assertEqual(validate_readiness_matrix(matrix, trusted_context=TRUST_CONTEXT), [])

    def test_otherwise_valid_unrelated_receipt_cannot_produce_go(self) -> None:
            rows = complete_rows()
            rows[2] = external_row("ci", external_evidence("ci", run_id="run-unrelated"))
            with self.assertRaisesRegex(ValueError, "receipt run_id must match external_identity.run_id"):
                build_readiness_matrix(
                    scope="release-1119",
                    task_id=TASK_ID,
                    revision=REVISION,
                    rows=rows,
                    created_at=CREATED,
                    evidence_fresh_after=FRESH_AFTER,
                )

    def test_external_binding_rejects_absent_mismatched_and_forged_associations(self) -> None:
            cases = {
                "absent": external_row("ci", external_evidence("ci")),
                "task": external_row("ci", external_evidence("ci", post_task_id="task-other")),
                "receipt_task": external_row("ci", external_evidence("ci", task_id="task-other")),
                "revision": external_row("ci", external_evidence("ci", post_revision="revision-other")),
                "receipt_revision": external_row("ci", external_evidence("ci", revision="revision-other")),
                "run": external_row("ci", external_evidence("ci", post_run_id="run-other")),
                "receipt": external_row("ci", external_evidence("ci")),
            }
            cases["absent"].pop("external_identity")
            cases["receipt"]["evidence"][0]["postcondition"]["receipt_id"] = "receipt-forged"
            for name, row in cases.items():
                with self.subTest(name=name):
                    rows = complete_rows()
                    rows[2] = row
                    with self.assertRaises(ValueError):
                        build_readiness_matrix(
                            scope="release-1119",
                            task_id=TASK_ID,
                            revision=REVISION,
                            rows=rows,
                            created_at=CREATED,
                            evidence_fresh_after=FRESH_AFTER,
                        )

    def test_duplicate_external_associations_are_rejected(self) -> None:
            duplicate = external_evidence("ci")
            rows = complete_rows()
            row = external_row("ci", duplicate)
            row["evidence"].append(copy.deepcopy(duplicate))
            rows[2] = row
            with self.assertRaisesRegex(ValueError, "exactly one external readiness association"):
                build_readiness_matrix(
                    scope="release-1119",
                    task_id=TASK_ID,
                    revision=REVISION,
                    rows=rows,
                    created_at=CREATED,
                    evidence_fresh_after=FRESH_AFTER,
                )

    def test_duplicate_receipt_association_across_rows_is_rejected(self) -> None:
            evidence = external_evidence("ci")
            rows = complete_rows()
            rows[2] = external_row("ci", evidence)
            rows[3] = external_row("ci", copy.deepcopy(evidence), effect_id="readiness:ci")
            with self.assertRaisesRegex(ValueError, "duplicate external readiness receipt associations"):
                build_readiness_matrix(
                    scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
                )

    def test_duplicate_postcondition_association_across_rows_is_rejected(self) -> None:
            first = external_evidence("ci")
            second = external_evidence("ci", run_id="run-second")
            second["postcondition"]["observation_id"] = first["postcondition"]["observation_id"]
            rows = complete_rows()
            rows[2] = external_row("ci", first)
            rows[3] = external_row("ci", second, run_id="run-second")
            with self.assertRaisesRegex(ValueError, "duplicate external readiness postcondition associations"):
                build_readiness_matrix(
                    scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
                )
