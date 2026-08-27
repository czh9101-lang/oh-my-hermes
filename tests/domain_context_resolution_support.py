from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from omh.skills.catalog import builtin_definitions
from omh.workflows import domain_intelligence_profile_snapshot as profile_snapshot

from domain_routing_context_support import (
    _approve_profile,
    _binding,
    _repository,
    _resolver,
    _store,
)


class DomainContextResolverMixin:
    def test_healthy_approved_exact_project_profile_resolves_once(self) -> None:
        with TemporaryDirectory() as tmp:
            root = _repository(Path(tmp) / "healthy-project")
            _approve_profile(root, domain_id="sales")
            with _binding(root) as binding:
                fragment = _resolver()(
                    binding, "Please do a pipeline review", locale="en"
                )

        sales = next(item for item in builtin_definitions() if item.name == "sales-development")
        self.assertEqual(
            fragment["domain_routing_context"]["question"],
            {"locale": "en", "text": sales.expert_questions[0].en},
        )
        self.assertEqual(
            fragment["domain_routing_context"]["required_input"],
            "account or segment",
        )

    def test_unrelated_scope_and_no_exact_phrase_do_not_resolve(self) -> None:
        with TemporaryDirectory() as tmp:
            root = _repository(Path(tmp) / "scope-project")
            _approve_profile(
                root,
                domain_id="foreign",
                scope_kind="organization",
                scope_ref="org-acme",
            )
            with _binding(root) as binding:
                self.assertIsNone(_resolver()(binding, "pipeline review", locale="en"))

            _approve_profile(root, domain_id="local", phrase="renewal review")
            with _binding(root) as binding:
                self.assertIsNone(_resolver()(binding, "pipeline review", locale="en"))

    def test_valid_plus_empty_hint_fails_closed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = _repository(Path(tmp) / "empty-coexistence")
            _approve_profile(root, domain_id="valid")
            _approve_profile(root, domain_id="empty", workflow_hints=[])
            with _binding(root) as binding:
                self.assertIsNone(_resolver()(binding, "pipeline review", locale="en"))

    def test_valid_plus_invalid_or_conflicting_hint_fails_closed(self) -> None:
        cases = (
            ("unknown-workflow", "pipeline_review"),
            ("finance-analysis", "pipeline_review"),
            ("sales-development", "different_canonical"),
        )
        for workflow_hint, canonical in cases:
            with (
                self.subTest(workflow_hint=workflow_hint, canonical=canonical),
                TemporaryDirectory() as tmp,
            ):
                root = _repository(Path(tmp) / "conflicting-coexistence")
                _approve_profile(root, domain_id="valid")
                _approve_profile(
                    root,
                    domain_id="other",
                    canonical=canonical,
                    workflow_hints=[workflow_hint],
                )
                with _binding(root) as binding:
                    self.assertIsNone(
                        _resolver()(binding, "pipeline review", locale="en")
                    )

    def test_missing_question_spec_fails_closed(self) -> None:
        from dataclasses import replace
        from omh.skills import catalog

        with TemporaryDirectory() as tmp:
            root = _repository(Path(tmp) / "missing-spec")
            _approve_profile(root, domain_id="sales")
            definitions = [
                replace(item, expert_questions=())
                if item.name == "sales-development"
                else item
                for item in catalog.routable_definitions()
            ]
            with patch.object(
                catalog, "routable_definitions", return_value=definitions
            ):
                with _binding(root) as binding:
                    self.assertIsNone(
                        _resolver()(binding, "pipeline review", locale="en")
                    )

    def test_exactly_64_matches_resolve_and_65_fail_closed(self) -> None:
        for count, expected in ((64, True), (65, False)):
            with self.subTest(count=count), TemporaryDirectory() as tmp:
                root = _repository(Path(tmp) / f"matches-{count}")
                for index in range(count):
                    _approve_profile(root, domain_id=f"sales-{index:02d}")
                with _binding(root) as binding:
                    result = _resolver()(binding, "pipeline review", locale="en")
                self.assertEqual(result is not None, expected)

    def test_each_health_directory_overflow_fails_closed_independently(self) -> None:
        for dirname in ("profiles", "reviews", "history"):
            with self.subTest(dirname=dirname), TemporaryDirectory() as tmp:
                root = _repository(Path(tmp) / f"overflow-{dirname}")
                _approve_profile(root, domain_id="sales")
                directory = _store(root) / dirname
                for index in range(1025 - len(list(directory.glob("*.json")))):
                    (directory / f"overflow-{index:04d}.json").write_text(
                        "{}", encoding="utf-8"
                    )
                with _binding(root) as binding:
                    self.assertIsNone(
                        _resolver()(binding, "pipeline review", locale="en")
                    )

    def test_unrelated_malformed_artifact_early_or_late_fails_closed(self) -> None:
        for dirname in ("profiles", "reviews", "history"):
            for filename in ("000-malformed.json", "zzz-malformed.json"):
                with (
                    self.subTest(dirname=dirname, filename=filename),
                    TemporaryDirectory() as tmp,
                ):
                    root = _repository(Path(tmp) / f"malformed-{dirname}")
                    _approve_profile(root, domain_id="sales")
                    (_store(root) / dirname / filename).write_bytes(b"\xff")
                    with _binding(root) as binding:
                        self.assertIsNone(
                            _resolver()(binding, "pipeline review", locale="en")
                        )

    def test_bad_digest_and_invalid_lineage_fail_closed(self) -> None:
        for mutation in ("digest", "lineage"):
            with self.subTest(mutation=mutation), TemporaryDirectory() as tmp:
                root = _repository(Path(tmp) / f"invalid-{mutation}")
                _approve_profile(root, domain_id="sales")
                profile_path = next((_store(root) / "profiles").glob("*.json"))
                profile = json.loads(profile_path.read_text(encoding="utf-8"))
                if mutation == "digest":
                    profile["payload_digest"] = "0" * 64
                else:
                    profile["base_profile_revision"] = 7
                profile_path.write_text(json.dumps(profile), encoding="utf-8")
                with _binding(root) as binding:
                    self.assertIsNone(
                        _resolver()(binding, "pipeline review", locale="en")
                    )

    def test_identity_conflicts_and_symlink_entries_fail_closed(self) -> None:
        for dirname in ("profiles", "reviews", "history"):
            for corruption in ("identity_conflict", "symlink"):
                with (
                    self.subTest(dirname=dirname, corruption=corruption),
                    TemporaryDirectory() as tmp,
                ):
                    root = _repository(Path(tmp) / f"unsafe-{dirname}-{corruption}")
                    _approve_profile(root, domain_id="sales")
                    _approve_profile(root, domain_id="sales")
                    directory = _store(root) / dirname
                    original = next(directory.glob("*.json"))
                    if corruption == "identity_conflict":
                        (directory / "alias.json").write_bytes(original.read_bytes())
                    else:
                        (directory / "linked.json").symlink_to(original.name)
                    with _binding(root) as binding:
                        self.assertIsNone(
                            _resolver()(binding, "pipeline review", locale="en")
                        )

    def test_malformed_candidates_and_operations_are_irrelevant(self) -> None:
        with TemporaryDirectory() as tmp:
            root = _repository(Path(tmp) / "irrelevant-artifacts")
            _approve_profile(root, domain_id="sales")
            for dirname in ("candidates", "operations"):
                directory = _store(root) / dirname
                directory.mkdir(exist_ok=True)
                (directory / "malformed.json").write_bytes(b"\xff")
            with _binding(root) as binding:
                self.assertIsNotNone(
                    _resolver()(binding, "pipeline review", locale="en")
                )

    def test_bound_descriptor_survives_store_path_swap(self) -> None:
        with TemporaryDirectory() as tmp:
            root = _repository(Path(tmp) / "descriptor-bound")
            _approve_profile(root, domain_id="sales")
            binding = _binding(root)
            self.addCleanup(binding.close)
            (root / ".omh").rename(root / ".omh-opened")
            replacement = _store(root)
            for dirname in ("profiles", "reviews", "history"):
                (replacement / dirname).mkdir(parents=True, exist_ok=True)
            (replacement / ".store.lock").write_text("", encoding="utf-8")

            self.assertIsNotNone(_resolver()(binding, "pipeline review", locale="en"))

    def test_noncooperating_snapshot_mutations_fail_closed_without_retry(self) -> None:
        for dirname in ("profiles", "reviews", "history"):
            for operation in (
                "create",
                "replace",
                "delete",
                "content",
                "directory_swap",
            ):
                with (
                    self.subTest(dirname=dirname, operation=operation),
                    TemporaryDirectory() as tmp,
                ):
                    root = _repository(Path(tmp) / f"mutation-{dirname}-{operation}")
                    _approve_profile(root, domain_id="sales")
                    _approve_profile(root, domain_id="sales")
                    directory = _store(root) / dirname
                    original = next(directory.glob("*.json"), None)
                    real_read = profile_snapshot.read_stable_json_at
                    mutated = False

                    def mutate_after_first_read(*args, **kwargs):
                        nonlocal mutated
                        value = real_read(*args, **kwargs)
                        if not mutated:
                            mutated = True
                            if operation == "create":
                                (directory / "zzz-created.json").write_text(
                                    "{}", encoding="utf-8"
                                )
                            elif operation == "replace" and original is not None:
                                replacement = directory / "replacement.tmp"
                                replacement.write_bytes(original.read_bytes())
                                os.replace(replacement, original)
                            elif operation == "delete" and original is not None:
                                original.unlink()
                            elif operation == "content" and original is not None:
                                original.write_bytes(original.read_bytes() + b" ")
                            elif operation == "directory_swap":
                                opened = directory.with_name(f"{dirname}-opened")
                                directory.rename(opened)
                                directory.mkdir()
                        return value

                    with patch.object(
                        profile_snapshot,
                        "read_stable_json_at",
                        side_effect=mutate_after_first_read,
                    ) as reader:
                        with _binding(root) as binding:
                            self.assertIsNone(
                                _resolver()(binding, "pipeline review", locale="en")
                            )
                    self.assertTrue(mutated)
                    self.assertLessEqual(reader.call_count, 4)
