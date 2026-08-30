from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _local_package import load_local_package

load_local_package()
from omh.goal_ledger import (
    build_goal_completion_gate,
    complete_goal_ledger,
    create_goal_ledger,
    record_goal_checkpoint,
)
from omh.paths import resolve_paths
from omh.quality.completion_integrity import (
    COMPLETION_INTEGRITY_SCHEMA_VERSION,
    REFUSAL_CATEGORIES,
    classify_completion_integrity,
)


def _categories(verdict: dict[str, object]) -> list[str]:
    refusals = verdict["refusals"]
    assert isinstance(refusals, list)
    return [str(item["category"]) for item in refusals]


class CompletionIntegrityChangedContentTests(unittest.TestCase):
    def test_unlinked_placeholder_note_in_changed_code_is_refused(self) -> None:
        verdict = classify_completion_integrity(
            changed_files=[{"path": "src/coding/retry.py", "content": "def retry():\n    # TODO: wire the retry path\n    return 1\n"}],
        )

        self.assertTrue(verdict["refused"])
        self.assertEqual(_categories(verdict), ["unfinished_work_marker"])
        refusal = verdict["refusals"][0]
        self.assertEqual(refusal["path"], "src/coding/retry.py")
        self.assertIn("TODO", refusal["excerpt"])
        self.assertIn("tracked issue", refusal["remedy"])

    def test_every_unfinished_marker_word_is_refused(self) -> None:
        for word in ("TODO", "FIXME", "XXX", "HACK", "WIP", "TBD"):
            with self.subTest(word=word):
                verdict = classify_completion_integrity(
                    changed_files=[{"path": "src/a.py", "content": f"x = 1  # {word}: finish this\n"}],
                )
                self.assertEqual(_categories(verdict), ["unfinished_work_marker"])

    def test_suppressed_tests_without_a_linked_reason_are_refused(self) -> None:
        for line in (
            "    @unittest.skip(\"flaky\")",
            "    @pytest.mark.skip(reason=\"slow\")",
            "test.only('renders', () => {})",
            "describe.skip('parser', () => {})",
        ):
            with self.subTest(line=line):
                verdict = classify_completion_integrity(
                    changed_files=[{"path": "tests/test_thing.py", "content": line + "\n"}],
                )
                self.assertEqual(_categories(verdict), ["skipped_test_without_linked_reason"])

    def test_stub_bodies_and_no_op_stubs_are_refused(self) -> None:
        for content in (
            "def parse(text):\n    raise NotImplementedError\n",
            "def parse(text):\n    pass  # stub until the parser lands\n",
            "fn parse() { todo!() }\n",
            "def parse(text):\n    ...  # placeholder\n",
        ):
            with self.subTest(content=content):
                verdict = classify_completion_integrity(
                    changed_files=[{"path": "src/parser.py", "content": content}],
                )
                self.assertEqual(_categories(verdict), ["stub_implementation"])

    def test_a_stub_stays_refused_even_when_it_links_an_issue(self) -> None:
        # A tracked stub is still an unwritten body, so the linked-reason
        # escape that clears a skip or a placeholder note does not clear this.
        verdict = classify_completion_integrity(
            changed_files=[{"path": "src/parser.py", "content": "    raise NotImplementedError  # tracked in #652\n"}],
        )

        self.assertEqual(_categories(verdict), ["stub_implementation"])

    def test_one_line_reports_at_most_one_refusal(self) -> None:
        verdict = classify_completion_integrity(
            changed_files=[{"path": "src/parser.py", "content": "    raise NotImplementedError  # TODO finish\n"}],
        )

        self.assertEqual(_categories(verdict), ["stub_implementation"])


