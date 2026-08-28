from __future__ import annotations

from copy import deepcopy
import unittest

from _local_package import load_local_package

load_local_package()

from omh.coding.fanout import build_fanout_contract  # noqa: E402
from omh.plugin_bundle.omh.subagent_graph import project_subagent_graph  # noqa: E402


def _contract(*, roots: int = 2) -> dict[str, object]:
    units = [
        {
            "unit_id": f"root-{index}",
            "file_scope": [f"src/root-{index}/"],
            "depends_on": [],
        }
        for index in range(roots)
    ]
    units.append(
        {
            "unit_id": "integrate",
            "file_scope": ["src/integrate/"],
            "depends_on": [str(unit["unit_id"]) for unit in units],
        }
    )
    return build_fanout_contract(
        "Exact roster projection",
        units,
        spawn_plan=(
            {
                "why_parallel": "independent roots",
                "why_not_single_unit": "one unit would serialize roots",
                "independence": "every root owns a disjoint directory",
                "expected_evidence_shape": "one result per root plus integration",
            }
            if len(units) > 4
            else None
        ),
    )


def _roster(
    contract: dict[str, object],
    statuses: dict[str, str] | None = None,
) -> dict[str, object]:
    fanout_id = str(contract["fanout_id"])
    units = contract["units"]
    assert isinstance(units, list)
    return {
        "schema_version": "omh_running_work_board/v1",
        "fanout_id": fanout_id,
        "units": [
            {
                "fanout_id": fanout_id,
                "unit_id": str(unit["unit_id"]),
                "status": (statuses or {}).get(
                    str(unit["unit_id"]),
                    "prepared_not_observed",
                ),
            }
            for unit in units
            if isinstance(unit, dict)
        ],
    }


class SubagentGraphRosterTests(unittest.TestCase):
    def test_roster_must_be_bounded_exact_and_schema_valid(self) -> None:
        contract = _contract()
        valid = _roster(contract)
        wrong_schema = deepcopy(valid)
        wrong_schema["schema_version"] = "wrong-purpose/v1"
        wrong_fanout = deepcopy(valid)
        wrong_fanout["fanout_id"] = "fanout-000000000000"
        wrong_fanout["units"][0]["fanout_id"] = "fanout-000000000000"
        partial = deepcopy(valid)
        partial["units"].pop()
        duplicate = deepcopy(valid)
        duplicate["units"][-1] = deepcopy(duplicate["units"][0])
        extra = deepcopy(valid)
        extra["units"].append(
            {
                "fanout_id": contract["fanout_id"],
                "unit_id": "extra",
                "status": "running",
            }
        )
        unknown_status = deepcopy(valid)
        unknown_status["units"][0]["status"] = "merge_observed"
        overwide = deepcopy(valid)
        overwide["units"] = [deepcopy(valid["units"][0]) for _ in range(65)]

        cases = (
            ("invalid_roster_schema", wrong_schema),
            ("unpaired_fanout_roster", wrong_fanout),
            ("unpaired_fanout_roster", partial),
            ("duplicate_roster_unit", duplicate),
            ("unpaired_fanout_roster", extra),
            ("invalid_roster_status", unknown_status),
            ("graph_limit_exceeded", overwide),
        )
        for reason, roster in cases:
            with self.subTest(reason=reason):
                graph = project_subagent_graph(contract, roster, preference="auto")
                self.assertEqual((graph["status"], graph["reason"]), ("inactive", reason))

    def test_dispatch_lifecycle_states_are_preserved_and_not_ready(self) -> None:
        contract = _contract()
        states = (
            "already_completed",
            "dry_run_planned",
            "capability_snapshot_invalid",
            "blocked_by_dependency",
            "executor_not_ready",
            "unsupported_for_local_dispatch",
            "not_selected",
            "interrupted",
            "model_choice_required",
        )
        for state in states:
            with self.subTest(state=state):
                graph = project_subagent_graph(
                    contract,
                    _roster(contract, {"root-0": state}),
                    preference="auto",
                )
                node = next(node for node in graph["nodes"] if node["node_id"] == "root-0")
                self.assertEqual(node["state"], state)
                self.assertFalse(node["in_frontier"])

    def test_frontier_includes_ready_nodes_hidden_by_node_limit(self) -> None:
        contract = _contract(roots=9)

        graph = project_subagent_graph(
            contract,
            _roster(contract),
            preference="auto",
        )

        self.assertEqual(len(graph["nodes"]), 8)
        self.assertEqual(len(graph["frontier"]), 9)
        self.assertEqual(graph["hidden_nodes"], 2)
