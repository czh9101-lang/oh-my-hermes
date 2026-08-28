from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()

from _cli_harness import run_cli  # noqa: E402

from omh.coding.fanout import build_fanout_contract  # noqa: E402
from omh.coding.fanout_artifacts import (  # noqa: E402
    fanout_dispatch_summary_path,
    write_fanout_contract,
)
from omh.plugin_bundle.omh.runtime_reader import read_omh_hud  # noqa: E402
from omh.plugin_bundle.omh.subagent_graph import GRAPH_SCHEMA_VERSION  # noqa: E402
from omh.system.paths import OmhPaths  # noqa: E402


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


class TuiSubagentDagModeTests(unittest.TestCase):
    def test_hud_environment_can_enable_an_edge_free_graph(self) -> None:
        contract = _contract(edges=False)
        fanout_id = str(contract["fanout_id"])
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            recorded = write_fanout_contract(paths, contract)
            fanout_dispatch_summary_path(paths, fanout_id).write_text(
                json.dumps(
                    {
                        "schema_version": "fanout_dispatch_summary/v1",
                        "fanout_id": fanout_id,
                        "units": [
                            {
                                "unit_id": str(unit["unit_id"]),
                                "status": "prepared_not_observed",
                            }
                            for unit in recorded["units"]
                            if isinstance(unit, dict)
                        ],
                    }
                ),
                encoding="utf-8",
            )

            automatic = read_omh_hud(
                paths.omh_home,
                paths.hermes_home,
                status={"runs": []},
            )
            with patch.dict(os.environ, {"OMH_SUBAGENT_GRAPH": "on"}):
                enabled = read_omh_hud(
                    paths.omh_home,
                    paths.hermes_home,
                    status={"runs": []},
                )

            self.assertEqual(automatic["graph"]["reason"], "no_dependency_edges")
            self.assertEqual(
                (enabled["graph"]["status"], enabled["graph"]["reason"]),
                ("active", "explicit_preference"),
            )

    def test_hud_composes_bounded_graph_from_recorded_fanout(self) -> None:
        contract = _contract(edges=True, unit_count=10)
        fanout_id = str(contract["fanout_id"])
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            recorded = write_fanout_contract(paths, contract)
            summary_path = fanout_dispatch_summary_path(paths, fanout_id)
            summary_path.write_text(
                json.dumps(
                    {
                        "schema_version": "fanout_dispatch_summary/v1",
                        "fanout_id": fanout_id,
                        "units": [
                            {
                                "unit_id": str(unit["unit_id"]),
                                "status": "prepared_not_observed",
                            }
                            for unit in recorded["units"]
                            if isinstance(unit, dict)
                        ],
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            payload = read_omh_hud(
                paths.omh_home,
                paths.hermes_home,
                status={"runs": []},
                graph_preference="on",
            )

            self.assertEqual(payload["schema_version"], "omh_hud/v1")
            self.assertEqual(payload["privacy"], "metadata_only")
            self.assertEqual(payload["graph"]["schema_version"], GRAPH_SCHEMA_VERSION)
            self.assertEqual(payload["graph"]["status"], "active")
            self.assertEqual(payload["graph"]["reason"], "explicit_preference")
            self.assertEqual(len(payload["graph"]["nodes"]), 8)
            self.assertEqual(payload["graph"]["hidden_nodes"], 2)
            self.assertEqual(payload["graph"]["edge_count"], 9)
            self.assertLess(len(json.dumps(payload, sort_keys=True).encode("utf-8")), 65_536)

            status, stdout, stderr = run_cli(
                [
                    "--omh-home",
                    str(paths.omh_home),
                    "--hermes-home",
                    str(paths.hermes_home),
                    "hud",
                    "--graph",
                    "--json",
                ],
                output_json=False,
            )

            self.assertEqual((status, stderr), (0, ""))
            cli_payload = json.loads(stdout)
            self.assertEqual(cli_payload["graph"]["reason"], "explicit_preference")

    def test_hud_derives_precedence_provenance_and_source_blockers(self) -> None:
        contract = _contract()
        fanout_id = str(contract["fanout_id"])
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            recorded = write_fanout_contract(paths, contract)
            fanout_dispatch_summary_path(paths, fanout_id).write_text(
                json.dumps(
                    {
                        "schema_version": "fanout_dispatch_summary/v1",
                        "fanout_id": fanout_id,
                        "units": [
                            {
                                "fanout_id": fanout_id,
                                "unit_id": str(unit["unit_id"]),
                                "status": "prepared_not_observed",
                            }
                            for unit in recorded["units"]
                            if isinstance(unit, dict)
                        ],
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            paths.omh_home.mkdir(parents=True, exist_ok=True)
            with patch.dict(os.environ, {"OMH_SUBAGENT_GRAPH": "off"}):
                explicit_off = read_omh_hud(
                    paths.omh_home,
                    paths.hermes_home,
                    status={"runs": []},
                    graph_preference="on",
                )

            self.assertEqual(
                (explicit_off["graph"]["status"], explicit_off["graph"]["reason"]),
                ("inactive", "explicit_off"),
            )

            provenance_path = (
                paths.omh_home
                / "coding"
                / "fanout"
                / fanout_id
                / "contract_provenance.json"
            )
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            provenance["contract_sha256"] = "0" * 64
            provenance_path.write_text(json.dumps(provenance), encoding="utf-8")

            unverified = read_omh_hud(
                paths.omh_home,
                paths.hermes_home,
                status={"runs": []},
            )

            self.assertEqual(
                (unverified["graph"]["status"], unverified["graph"]["reason"]),
                ("inactive", "unverified_fanout_contract"),
            )

        native = {
            "status": "observed",
            "rows": [{"task_id": "native-1", "state": "running"}],
            "active": 1,
            "running": 1,
            "blocked": 0,
            "completed": 0,
            "hidden": 0,
        }
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            with patch(
                    "omh.plugin_bundle.omh.runtime_reader.read_hermes_native_subagents",
                    return_value=native,
                ):
                native_only = read_omh_hud(
                    paths.omh_home,
                    paths.hermes_home,
                    status={"runs": []},
                )
            self.assertEqual(native_only["graph"]["reason"], "host_no_native_dag")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            write_fanout_contract(paths, contract)
            with patch(
                    "omh.plugin_bundle.omh.runtime_reader.read_hermes_native_subagents",
                    return_value=native,
                ):
                mixed = read_omh_hud(
                    paths.omh_home,
                    paths.hermes_home,
                    status={"runs": []},
                )
            self.assertEqual(mixed["graph"]["reason"], "live_team")
