from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()

from omh.coding.fanout import build_fanout_contract  # noqa: E402
from omh.coding.fanout_artifacts import (  # noqa: E402
    fanout_dispatch_summary_path,
    write_fanout_contract,
)
from omh.plugin_bundle.omh.runtime_reader import read_omh_hud  # noqa: E402
from omh.system.paths import OmhPaths  # noqa: E402


def _record(paths: OmhPaths) -> tuple[dict[str, object], Path]:
    contract = build_fanout_contract(
        "Adversarial graph record",
        [
            {"unit_id": "api", "file_scope": ["src/api/"], "depends_on": []},
            {
                "unit_id": "integrate",
                "file_scope": ["src/integrate/"],
                "depends_on": ["api"],
            },
        ],
    )
    recorded = write_fanout_contract(paths, contract)
    summary_path = fanout_dispatch_summary_path(paths, str(recorded["fanout_id"]))
    return recorded, summary_path


def _summary(contract: dict[str, object]) -> dict[str, object]:
    units = contract["units"]
    assert isinstance(units, list)
    return {
        "schema_version": "fanout_dispatch_summary/v1",
        "fanout_id": contract["fanout_id"],
        "units": [
            {
                "unit_id": str(unit["unit_id"]),
                "status": "prepared_not_observed",
            }
            for unit in units
            if isinstance(unit, dict)
        ],
    }


def _reason(paths: OmhPaths) -> str:
    return str(
        read_omh_hud(
            paths.omh_home,
            paths.hermes_home,
            status={"runs": []},
        )["graph"]["reason"]
    )


class SubagentGraphReaderAdversarialTests(unittest.TestCase):
    def test_dispatch_summary_identity_roster_and_status_fail_closed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            contract, summary_path = _record(paths)
            valid = _summary(contract)
            cases: list[dict[str, object]] = []
            wrong_schema = deepcopy(valid)
            wrong_schema["schema_version"] = "evil/v9"
            cases.append(wrong_schema)
            wrong_fanout = deepcopy(valid)
            wrong_fanout["fanout_id"] = "fanout-000000000000"
            cases.append(wrong_fanout)
            duplicate = deepcopy(valid)
            duplicate["units"][-1] = deepcopy(duplicate["units"][0])
            cases.append(duplicate)
            partial = deepcopy(valid)
            partial["units"].pop()
            cases.append(partial)
            extra = deepcopy(valid)
            extra["units"].append({"unit_id": "extra", "status": "running"})
            cases.append(extra)
            unknown_status = deepcopy(valid)
            unknown_status["units"][0]["status"] = "merge_observed"
            cases.append(unknown_status)

            for summary in cases:
                with self.subTest(summary=summary):
                    summary_path.write_text(json.dumps(summary), encoding="utf-8")
                    self.assertEqual(_reason(paths), "invalid_fanout_status")

    def test_invalid_or_unreadable_inflight_markers_fail_closed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            contract, summary_path = _record(paths)
            summary_path.write_text(json.dumps(_summary(contract)), encoding="utf-8")
            fanout_dir = summary_path.parent
            inflight = fanout_dir / "inflight"
            inflight.mkdir()
            (inflight / "api.json").write_text(
                json.dumps(
                    {
                        "schema_version": "omh_inflight_marker/v1",
                        "fanout_id": "fanout-000000000000",
                        "unit_id": "other",
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(_reason(paths), "invalid_fanout_status")

            (inflight / "api.json").write_text("{", encoding="utf-8")
            self.assertEqual(_reason(paths), "unreadable_fanout_status")

    def test_dispatch_lifecycle_state_reaches_the_graph_unchanged(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            contract, summary_path = _record(paths)
            summary = _summary(contract)
            summary["units"][0]["status"] = "executor_not_ready"
            summary["units"][1]["status"] = "not_selected"
            summary_path.write_text(json.dumps(summary), encoding="utf-8")

            graph = read_omh_hud(
                paths.omh_home,
                paths.hermes_home,
                status={"runs": []},
            )["graph"]

            states = {node["node_id"]: node["state"] for node in graph["nodes"]}
            self.assertEqual(
                states,
                {"api": "executor_not_ready", "integrate": "not_selected"},
            )
            self.assertEqual(graph["frontier"], [])

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            contract, summary_path = _record(paths)
            summary_path.write_text(json.dumps(_summary(contract)), encoding="utf-8")
            outside = root / "outside"
            outside.mkdir()
            (summary_path.parent / "inflight").symlink_to(outside, target_is_directory=True)
            self.assertEqual(_reason(paths), "unreadable_fanout_status")

    def test_managed_fanout_filter_precedes_deterministic_limit(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            contract, summary_path = _record(paths)
            summary_path.write_text(json.dumps(_summary(contract)), encoding="utf-8")
            fanout_root = summary_path.parent.parent
            invalid = []
            for index in range(64):
                child = fanout_root / f"junk-{index:02d}"
                child.mkdir()
                invalid.append(child)
            original_iterdir = Path.iterdir

            def ordered_iterdir(path: Path):
                if path == fanout_root:
                    return iter([*invalid, summary_path.parent])
                return original_iterdir(path)

            with patch.object(Path, "iterdir", ordered_iterdir):
                payload = read_omh_hud(
                    paths.omh_home,
                    paths.hermes_home,
                    status={"runs": []},
                )

            self.assertEqual(payload["graph"]["status"], "active")

    def test_symlinked_fanout_root_is_an_explicit_read_failure(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            outside = root / "outside"
            outside.mkdir()
            paths.omh_home.mkdir(parents=True)
            (paths.omh_home / "coding").mkdir()
            (paths.omh_home / "coding" / "fanout").symlink_to(
                outside,
                target_is_directory=True,
            )

            self.assertEqual(_reason(paths), "unreadable_fanout_root")
