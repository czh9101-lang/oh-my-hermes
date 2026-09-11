"""Contracts for the unacknowledged-dispatch-outcome reader.

A dispatched unit ending and nobody writing it down is the exact shape of the
2026-09-11 incident. These tests pin what "nobody wrote it down" means: it
finished after the plan was last touched, and no plan item names it.

The unit states themselves are never restated here or in the reader; the
parity class at the bottom is what keeps that true in both directions -- the
module must bind the shared constants, and its standalone fallback must not
smuggle a copy of them back in.
"""

import ast
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from omh.plugin_bundle.omh.dispatch_outcomes import (
    NEXT_ADVANCE_NEXT_ITEM,
    NEXT_RECORD_BLOCKED,
    NEXT_RUN_RECOVERY,
    NEXT_VERIFY_RESULT,
    OUTCOME_MAX_AGE_SECONDS,
    unacknowledged_outcomes,
)
from omh.plugin_bundle.omh.todo_store import build_todo_record, write_todo

FANOUT_A = "fanout-0123456789ab"
FANOUT_B = "fanout-ba9876543210"


def _stamp(moment):
    return moment.isoformat().replace("+00:00", "Z")


class DispatchOutcomesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "omh"
        # Every timestamp is derived from the plan's OWN `updated_at`, which
        # `build_todo_record` stamps with the wall clock. A fixed literal here
        # would make "finished after the plan" true only on the day it was
        # written -- the freshness fixtures that went red after 24h.
        self.now = datetime.now(timezone.utc)

    def _write_plan(self, item_texts, session_ref=""):
        items = [{"text": text, "state": "pending"} for text in item_texts]
        items[0]["state"] = "active"
        record = build_todo_record("plan", items, source="test", session_ref=session_ref)
        write_todo(self.home, record)
        self.now = datetime.fromisoformat(record["updated_at"].replace("Z", "+00:00"))

    def _write_summary(self, fanout_id, units):
        directory = self.home / "coding" / "fanout" / fanout_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "dispatch_summary.json").write_text(
            json.dumps(
                {
                    "schema_version": "fanout_dispatch_summary/v1",
                    "fanout_id": fanout_id,
                    "units": units,
                }
            ),
            encoding="utf-8",
        )

    def _outcomes(self, session_ref=""):
        return unacknowledged_outcomes(
            str(self.home), "", session_ref, now=self.now + timedelta(minutes=1)
        )

    def test_only_the_unreferenced_run_finished_after_the_plan_is_reported(self):
        # Three finished units, one reason each to stay silent: one is named
        # on the plan, one finished before the plan was last touched, and only
        # the third is an event nobody has answered.
        before_plan = _stamp(self.now - timedelta(minutes=30))
        self._write_plan(
            [
                f"verify {FANOUT_A}-referenced",
                "open the PR",
            ]
        )
        after_plan = _stamp(self.now + timedelta(seconds=30))
        self._write_summary(
            FANOUT_A,
            [
                {
                    "unit_id": "referenced",
                    "run_ref": f"{FANOUT_A}-referenced",
                    "status": "completed",
                    "process_succeeded": True,
                    "finished_at": after_plan,
                },
                {
                    "unit_id": "early",
                    "run_ref": f"{FANOUT_A}-early",
                    "status": "completed",
                    "process_succeeded": True,
                    "finished_at": before_plan,
                },
                {
                    "unit_id": "silent",
                    "run_ref": f"{FANOUT_A}-silent",
                    "status": "completed",
                    "process_succeeded": True,
                    "finished_at": after_plan,
                },
            ],
        )

        outcomes = self._outcomes()

        self.assertEqual([outcome["unit_id"] for outcome in outcomes], ["silent"])
        self.assertEqual(outcomes[0]["run_ref"], f"{FANOUT_A}-silent")
        self.assertEqual(outcomes[0]["next"], NEXT_VERIFY_RESULT)
        self.assertEqual(outcomes[0]["finished_at"], after_plan)

    def test_the_verb_follows_what_the_summary_observed(self):
        self._write_plan(["ship it"])
        finished = _stamp(self.now + timedelta(seconds=30))
        self._write_summary(
            FANOUT_A,
            [
                {
                    "unit_id": "verified",
                    "status": "completed",
                    "process_succeeded": True,
                    "unit_verification_observed": True,
                    "finished_at": finished,
                },
                {
                    "unit_id": "limited",
                    "status": "failed",
                    "failure_kind": "limit_shaped",
                    "finished_at": finished,
                },
                {
                    "unit_id": "crashed",
                    "status": "failed",
                    "failure_kind": "crash",
                    "finished_at": finished,
                },
                {
                    "unit_id": "stalled",
                    "status": "failed",
                    "unit_state": "progress_stalled",
                    "finished_at": finished,
                },
            ],
        )

        verbs = {outcome["unit_id"]: outcome["next"] for outcome in self._outcomes()}

        self.assertEqual(verbs["verified"], NEXT_ADVANCE_NEXT_ITEM)
        self.assertEqual(verbs["limited"], NEXT_RECORD_BLOCKED)
        self.assertEqual(verbs["crashed"], NEXT_RUN_RECOVERY)
        self.assertEqual(verbs["stalled"], NEXT_RECORD_BLOCKED)

    def test_every_shared_stuck_state_routes_to_record_blocked(self):
        # Derived from the shared vocabulary rather than a copy of it: a stuck
        # state added to `unit_execution_state` has to reach this routing.
        from omh.coding.unit_execution_state import UNIT_STUCK_STATES

        self._write_plan(["ship it"])
        finished = _stamp(self.now + timedelta(seconds=30))
        self._write_summary(
            FANOUT_A,
            [
                {
                    "unit_id": f"stuck-{index}",
                    "status": "failed",
                    "unit_state": state,
                    "finished_at": finished,
                }
                for index, state in enumerate(sorted(UNIT_STUCK_STATES))
            ],
        )

        outcomes = self._outcomes()

        self.assertEqual(len(outcomes), len(UNIT_STUCK_STATES))
        for outcome in outcomes:
            with self.subTest(state=outcome["unit_state"]):
                self.assertEqual(outcome["next"], NEXT_RECORD_BLOCKED)

    def test_a_unit_state_written_by_the_run_state_lane_is_carried(self):
        # The field is additive and may be absent; when present it is what the
        # line reports, so the reminder and the status board say the same word.
        self._write_plan(["ship it"])
        self._write_summary(
            FANOUT_A,
            [
                {
                    "unit_id": "graded",
                    "status": "failed",
                    "unit_state": "data_missing",
                    "finished_at": _stamp(self.now + timedelta(seconds=30)),
                }
            ],
        )

        outcome = self._outcomes()[0]

        self.assertEqual(outcome["unit_state"], "data_missing")
        self.assertEqual(outcome["next"], NEXT_RECORD_BLOCKED)

    def test_a_summary_without_a_state_still_names_the_observed_status(self):
        self._write_plan(["ship it"])
        self._write_summary(
            FANOUT_A,
            [
                {
                    "unit_id": "bare",
                    "status": "completed",
                    "process_succeeded": True,
                    "finished_at": _stamp(self.now + timedelta(seconds=30)),
                }
            ],
        )

        outcome = self._outcomes()[0]

        self.assertEqual(outcome["status"], "completed")
        self.assertNotIn("unit_state", outcome)

    def test_outcomes_from_several_fanouts_come_back_newest_first(self):
        self._write_plan(["ship it"])
        self._write_summary(
            FANOUT_A,
            [
                {
                    "unit_id": "older",
                    "status": "completed",
                    "process_succeeded": True,
                    "finished_at": _stamp(self.now + timedelta(seconds=10)),
                }
            ],
        )
        self._write_summary(
            FANOUT_B,
            [
                {
                    "unit_id": "newer",
                    "status": "completed",
                    "process_succeeded": True,
                    "finished_at": _stamp(self.now + timedelta(seconds=40)),
                }
            ],
        )

        self.assertEqual(
            [outcome["unit_id"] for outcome in self._outcomes()], ["newer", "older"]
        )

    def test_an_outcome_older_than_the_window_is_history_not_an_event(self):
        # A plan left open overnight must not resurrect yesterday's dispatches
        # on every turn.
        self._write_plan(["ship it"])
        self._write_summary(
            FANOUT_A,
            [
                {
                    "unit_id": "yesterday",
                    "status": "completed",
                    "process_succeeded": True,
                    "finished_at": _stamp(self.now + timedelta(seconds=30)),
                }
            ],
        )

        stale_now = self.now + timedelta(seconds=OUTCOME_MAX_AGE_SECONDS + 120)

        self.assertEqual(
            unacknowledged_outcomes(str(self.home), "", "", now=stale_now), []
        )

    def test_without_a_plan_of_its_own_the_reader_claims_nothing(self):
        self._write_summary(
            FANOUT_A,
            [
                {
                    "unit_id": "orphan",
                    "status": "completed",
                    "process_succeeded": True,
                    "finished_at": _stamp(self.now + timedelta(seconds=30)),
                }
            ],
        )

        self.assertEqual(self._outcomes(), [])

    def test_a_finished_plan_still_owes_its_dispatches_an_answer(self):
        write_todo(
            self.home,
            build_todo_record(
                "plan", [{"text": "ship it", "state": "done"}], source="test"
            ),
        )
        self._write_summary(
            FANOUT_A,
            [
                {
                    "unit_id": "late",
                    "status": "completed",
                    "process_succeeded": True,
                    "finished_at": _stamp(self.now + timedelta(seconds=30)),
                }
            ],
        )

        self.assertEqual([outcome["unit_id"] for outcome in self._outcomes()], ["late"])

    def test_unreadable_and_half_written_records_are_skipped_not_raised(self):
        self._write_plan(["ship it"])
        directory = self.home / "coding" / "fanout" / FANOUT_A
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "dispatch_summary.json").write_text("{not json", encoding="utf-8")
        self._write_summary(FANOUT_B, [{"unit_id": "", "finished_at": ""}, "not a dict"])

        self.assertEqual(self._outcomes(), [])

    def test_a_missing_runtime_home_yields_nothing(self):
        self.assertEqual(
            unacknowledged_outcomes(str(self.home / "absent"), "", "", now=self.now), []
        )


