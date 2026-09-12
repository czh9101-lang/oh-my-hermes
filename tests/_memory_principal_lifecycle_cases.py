from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from omh.paths import resolve_paths
from omh.plugin_bundle.omh.memory_principals import build_memory_identity
from omh.system.local_store import atomic_write_json
from omh.workflows.memory import approve_project_memory_candidate, capture_project_memory_candidate
from omh.workflows.memory_lifecycle import (
    apply_memory_correction,
    apply_memory_prune,
    apply_memory_reapproval,
    apply_memory_restore,
    apply_memory_retirement,
    build_memory_correction,
    build_memory_prune,
    build_memory_reapproval,
    build_memory_restore,
    build_memory_retirement,
)
from omh.workflows.memory_lifecycle_executor import execute_memory_lifecycle
from omh.workflows.memory_principal_migration import (
    apply_principal_migration,
    build_principal_migration_report,
    export_principal_memory,
    principal_migration_plan_digest,
    rollback_principal_migration,
)

PRINCIPAL_A = "principal:v1:" + "a" * 64
PRINCIPAL_B = "principal:v1:" + "b" * 64
PRINCIPAL_C = "principal:v1:" + "c" * 64
NOW = datetime.fromisoformat("2026-09-12T00:00:00+00:00")


def _context(principal: str) -> dict[str, Any]:
    return {
        "schema_version": "memory_principal_context/v1",
        "principal": principal,
        "profile_ref": "profile_fixture",
        "surface_ref": "fixture",
        "session_ref": "shared_fixture",
        "turn_ref": "turn_1",
        "actor_kind": "human",
        "identity_evidence_refs": ["evidence:fixture"],
        "binding_state": "validated_local",
    }


def _approved(
    paths,
    summary: str,
    *,
    retention_class: str = "standard",
    ttl_days: int | None = None,
) -> dict[str, Any]:
    captured: Any = capture_project_memory_candidate(
        paths,
        summary,
        scope_kind="user",
        principal_context=_context(PRINCIPAL_A),
        ttl_days=ttl_days,
        retention_class=retention_class,
    )
    approved: Any = approve_project_memory_candidate(
        paths,
        str(captured["candidate"]["candidate_id"]),
        reviewer_principal=PRINCIPAL_B,
    )
    return approved["record"]


