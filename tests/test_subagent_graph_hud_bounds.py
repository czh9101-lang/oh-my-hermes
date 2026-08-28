from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _local_package import load_local_package

load_local_package()

from omh.coding.fanout import build_fanout_contract  # noqa: E402
from omh.coding.fanout_artifacts import (  # noqa: E402
    fanout_dispatch_summary_path,
    write_fanout_contract,
)
from omh.plugin_bundle.omh.runtime_reader import read_omh_hud  # noqa: E402
from omh.system.paths import OmhPaths  # noqa: E402


class SubagentGraphHudBoundsTests(unittest.TestCase):
    def test_hud_scopes_statuses_to_every_unit_in_the_selected_fanout(self) -> None:
        roots = [
            {
                "unit_id": f"z{index:02d}",
                "file_scope": [f"src/root-{index}/"],
                "depends_on": [],
            }
            for index in range(8)
        ]
        downstream = [
            {
                "unit_id": f"a{index:02d}",
                "file_scope": [f"src/downstream-{index}/"],
                "depends_on": ["z00"],
            }
            for index in range(32)
        ]
        contract = build_fanout_contract(
            "Bounded roster projection",
            [*roots, *downstream],
            spawn_plan={
                "why_parallel": "independent downstream scopes",
                "why_not_single_unit": "one unit would serialize disjoint work",
                "independence": "every unit owns a disjoint directory",
                "expected_evidence_shape": "one result per unit",
            },
        )
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
                                "status": (
                                    "completed"
                                    if str(unit["unit_id"]).startswith("z")
                                    else "running"
                                ),
                            }
                            for unit in recorded["units"]
                            if isinstance(unit, dict)
                        ],
                    }
                ),
                encoding="utf-8",
            )

            payload = read_omh_hud(
                paths.omh_home,
                paths.hermes_home,
                status={"runs": []},
            )

            self.assertEqual(payload["graph"]["status"], "active")
            self.assertEqual(payload["graph"]["frontier"], [])
            self.assertEqual(
                [node["state"] for node in payload["graph"]["nodes"]],
                ["completed"] * 8,
            )

    def test_hud_rejects_an_oversized_local_status_record(self) -> None:
        contract = build_fanout_contract(
            "Oversized local status",
            [
                {
                    "unit_id": "api",
                    "file_scope": ["src/api/"],
                    "depends_on": [],
                },
                {
                    "unit_id": "integrate",
                    "file_scope": ["src/integrate/"],
                    "depends_on": ["api"],
                },
            ],
        )
        fanout_id = str(contract["fanout_id"])
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            write_fanout_contract(paths, contract)
            fanout_dispatch_summary_path(paths, fanout_id).write_text(
                json.dumps(
                    {
                        "schema_version": "fanout_dispatch_summary/v1",
                        "padding": "x" * 300_000,
                        "units": [{"unit_id": "api", "status": "completed"}],
                    }
                ),
                encoding="utf-8",
            )

            payload = read_omh_hud(
                paths.omh_home,
                paths.hermes_home,
                status={"runs": []},
            )

            self.assertEqual(
                (payload["graph"]["status"], payload["graph"]["reason"]),
                ("inactive", "unreadable_fanout_status"),
            )