class SharedUnitVocabularyParityTest(unittest.TestCase):
    """The reader uses the shared unit states and never keeps its own copy.

    `dispatch_outcomes` imports the vocabulary under a `try`, because the
    installed bundle has to register with no `omh` package at all. That guard
    is the one place a copy of the state names could reappear and then drift
    from `omh.coding.unit_execution_state` unnoticed, so both directions are
    pinned: what the module binds when the import succeeds, and what its
    fallback is allowed to define when it does not.
    """

    def test_the_module_binds_the_shared_constants_rather_than_a_copy(self):
        from omh.coding import unit_execution_state as shared
        from omh.plugin_bundle.omh import dispatch_outcomes

        # Identity, not equality: an equal-but-separate frozenset is exactly
        # the copy this test exists to reject.
        self.assertIs(dispatch_outcomes.UNIT_STUCK_STATES, shared.UNIT_STUCK_STATES)
        self.assertIs(dispatch_outcomes.UNIT_EXECUTION_STATES, shared.UNIT_EXECUTION_STATES)
        self.assertEqual(dispatch_outcomes.UNIT_STATE_VERIFIED, shared.UNIT_STATE_VERIFIED)
        self.assertEqual(dispatch_outcomes.UNIT_STATE_FAILED, shared.UNIT_STATE_FAILED)
        for name in ("_STUCK_STATES", "_VERIFIED_STATE", "_KNOWN_UNIT_STATES"):
            with self.subTest(name=name):
                self.assertFalse(
                    hasattr(dispatch_outcomes, name),
                    f"{name} reintroduces a local unit-state vocabulary; import the "
                    "shared one from omh.coding.unit_execution_state instead",
                )

    def test_the_standalone_fallback_names_no_unit_state(self):
        # Re-derived from the source, so it holds whatever the handler is
        # rewritten to look like: no string it defines may be a unit state.
        from omh.coding.unit_execution_state import UNIT_EXECUTION_STATES
        from omh.plugin_bundle.omh import dispatch_outcomes

        source = Path(dispatch_outcomes.__file__).read_text(encoding="utf-8")
        handlers = [
            handler
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Try)
            for handler in node.handlers
            if any(
                isinstance(name, ast.alias) and name.name in {"UNIT_EXECUTION_STATES"}
                for stmt in node.body
                if isinstance(stmt, ast.ImportFrom)
                for name in stmt.names
            )
        ]
        self.assertEqual(len(handlers), 1, "expected one guarded vocabulary import")
        literals = {
            node.value
            for node in ast.walk(handlers[0])
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }

        self.assertEqual(
            literals & set(UNIT_EXECUTION_STATES),
            set(),
            "the ImportError fallback names a unit state; it must define none, "
            "or the copy will drift from the shared vocabulary",
        )


if __name__ == "__main__":
    unittest.main()
