from __future__ import annotations

from copy import deepcopy
import json
import unittest

from _local_package import load_local_package

load_local_package()

from omh.coding.fanout import build_fanout_contract  # noqa: E402
from omh.plugin_bundle.omh.subagent_graph import (  # noqa: E402
    GRAPH_SCHEMA_VERSION,
    project_subagent_graph,
)


def _contract(*, edges: bool = True, unit_count: int = 3) -> dict[str, object]:
    units: list[dict[str, object]] = []
    for index in range(unit_count):
        unit_id = ("api", "ui", "integrate")[index] if unit_count == 3 else f"unit-{index}"
        depends_on: list[str] = []
        if edges and index == unit_count - 1:
            depends_on = [str(unit["unit_id"]) for unit in units]
        units.append(
            {
                "unit_id": unit_id,
                "file_scope": [f"src/{unit_id}/"],
                "depends_on": depends_on,
            }
        )
    spawn_plan = (
        {
            "why_parallel": "independent producer units can run concurrently",
            "why_not_single_unit": "one owner would serialize unrelated scopes",
            "independence": "every producer owns a disjoint directory",
            "expected_evidence_shape": "per-unit result plus integration verification",
        }
        if unit_count > 4
        else None
    )
    return build_fanout_contract(
        "API and UI then integrate",
        units,
        spawn_plan=spawn_plan,
    )


def _roster(
    contract: dict[str, object],
    *,
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
                "status": (statuses or {}).get(str(unit["unit_id"]), "prepared_not_observed"),
            }
            for unit in units
            if isinstance(unit, dict)
        ],
    }


def _inactive_reason(
    contract: dict[str, object] | None,
    roster: dict[str, object],
    *,
    preference: str = "auto",
    blockers: tuple[str, ...] = (),
) -> str:
    graph = project_subagent_graph(
        contract,
        roster,
        preference=preference,
        blockers=blockers,
    )
    assert graph["status"] == "inactive"
    return str(graph["reason"])


class SubagentGraphProjectionTests(unittest.TestCase):
    def test_auto_activates_only_for_valid_dependency_graph(self) -> None:
        contract = _contract()

        graph = project_subagent_graph(contract, _roster(contract), preference="auto")

        self.assertEqual(graph["schema_version"], GRAPH_SCHEMA_VERSION)
        self.assertEqual((graph["status"], graph["reason"]), ("active", "dependency_edges"))
        self.assertEqual(graph["edges"], [["api", "integrate"], ["ui", "integrate"]])
        self.assertEqual(graph["edge_count"], 2)
        self.assertEqual(graph["frontier"], ["api", "ui"])
        self.assertIn("Prepared", graph["claim_boundary"])
        nodes = {node["node_id"]: node for node in graph["nodes"]}
        self.assertEqual(nodes["integrate"]["blocked_by"], ["api", "ui"])
        self.assertFalse(nodes["integrate"]["in_frontier"])
        encoded = json.dumps(graph, sort_keys=True)
        self.assertNotIn("elapsed_seconds", encoded)
        self.assertNotIn("tokens", encoded)
        self.assertNotIn("cost", encoded)

    def test_activation_boundaries_emit_stable_reason_tokens(self) -> None:
        valid = _contract()
        edge_free = _contract(edges=False)
        single_owner = deepcopy(edge_free)
        single_owner["units"] = list(single_owner["units"])[:1]
        empty = deepcopy(edge_free)
        empty["units"] = []
        unknown = deepcopy(valid)
        unknown["units"][-1]["depends_on"] = ["missing"]
        self_dependency = deepcopy(valid)
        self_dependency["units"][0]["depends_on"] = ["api"]
        cyclic = deepcopy(valid)
        cyclic["units"][0]["depends_on"] = ["integrate"]
        overlap = deepcopy(edge_free)
        overlap["units"][1]["boundary"]["file_scope"] = list(
            overlap["units"][0]["boundary"]["file_scope"]
        )

        cases = (
            ("explicit_off", valid, _roster(valid), "off", ()),
            ("no_dependency_edges", edge_free, _roster(edge_free), "auto", ()),
            ("single_owner", single_owner, _roster(single_owner), "auto", ()),
            ("live_team", valid, _roster(valid), "auto", ("live_team",)),
            ("unknown_dependency", unknown, _roster(unknown), "auto", ()),
            ("self_dependency", self_dependency, _roster(self_dependency), "auto", ()),
            ("cyclic_dependency", cyclic, _roster(cyclic), "auto", ()),
            (
                "overlapping_writes_without_edge",
                overlap,
                _roster(overlap),
                "auto",
                (),
            ),
            ("empty_graph", empty, _roster(empty), "auto", ()),
            (
                "host_no_native_dag",
                valid,
                _roster(valid),
                "on",
                ("host_no_native_dag",),
            ),
            (
                "unpaired_native_rows",
                None,
                {"units": []},
                "on",
                ("unpaired_native_rows",),
            ),
        )
        for expected, contract, roster, preference, blockers in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    _inactive_reason(
                        contract,
                        roster,
                        preference=preference,
                        blockers=blockers,
                    ),
                    expected,
                )

        explicit = project_subagent_graph(edge_free, _roster(edge_free), preference="on")
        self.assertEqual((explicit["status"], explicit["reason"]), ("active", "explicit_preference"))

    def test_invalid_or_unbounded_contracts_fail_closed(self) -> None:
        valid = _contract()
        wrong_schema = deepcopy(valid)
        wrong_schema["schema_version"] = "fanout_contract/v1"
        missing_schema = deepcopy(valid)
        missing_schema.pop("schema_version")
        wrong_status = deepcopy(valid)
        wrong_status["status"] = "running"
        invalid_id = deepcopy(valid)
        invalid_id["fanout_id"] = "../fanout-escape"
        missing_boundary = deepcopy(valid)
        missing_boundary["units"][0].pop("boundary")
        duplicate_dependency = deepcopy(valid)
        duplicate_dependency["units"][-1]["depends_on"] = ["api", "api"]
        overwide = _contract(unit_count=65)

        cases = (
            ("invalid_contract_schema", wrong_schema),
            ("invalid_contract_schema", missing_schema),
            ("invalid_contract_status", wrong_status),
            ("invalid_fanout_id", invalid_id),
            ("invalid_contract_boundary", missing_boundary),
            ("duplicate_dependency", duplicate_dependency),
            ("graph_limit_exceeded", overwide),
        )
        for expected, contract in cases:
            with self.subTest(expected=expected):
                self.assertEqual(_inactive_reason(contract, _roster(contract)), expected)

    def test_dependency_failure_propagates_through_the_projected_dag(self) -> None:
        contract = _contract()
        contract["units"] = [
            {
                **unit,
                "depends_on": [] if index == 0 else [str(contract["units"][index - 1]["unit_id"])],
            }
            for index, unit in enumerate(contract["units"])
        ]

        graph = project_subagent_graph(
            contract,
            _roster(contract, statuses={"api": "failed"}),
            preference="auto",
        )

        nodes = {node["node_id"]: node for node in graph["nodes"]}
        self.assertEqual(nodes["api"]["state"], "failed")
        self.assertEqual(nodes["ui"]["state"], "blocked")
        self.assertEqual(nodes["integrate"]["state"], "blocked")
