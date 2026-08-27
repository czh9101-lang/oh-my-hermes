from __future__ import annotations

# ruff: noqa: F405
from production_readiness_test_support import *  # noqa: F403


class ReadinessMatrixTestsMixin:
    def test_post_child_snapshot_mutations_cannot_escape_root_authority(self) -> None:
                stable_errors = [
                    "readiness_matrix signed data is not safely bounded canonical JSON",
                    "external readiness evidence is unauthenticated",
                    "verdict must match derived verdict HOLD",
                ]
                cases = (
                    ("receipt", ("rows", 2, "evidence", 0, "receipt"), "dict"),
                    ("postcondition", ("rows", 2, "evidence", 0, "postcondition"), "dict"),
                    ("row", ("rows", 2), "dict"),
                    ("external_identity", ("rows", 2, "external_identity"), "dict"),
                    ("evidence_list", ("rows", 2, "evidence"), "list"),
                )
                for name, path, container_kind in cases:
                    with self.subTest(window=name):
                        matrix = build_readiness_matrix(
                            scope="release-1119",
                            rows=complete_rows(),
                            created_at=CREATED,
                            evidence_fresh_after=FRESH_AFTER,
                        )
                        target: object = matrix
                        for part in path:
                            target = target[part]
                        mutation_observed = threading.Event()
                        target_copy_calls = 0
                        if container_kind == "dict":
                            original_copy = readiness_json._copy_plain_dict

                            def controlled_dict_copy(value: dict[object, object]) -> dict[object, object]:
                                nonlocal target_copy_calls
                                detached = original_copy(value)
                                if value is target:
                                    target_copy_calls += 1
                                    if target_copy_calls == 2:
                                        value["post-snapshot-mutation"] = True
                                        mutation_observed.set()
                                return detached

                            manager = patch.object(
                                readiness_json,
                                "_copy_plain_dict",
                                side_effect=controlled_dict_copy,
                            )
                        else:
                            original_list_copy = readiness_json._copy_plain_list

                            def controlled_list_copy(value: list[object]) -> list[object]:
                                nonlocal target_copy_calls
                                detached = original_list_copy(value)
                                if value is target:
                                    target_copy_calls += 1
                                    if target_copy_calls == 2:
                                        value.append({"post-snapshot-mutation": True})
                                        mutation_observed.set()
                                return detached

                            manager = patch.object(
                                readiness_json,
                                "_copy_plain_list",
                                side_effect=controlled_list_copy,
                            )
                        with manager:
                            errors = validate_readiness_matrix(matrix, trusted_context=TRUST_CONTEXT)
                        self.assertTrue(mutation_observed.is_set())
                        self.assertGreaterEqual(target_copy_calls, 2)
                        self.assertEqual(errors, stable_errors)

                        accepted_matrix = build_readiness_matrix(
                            scope="release-1119",
                            rows=complete_rows(),
                            created_at=CREATED,
                            evidence_fresh_after=FRESH_AFTER,
                        )
                        accepted_target: object = accepted_matrix
                        for part in path:
                            accepted_target = accepted_target[part]
                        captured_before = json.dumps(accepted_target, sort_keys=True)
                        root_snapshot_calls = 0
                        original_snapshot = readiness_json._canonical_json_snapshot

                        def mutate_after_second_root_snapshot(value: object) -> object:
                            nonlocal root_snapshot_calls
                            snapshot = original_snapshot(value)
                            if value is accepted_matrix:
                                root_snapshot_calls += 1
                                if root_snapshot_calls == 2:
                                    if container_kind == "dict":
                                        accepted_target["post-snapshot-mutation"] = True
                                    else:
                                        accepted_target.append({"post-snapshot-mutation": True})
                            return snapshot

                        with patch.object(
                            readiness_json,
                            "_canonical_json_snapshot",
                            side_effect=mutate_after_second_root_snapshot,
                        ):
                            result = readiness_workflow.parse_readiness_matrix(
                                accepted_matrix, trusted_context=TRUST_CONTEXT
                            )
                        self.assertGreaterEqual(root_snapshot_calls, 2)
                        self.assertTrue(result.accepted)
                        self.assertEqual(result.verdict, "GO")
                        captured_target: object = result.require_artifact().detached_copy()
                        for part in path:
                            captured_target = captured_target[part]
                        self.assertEqual(json.dumps(captured_target, sort_keys=True), captured_before)
                        self.assertNotEqual(json.dumps(accepted_target, sort_keys=True), captured_before)