class CompletionIntegrityNegativeControlTests(unittest.TestCase):
    def test_a_todo_in_prose_is_not_unfinished_code(self) -> None:
        for path in ("docs/PLAN.md", "README.md", "notes/backlog.txt"):
            with self.subTest(path=path):
                verdict = classify_completion_integrity(
                    changed_files=[{"path": path, "content": "# TODO: write the migration guide\n"}],
                )
                self.assertFalse(verdict["refused"])

    def test_a_report_about_markers_does_not_refuse_itself(self) -> None:
        # The audit report names every marker word it found. Scanning prose
        # would make the report that documents unfinished work read as
        # unfinished work.
        report = (
            "# Marker audit\n"
            "We found TODO, FIXME, XXX, and HACK notes across the retry path.\n"
            "None of them are linked to an issue.\n"
        )

        verdict = classify_completion_integrity(
            summary="Audited every TODO and FIXME marker in the retry path.",
            changed_files=[{"path": "docs/marker-audit.md", "content": report}],
            evidence=["rg --line-number TODO src/"],
        )

        self.assertFalse(verdict["refused"])

    def test_a_code_sample_under_a_docs_tree_is_prose(self) -> None:
        verdict = classify_completion_integrity(
            changed_files=[{"path": "docs/examples/snippet.py", "content": "x = 1  # TODO: the reader fills this in\n"}],
        )

        self.assertFalse(verdict["refused"])

    def test_a_skip_with_a_linked_reason_passes(self) -> None:
        for line in (
            "    @unittest.skip(\"blocked on #652\")",
            "    @pytest.mark.skip(reason=\"https://github.com/rlaope/oh-my-hermes/issues/652\")",
        ):
            with self.subTest(line=line):
                verdict = classify_completion_integrity(
                    changed_files=[{"path": "tests/test_thing.py", "content": line + "\n"}],
                )
                self.assertFalse(verdict["refused"])

    def test_a_marker_note_linked_to_an_issue_passes(self) -> None:
        verdict = classify_completion_integrity(
            changed_files=[{"path": "src/a.py", "content": "x = 1  # TODO(#652): enable the broad-exception rule\n"}],
        )

        self.assertFalse(verdict["refused"])

    def test_an_abstract_method_raise_is_not_a_stub(self) -> None:
        content = (
            "class Store:\n"
            "    @abstractmethod\n"
            "    def read(self):\n"
            "        raise NotImplementedError\n"
        )

        verdict = classify_completion_integrity(changed_files=[{"path": "src/store.py", "content": content}])

        self.assertFalse(verdict["refused"])

    def test_a_marker_word_outside_a_comment_is_not_a_note(self) -> None:
        content = (
            "LABEL = \"todo list\"\n"
            "parser.add_argument(\"--todo-path\")\n"
            "def hack_around(value):\n"
            "    return value\n"
        )

        verdict = classify_completion_integrity(changed_files=[{"path": "src/a.py", "content": content}])

        self.assertFalse(verdict["refused"])

    def test_a_changed_path_with_no_content_contributes_nothing(self) -> None:
        verdict = classify_completion_integrity(changed_files=[{"path": "src/a.py"}])

        self.assertFalse(verdict["refused"])


class CompletionIntegrityEvidenceTests(unittest.TestCase):
    def test_placeholder_evidence_values_are_refused(self) -> None:
        for value in ("", "   ", "TBD", "todo", "N/A", "placeholder", "stub", "-"):
            with self.subTest(value=value):
                verdict = classify_completion_integrity(evidence=[value])
                self.assertEqual(_categories(verdict), ["empty_evidence"])

    def test_self_referential_evidence_is_refused(self) -> None:
        for value in ("works as expected", "should pass", "looks good to me", "manually verified"):
            with self.subTest(value=value):
                verdict = classify_completion_integrity(evidence=[value])
                self.assertEqual(_categories(verdict), ["self_referential_evidence"])

    def test_prepared_evidence_may_not_claim_execution(self) -> None:
        verdict = classify_completion_integrity(
            evidence=[{"reference": "the suite ran green", "status": "prepared_not_observed"}],
        )

        self.assertEqual(_categories(verdict), ["prepared_evidence_claimed_as_observed"])
        self.assertIn("prepared_not_observed", verdict["refusals"][0]["remedy"])

    def test_a_prepared_entry_that_claims_nothing_is_admissible(self) -> None:
        verdict = classify_completion_integrity(
            evidence=[{"reference": "planned unittest discovery", "status": "prepared_not_observed"}],
        )

        self.assertFalse(verdict["refused"])

    def test_a_tests_pass_claim_without_a_named_command_is_refused(self) -> None:
        verdict = classify_completion_integrity(evidence=["all tests pass on my machine"])

        self.assertEqual(_categories(verdict), ["unnamed_verification_command"])
        self.assertIn("unittest", verdict["refusals"][0]["remedy"])

    def test_a_tests_pass_claim_naming_a_command_is_admissible(self) -> None:
        verdict = classify_completion_integrity(
            evidence=["PYTHONPATH=tests uv run python -m unittest discover -s tests: all tests pass"],
        )

        self.assertFalse(verdict["refused"])


