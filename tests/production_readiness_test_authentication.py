from __future__ import annotations

# ruff: noqa: F405
from production_readiness_test_support import *  # noqa: F403


class ReadinessMatrixTestsMixin:
    def test_timestamp_validation_rejects_naive_aware_orderings_without_crashing(self) -> None:
            cases = (
                ("2026-08-26T12:00:00", FRESH_AFTER, CREATED),
                (CREATED, "2026-08-26T11:00:00", CREATED),
                (CREATED, FRESH_AFTER, "2026-08-26T11:30:00"),
            )
            for observed_at, fresh_after, created_at in cases:
                with self.subTest(observed_at=observed_at, fresh_after=fresh_after, created_at=created_at):
                    rows = complete_rows()
                    rows[0]["evidence"] = [observed_check("build", observed_at=observed_at)]
                    with self.assertRaisesRegex(ValueError, "timezone-aware ISO-8601 timestamp"):
                        build_readiness_matrix(
                            scope="release-1119",
                            task_id=TASK_ID,
                            revision=REVISION,
                            rows=rows,
                            created_at=created_at,
                            evidence_fresh_after=fresh_after,
                        )

    def test_coherently_forged_public_bundle_cannot_authenticate(self) -> None:
            rows = complete_rows()
            rows[2] = external_row("ci", external_evidence("ci"))
            matrix = build_readiness_matrix(
                scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
            )
            forged = copy.deepcopy(matrix)
            forged_task = "task-forged"
            forged_revision = "revision-forged"
            forged_run = "run-forged"
            forged_effect = "readiness:forged"
            forged["task_id"] = forged_task
            forged["revision"] = forged_revision
            forged["matrix_id"] = "readiness-" + hashlib.sha256(
                json.dumps(
                    {
                        "scope": forged["scope"],
                        "task_id": forged_task,
                        "revision": forged_revision,
                        "created_at": forged["created_at"],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()[:16]
            row = forged["rows"][2]
            row["external_identity"] = {"effect_id": forged_effect, "run_id": forged_run}
            receipt = row["evidence"][0]["receipt"]
            receipt["effect_id"] = forged_effect
            receipt["run_id"] = forged_run
            receipt["evidence_refs"] = [f"task:{forged_task}", f"revision:{forged_revision}"]
            receipt["receipt_id"] = "receipt-" + hashlib.sha256(b"coherent-public-forgery").hexdigest()[:24]
            postcondition = row["evidence"][0]["postcondition"]
            postcondition.update(
                {
                    "receipt_id": receipt["receipt_id"],
                    "effect_id": forged_effect,
                    "run_id": forged_run,
                    "task_id": forged_task,
                    "revision": forged_revision,
                }
            )
            row["evidence"][0]["authenticity"]["hmac_sha256"] = hashlib.sha256(
                json.dumps(row["evidence"][0], sort_keys=True, default=str).encode()
            ).hexdigest()

            errors = validate_readiness_matrix(forged, trusted_context=TRUST_CONTEXT)

            self.assertIn("external readiness authenticity tag does not match trusted context", "; ".join(errors))
            self.assertIn("verdict must match derived verdict HOLD", "; ".join(errors))

    def test_authenticated_stale_hold_cannot_widen_freshness_into_go(self) -> None:
            rows = complete_rows()
            stale = external_evidence(
                "ci",
                receipt_observed_at="2026-08-26T10:59:59Z",
                postcondition_observed_at="2026-08-26T10:59:59Z",
            )
            rows[2] = external_row("ci", stale)
            matrix = build_readiness_matrix(
                scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
            )
            self.assertEqual(matrix["verdict"], "HOLD")
            widened = copy.deepcopy(matrix)
            widened["evidence_fresh_after"] = "2026-08-26T10:00:00Z"
            widened["rows"][2]["evidence_state"] = "observed"
            widened["verdict"] = "GO"
            widened["missing_categories"] = []

            errors = validate_readiness_matrix(widened, trusted_context=TRUST_CONTEXT)

            self.assertIn("external readiness authenticity tag does not match trusted context", "; ".join(errors))
            self.assertIn("verdict must match derived verdict HOLD", "; ".join(errors))

    def test_authentication_binds_all_mutable_verdict_authority_fields(self) -> None:
            rows = complete_rows()
            rows[2] = external_row("ci", external_evidence("ci"))
            matrix = build_readiness_matrix(
                scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
            )
            mutations = []
            changed_created = copy.deepcopy(matrix)
            changed_created["created_at"] = "2026-08-26T12:30:00Z"
            changed_created["matrix_id"] = "readiness-" + hashlib.sha256(
                json.dumps(
                    {
                        "scope": changed_created["scope"],
                        "task_id": changed_created["task_id"],
                        "revision": changed_created["revision"],
                        "created_at": changed_created["created_at"],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()[:16]
            mutations.append(changed_created)
            changed_schema = copy.deepcopy(matrix)
            changed_schema["schema_version"] = "readiness_matrix/v2"
            mutations.append(changed_schema)
            changed_external_mode = copy.deepcopy(matrix)
            changed_external_mode["rows"][2]["requires_external_observation"] = False
            mutations.append(changed_external_mode)
            for mutated in mutations:
                with self.subTest(mutation=mutated):
                    self.assertIn(
                        "external readiness authenticity tag does not match trusted context",
                        "; ".join(validate_readiness_matrix(mutated, trusted_context=TRUST_CONTEXT)),
                    )

    def test_trust_context_retains_no_addressable_raw_key_and_subclasses_fail_closed(self) -> None:
            context = ReadinessTrustContext("operator-readiness", TRUST_KEY)
            self.assertFalse(hasattr(context, "_ReadinessTrustContext__hmac_key"))
            self.assertFalse(
                any(
                    isinstance(getattr(context, name), bytes)
                    for name in ("context_id", "_ReadinessTrustContext__hmac_template", "_ReadinessTrustContext__usable")
                )
            )

            def bytes_leaves(value: object) -> list[bytes]:
                if isinstance(value, bytes):
                    return [value]
                if isinstance(value, dict):
                    return [leaf for item in value.values() for leaf in bytes_leaves(item)]
                if isinstance(value, tuple):
                    return [leaf for item in value for leaf in bytes_leaves(item)]
                return []

            self.assertNotIn(TRUST_KEY, bytes_leaves(object.__getstate__(context)))

            class HostileTrustContext(ReadinessTrustContext):
                def _usable(self) -> bool:
                    raise RuntimeError("raw hostile trust failure")

                def _verify(self, payload: bytes, candidate: str) -> bool:
                    raise RuntimeError("raw hostile trust failure")

            rows = complete_rows()
            rows[2] = external_row("ci", external_evidence("ci"))
            matrix = build_readiness_matrix(
                scope="release-1119", rows=rows, created_at=CREATED, evidence_fresh_after=FRESH_AFTER
            )
            hostile = HostileTrustContext("operator-readiness", TRUST_KEY)

            errors = validate_readiness_matrix(matrix, trusted_context=hostile)

            self.assertIn("external readiness authenticity requires a usable trusted context", errors)
            self.assertIn("verdict must match derived verdict HOLD", errors)
            self.assertNotIn("hostile", "; ".join(errors))
            self.assertEqual(
                artifact_contracts_for_workflow("production-audit")[0].consumer_id,
                "parse_readiness_matrix",
            )

    def test_trust_context_rendering_redacts_key_material(self) -> None:
            key = b"trusted-readiness-key-material-1119"
            context = ReadinessTrustContext("operator-readiness", key)
            rendered = (
                repr(context),
                str(context),
                f"{context}",
                "%r" % context,
                "%s" % context,
                format(context),
                str(RuntimeError(context)),
                logging.Formatter("%(message)s").format(
                    logging.LogRecord("test", logging.ERROR, __file__, 1, "%r", (context,), None)
                ),
            )
            forbidden = (key.decode(), key.hex(), base64.b64encode(key).decode())
            for text in rendered:
                self.assertIn("redacted", text.lower())
                for value in forbidden:
                    self.assertNotIn(value, text)
