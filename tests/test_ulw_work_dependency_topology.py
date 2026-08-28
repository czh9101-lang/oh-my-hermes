from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()

from omh.capabilities.orchestration import orchestration_patterns  # noqa: E402
from omh.capabilities.skills import skill_capabilities  # noqa: E402
from omh.coding.fanout import build_fanout_contract  # noqa: E402
from omh.coding.fanout_contracts import FanoutContractError  # noqa: E402
from omh.routing.chat import route_chat_message  # noqa: E402
from omh.routing.recommend import recommend_skills  # noqa: E402
from omh.skills.packaging import builtin_skill_reference_templates  # noqa: E402


class UlwWorkDependencyTopologyTests(unittest.TestCase):
    def test_dependency_shaped_plan_selects_graph_execution(self) -> None:
        patterns = {item["id"]: item for item in orchestration_patterns()}

        team_pipeline = patterns["team_staged_pipeline"]
        contract = build_fanout_contract(
            "analyze API and UI in parallel, then integrate the results and verify",
            [
                {"unit_id": "api", "file_scope": ["src/api/"]},
                {"unit_id": "ui", "file_scope": ["src/ui/"]},
                {
                    "unit_id": "integrate",
                    "file_scope": ["src/integration/"],
                    "depends_on": ["api", "ui"],
                },
            ],
        )

        self.assertIn("dependency_topology", team_pipeline["required_decisions"])
        self.assertEqual(team_pipeline["observed_evidence_required"][0], "topology_prepared")
        self.assertEqual(contract["merge_plan"]["merge_order"][-1], "integrate")

    def test_independent_or_overlapping_lanes_avoid_wrong_graph_mode(self) -> None:
        capabilities = {item["id"]: item for item in skill_capabilities()}
        reference_paths = {
            (template.skill_name, template.relative_path)
            for template in builtin_skill_reference_templates()
        }

        ultrawork = capabilities["ultrawork"]

        self.assertIn("dependency edges or shared invariants", ultrawork["required_inputs"])
        self.assertIn(
            ("ultrawork", "references/dependency-topology.md"),
            reference_paths,
        )

    def test_independent_disjoint_units_preserve_parallel_frontier(self) -> None:
        contract = build_fanout_contract(
            "analyze independent API and UI surfaces",
            [
                {"unit_id": "api", "file_scope": ["src/api/"]},
                {"unit_id": "ui", "file_scope": ["src/ui/"]},
            ],
        )

        units = {unit["unit_id"]: unit for unit in contract["units"]}

        self.assertEqual(units["api"]["depends_on"], [])
        self.assertEqual(units["ui"]["depends_on"], [])

    def test_overlapping_writes_without_ordering_edge_remain_rejected(self) -> None:
        with self.assertRaises(FanoutContractError):
            build_fanout_contract(
                "edit one shared module twice",
                [
                    {"unit_id": "first", "file_scope": ["src/shared.py"]},
                    {"unit_id": "second", "file_scope": ["src/shared.py"]},
                ],
            )

    def test_dependency_shaped_prompt_nominates_ultrawork(self) -> None:
        recommendations = recommend_skills(
            "analyze API and UI in parallel, then integrate the results and verify",
            limit=5,
        )

        self.assertEqual(recommendations[0]["skill"], "ultrawork")

    def test_dependency_graph_concept_question_stays_direct(self) -> None:
        route = route_chat_message("what is a dependency graph?", source="discord")

        self.assertEqual(route["action"], "fallback")
        self.assertEqual(route["selected_skill"], "oh-my-hermes")
