from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from _local_package import load_local_package

load_local_package()
from omh.skills.catalog import builtin_definitions

from test_plugin_distribution import FakeHermesContext, load_installed_plugin


_DEFINITIONS = {definition.name: definition for definition in builtin_definitions()}
KO_SALES_QUESTION = _DEFINITIONS["sales-development"].expert_questions[0].ko
EN_FINANCE_QUESTION = _DEFINITIONS["finance-analysis"].expert_questions[0].en


def _session_turn(
    root: Path,
    message: str,
    *,
    event_id: str = "event-lifecycle",
    channel_ref: str = "channel-lifecycle",
) -> dict[str, object]:
    from test_domain_context_lifecycle import _run_omh

    return _run_omh(
        root,
        "--scope",
        "project",
        "chat",
        "session",
        "start",
        "--source",
        "discord",
        "--source-event-id",
        event_id,
        "--channel-ref",
        channel_ref,
        message,
    )


class DomainContextLifecycleJourneyMixin:
    def test_public_capture_approve_replace_retire_journey(self) -> None:
        from test_domain_context_lifecycle import (
            EN_SALES_QUESTION,
            _approve_candidate,
            _assert_applied_context,
            _block_external_connections,
            _canonical,
            _capture_candidate,
            _chat,
            _repository,
            _run_omh,
            _unresolved_message,
        )

        with TemporaryDirectory() as temporary:
            root = _repository(Path(temporary) / "redwood-project")
            phrase = "expert-marker-100-en"
            message = _unresolved_message(phrase)

            before_approval = _chat(root, message)
            self.assertNotIn("domain_routing_context", before_approval)
            generic_body = before_approval["chat_response"]["body"]
            baseline_route = _canonical(before_approval["route"])
            baseline_candidate = _canonical(
                before_approval["route"].get("candidate_handoff")
            )

            first_candidate_id = _capture_candidate(
                root,
                scope_kind="project",
                scope_ref="redwood-project",
                domain="redwood-operations",
                phrase=phrase,
                canonical="redwood_review_marker",
                workflow_hints=("sales-development",),
            )
            first_approval = _approve_candidate(root, first_candidate_id)
            self.assertEqual(first_approval["decision"], "approved")

            english = _chat(root, message)
            korean = _chat(root, f"뭔가 {phrase} 관련해서 애매해요")
            _assert_applied_context(
                self,
                english,
                workflow="sales-development",
                locale="en",
                question=EN_SALES_QUESTION,
            )
            _assert_applied_context(
                self,
                korean,
                workflow="sales-development",
                locale="ko",
                question=KO_SALES_QUESTION,
            )
            self.assertEqual(_canonical(english["route"]), baseline_route)
            self.assertEqual(
                _canonical(english["route"].get("candidate_handoff")),
                baseline_candidate,
            )

            setup = _run_omh(
                root, "--scope", "project", "setup", "--with-plugin", "--json"
            )
            self.assertIn(setup.get("status"), {"installed", "updated", "ok", None})
            plugin = load_installed_plugin(root / ".hermes" / "plugins" / "omh")
            context = FakeHermesContext()
            plugin.register(context)
            plugin_handler = context.tools["omh_interact"]["args"][2]
            with _block_external_connections():
                plugin_interaction = json.loads(
                    plugin_handler(
                        {
                            "message": message,
                            "source": "discord",
                            "record_session": False,
                        },
                        project_root=str(root),
                    )
                )
            _assert_applied_context(
                self,
                plugin_interaction,
                workflow="sales-development",
                locale="en",
                question=EN_SALES_QUESTION,
            )

            first_turn = _session_turn(root, message)
            self.assertFalse(first_turn["resumed"])
            _assert_applied_context(
                self,
                first_turn["interaction"],
                workflow="sales-development",
                locale="en",
                question=EN_SALES_QUESTION,
            )

            replacement_id = _capture_candidate(
                root,
                scope_kind="project",
                scope_ref="redwood-project",
                domain="redwood-operations",
                phrase=phrase,
                canonical="redwood_review_marker",
                workflow_hints=("finance-analysis",),
            )
            replacement = _approve_candidate(root, replacement_id)
            self.assertEqual(replacement["profile"]["revision"], 2)
            next_turn = _session_turn(root, message)
            self.assertTrue(next_turn["resumed"])
            self.assertEqual(
                next_turn["session"]["session_id"], first_turn["session"]["session_id"]
            )
            _assert_applied_context(
                self,
                next_turn["interaction"],
                workflow="finance-analysis",
                locale="en",
                question=EN_FINANCE_QUESTION,
            )

            retired = _run_omh(
                root,
                "--scope",
                "project",
                "memory",
                "domain-retire",
                "--scope-kind",
                "project",
                "--scope-ref",
                "redwood-project",
                "--domain",
                "redwood-operations",
                "--retired-by",
                "lifecycle-test",
                "--reason",
                "superseded",
            )
            self.assertEqual(retired["decision"], "retired")
            after_retirement = _chat(root, message)
            self.assertNotIn("domain_routing_context", after_retirement)
            self.assertEqual(after_retirement["chat_response"]["body"], generic_body)
            self.assertEqual(_canonical(after_retirement["route"]), baseline_route)
