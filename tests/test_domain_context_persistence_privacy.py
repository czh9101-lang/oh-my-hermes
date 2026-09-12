from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

from _local_package import load_local_package

load_local_package()

from omh.coding_lifecycle import start_codex_delegation_lifecycle
from project_identity_fixture import project_identity
from omh.runtime_records import (
    WRAPPER_SESSION_RECORD_KEYS,
    validate_wrapper_session_record,
)
from omh.workflows.domain_intelligence import retire_domain_profile
from omh.wrapper_sessions import create_or_resume_wrapper_session


class DomainContextPersistencePrivacyMixin:
    def test_applied_interaction_leaves_authoritative_sources_unchanged(self) -> None:
        from test_domain_context_privacy import (
            AUTHORITATIVE_DIRECTORIES,
            _assert_no_private_material,
            _authoritative_snapshot,
        )

        before = _authoritative_snapshot(self.store)
        stdout = StringIO()
        stderr = StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            interaction = self._applied_interaction()

        self.assertIn("domain_routing_context", interaction)
        self.assertEqual(_authoritative_snapshot(self.store), before)
        self.assertEqual(
            {
                dirname: len(list((self.store / dirname).glob("*.json")))
                for dirname in AUTHORITATIVE_DIRECTORIES
            },
            {"candidates": 1, "reviews": 1, "profiles": 1, "history": 0},
        )
        _assert_no_private_material(
            self,
            {"stdout": stdout.getvalue(), "stderr": stderr.getvalue()},
            self.private_values,
            label="interaction output streams",
        )

    def test_session_second_turn_rederives_state_without_retaining_context(self) -> None:
        from test_domain_context_privacy import _assert_no_private_material

        calls = 0

        def binding_factory():
            nonlocal calls
            calls += 1
            return self._binding_factory()

        metadata = {"source_event_id": "event-002", "channel_ref": "channel-002"}
        first = create_or_resume_wrapper_session(
            self.paths,
            self.message,
            source="discord",
            source_metadata=metadata,
            _host_project_binding_factory=binding_factory,
        )
        self.assertIn("domain_routing_context", first["interaction"])
        self.assertNotIn("domain_routing_context", first["session"])

        retire_domain_profile(
            self.paths,
            scope_kind="project",
            scope_ref=project_identity(self.root),
            domain_id=str(self.profile["domain_id"]),
            reason="superseded",
        )
        second = create_or_resume_wrapper_session(
            self.paths,
            self.message,
            source="discord",
            source_metadata=metadata,
            _host_project_binding_factory=binding_factory,
        )

        self.assertTrue(second["resumed"])
        self.assertNotIn("domain_routing_context", second["interaction"])
        self.assertEqual(calls, 2)
        # The privacy property is that the record carries no key outside the
        # canonical set. Two canonical keys are optional on a fresh legacy
        # session: `applied_mutations_floor_revision` is written only once
        # mutation history has actually been evicted, and `platform` is
        # written only when the session was opened with a platform envelope.
        self.assertLessEqual(set(second["session"]), set(WRAPPER_SESSION_RECORD_KEYS))
        self.assertEqual(
            set(WRAPPER_SESSION_RECORD_KEYS) - set(second["session"]),
            {"applied_mutations_floor_revision", "platform"},
        )
        self.assertEqual(validate_wrapper_session_record(second["session"]), [])
        _assert_no_private_material(
            self,
            {
                "source_metadata": second["session"]["source_metadata"],
                "thread_key": second["session"]["thread_key"],
                "session": second["session"],
                "status": second["status"],
            },
            self.private_values,
            label="session continuity",
        )

    def test_non_authoritative_files_and_logs_contain_no_private_material(self) -> None:
        from test_domain_context_privacy import _assert_no_private_material

        self._applied_interaction()
        create_or_resume_wrapper_session(
            self.paths,
            self.message,
            source="discord",
            source_metadata={"source_event_id": "event-004", "channel_ref": "channel-004"},
            _host_project_binding_factory=self._binding_factory,
        )
        start_codex_delegation_lifecycle(
            self.paths,
            "Implement the bounded validation and add focused tests",
            source="discord",
            source_metadata={"source_event_id": "event-005", "channel_ref": "channel-005"},
        )

        scanned: dict[str, str] = {}
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or self.store in path.parents:
                continue
            scanned[str(path.relative_to(self.root))] = path.read_text(
                encoding="utf-8",
                errors="replace",
            )

        self.assertTrue(scanned)
        _assert_no_private_material(
            self,
            scanned,
            self.private_values,
            label="non-authoritative recursive file and log scan",
        )
