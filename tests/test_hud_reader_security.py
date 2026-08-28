from __future__ import annotations

import json
import os
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
from omh.plugin_bundle.omh import runtime_reader  # noqa: E402
from omh.plugin_bundle.omh.subagent_graph import project_subagent_graph  # noqa: E402
from omh.system.paths import OmhPaths  # noqa: E402


def _record(
    paths: OmhPaths,
    *,
    numeric_ids: bool = False,
    goal: str = "Secure HUD reader",
) -> tuple[dict[str, object], Path]:
    root_id = "1" if numeric_ids else "api"
    child_id = "2" if numeric_ids else "integrate"
    contract = build_fanout_contract(
        goal,
        [
            {"unit_id": root_id, "file_scope": ["src/api/"], "depends_on": []},
            {
                "unit_id": child_id,
                "file_scope": ["src/integrate/"],
                "depends_on": [root_id],
            },
        ],
    )
    recorded = write_fanout_contract(paths, contract)
    summary_path = fanout_dispatch_summary_path(paths, str(recorded["fanout_id"]))
    summary_path.write_text(json.dumps(_summary(recorded)), encoding="utf-8")
    return recorded, summary_path


def _summary(contract: dict[str, object]) -> dict[str, object]:
    units = contract["units"]
    assert isinstance(units, list)
    return {
        "schema_version": "fanout_dispatch_summary/v1",
        "fanout_id": contract["fanout_id"],
        "units": [
            {
                "unit_id": unit["unit_id"],
                "status": "prepared_not_observed",
            }
            for unit in units
            if isinstance(unit, dict)
        ],
    }


def _graph(paths: OmhPaths) -> dict[str, object]:
    return runtime_reader.read_omh_hud(
        paths.omh_home,
        paths.hermes_home,
        status={"runs": []},
    )["graph"]


class HudReaderSecurityTests(unittest.TestCase):
    def test_verified_graph_survives_newer_forged_managed_directories(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            contract, summary_path = _record(paths)
            fanout_root = summary_path.parent.parent
            newer = summary_path.stat().st_mtime + 100
            forged_count = 0
            index = 0
            while forged_count < 64:
                fanout_id = f"fanout-{index:012x}"
                index += 1
                if fanout_id == contract["fanout_id"]:
                    continue
                forged = fanout_root / fanout_id
                forged.mkdir()
                forged_contract = forged / "fanout_contract.json"
                forged_contract.write_text("{}", encoding="utf-8")
                os.utime(forged_contract, (newer + forged_count, newer + forged_count))
                forged_count += 1

            graph = _graph(paths)

            self.assertEqual(graph["status"], "active")
            self.assertEqual(graph["reason"], "dependency_edges")

    def test_bounded_json_parser_failures_become_inactive_graphs(self) -> None:
        malformed = (
            '{"value":' + ("9" * 5_000) + "}",
            '{"value":' + ("[" * 1_100) + "0" + ("]" * 1_100) + "}",
        )
        for raw in malformed:
            with self.subTest(kind=raw[:20]):
                with TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
                    _, summary_path = _record(paths)
                    (summary_path.parent / "fanout_contract.json").write_text(
                        raw,
                        encoding="utf-8",
                    )

                    graph = _graph(paths)

                    self.assertEqual(graph["status"], "inactive")
                    self.assertEqual(graph["reason"], "unverified_fanout_contract")

    def test_minimal_hud_line_strips_terminal_controls(self) -> None:
        control = "\x1b]52;c;SEVMTE8=\x07"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = runtime_reader.read_omh_hud(
                root / ".omh",
                root / ".hermes",
                status={
                    "runs": [
                        {
                            "run_id": "run-1",
                            "workflow": f"{control}workflow",
                            "phase": f"{control}runtime",
                        }
                    ]
                },
                preset="minimal",
            )

            line = str(payload["display"]["line"])

            self.assertFalse(
                any(ord(character) < 0x20 or 0x7F <= ord(character) < 0xA0 for character in line)
            )

    def test_explicit_root_symlink_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside.mkdir()
            alias = root / "omh-link"
            alias.symlink_to(outside, target_is_directory=True)

            with self.assertRaises(RuntimeError):
                runtime_reader.read_omh_hud(alias, root / ".hermes", status={"runs": []})

    def test_metadata_read_faults_are_not_collapsed_to_absence(self) -> None:
        for target_name, reason in (
            ("fanout_contract.json", "unverified_fanout_contract"),
            ("dispatch_summary.json", "unreadable_fanout_status"),
        ):
            with self.subTest(target_name=target_name):
                with TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
                    _, summary_path = _record(paths)
                    target = (summary_path.parent / target_name).resolve()
                    original_read = runtime_reader._read_hud_text
                    original_exists = Path.exists

                    def unreadable(
                        path: Path,
                        *,
                        root: Path | None = None,
                    ) -> str | None:
                        if path == target:
                            return None
                        return original_read(path, root=root)

                    def collapsed_exists(path: Path) -> bool:
                        return False if path == target else original_exists(path)

                    with (
                        patch.object(runtime_reader, "_read_hud_text", unreadable),
                        patch.object(Path, "exists", collapsed_exists),
                    ):
                        graph = _graph(paths)

                    self.assertEqual(graph["status"], "inactive")
                    self.assertEqual(graph["reason"], reason)

    def test_newer_unreadable_contract_blocks_older_verified_graph(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            _, older_summary = _record(paths, goal="Older verified graph")
            _, newer_summary = _record(paths, goal="Newer unreadable graph")
            newer_contract = (newer_summary.parent / "fanout_contract.json").resolve()
            newer_mtime = older_summary.stat().st_mtime + 100
            os.utime(newer_contract, (newer_mtime, newer_mtime))
            original_read = runtime_reader._read_hud_text

            def unreadable(path: Path, *, root: Path | None = None) -> str | None:
                if path == newer_contract:
                    return None
                return original_read(path, root=root)

            with patch.object(runtime_reader, "_read_hud_text", unreadable):
                graph = _graph(paths)

            self.assertEqual(graph["status"], "inactive")
            self.assertEqual(graph["reason"], "unverified_fanout_contract")

    def test_roster_and_summary_reject_numeric_unit_identity(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            contract, summary_path = _record(paths, numeric_ids=True)
            roster = {
                "schema_version": "omh_running_work_board/v1",
                "fanout_id": contract["fanout_id"],
                "units": [
                    {
                        "fanout_id": contract["fanout_id"],
                        "unit_id": 1 if unit["unit_id"] == "1" else unit["unit_id"],
                        "status": "prepared_not_observed",
                    }
                    for unit in contract["units"]
                    if isinstance(unit, dict)
                ],
            }
            summary = _summary(contract)
            summary["units"][0]["unit_id"] = 1

            projected = project_subagent_graph(contract, roster, preference="auto")
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            graph = _graph(paths)

            self.assertEqual(projected["reason"], "unpaired_fanout_roster")
            self.assertEqual(graph["reason"], "invalid_fanout_status")


if __name__ == "__main__":
    unittest.main()
