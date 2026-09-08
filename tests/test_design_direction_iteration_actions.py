from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _local_package import load_local_package

load_local_package()

from omh.paths import OmhPaths
from omh.workflows.design_direction_iterations import (
    build_design_direction_iteration,
    revise_design_direction_iteration,
    write_design_direction_iteration,
)
from omh.workflows.design_directions import build_design_direction_set
from omh.workflows.memory import build_project_memory_status
from omh.wrapper.sessions import create_or_resume_wrapper_session


_OPTION_A = (
    "a",
    "task_first",
    "restrained_neutral",
    "system_sans",
    "single_column",
    "progress_trace",
    ("placeholder_copy",),
)
_OPTION_B = (
    "b",
    "evidence_first",
    "contextual_accent",
    "editorial_serif",
    "split_panel",
    "evidence_rail",
    ("generic_glass",),
)
_OPTION_B_REVISED = (
    "b",
    "evidence_first",
    "restrained_neutral",
    "editorial_serif",
    "split_panel",
    "evidence_rail",
    ("generic_glass",),
)


def _paths(root: Path) -> OmhPaths:
    return OmhPaths(omh_home=root / "omh", hermes_home=root / "hermes")


def _direction_set(*options: tuple[object, ...]) -> dict[str, object]:
    return build_design_direction_set(
        surface="workflow_screen",
        audience="operator",
        primary_task="decide",
        platform="web",
        mode="new",
        context_references=(
            ("design_system", "design_ref_a1b2c3d4e5f60718", "project_local"),
        ),
        options=options or (_OPTION_A, _OPTION_B),
    )


def _prepared(paths: OmhPaths) -> dict[str, object]:
    return write_design_direction_iteration(
        paths,
        build_design_direction_iteration(
            _direction_set(),
            source_revision_digest="a" * 64,
            criteria_revision="direction-fit-v1",
            criteria_dimensions=("clarity",),
            score_threshold=90,
        ),
    )