class CompletionIntegrityClaimBindingTests(unittest.TestCase):
    def test_a_proof_word_without_observed_command_evidence_is_refused(self) -> None:
        verdict = classify_completion_integrity(
            summary="Fixed the parser and verified the retry path.",
            evidence=["notes/handoff.md"],
        )

        self.assertEqual(_categories(verdict), ["unproven_claim_word"])
        self.assertEqual(verdict["refusals"][0]["path"], "summary")

    def test_a_proof_word_backed_by_a_named_command_passes(self) -> None:
        verdict = classify_completion_integrity(
            summary="Fixed the parser and verified the retry path.",
            evidence=["uv run python -m unittest tests/test_parser.py"],
        )

        self.assertFalse(verdict["refused"])

    def test_a_proof_word_backed_by_a_recorded_observation_passes(self) -> None:
        verdict = classify_completion_integrity(
            summary="Fixed the parser.",
            evidence=["observed:suite-green"],
        )

        self.assertFalse(verdict["refused"])

    def test_a_status_word_is_not_a_proof_word(self) -> None:
        # "done" and "complete" state a position in the workflow; only words
        # that assert the work was checked need a command behind them.
        verdict = classify_completion_integrity(summary="Done: the retry path is complete.", evidence=["unit"])

        self.assertFalse(verdict["refused"])

    def test_prepared_evidence_never_backs_a_proof_word(self) -> None:
        verdict = classify_completion_integrity(
            summary="Verified the retry path.",
            evidence=[{"reference": "uv run python -m unittest", "status": "prepared_not_observed"}],
        )

        self.assertEqual(_categories(verdict), ["unproven_claim_word"])


class CompletionIntegrityVerdictShapeTests(unittest.TestCase):
    def test_a_clean_claim_returns_a_bounded_passing_verdict(self) -> None:
        verdict = classify_completion_integrity(
            summary="Reworked the retry path and verified it.",
            changed_files=[{"path": "src/coding/retry.py", "content": "def retry():\n    return 1\n"}],
            evidence=["uv run python -m unittest tests/test_retry.py"],
        )

        self.assertEqual(verdict["schema_version"], COMPLETION_INTEGRITY_SCHEMA_VERSION)
        self.assertFalse(verdict["refused"])
        self.assertEqual(verdict["refusals"], [])
        self.assertEqual(verdict["categories"], [])
        self.assertIn("not observed execution evidence", verdict["claim_boundary"])

    def test_every_shipped_category_is_declared(self) -> None:
        verdict = classify_completion_integrity(
            summary="Verified everything.",
            changed_files=[
                {
                    "path": "src/a.py",
                    "content": (
                        "x = 1  # TODO: finish\n"
                        "@unittest.skip(\"flaky\")\n"
                        "def parse():\n"
                        "    raise NotImplementedError\n"
                    ),
                    "diff": "@@\n-    raise PermissionError(\"denied\")\n+    pass\n",
                }
            ],
            evidence=[
                "TBD",
                "works as expected",
                {"reference": "the suite ran green", "status": "prepared_not_observed"},
                "all tests pass",
            ],
        )

        self.assertEqual(sorted(REFUSAL_CATEGORIES), verdict["categories"])