class MemoryPrincipalLifecycleTests(unittest.TestCase):
    def test_M5_lifecycle_and_export_enforce_and_preserve_subject(self) -> None:
        # Given an owner-bound record and a foreign actor.
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / "omh", Path(tmp) / "hermes")
            record = _approved(paths, "Original owner fact")
            record_id = str(record["record_id"])

            # When foreign and owner lifecycle/export operations are invoked.
            foreign = build_memory_correction(paths, record_id, 1, "foreign edit", now=NOW, principal_context=_context(PRINCIPAL_B))
            owner = build_memory_correction(paths, record_id, 1, "owner edit", now=NOW, principal_context=_context(PRINCIPAL_A))
            exported_a = export_principal_memory(paths, _context(PRINCIPAL_A))
            exported_b = export_principal_memory(paths, _context(PRINCIPAL_B))
            applied = apply_memory_correction(paths, owner, transaction_executor=execute_memory_lifecycle, principal_context=_context(PRINCIPAL_A))
            candidate_id = str(owner.mutations[-1].payload["candidate_id"]) if isinstance(owner.mutations[-1].payload, dict) else ""
            reapproval_builder: Any = build_memory_reapproval
            reapproval_apply: Any = apply_memory_reapproval
            denied_review = reapproval_builder(paths, candidate_id, reviewer_claim="reviewer", now=NOW, principal_context=_context(PRINCIPAL_C))
            reviewed = reapproval_builder(paths, candidate_id, reviewer_claim="reviewer", now=NOW, principal_context=_context(PRINCIPAL_B))
            reapproved = reapproval_apply(paths, reviewed, transaction_executor=execute_memory_lifecycle, principal_context=_context(PRINCIPAL_B))

            # Then only the owner/designated reviewer acts and identity survives.
            self.assertEqual(foreign.report["reason_code"], "principal_mismatch")
            self.assertTrue(applied["applied"])
            self.assertEqual(denied_review.report["reason_code"], "principal_mismatch")
            self.assertTrue(reapproved["applied"])
            candidate = json.loads((paths.memory_dir / owner.mutations[-1].target).read_text())
            self.assertEqual(candidate["replacement"]["identity"]["subject_principal"], PRINCIPAL_A)
            self.assertEqual(exported_a["record_count"], 1)
            self.assertEqual(exported_b["record_count"], 0)

    def test_M5_retire_restore_and_delete_plans_reject_foreign_principal(self) -> None:
        # Given expired owner-bound standard and volatile records.
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / "omh", Path(tmp) / "hermes")
            standard = _approved(paths, "Expiring standard", ttl_days=1)
            volatile = _approved(paths, "Expiring volatile", retention_class="volatile", ttl_days=1)
            future = datetime.fromisoformat(str(standard["retention"]["expires_at"]).replace("Z", "+00:00")) + timedelta(seconds=1)

            # When retirement, restore, and delete run through real lifecycle operations.
            retire = build_memory_retirement(paths, str(standard["record_id"]), 1, now=future, principal_context=_context(PRINCIPAL_A))
            apply_memory_retirement(paths, retire, transaction_executor=execute_memory_lifecycle, principal_context=_context(PRINCIPAL_A))
            restore_foreign = build_memory_restore(paths, str(standard["record_id"]), 1, now=future, principal_context=_context(PRINCIPAL_B))
            restore_owner = build_memory_restore(paths, str(standard["record_id"]), 1, now=future, principal_context=_context(PRINCIPAL_A))
            restored = apply_memory_restore(paths, restore_owner, transaction_executor=execute_memory_lifecycle, principal_context=_context(PRINCIPAL_A))
            prune_foreign = build_memory_prune(paths, str(volatile["record_id"]), 1, now=future, principal_context=_context(PRINCIPAL_B))
            prune_owner = build_memory_prune(paths, str(volatile["record_id"]), 1, now=future, principal_context=_context(PRINCIPAL_A))
            pruned = apply_memory_prune(paths, prune_owner, transaction_executor=execute_memory_lifecycle, confirm_hard_delete_local=True, principal_context=_context(PRINCIPAL_A))

            # Then foreign operations deny and owner operations preserve identity.
            self.assertEqual(restore_foreign.report["reason_code"], "principal_mismatch")
            self.assertTrue(restored["applied"])
            self.assertEqual(prune_foreign.report["reason_code"], "principal_mismatch")
            self.assertTrue(pruned["applied"])
            restore_payload: Any = restore_owner.mutations[0].payload
            self.assertEqual(restore_payload["replacement"]["identity"]["subject_principal"], PRINCIPAL_A)
            self.assertTrue((paths.memory_dir / f"tombstones/hard-deleted-{volatile['record_id']}-r1.json").is_file())

    def test_M6_report_apply_repeat_rollback_and_conflict_are_safe(self) -> None:
        # Given one approved legacy v2 record and its immutable review.
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / "omh", Path(tmp) / "hermes")
            captured: Any = capture_project_memory_candidate(paths, "Legacy assignment candidate")
            legacy_result: Any = approve_project_memory_candidate(paths, str(captured["candidate"]["candidate_id"]))
            legacy: Any = legacy_result["record"]
            report = build_principal_migration_report(paths)
            identity = build_memory_identity(_context(PRINCIPAL_A), scope_kind="user", reviewer_principal=PRINCIPAL_B, review_ref=str(legacy["admission"]["review_id"]))
            plan: dict[str, Any] = {"schema_version": "memory_principal_migration_plan/v1", "record_id": legacy["record_id"], "revision": 1, "review_id": legacy["admission"]["review_id"], "identity": identity}
            plan["plan_digest"] = principal_migration_plan_digest(plan)

            # When apply repeats, conflicting rollback refuses, and clean rollback repeats.
            first = apply_principal_migration(paths, plan)
            second = apply_principal_migration(paths, plan)
            target_path = paths.memory_dir / "records" / f"{legacy['record_id']}.json"
            migrated = json.loads(target_path.read_text())
            atomic_write_json(target_path, {**migrated, "summary": "newer revision wins"}, private=True)
            conflict = rollback_principal_migration(paths, str(first["operation_id"]))
            atomic_write_json(target_path, migrated, private=True)
            rolled_back = rollback_principal_migration(paths, str(first["operation_id"]))
            rolled_back_again = rollback_principal_migration(paths, str(first["operation_id"]))

            # Then no content enters reports and no newer revision is clobbered.
            self.assertEqual(report["record_count"], 1)
            self.assertNotIn("Legacy assignment candidate", json.dumps(report))
            self.assertTrue(first["applied"])
            self.assertTrue(second["idempotent"])
            self.assertEqual(conflict["reason_code"], "rollback_target_changed")
            self.assertTrue(rolled_back["applied"])
            self.assertTrue(rolled_back_again["idempotent"])
            restored = json.loads((paths.memory_dir / "records" / f"{legacy['record_id']}.json").read_text())
            self.assertEqual(restored["schema_version"], "project_memory_record/v2")
