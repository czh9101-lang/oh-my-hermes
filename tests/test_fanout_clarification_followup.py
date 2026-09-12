from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()

from omh.paths import OmhPaths
from omh.wrapper.executor_sessions import build_fanout_session_followup


class ParentFanoutFollowupTests(unittest.TestCase):
    def test_selected_clarification_survives_parent_projection(self) -> None:
        # Given: the authoritative roster has a pending selected-unit decision.
        with TemporaryDirectory() as directory:
            root = Path(directory)
            paths = OmhPaths(root / "omh", root / "hermes")
            clarification = {
                "request": "prepared",
                "answer": "none",
                "redispatch": "none",
            }
            unit = {
                "unit_id": "unit-b",
                "executor_session": None,
                "resume": {"available": False},
                "clarification": clarification,
                "clarification_state": "prepared_not_answered",
            }
            with patch(
                "omh.coding.fanout_status.project_fanout_status",
                return_value={"units": [unit]},
            ) as project_status:
                # When: the parent requests that unit's continuation projection.
                followup = build_fanout_session_followup(
                    paths, fanout_id="fo-parent", unit_id="unit-b"
                )

            # Then: scoped decision evidence reaches the parent without execution.
            project_status.assert_called_once_with(paths, "fo-parent", unit_id="unit-b")
            self.assertEqual(followup.get("clarification"), clarification)
            self.assertEqual(followup.get("clarification_state"), "prepared_not_answered")
            self.assertEqual(followup["unit_id"], "unit-b")
            self.assertEqual(followup["execution_policy"], "copy_only")

    def test_legacy_followup_keeps_its_existing_shape(self) -> None:
        # Given: an existing selected-unit roster has no clarification metadata.
        with TemporaryDirectory() as directory:
            root = Path(directory)
            paths = OmhPaths(root / "omh", root / "hermes")
            resume = {"available": False}
            unit = {"unit_id": "unit-a", "executor_session": None, "resume": resume}
            with patch(
                "omh.coding.fanout_status.project_fanout_status",
                return_value={"units": [unit]},
            ):
                # When: the parent requests the unchanged continuation surface.
                followup = build_fanout_session_followup(
                    paths, fanout_id="fo-parent", unit_id="unit-a"
                )

            # Then: no clarification or execution authority is invented.
            self.assertEqual(
                followup,
                {
                    "fanout_id": "fo-parent",
                    "unit_id": "unit-a",
                    "executor_session": None,
                    "resume": resume,
                    "execution_policy": "copy_only",
                },
            )
