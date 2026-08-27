from __future__ import annotations

from production_readiness_test_support import (
    CREATED,
    FRESH_AFTER,
    READINESS_CATEGORIES,
    READINESS_CATEGORY_POLICY,
    READINESS_CATEGORY_POLICY_SCHEMA_VERSION,
    RUN_ID,
    TRUST_CONTEXT,
    build_readiness_matrix,
    complete_rows,
    copy,
    external_evidence,
    external_row,
    json,
    observed_check,
    validate_readiness_matrix,
)

class ReadinessMatrixTestsMixin:
    def test_complete_unique_observed_rows_derive_go(self) -> None:
            matrix = build_readiness_matrix(
                scope="release-1119",
                rows=complete_rows(),
                created_at=CREATED,
                evidence_fresh_after=FRESH_AFTER,
            )
            self.assertEqual(matrix["schema_version"], "readiness_matrix/v1")
            self.assertEqual([row["category"] for row in matrix["rows"]], list(READINESS_CATEGORIES))
            self.assertEqual(len(READINESS_CATEGORY_POLICY), 9)
            self.assertEqual(dict(READINESS_CATEGORY_POLICY), {category: category == "ci" for category in READINESS_CATEGORIES})
            self.assertEqual(
                matrix["contract_ref"]["category_policy"]["schema_version"],
                READINESS_CATEGORY_POLICY_SCHEMA_VERSION,
            )
            self.assertEqual({row["evidence_state"] for row in matrix["rows"]}, {"observed"})
            self.assertEqual(matrix["verdict"], "GO")
            self.assertEqual(validate_readiness_matrix(matrix, trusted_context=TRUST_CONTEXT), [])
            self.assertFalse(any(key in json.dumps(matrix).lower() for key in ('"score"', '"rank"', '"badge"')))

    def test_builder_detaches_every_mutable_caller_container(self) -> None:
            caller_rows = complete_rows()

            matrix = build_readiness_matrix(
                scope="release-1119",
                rows=caller_rows,
                created_at=CREATED,
                evidence_fresh_after=FRESH_AFTER,
            )
            captured = json.dumps(matrix, sort_keys=True)

            def mutable_container_ids(value: object) -> set[int]:
                if type(value) is dict:
                    return {id(value)} | {
                        identity
                        for item in value.values()
                        for identity in mutable_container_ids(item)
                    }
                if type(value) is list:
                    return {id(value)} | {
                        identity
                        for item in value
                        for identity in mutable_container_ids(item)
                    }
                return set()

            self.assertFalse(mutable_container_ids(caller_rows) & mutable_container_ids(matrix))
            caller_rows[0]["evidence"][0]["result"] = "failed"
            caller_rows[2]["external_identity"]["effect_id"] = "readiness:mutated"
            caller_rows[2]["evidence"][0]["receipt"]["summary"] = "mutated caller receipt"
            caller_rows[2]["evidence"][0]["postcondition"]["result"] = "failed"

            self.assertEqual(json.dumps(matrix, sort_keys=True), captured)
            self.assertEqual(matrix["verdict"], "GO")
            self.assertEqual(matrix["rows"][0]["evidence"][0]["result"], "passed")
            self.assertEqual(matrix["rows"][2]["external_identity"]["effect_id"], "readiness:ci")
            self.assertEqual(matrix["rows"][2]["evidence"][0]["receipt"]["summary"], "")

    def test_missing_or_duplicate_rows_are_rejected(self) -> None:
            matrix = build_readiness_matrix(
                scope="release-1119", rows=complete_rows(), created_at=CREATED, evidence_fresh_after=FRESH_AFTER
            )
            missing = copy.deepcopy(matrix)
            missing["rows"].pop()
            duplicate = copy.deepcopy(matrix)
            duplicate["rows"][-1] = copy.deepcopy(duplicate["rows"][0])
            self.assertIn("missing readiness categories", "; ".join(validate_readiness_matrix(missing)))
            self.assertIn("duplicate readiness categories", "; ".join(validate_readiness_matrix(duplicate)))

    def test_prepared_only_rows_derive_hold(self) -> None:
            rows = [
                {
                    "category": category,
                    "requires_external_observation": category == "ci",
                    "external_identity": ({"effect_id": "readiness:ci", "run_id": RUN_ID} if category == "ci" else {}),
                    "evidence": [],
                }
                for category in READINESS_CATEGORIES
            ]
            matrix = build_readiness_matrix(
                scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
            )
            self.assertEqual(matrix["verdict"], "HOLD")
            self.assertEqual({row["evidence_state"] for row in matrix["rows"]}, {"missing"})
            self.assertEqual(validate_readiness_matrix(matrix), [])

    def test_failed_observation_derives_block(self) -> None:
            rows = complete_rows()
            rows[0]["evidence"] = [{**observed_check("build"), "result": "failed"}]
            matrix = build_readiness_matrix(
                scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
            )
            self.assertEqual(matrix["verdict"], "BLOCK")
            self.assertEqual(matrix["rows"][0]["evidence_state"], "failed")

    def test_external_success_requires_matching_receipt_and_postcondition(self) -> None:
            rows = complete_rows()
            rows[2] = external_row("ci", external_evidence("ci"))
            matrix = build_readiness_matrix(
                scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
            )
            self.assertEqual(matrix["verdict"], "GO")
            self.assertEqual(matrix["rows"][2]["evidence_state"], "observed")
            self.assertEqual(validate_readiness_matrix(matrix, trusted_context=TRUST_CONTEXT), [])

    def test_registry_owned_external_policy_cannot_be_coherently_downgraded(self) -> None:
            rows = complete_rows()
            rows[2] = external_row("ci", external_evidence("ci"))
            matrix = build_readiness_matrix(
                scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
            )
            self.assertEqual(matrix["verdict"], "GO")
            downgraded = copy.deepcopy(matrix)
            ci_row = downgraded["rows"][2]
            ci_row["requires_external_observation"] = False
            ci_row["external_identity"] = {}
            ci_row["evidence"] = [observed_check("ci")]
            ci_row["evidence_state"] = "observed"
            downgraded["verdict"] = "GO"
            downgraded["missing_categories"] = []

            errors = validate_readiness_matrix(downgraded, trusted_context=TRUST_CONTEXT)

            self.assertIn("category policy requires external observation", "; ".join(errors))
            self.assertIn("verdict must match derived verdict HOLD", "; ".join(errors))

            upgraded = copy.deepcopy(matrix)
            build_row = upgraded["rows"][0]
            build_row["requires_external_observation"] = True
            build_row["external_identity"] = copy.deepcopy(matrix["rows"][2]["external_identity"])
            build_row["evidence"] = copy.deepcopy(matrix["rows"][2]["evidence"])
            build_row["evidence_state"] = "observed"
            upgraded_errors = "; ".join(validate_readiness_matrix(upgraded, trusted_context=TRUST_CONTEXT))
            self.assertIn("category policy requires local observation for build", upgraded_errors)
            self.assertIn("verdict must match derived verdict HOLD", upgraded_errors)