class DesignDirectionIterationActionTests(unittest.TestCase):
    def test_disabled_chat_path_does_not_load_iteration_adapter_or_touch_iteration_store(self) -> None:
        script = """
import json
import sys
from pathlib import Path
from _local_package import load_local_package
load_local_package()
from omh.paths import OmhPaths
from omh.wrapper.contract import build_chat_interaction_payload
root = Path(sys.argv[1])
paths = OmhPaths(omh_home=root / 'omh', hermes_home=root / 'hermes')
payload = build_chat_interaction_payload('what is a design system?', source='discord', paths=paths)
print(json.dumps({
    'action': payload['next_action'],
    'adapter_loaded': 'omh.wrapper.design_direction_iteration_actions' in sys.modules,
    'core_loaded': 'omh.workflows.design_direction_iterations' in sys.modules,
    'store_exists': (paths.omh_home / 'design-direction-iterations').exists(),
}))
"""
        with TemporaryDirectory() as temporary:
            result = subprocess.run(
                [sys.executable, '-c', script, temporary],
                env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parent)},
                capture_output=True, text=True, check=True, timeout=30,
            )
        result = json.loads(result.stdout)
        self.assertEqual(result['action'], 'answer_directly')
        self.assertFalse(result['adapter_loaded'])
        self.assertFalse(result['core_loaded'])
        self.assertFalse(result['store_exists'])

    def test_wrapper_session_routes_trusted_feedback_to_returned_revision_action(self) -> None:
        with TemporaryDirectory() as temporary:
            paths = _paths(Path(temporary))
            iteration = _prepared(paths)
            current = iteration["snapshots"][-1]

            session = create_or_resume_wrapper_session(
                paths,
                "Could we revise these directions around the feedback I just gave?",
                source="discord",
                source_metadata={"channel_ref": "design", "thread_ref": "round-1"},
                design_direction_iteration_context={
                    "iteration_id": iteration["iteration_id"],
                    "revision_digest": current["revision_digest"],
                },
            )

            response = session["interaction"]["chat_response"]
            actions = {action["id"]: action for action in response["actions"]}
            self.assertEqual(session["interaction"]["next_action"], "revise_design_direction_iteration")
            self.assertEqual(actions["revise_design_direction_iteration"]["payload"]["iteration_id"], iteration["iteration_id"])
            self.assertEqual(actions["revise_design_direction_iteration"]["payload"]["revision_digest"], current["revision_digest"])
            self.assertEqual(
                session["session"]["route"]["design_direction_iteration"],
                {
                    "iteration_id": iteration["iteration_id"],
                    "revision_digest": current["revision_digest"],
                },
            )

    def test_session_selection_uses_the_revision_returned_by_its_prior_action(self) -> None:
        from omh.wrapper import sessions

        execute = getattr(sessions, "execute_design_direction_iteration_session_action", None)
        self.assertTrue(callable(execute))
        with TemporaryDirectory() as temporary:
            paths = _paths(Path(temporary))
            iteration = _prepared(paths)
            current = iteration["snapshots"][-1]
            session = create_or_resume_wrapper_session(
                paths,
                "Revise the directions using this feedback.",
                source="discord",
                source_metadata={"channel_ref": "design", "thread_ref": "round-1"},
                design_direction_iteration_context={
                    "iteration_id": iteration["iteration_id"],
                    "revision_digest": current["revision_digest"],
                },
            )["session"]
            revised = execute(
                paths,
                session["session_id"],
                "revise_design_direction_iteration",
                {
                    "iteration_id": iteration["iteration_id"],
                    "revision_digest": current["revision_digest"],
                    "feedback_reference": "feedback-001",
                    "feedback_delta": ["palette"],
                    "direction_set": _direction_set(_OPTION_A, _OPTION_B_REVISED),
                    "successors": [("preserved", ("a",), "a"), ("revised", ("b",), "b")],
                    "criteria_revision": "direction-fit-v1",
                    "criteria_dimensions": ["clarity"],
                },
            )
            latest = revised["iteration"]["snapshots"][-1]

            selected = execute(
                paths,
                session["session_id"],
                "select_design_direction_option",
                {
                    "iteration_id": iteration["iteration_id"],
                    "revision_digest": latest["revision_digest"],
                    "option_ref": latest["option_refs"][0],
                },
            )

            self.assertEqual(
                selected["iteration"]["terminal"]["accepted_revision_digest"],
                latest["revision_digest"],
            )
            persisted = sessions.read_wrapper_session(paths, session["session_id"])
            self.assertEqual(
                persisted["route"]["design_direction_iteration"]["revision_digest"],
                latest["revision_digest"],
            )

    def test_select_refuses_a_stale_digest_even_when_the_option_reference_is_current(self) -> None:
        from omh.wrapper.design_direction_iteration_actions import execute_design_direction_iteration_action

        with TemporaryDirectory() as temporary:
            paths = _paths(Path(temporary))
            root = _prepared(paths)
            root_snapshot = root["snapshots"][-1]
            revised = revise_design_direction_iteration(
                root,
                parent_revision_digest=root_snapshot["revision_digest"],
                feedback_reference="feedback-001",
                feedback_delta=("palette",),
                direction_set=_direction_set(_OPTION_A, _OPTION_B_REVISED),
                successors=(
                    ("preserved", ("a",), "a"),
                    ("revised", ("b",), "b"),
                ),
                criteria_revision="direction-fit-v1",
                criteria_dimensions=("clarity",),
            )
            execute_design_direction_iteration_action(
                paths,
                "revise_design_direction_iteration",
                {
                    "iteration_id": root["iteration_id"],
                    "revision_digest": root_snapshot["revision_digest"],
                    "feedback_reference": "feedback-001",
                    "feedback_delta": ["palette"],
                    "direction_set": _direction_set(_OPTION_A, _OPTION_B_REVISED),
                    "successors": [
                        ("preserved", ("a",), "a"),
                        ("revised", ("b",), "b"),
                    ],
                    "criteria_revision": "direction-fit-v1",
                    "criteria_dimensions": ["clarity"],
                },
            )
            current = revised["snapshots"][-1]

            with self.assertRaisesRegex(ValueError, "stale revision"):
                execute_design_direction_iteration_action(
                    paths,
                    "select_design_direction_option",
                    {
                        "iteration_id": root["iteration_id"],
                        "revision_digest": root_snapshot["revision_digest"],
                        "option_ref": current["option_refs"][0],
                    },
                )

    def test_memory_review_requires_persisted_explicit_request_and_is_idempotent(self) -> None:
        from omh.wrapper.design_direction_iteration_actions import execute_design_direction_iteration_action

        with TemporaryDirectory() as temporary:
            paths = _paths(Path(temporary))
            iteration = _prepared(paths)
            current = iteration["snapshots"][-1]
            forged_request = {
                "action": "memory-new",
                "review_required": True,
                "accepted_revision_digest": current["revision_digest"],
                "automatic_write": False,
                "global_promotion": False,
            }
            request_payload = {
                "iteration_id": iteration["iteration_id"],
                "revision_digest": current["revision_digest"],
                "thread_key": "discord:design:round-1",
                "memory_promotion_request": forged_request,
            }

            with self.assertRaisesRegex(ValueError, "persisted accepted memory-promotion request"):
                execute_design_direction_iteration_action(
                    paths,
                    "request_design_direction_memory_review",
                    request_payload,
                )

            selected = execute_design_direction_iteration_action(
                paths,
                "select_design_direction_option",
                {
                    "iteration_id": iteration["iteration_id"],
                    "revision_digest": current["revision_digest"],
                    "option_ref": current["option_refs"][0],
                    "remember_this": True,
                },
            )
            request_payload["memory_promotion_request"] = selected["iteration"]["memory_promotion"]["request"]
            remember_actions = [
                action for action in selected["machine_actions"]
                if action["action"] == "request_design_direction_memory_review"
            ]
            self.assertEqual(len(remember_actions), 1)
            self.assertEqual(
                remember_actions[0]["memory_promotion_request"],
                request_payload["memory_promotion_request"],
            )
            first = execute_design_direction_iteration_action(
                paths,
                "request_design_direction_memory_review",
                request_payload,
            )
            second = execute_design_direction_iteration_action(
                paths,
                "request_design_direction_memory_review",
                request_payload,
            )

            self.assertEqual(first, second)
            self.assertEqual(build_project_memory_status(paths)["counts"]["candidates"], 1)
            self.assertFalse(first["memory_capture"]["auto_approved"])


    def test_session_memory_retry_reuses_the_original_review_result(self) -> None:
        from omh.wrapper.sessions import execute_design_direction_iteration_session_action

        with TemporaryDirectory() as temporary:
            paths = _paths(Path(temporary))
            iteration = _prepared(paths)
            current = iteration["snapshots"][-1]
            session = create_or_resume_wrapper_session(
                paths,
                "Revise the directions using this feedback.",
                source="discord",
                source_metadata={"channel_ref": "design", "thread_ref": "round-1"},
                design_direction_iteration_context={
                    "iteration_id": iteration["iteration_id"],
                    "revision_digest": current["revision_digest"],
                },
            )["session"]
            selected = execute_design_direction_iteration_session_action(
                paths, session["session_id"], "select_design_direction_option",
                {
                    "iteration_id": iteration["iteration_id"],
                    "revision_digest": current["revision_digest"],
                    "option_ref": current["option_refs"][0],
                    "remember_this": True,
                },
            )
            payload = {
                "iteration_id": iteration["iteration_id"],
                "revision_digest": current["revision_digest"],
                "memory_promotion_request": selected["iteration"]["memory_promotion"]["request"],
            }
            first = execute_design_direction_iteration_session_action(
                paths, session["session_id"], "request_design_direction_memory_review", payload
            )
            before = {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in paths.memory_dir.rglob("*") if path.is_file()
            }

            replay = execute_design_direction_iteration_session_action(
                paths, session["session_id"], "request_design_direction_memory_review", payload
            )

            self.assertEqual(first, replay)
            self.assertEqual(
                before,
                {
                    path: (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in paths.memory_dir.rglob("*") if path.is_file()
                },
            )


if __name__ == "__main__":
    unittest.main()
