from __future__ import annotations

# ruff: noqa: F405
from production_readiness_test_support import *  # noqa: F403


class ReadinessMatrixTestsMixin:
    def test_typed_parse_result_is_the_only_accepted_artifact_authority(self) -> None:
            parse = getattr(readiness_workflow, "parse_readiness_matrix", None)
            self.assertTrue(callable(parse))
            matrix = build_readiness_matrix(
                scope="release-1119", rows=complete_rows(), created_at=CREATED, evidence_fresh_after=FRESH_AFTER
            )
            root_copy_calls = 0
            original_copy = readiness_json._copy_plain_dict

            def count_root_copies(value: dict[object, object]) -> dict[object, object]:
                nonlocal root_copy_calls
                if value is matrix:
                    root_copy_calls += 1
                return original_copy(value)

            with patch.object(readiness_json, "_copy_plain_dict", side_effect=count_root_copies):
                result = parse(matrix, trusted_context=TRUST_CONTEXT)
            self.assertTrue(result.accepted)
            self.assertEqual(result.errors, ())
            self.assertGreaterEqual(root_copy_calls, 2)
            artifact = result.require_artifact()
            self.assertEqual(artifact.verdict, "GO")
            captured = artifact.detached_copy()
            matrix["verdict"] = "HOLD"
            matrix["rows"][2]["evidence_state"] = "missing"
            self.assertEqual(artifact.verdict, "GO")
            self.assertEqual(artifact.detached_copy(), captured)
            self.assertIsNot(artifact.detached_copy(), artifact.detached_copy())
            with self.assertRaises(FrozenInstanceError):
                artifact.verdict = "HOLD"
            with TemporaryDirectory() as tmp:
                persisted = Path(tmp) / "readiness.json"
                persisted.write_bytes(artifact.canonical_bytes)
                self.assertEqual(json.loads(persisted.read_bytes()), captured)
            registered = artifact_contracts_for_workflow("production-audit")[0]
            self.assertEqual(registered.consumer_id, "parse_readiness_matrix")
            self.assertIs(resolve_artifact_contract_consumer(registered.consumer_id), parse)
            shared_leaf = {"nested": ["accepted"]}
            shared_snapshot = readiness_json._canonical_json_snapshot(
                {"left": shared_leaf, "right": shared_leaf}
            )
            self.assertIsNotNone(shared_snapshot)
            detached_dag = shared_snapshot[0]
            self.assertEqual(detached_dag["left"], detached_dag["right"])
            self.assertIsNot(detached_dag["left"], detached_dag["right"])

    def test_concurrent_builtin_container_mutation_fails_closed_without_retry(self) -> None:
            rows = complete_rows()
            rows[2] = external_row("ci", external_evidence("ci"))
            matrix = build_readiness_matrix(
                scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
            )
            trigger = threading.Event()
            mutated = threading.Event()
            original_consume = readiness_json._consume_json_bytes
            original_copy = readiness_json._copy_plain_dict
            consume_calls = 0
            root_copy_calls = 0

            def mutate_once() -> None:
                self.assertTrue(trigger.wait(timeout=2))
                matrix["concurrent-mutation"] = True
                mutated.set()

            def controlled_consume(amount: int, bytes_used: list[int]) -> bool:
                nonlocal consume_calls
                consume_calls += 1
                accepted = original_consume(amount, bytes_used)
                if consume_calls == 2:
                    trigger.set()
                    self.assertTrue(mutated.wait(timeout=2))
                return accepted

            def controlled_copy(value: dict[object, object]) -> dict[object, object]:
                nonlocal root_copy_calls
                if value is matrix:
                    root_copy_calls += 1
                return original_copy(value)

            worker = threading.Thread(target=mutate_once)
            worker.start()
            try:
                with (
                    patch.object(readiness_json, "_consume_json_bytes", side_effect=controlled_consume),
                    patch.object(readiness_json, "_copy_plain_dict", side_effect=controlled_copy),
                ):
                    errors = validate_readiness_matrix(matrix, trusted_context=TRUST_CONTEXT)
            finally:
                worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(
                errors,
                [
                    "readiness_matrix signed data is not safely bounded canonical JSON",
                    "external readiness evidence is unauthenticated",
                    "verdict must match derived verdict HOLD",
                ],
            )
            self.assertGreaterEqual(consume_calls, 2)
            self.assertGreaterEqual(root_copy_calls, 2)

            list_matrix = build_readiness_matrix(
                scope="release-1119", rows=complete_rows(), created_at=CREATED, evidence_fresh_after=FRESH_AFTER
            )
            rows_list = list_matrix["rows"]
            list_trigger = threading.Event()
            list_mutated = threading.Event()
            original_list_copy = readiness_json._copy_plain_list
            rows_copy_calls = 0

            def mutate_list_once() -> None:
                self.assertTrue(list_trigger.wait(timeout=2))
                rows_list.append(copy.deepcopy(rows_list[0]))
                list_mutated.set()

            def controlled_list_copy(value: list[object]) -> list[object]:
                nonlocal rows_copy_calls
                detached = original_list_copy(value)
                if value is rows_list:
                    rows_copy_calls += 1
                    if rows_copy_calls == 1:
                        list_trigger.set()
                        self.assertTrue(list_mutated.wait(timeout=2))
                return detached

            list_worker = threading.Thread(target=mutate_list_once)
            list_worker.start()
            try:
                with patch.object(readiness_json, "_copy_plain_list", side_effect=controlled_list_copy):
                    list_errors = validate_readiness_matrix(list_matrix, trusted_context=TRUST_CONTEXT)
            finally:
                list_worker.join(timeout=2)
            self.assertFalse(list_worker.is_alive())
            self.assertEqual(list_errors, errors)
            self.assertGreaterEqual(rows_copy_calls, 2)

    def test_trust_context_is_opaque_and_non_serializable(self) -> None:
            context = ReadinessTrustContext("operator-readiness", TRUST_KEY)
            operations = (
                lambda: asdict(context),
                lambda: copy.copy(context),
                lambda: copy.deepcopy(context),
                lambda: pickle.dumps(context),
                lambda: context.__reduce__(),
                lambda: context.__reduce_ex__(4),
                lambda: memoryview(context),
                lambda: json.dumps(context),
            )
            for operation in operations:
                with self.subTest(operation=operation):
                    with self.assertRaises((TypeError, pickle.PicklingError)):
                        operation()
            self.assertFalse(hasattr(context, "hmac_key"))
            self.assertFalse(hasattr(context, "material"))

    def test_stale_public_matrix_id_is_rejected(self) -> None:
            matrix = build_readiness_matrix(
                scope="release-1119", rows=complete_rows(), created_at=CREATED, evidence_fresh_after=FRESH_AFTER
            )
            matrix["task_id"] = "task-forged"
            self.assertIn(
                "matrix_id must match canonical scope, task, revision, and created_at identity",
                validate_readiness_matrix(matrix),
            )

    def test_absent_wrong_or_untrusted_context_cannot_advance_external_evidence(self) -> None:
            rows = complete_rows()
            rows[2] = external_row("ci", external_evidence("ci"))
            valid = build_readiness_matrix(
                scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
            )
            contexts = (
                None,
                ReadinessTrustContext("operator-readiness", b"wrong-readiness-key-material-1119"),
                ReadinessTrustContext("other-context", b"trusted-readiness-key-material-1119"),
                ReadinessTrustContext("operator-readiness", b"short"),
            )
            for context in contexts:
                with self.subTest(context=context):
                    errors = validate_readiness_matrix(valid, trusted_context=context)
                    self.assertIn("external readiness authenticity", "; ".join(errors))
                    rebuilt = build_readiness_matrix(
                        scope="release-1119",
                        rows=rows,
                        created_at=CREATED,
                        evidence_fresh_after=FRESH_AFTER,
                        trusted_context=context,
                    )
                    self.assertEqual(rebuilt["verdict"], "HOLD")
                    self.assertEqual(rebuilt["rows"][2]["evidence_state"], "missing")

    def test_valid_authentication_persists_no_secret(self) -> None:
            rows = complete_rows()
            rows[2] = external_row("ci", external_evidence("ci"))
            matrix = build_readiness_matrix(
                scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
            )
            rendered = json.dumps(matrix, sort_keys=True)
            self.assertEqual(matrix["verdict"], "GO")
            self.assertEqual(validate_readiness_matrix(matrix, trusted_context=TRUST_CONTEXT), [])
            self.assertNotIn(TRUST_KEY.decode(), rendered)

    def test_validator_rejects_each_invalid_top_level_state(self) -> None:
            matrix = build_readiness_matrix(
                scope="release-1119", rows=complete_rows(), created_at=CREATED, evidence_fresh_after=FRESH_AFTER
            )
            mutations = {
                "schema": ("schema_version", "wrong/v1", "schema_version must be readiness_matrix/v1"),
                "scope": ("scope", "", "scope is required"),
                "verdict": ("verdict", "PASS", "unsupported verdict"),
            }
            for name, (key, value, expected) in mutations.items():
                with self.subTest(name=name):
                    invalid = copy.deepcopy(matrix)
                    invalid[key] = value
                    self.assertIn(expected, "; ".join(validate_readiness_matrix(invalid)))
