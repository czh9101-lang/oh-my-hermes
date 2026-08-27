from __future__ import annotations

from production_readiness_test_support import (
    CREATED,
    FRESH_AFTER,
    READINESS_CANONICAL_JSON_MAX_BYTES,
    READINESS_CANONICAL_JSON_MAX_DEPTH,
    READINESS_CANONICAL_JSON_MAX_NODES,
    TRUST_CONTEXT,
    TRUST_KEY,
    ReadinessTrustContext,
    build_readiness_matrix,
    complete_rows,
    copy,
    external_evidence,
    external_row,
    validate_readiness_matrix,
)

class ReadinessMatrixTestsMixin:
    def test_same_key_cannot_relabel_authenticated_evidence_to_another_context(self) -> None:
                rows = complete_rows()
                rows[2] = external_row("ci", external_evidence("ci"))
                matrix = build_readiness_matrix(
                    scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
                )
                matrix["rows"][2]["evidence"][0]["authenticity"]["context_id"] = "other-context"
                other_context = ReadinessTrustContext("other-context", TRUST_KEY)
                errors = validate_readiness_matrix(matrix, trusted_context=other_context)
                self.assertIn("external readiness authenticity tag does not match trusted context", "; ".join(errors))
                self.assertIn("verdict must match derived verdict HOLD", "; ".join(errors))

    def test_every_mutable_verdict_or_trust_authority_has_a_rejection_path(self) -> None:
                rows = complete_rows()
                rows[2] = external_row("ci", external_evidence("ci"))
                original = build_readiness_matrix(
                    scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
                )

                def mutate(path: tuple[object, ...], value: object) -> dict[str, object]:
                    record = copy.deepcopy(original)
                    target = record
                    for part in path[:-1]:
                        target = target[part]
                    target[path[-1]] = value
                    return record

                authentication_cases = (
                    (("schema_version",), "readiness_matrix/v2"),
                    (("matrix_id",), "readiness-public-forgery"),
                    (("scope",), "scope-forged"),
                    (("task_id",), "task-forged"),
                    (("revision",), "revision-forged"),
                    (("created_at",), "2026-08-26T12:30:00Z"),
                    (("evidence_fresh_after",), "2026-08-26T10:00:00Z"),
                    (("contract_ref", "consumer_id"), "forged_consumer"),
                    (("rows", 2, "category"), "category-forged"),
                    (("rows", 2, "requires_external_observation"), False),
                    (("rows", 2, "external_identity", "effect_id"), "readiness:forged"),
                    (("rows", 2, "external_identity", "run_id"), "run-forged"),
                    (("rows", 2, "evidence", 0, "authenticity", "schema_version"), "external_readiness_authenticity/v2"),
                    (("rows", 2, "evidence", 0, "authenticity", "algorithm"), "public-sha256"),
                    (("rows", 2, "evidence", 0, "authenticity", "context_id"), "context-forged"),
                    (("rows", 2, "evidence", 0, "authenticity", "hmac_sha256"), "0" * 64),
                )
                for path, value in authentication_cases:
                    with self.subTest(path=path):
                        errors = validate_readiness_matrix(mutate(path, value), trusted_context=TRUST_CONTEXT)
                        self.assertIn("external readiness authenticity", "; ".join(errors))

                def leaf_paths(value: object, prefix: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
                    if isinstance(value, dict):
                        return [path for key, item in value.items() for path in leaf_paths(item, (*prefix, key))]
                    if isinstance(value, list):
                        return [prefix]
                    return [prefix]

                unsigned_evidence = original["rows"][2]["evidence"][0]
                for relative_path in leaf_paths(
                    {key: value for key, value in unsigned_evidence.items() if key != "authenticity"}
                ):
                    path = ("rows", 2, "evidence", 0, *relative_path)
                    current = unsigned_evidence
                    for part in relative_path:
                        current = current[part]
                    changed = [*current, "forged"] if isinstance(current, list) else f"{current}-forged"
                    with self.subTest(authenticated_evidence_path=path):
                        errors = validate_readiness_matrix(mutate(path, changed), trusted_context=TRUST_CONTEXT)
                        self.assertIn("external readiness authenticity", "; ".join(errors))

                derived_cases = (
                    (("verdict",), "HOLD"),
                    (("missing_categories",), ["ci"]),
                    (("rows", 2, "evidence_state"), "missing"),
                    (("rollback_contract", "legacy_evidence_state_action"), "upgrade"),
                )
                for path, value in derived_cases:
                    with self.subTest(path=path):
                        self.assertTrue(validate_readiness_matrix(mutate(path, value), trusted_context=TRUST_CONTEXT))

                signed_paths = (
                    ("schema_version",),
                    ("matrix_id",),
                    ("contract_ref",),
                    ("scope",),
                    ("task_id",),
                    ("revision",),
                    ("created_at",),
                    ("evidence_fresh_after",),
                    ("rows", 2, "category"),
                    ("rows", 2, "requires_external_observation"),
                    ("rows", 2, "external_identity"),
                    ("rows", 2, "evidence", 0, "schema_version"),
                    *(("rows", 2, "evidence", 0, "receipt", field) for field in original["rows"][2]["evidence"][0]["receipt"]),
                    *(("rows", 2, "evidence", 0, "postcondition", field) for field in original["rows"][2]["evidence"][0]["postcondition"]),
                )
                adversarial_values = (
                    None,
                    False,
                    7,
                    1.5,
                    [],
                    {},
                    {"nested": []},
                    set(),
                    ("tuple",),
                    b"bytes",
                    float("nan"),
                    {"string-key": "value", 1: "mixed-key"},
                )
                for path in signed_paths:
                    for value in adversarial_values:
                        with self.subTest(adversarial_signed_path=path, value_type=type(value).__name__):
                            errors = validate_readiness_matrix(mutate(path, value), trusted_context=TRUST_CONTEXT)
                            rendered = "; ".join(errors)
                            self.assertTrue(errors)
                            self.assertIn("verdict must match derived verdict HOLD", rendered)
                            self.assertTrue(
                                "external readiness authenticity" in rendered
                                or "readiness_matrix signed data is not safely bounded canonical JSON" in rendered
                            )
                            self.assertNotIn(TRUST_KEY.decode(), rendered)

                stable_boundary_errors = [
                    "readiness_matrix signed data is not safely bounded canonical JSON",
                    "external readiness evidence is unauthenticated",
                    "verdict must match derived verdict HOLD",
                ]
                cyclic_top = copy.deepcopy(original)
                cyclic_top["cycle"] = cyclic_top
                cyclic_receipt = copy.deepcopy(original)
                receipt = cyclic_receipt["rows"][2]["evidence"][0]["receipt"]
                receipt["cycle"] = receipt
                cyclic_postcondition = copy.deepcopy(original)
                postcondition = cyclic_postcondition["rows"][2]["evidence"][0]["postcondition"]
                postcondition["cycle"] = postcondition
                excessive_nesting: object = "leaf"
                for _ in range(1_500):
                    excessive_nesting = [excessive_nesting]
                deeply_nested = copy.deepcopy(original)
                deeply_nested["rows"][2]["evidence"][0]["receipt"]["summary"] = excessive_nesting
                excessive_nodes = copy.deepcopy(original)
                excessive_nodes["rows"][2]["evidence"][0]["receipt"]["summary"] = [
                    None
                ] * READINESS_CANONICAL_JSON_MAX_NODES
                excessive_bytes = copy.deepcopy(original)
                excessive_bytes["rows"][2]["evidence"][0]["receipt"]["summary"] = (
                    "x" * READINESS_CANONICAL_JSON_MAX_BYTES
                )
                self.assertEqual(
                    (
                        READINESS_CANONICAL_JSON_MAX_DEPTH,
                        READINESS_CANONICAL_JSON_MAX_NODES,
                        READINESS_CANONICAL_JSON_MAX_BYTES,
                    ),
                    (16, 2_048, 65_536),
                )

                class HostileDict(dict[object, object]):
                    item_calls = 0

                    def items(self) -> object:
                        type(self).item_calls += 1
                        raise KeyError("hostile mapping hook must not escape or render")

                class HostileList(list[object]):
                    iteration_calls = 0

                    def __iter__(self) -> object:
                        type(self).iteration_calls += 1
                        raise ValueError("hostile sequence hook must not escape or render")

                hostile_top = HostileDict(original)
                hostile_receipt = copy.deepcopy(original)
                hostile_receipt["rows"][2]["evidence"][0]["receipt"] = HostileDict(
                    hostile_receipt["rows"][2]["evidence"][0]["receipt"]
                )
                hostile_postcondition = copy.deepcopy(original)
                hostile_postcondition["rows"][2]["evidence"][0]["postcondition"] = HostileDict(
                    hostile_postcondition["rows"][2]["evidence"][0]["postcondition"]
                )
                hostile_sequence = copy.deepcopy(original)
                hostile_sequence["rows"] = HostileList(hostile_sequence["rows"])
                boundary_cases = (
                    cyclic_top,
                    cyclic_receipt,
                    cyclic_postcondition,
                    deeply_nested,
                    excessive_nodes,
                    excessive_bytes,
                    hostile_top,
                    hostile_receipt,
                    hostile_postcondition,
                    hostile_sequence,
                )
                for case in boundary_cases:
                    with self.subTest(public_boundary_case=type(case).__name__):
                        errors = validate_readiness_matrix(case, trusted_context=TRUST_CONTEXT)
                        self.assertEqual(errors, stable_boundary_errors)
                        rendered = "; ".join(errors)
                        self.assertNotIn(TRUST_KEY.decode(), rendered)
                        self.assertNotIn("hostile", rendered)
                self.assertEqual(HostileDict.item_calls, 0)
                self.assertEqual(HostileList.iteration_calls, 0)

    def test_supplied_matrix_id_is_authenticated_not_only_structurally_checked(self) -> None:
            rows = complete_rows()
            rows[2] = external_row("ci", external_evidence("ci"))
            matrix = build_readiness_matrix(
                scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
            )
            matrix["matrix_id"] = "readiness-forged-public-id"
            errors = validate_readiness_matrix(matrix, trusted_context=TRUST_CONTEXT)
            self.assertIn("matrix_id must match canonical", "; ".join(errors))
            self.assertIn("external readiness authenticity tag does not match trusted context", "; ".join(errors))
