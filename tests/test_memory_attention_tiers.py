"""Attention tiers: active, reference, archive (issue #829).

The contract under test is narrow and load-bearing:

- a tier change is previewable before it is applied, and the preview writes
  nothing (AC1);
- identical records and tiers produce an identical recall order (AC2);
- archive is an attention tier, not retirement: the record stays in the store,
  stays readable, stays queryable, and is never deleted (AC3).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _cli_harness import run_cli
from _local_package import load_local_package

load_local_package()
from omh.memory import (
    apply_memory_attention_change,
    apply_memory_retirement,
    approve_project_memory_candidate,
    build_memory_attention_change,
    build_project_memory_recall_pack,
    capture_project_memory_candidate,
    memory_recall_pack_for_handoff,
    read_memory_attention_journal,
    record_attention_tier,
    validate_project_memory_record,
    validate_project_memory_recall_pack,
)
from project_identity_fixture import memory_paths as resolve_paths
from omh.plugin_bundle.omh.memory_governance import canonical_payload_digest

_NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)
# Approval stamps records with the real clock, so an expiry threshold has to be
# relative to it. Nothing hashed, ordered, or byte-compared in this module
# carries this value; it only decides whether a TTL deadline has passed.
_AFTER_EXPIRY = datetime.now(timezone.utc) + timedelta(days=365)


def _approve(paths, summary: str, **kwargs) -> dict:
    captured = capture_project_memory_candidate(paths, summary, **kwargs)
    return approve_project_memory_candidate(paths, captured["candidate"]["candidate_id"])["record"]


def _store_digest(paths) -> str:
    """One digest over every byte under .omh/memory, path-normalized.

    Relative paths are compared as POSIX text so the digest is identical on
    Windows and POSIX; the bytes themselves are read raw, so a line-ending
    difference inside a stored file would still be caught.
    """
    root = paths.memory_dir
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _included_ids(pack: dict) -> list[str]:
    return [str(item["record_id"]) for item in pack["included_records"]]


def _cli(home: Path, *args):
    status, stdout, stderr = run_cli(["--omh-home", str(home / ".omh"), "--hermes-home", str(home / ".hermes"), *args])
    assert status == 0, f"{args} failed: {stderr or stdout}"
    return json.loads(stdout)


class AttentionTierPreviewTests(unittest.TestCase):
    """AC1: preview and apply are separate steps, and preview mutates nothing."""

    def test_preview_reports_the_resulting_working_context_and_writes_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            first = _approve(paths, "Deploys go through staging first", record_type="decision", tags=["deploy"])
            second = _approve(paths, "Deploys use canary batches", record_type="decision", tags=["deploy"])
            before_digest = _store_digest(paths)

            preview = build_memory_attention_change(
                paths, first["record_id"], tier="archive", reason="superseded by the canary policy", query="deploys", now=_NOW
            )

            self.assertTrue(preview["eligible"])
            self.assertFalse(preview["applied"])
            self.assertEqual(preview["reason_code"], "planned")
            self.assertEqual(preview["current_tier"], "active")
            self.assertEqual(preview["requested_tier"], "archive")
            self.assertEqual(preview["leaving_working_context"], [first["record_id"]])
            self.assertEqual(preview["working_context_after"]["record_ids"], [second["record_id"]])
            self.assertIn("Nothing has changed yet", preview["next_action"])
            self.assertEqual(_store_digest(paths), before_digest, "a preview must not touch a single byte of the store")

    def test_apply_produces_exactly_the_previewed_working_context(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            first = _approve(paths, "Deploys go through staging first", record_type="decision", tags=["deploy"])
            _approve(paths, "Deploys use canary batches", record_type="decision", tags=["deploy"])
            preview = build_memory_attention_change(paths, first["record_id"], tier="archive", query="deploys", now=_NOW)
            before_digest = _store_digest(paths)

            applied = apply_memory_attention_change(paths, first["record_id"], tier="archive", query="deploys", now=_NOW)

            self.assertTrue(applied["applied"])
            self.assertEqual(applied["reason_code"], "applied")
            self.assertNotEqual(_store_digest(paths), before_digest, "apply is the step that writes")
            pack = build_project_memory_recall_pack(paths, "deploys", now=_NOW)
            self.assertEqual(
                _included_ids(pack),
                preview["working_context_after"]["record_ids"],
                "the previewed working context must be the one the operator actually gets",
            )

    def test_apply_journals_the_prior_tier_so_the_change_is_reversible(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            record = _approve(paths, "Deploys go through staging first")

            apply_memory_attention_change(paths, record["record_id"], tier="archive", reason="old policy", now=_NOW)

            entries = read_memory_attention_journal(paths, record_id=record["record_id"])
            self.assertEqual([(entry["previous_tier"], entry["tier"]) for entry in entries], [("active", "archive")])
            self.assertEqual(entries[0]["reason"], "old policy")
            stored = json.loads((paths.memory_dir / "records" / f"{record['record_id']}.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["attention"]["previous_tier"], "active")

            apply_memory_attention_change(paths, record["record_id"], tier="active", reason="still current", now=_NOW)

            self.assertEqual(record_attention_tier(_read_record(paths, record["record_id"])), "active")
            self.assertEqual(
                [(entry["previous_tier"], entry["tier"]) for entry in read_memory_attention_journal(paths, record_id=record["record_id"])],
                [("active", "archive"), ("archive", "active")],
            )

    def test_cli_previews_before_it_applies(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            paths = resolve_paths(home / ".omh", home / ".hermes")
            record = _approve(paths, "Deploys go through staging first")
            before_digest = _store_digest(paths)

            preview = _cli(home, "memory", "attention", record["record_id"], "--tier", "reference")

            self.assertFalse(preview["applied"])
            self.assertEqual(_store_digest(paths), before_digest)

            applied = _cli(home, "memory", "attention", record["record_id"], "--tier", "reference", "--apply")

            self.assertTrue(applied["applied"])
            self.assertEqual(record_attention_tier(_read_record(paths, record["record_id"])), "reference")


class AttentionTierOrderTests(unittest.TestCase):
    """AC2: identical records and tier metadata produce an identical order."""

    def test_repeated_builds_produce_the_same_order(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            for summary in ("Deploys stage first", "Deploys canary next", "Deploys roll back last"):
                _approve(paths, summary, tags=["deploy"])
            apply_memory_attention_change(paths, _ids(paths)[0], tier="reference", now=_NOW)

            orders = {tuple(_included_ids(build_project_memory_recall_pack(paths, "deploys", now=_NOW))) for _ in range(5)}

            self.assertEqual(len(orders), 1, orders)

    def test_order_does_not_depend_on_the_order_files_were_written(self) -> None:
        with TemporaryDirectory() as tmp:
            source = resolve_paths(Path(tmp) / "a" / ".omh", Path(tmp) / "a" / ".hermes")
            for summary in ("Deploys stage first", "Deploys canary next", "Deploys roll back last"):
                _approve(source, summary, tags=["deploy"])
            apply_memory_attention_change(source, _ids(source)[0], tier="reference", now=_NOW)
            expected = _included_ids(build_project_memory_recall_pack(source, "deploys", now=_NOW))

            mirror = resolve_paths(Path(tmp) / "b" / ".omh", Path(tmp) / "b" / ".hermes")
            for path in sorted(p for p in source.memory_dir.rglob("*") if p.is_file())[::-1]:
                target = mirror.memory_dir / path.relative_to(source.memory_dir)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)

            self.assertEqual(_included_ids(build_project_memory_recall_pack(mirror, "deploys", now=_NOW)), expected)

    def test_reverting_a_tier_restores_the_original_order(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            for summary in ("Deploys stage first", "Deploys canary next", "Deploys roll back last"):
                _approve(paths, summary, tags=["deploy"])
            original = _included_ids(build_project_memory_recall_pack(paths, "deploys", now=_NOW))
            demoted = original[0]

            apply_memory_attention_change(paths, demoted, tier="reference", now=_NOW)
            reordered = _included_ids(build_project_memory_recall_pack(paths, "deploys", now=_NOW))
            apply_memory_attention_change(paths, demoted, tier="active", now=_NOW)

            self.assertEqual(reordered[-1], demoted, "a reference record yields to its active peers")
            self.assertNotEqual(reordered, original)
            self.assertEqual(_included_ids(build_project_memory_recall_pack(paths, "deploys", now=_NOW)), original)

    def test_tier_outranks_keyword_relevance_inside_the_existing_ladder(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            strong = _approve(paths, "Deploys canary rollback staging checklist", tags=["deploy", "canary", "rollback"])
            weak = _approve(paths, "Deploys need review", tags=["deploy"])
            query = "deploys canary rollback staging checklist"
            self.assertEqual(_included_ids(build_project_memory_recall_pack(paths, query, now=_NOW))[0], strong["record_id"])

            apply_memory_attention_change(paths, strong["record_id"], tier="reference", now=_NOW)

            pack = build_project_memory_recall_pack(paths, query, now=_NOW)
            self.assertEqual(_included_ids(pack), [weak["record_id"], strong["record_id"]])
            ranks = {item["record_id"]: item["ranking"]["attention_rank"] for item in pack["included_records"]}
            self.assertEqual(ranks, {weak["record_id"]: 0, strong["record_id"]: 1})
            self.assertEqual(
                [item["ranking"]["relevance_rank"] for item in pack["included_records"]],
                [2, 1],
                "the tier reorders the pack without rewriting the relevance evidence that explains it",
            )

    def test_pack_discloses_when_reference_records_are_included(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            first = _approve(paths, "Deploys stage first", tags=["deploy"])
            _approve(paths, "Deploys canary next", tags=["deploy"])
            apply_memory_attention_change(paths, first["record_id"], tier="reference", now=_NOW)

            pack = build_project_memory_recall_pack(paths, "deploys", now=_NOW)

            self.assertEqual(validate_project_memory_recall_pack(pack), [])
            self.assertEqual(pack["attention"]["active_included"], 1)
            self.assertEqual(pack["attention"]["reference_included"], 1)
            self.assertIn("reference-tier record(s) are included", pack["attention"]["detail"])
            self.assertEqual({item["attention_tier"] for item in pack["included_records"]}, {"active", "reference"})

    def test_a_malformed_attention_block_fails_pack_validation(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            _approve(paths, "Deploys stage first", tags=["deploy"])
            pack = build_project_memory_recall_pack(paths, "deploys", now=_NOW)

            pack["attention"] = {**pack["attention"], "smuggled": {"nested": "value"}}

            self.assertEqual(
                validate_project_memory_recall_pack(pack),
                [
                    "memory_recall_pack.attention has unsupported keys: ['smuggled']",
                    "memory_recall_pack.attention.smuggled must be scalar metadata",
                ],
            )


class ArchiveTierTests(unittest.TestCase):
    """AC3: archived records stay discoverable and are never silently deleted."""

    def test_archived_record_leaves_the_default_pack_named_not_silently(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            record = _approve(paths, "Deploys go through staging first", tags=["deploy"])
            apply_memory_attention_change(paths, record["record_id"], tier="archive", now=_NOW)

            pack = build_project_memory_recall_pack(paths, "deploys", now=_NOW)

            self.assertEqual(_included_ids(pack), [])
            self.assertEqual(
                [entry for entry in pack["excluded_records"] if entry["reason"] == "archived_tier"],
                [{"record_id": record["record_id"], "reason": "archived_tier", "staleness": {"state": "not_checked"}}],
            )
            self.assertEqual(pack["attention"]["archived_excluded"], 1)
            self.assertIn("remain in the store", pack["attention"]["detail"])
            self.assertEqual(validate_project_memory_recall_pack(pack), [])

    def test_archived_record_stays_readable_and_answers_an_explicit_query(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            record = _approve(paths, "Deploys go through staging first", tags=["deploy"])
            apply_memory_attention_change(paths, record["record_id"], tier="archive", now=_NOW)

            stored = _read_record(paths, record["record_id"])

            self.assertEqual(validate_project_memory_record(stored), [])
            self.assertEqual(stored["summary"], record["summary"])
            self.assertEqual(record_attention_tier(stored), "archive")
            explicit = build_project_memory_recall_pack(paths, "deploys", now=_NOW, include_archived=True)
            self.assertEqual(_included_ids(explicit), [record["record_id"]])
            self.assertEqual(explicit["attention"]["archived_included"], 1)
            self.assertTrue(explicit["attention"]["include_archived"])

    def test_a_handoff_pack_stops_carrying_an_archived_record(self) -> None:
        """The point of the tier: what an executor receives actually shrinks."""
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            stale_policy = _approve(paths, "Deploys go through a shared runner", tags=["deploy"])
            current = _approve(paths, "Deploys use canary batches", tags=["deploy"])
            before = memory_recall_pack_for_handoff(paths, "deploys", executor_target="codex")
            self.assertEqual(
                {str(item["record_id"]) for item in before["included_records"]},
                {stale_policy["record_id"], current["record_id"]},
            )

            apply_memory_attention_change(paths, stale_policy["record_id"], tier="archive", now=_NOW)

            after = memory_recall_pack_for_handoff(paths, "deploys", executor_target="codex")
            self.assertEqual(_included_ids(after), [current["record_id"]])
            self.assertEqual(after["attention"]["archived_excluded"], 1)

    def test_archiving_a_record_never_removes_it(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            record = _approve(paths, "Deploys go through staging first")
            record_path = paths.memory_dir / "records" / f"{record['record_id']}.json"
            payload_before = json.loads(record_path.read_text(encoding="utf-8"))

            apply_memory_attention_change(paths, record["record_id"], tier="archive", now=_NOW)

            self.assertTrue(record_path.exists())
            payload_after = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(
                {key: value for key, value in payload_after.items() if key != "attention"},
                {key: value for key, value in payload_before.items() if key != "attention"},
                "a tier change rewrites the attention block and nothing else",
            )
            self.assertEqual(
                payload_after["admission"]["payload_digest"],
                canonical_payload_digest(payload_after),
                "the tier is attention metadata, so it stays outside the reviewed payload digest",
            )

    def test_archive_the_tier_is_a_different_state_from_retirement(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            evergreen = _approve(paths, "Deploys go through staging first", record_type="decision")
            expiring = _approve(paths, "Deploys once used a shared runner", record_type="episode", ttl_days=1)
            both = _approve(paths, "Deploys once pinned an old image", record_type="episode", ttl_days=1)
            apply_memory_attention_change(paths, evergreen["record_id"], tier="archive", now=_NOW)
            apply_memory_attention_change(paths, both["record_id"], tier="archive", now=_NOW)

            applied = apply_memory_retirement(paths, now=_AFTER_EXPIRY)

            records_dir = paths.memory_dir / "records"
            archive_dir = paths.memory_dir / "archive"
            self.assertEqual(
                [path.stem for path in sorted(records_dir.glob("*.json"))],
                [evergreen["record_id"]],
                "an archive-tier record is still a live record; only retirement moves a file",
            )
            self.assertEqual(
                {str(row["record_id"]) for row in applied["moved"]},
                {expiring["record_id"], both["record_id"]},
                "the tier is not an exemption: an expired archive-tier record still retires",
            )
            self.assertEqual(record_attention_tier(_read_record(paths, evergreen["record_id"])), "archive")
            self.assertFalse(
                list(archive_dir.glob(f"{evergreen['record_id']}.*.json")),
                "the tier writes no archive file and no tombstone",
            )
            self.assertTrue(list(archive_dir.glob(f"{expiring['record_id']}.*.json")))


class AttentionTierGuardTests(unittest.TestCase):
    def test_unknown_tier_is_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            record = _approve(paths, "Deploys go through staging first")

            with self.assertRaises(ValueError) as raised:
                build_memory_attention_change(paths, record["record_id"], tier="cold-storage", now=_NOW)

            self.assertIn("unsupported memory attention tier", str(raised.exception))
            self.assertIn("active, reference, archive", str(raised.exception))
            self.assertEqual(record_attention_tier(_read_record(paths, record["record_id"])), "active")

    def test_tier_change_on_a_missing_record_is_refused_with_a_readable_reason(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            _approve(paths, "Deploys go through staging first")
            before_digest = _store_digest(paths)

            report = build_memory_attention_change(paths, "mem_00000000000000ee", tier="archive", now=_NOW)

            self.assertFalse(report["eligible"])
            self.assertEqual(report["reason_code"], "record_not_found")
            self.assertEqual(report["detail"], "No approved OMH memory record carries that id, so there is no attention tier to change.")
            with self.assertRaises(ValueError) as raised:
                apply_memory_attention_change(paths, "mem_00000000000000ee", tier="archive", now=_NOW)
            self.assertIn("record_not_found", str(raised.exception))
            self.assertEqual(_store_digest(paths), before_digest)

    def test_a_no_op_tier_change_is_refused_so_every_journal_line_is_a_real_move(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            record = _approve(paths, "Deploys go through staging first")

            report = build_memory_attention_change(paths, record["record_id"], tier="active", now=_NOW)

            self.assertFalse(report["eligible"])
            self.assertEqual(report["reason_code"], "tier_unchanged")
            self.assertEqual(report["current_tier"], "active")
            with self.assertRaises(ValueError):
                apply_memory_attention_change(paths, record["record_id"], tier="active", now=_NOW)
            self.assertEqual(read_memory_attention_journal(paths), [])

    def test_a_sensitive_reason_is_redacted_before_it_reaches_the_record(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            record = _approve(paths, "Deploys go through staging first")

            applied = apply_memory_attention_change(
                paths, record["record_id"], tier="archive", reason="held the deploy password", now=_NOW
            )

            self.assertEqual(applied["reason"], "[redacted]")
            self.assertEqual(_read_record(paths, record["record_id"])["attention"]["reason"], "[redacted]")
            self.assertEqual(read_memory_attention_journal(paths)[0]["reason"], "[redacted]")

    def test_unsafe_record_id_is_refused_before_any_store_read(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            with self.assertRaises(ValueError) as raised:
                build_memory_attention_change(paths, "../escape", tier="archive", now=_NOW)
            self.assertIn("unsafe memory record id", str(raised.exception))


class AttentionTierLifecycleTests(unittest.TestCase):
    """The tier must survive the correction/restore round trip.

    A new record field that is not carried through `_pending_candidate` and
    `_approved_record` is dropped silently by correct/restore, which would
    quietly return an archived record to the active working set.
    """

    def test_tier_survives_capture_approve_correct_reapprove(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            paths = resolve_paths(home / ".omh", home / ".hermes")
            record = _approve(paths, "Deploys go through staging first", record_type="decision")
            apply_memory_attention_change(paths, record["record_id"], tier="reference", reason="background context", now=_NOW)

            _cli(home, "memory", "correct", record["record_id"], "Deploys go through staging, then canary", "--revision", "1", "--apply")
            correction = _lifecycle_candidate(paths, "correction")
            result = _cli(home, "memory", "approve", correction)

            self.assertTrue(result.get("applied"), str(result.get("reason_code")))
            live = _read_record(paths, record["record_id"])
            self.assertEqual(live["revision"], 2)
            self.assertEqual(record_attention_tier(live), "reference")
            self.assertEqual(live["attention"]["reason"], "background context")
            self.assertEqual(live["attention"]["previous_tier"], "active")

    def test_tier_survives_retire_restore_reapprove(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            paths = resolve_paths(home / ".omh", home / ".hermes")
            record = _approve(paths, "Deploys once used a shared runner", record_type="episode", ttl_days=1)
            apply_memory_attention_change(paths, record["record_id"], tier="reference", reason="background context", now=_NOW)

            apply_memory_retirement(paths, now=_AFTER_EXPIRY)
            _cli(home, "memory", "restore", record["record_id"], "--revision", "1", "--apply")
            result = _cli(home, "memory", "approve", _lifecycle_candidate(paths, "restore"))

            self.assertTrue(result.get("applied"), str(result.get("reason_code")))
            self.assertEqual(record_attention_tier(_read_record(paths, record["record_id"])), "reference")


def _ids(paths) -> list[str]:
    return sorted(path.stem for path in (paths.memory_dir / "records").glob("*.json"))


def _read_record(paths, record_id: str) -> dict:
    return json.loads((paths.memory_dir / "records" / f"{record_id}.json").read_text(encoding="utf-8"))


def _lifecycle_candidate(paths, lifecycle: str) -> str:
    for path in sorted((paths.memory_dir / "candidates").glob("*.json")):
        candidate = json.loads(path.read_text(encoding="utf-8"))
        if candidate.get("lifecycle") == lifecycle:
            return str(candidate["candidate_id"])
    raise AssertionError(f"no {lifecycle} candidate was staged")


if __name__ == "__main__":
    unittest.main()