class CompletionIntegrityGuardDeletionTests(unittest.TestCase):
    _GUARD_DIFF = (
        "@@\n"
        "-    if not is_allowed(user):\n"
        "-        raise PermissionError(\"denied\")\n"
        "+    pass\n"
    )

    def test_a_deleted_refusal_line_with_no_new_negative_case_is_refused(self) -> None:
        verdict = classify_completion_integrity(
            changed_files=[{"path": "src/security/gate.py", "diff": self._GUARD_DIFF}],
        )

        self.assertEqual(_categories(verdict), ["guard_deletion_without_adversarial_regression"])
        remedy = verdict["refusals"][0]["remedy"]
        self.assertIn("adversarial", remedy)

    def test_a_deleted_negative_test_with_no_replacement_is_refused(self) -> None:
        verdict = classify_completion_integrity(
            changed_files=[
                {
                    "path": "tests/test_gate.py",
                    "diff": (
                        "@@\n"
                        "-def test_gate_refuses_unauthorized_user():\n"
                        "-    assert not gate.allow(bad_user)\n"
                        "+\n"
                    ),
                }
            ],
        )

        self.assertEqual(_categories(verdict), ["guard_deletion_without_adversarial_regression"])

    def test_a_widened_allowlist_with_no_new_negative_case_is_refused(self) -> None:
        verdict = classify_completion_integrity(
            changed_files=[
                {
                    "path": "src/security/allowlist.py",
                    "diff": (
                        "@@\n"
                        '-ALLOWLIST = ("a.example.com", "b.example.com")\n'
                        '+ALLOWLIST = ("a.example.com", "b.example.com", "evil.example.com")\n'
                    ),
                }
            ],
        )

        self.assertEqual(_categories(verdict), ["guard_deletion_without_adversarial_regression"])

    def test_a_guard_moved_not_deleted_is_not_refused(self) -> None:
        # The identical removed line reappears added elsewhere in the same
        # change: the guard moved, it was not dropped.
        verdict = classify_completion_integrity(
            changed_files=[
                {"path": "src/security/gate.py", "diff": "@@\n-        raise PermissionError(\"denied\")\n"},
                {"path": "src/security/gate_helpers.py", "diff": "@@\n+        raise PermissionError(\"denied\")\n"},
            ],
        )

        self.assertFalse(verdict["refused"])

    def test_a_renamed_test_that_keeps_the_negative_case_is_not_refused(self) -> None:
        # A refactor renames the test function; the negative-case vocabulary
        # in the new name still proves the boundary is exercised.
        verdict = classify_completion_integrity(
            changed_files=[
                {
                    "path": "tests/test_gate.py",
                    "diff": (
                        "@@\n"
                        "-def test_gate_refuses_unauthorized_user():\n"
                        "-    assert not gate.allow(bad_user)\n"
                        "+def test_gate_rejects_bad_credentials():\n"
                        "+    assert not gate.allow(bad_user)\n"
                    ),
                }
            ],
        )

        self.assertFalse(verdict["refused"])

    def test_a_deletion_with_a_new_adversarial_test_in_the_diff_is_not_refused(self) -> None:
        verdict = classify_completion_integrity(
            changed_files=[
                {"path": "src/security/gate.py", "diff": self._GUARD_DIFF},
                {
                    "path": "tests/test_gate.py",
                    "diff": (
                        "@@\n"
                        "+def test_gate_still_refuses_unauthorized_user_without_the_inline_check():\n"
                        "+    assert not gate.allow(bad_user)\n"
                    ),
                },
            ],
        )

        self.assertFalse(verdict["refused"])

    def test_a_deletion_with_evidence_naming_an_adversarial_regression_is_not_refused(self) -> None:
        verdict = classify_completion_integrity(
            changed_files=[{"path": "src/security/gate.py", "diff": self._GUARD_DIFF}],
            evidence=["uv run python -m unittest tests/test_gate_adversarial_regression.py"],
        )

        self.assertFalse(verdict["refused"])

    def test_a_changed_file_with_no_diff_contributes_nothing(self) -> None:
        verdict = classify_completion_integrity(
            changed_files=[{"path": "src/security/gate.py", "content": "def allow(user):\n    return True\n"}],
        )

        self.assertFalse(verdict["refused"])


class CompletionIntegrityGuardDeletionGateIntegrationTests(unittest.TestCase):
    _GUARD_DIFF = (
        "@@\n"
        "-    if not is_allowed(user):\n"
        "-        raise PermissionError(\"denied\")\n"
        "+    pass\n"
    )

    def test_a_caller_holding_a_diff_blocks_what_the_ledger_alone_misses(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            create_goal_ledger(paths, "Simplify the gate", ["Criterion one"], goal_id="goal-guard-deletion")
            record_goal_checkpoint(
                paths,
                "goal-guard-deletion",
                "Simplified the permission gate",
                criteria_refs=["AC001"],
                evidence_refs=["uv run python -m unittest tests/test_gate.py"],
            )

            gate = build_goal_completion_gate(paths, "goal-guard-deletion")
            # The ledger stores no diff (see `_completion_integrity_refusals`),
            # so the gate alone is blind to the deleted guard.
            self.assertTrue(gate["ready"])

            # A caller holding the diff -- the seam the goal-ledger docstring
            # names -- classifies it directly and must refuse before this
            # completion claim stands, exactly as content scanning already
            # does for a caller holding changed-file content.
            diff_verdict = classify_completion_integrity(
                changed_files=[{"path": "src/security/gate.py", "diff": self._GUARD_DIFF}],
                evidence=["uv run python -m unittest tests/test_gate.py"],
            )

            self.assertTrue(diff_verdict["refused"])
            self.assertEqual(diff_verdict["categories"], ["guard_deletion_without_adversarial_regression"])

    def test_evidence_naming_a_regression_criterion_clears_the_same_diff(self) -> None:
        diff_verdict = classify_completion_integrity(
            changed_files=[{"path": "src/security/gate.py", "diff": self._GUARD_DIFF}],
            evidence=["uv run python -m unittest tests/test_gate_regression.py"],
        )

        self.assertFalse(diff_verdict["refused"])


class CompletionGateIntegrityIntegrationTests(unittest.TestCase):
    def test_the_completion_gate_refuses_placeholder_evidence(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            create_goal_ledger(paths, "Ship the retry path", ["Criterion one"], goal_id="goal-placeholder")
            record_goal_checkpoint(
                paths, "goal-placeholder", "Wired the retry path", criteria_refs=["AC001"], evidence_refs=["TBD"]
            )

            gate = build_goal_completion_gate(paths, "goal-placeholder")

            self.assertFalse(gate["ready"])
            self.assertEqual([item["category"] for item in gate["integrity_refusals"]], ["empty_evidence"])
            self.assertIn("completion-integrity refusals", gate["summary"])
            self.assertEqual(gate["next_action"], "record_checkpoint")
            self.assertFalse(complete_goal_ledger(paths, "goal-placeholder", evidence_refs=["TBD"])["completed"])

    def test_the_completion_gate_refuses_an_unproven_proof_word(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            create_goal_ledger(paths, "Ship the retry path", ["Criterion one"], goal_id="goal-unproven")
            record_goal_checkpoint(
                paths,
                "goal-unproven",
                "Fixed and verified the retry path",
                criteria_refs=["AC001"],
                evidence_refs=["notes/handoff.md"],
            )

            gate = build_goal_completion_gate(paths, "goal-unproven")

            self.assertFalse(gate["ready"])
            self.assertEqual([item["category"] for item in gate["integrity_refusals"]], ["unproven_claim_word"])

    def test_a_goal_with_command_evidence_still_completes(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")
            create_goal_ledger(paths, "Ship the retry path", ["Criterion one"], goal_id="goal-clean")
            record_goal_checkpoint(
                paths,
                "goal-clean",
                "Fixed and verified the retry path",
                criteria_refs=["AC001"],
                evidence_refs=["uv run python -m unittest tests/test_retry.py"],
            )

            gate = build_goal_completion_gate(paths, "goal-clean")

            self.assertTrue(gate["ready"])
            self.assertEqual(gate["integrity_refusals"], [])
            completed = complete_goal_ledger(
                paths, "goal-clean", evidence_refs=["uv run python -m unittest tests/test_retry.py"]
            )
            self.assertTrue(completed["completed"])


if __name__ == "__main__":
    unittest.main()
