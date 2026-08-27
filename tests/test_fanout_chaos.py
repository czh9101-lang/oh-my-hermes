"""Seeded adversarial invariants for the fanout dispatch scheduler.

Adopted from OMO's chaos bench discipline: enumerated tests only find bugs
someone already imagined, so this file drives the real dispatch engine over
randomly shaped DAGs, outcomes, pool widths, and owner lanes, and checks
invariants that must hold for EVERY shape. No wall clock and no bare
`random` — every draw derives from one seed (`SEED` env replays a failure;
each iteration gets an independent sub-stream so one iteration reproduces
in isolation), and the seed is embedded in every assertion message.
"""

from __future__ import annotations

import os
import random
import subprocess
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from omh.coding.fanout import build_fanout_contract
from omh.coding.fanout_artifacts import write_fanout_contract
from omh.coding.fanout_dispatch import dispatch_fanout
from omh.system.paths import OmhPaths

_SEED = int(os.environ.get("SEED", "20260827"))
_ITERATIONS = int(os.environ.get("CHAOS_ITERATIONS", "25"))
_GOAL = "chaos drill"
_OWNERS = ("codex", "claude-code")


def _ready(paths, profile, **kwargs):
    return {"status": "ready", "profile": profile}


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _make_repo(root: Path) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
        cwd=str(repo),
        check=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()
    return repo, sha


def _random_units(rng: random.Random) -> list[dict[str, object]]:
    # Four is the spawn-plan-free ceiling; a larger split needs the
    # spawn_plan rationale block, which is contract policy, not scheduling.
    count = rng.randint(2, 4)
    units: list[dict[str, object]] = []
    for index in range(count):
        unit: dict[str, object] = {
            "unit_id": f"u{index}",
            "title": f"Unit {index}",
            "owner": rng.choice(_OWNERS),
            "file_scope": [f"src/u{index}/"],
        }
        # Dependencies only point at lower indices, so every generated DAG
        # is acyclic by construction and the contract validator accepts it.
        candidates = [f"u{dep}" for dep in range(index)]
        depends_on = [dep for dep in candidates if rng.random() < 0.35]
        if depends_on:
            unit["depends_on"] = depends_on
        units.append(unit)
    return units


class FanoutChaosTests(unittest.TestCase):
    def test_random_dags_hold_the_scheduler_invariants(self) -> None:
        for iteration in range(_ITERATIONS):
            rng = random.Random(f"{_SEED}:{iteration}")
            label = f"SEED={_SEED} iteration={iteration}"
            with self.subTest(label), TemporaryDirectory() as tmp:
                root = Path(tmp)
                paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
                repo, sha = _make_repo(root)
                units = _random_units(rng)
                units_by_id = {str(unit["unit_id"]): unit for unit in units}
                outcomes = {
                    str(unit["unit_id"]): rng.choices(
                        ["ok", "fail", "timeout", "missing"], weights=[6, 2, 1, 1]
                    )[0]
                    for unit in units
                }
                dispatch_counts: dict[str, int] = {}
                counts_lock = threading.Lock()

                def runner(argv, **kwargs):
                    if argv[0] == "git":
                        return subprocess.run(argv, **kwargs)
                    # The worktree path ends `-fanout-<unit_id>` — the one
                    # anchor that names exactly this unit; prompt text also
                    # mentions dependency ids and would misattribute.
                    cwd = str(kwargs.get("cwd", ""))
                    self.assertIn("-fanout-", cwd, f"{label}: unattributable spawn cwd {cwd!r}")
                    unit_id = cwd.rsplit("-fanout-", 1)[-1]
                    # Read-modify-write under a lock: a lost update would HIDE
                    # a double dispatch, making the bench quieter, not louder.
                    with counts_lock:
                        dispatch_counts[unit_id] = dispatch_counts.get(unit_id, 0) + 1
                    outcome = outcomes.get(unit_id, "ok")
                    if outcome == "timeout":
                        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))
                    if outcome == "missing":
                        raise FileNotFoundError(argv[0])
                    return _FakeCompleted(0 if outcome == "ok" else 1, f"unit {unit_id} done")

                contract = write_fanout_contract(paths, build_fanout_contract(_GOAL, units))
                summary = dispatch_fanout(
                    paths,
                    contract,
                    goal_text=_GOAL,
                    repo_root=repo,
                    base_sha=sha,
                    concurrency=rng.randint(1, 4),
                    per_owner_lanes={"codex": 1} if rng.random() < 0.5 else None,
                    runner=runner,
                    readiness=_ready,
                )

                entries = {entry["unit_id"]: entry for entry in summary["units"]}
                # 1. Every planned unit reaches exactly one terminal entry.
                self.assertEqual(set(entries), set(units_by_id), label)
                # 2. No unit is ever dispatched twice within one run.
                for unit_id, count in dispatch_counts.items():
                    self.assertLessEqual(count, 1, f"{label}: {unit_id} dispatched {count}x")
                # 3. merge_order is a topological order of depends_on.
                positions = {unit_id: index for index, unit_id in enumerate(summary["merge_order"])}
                for unit_id, unit in units_by_id.items():
                    for dep in unit.get("depends_on", []) or []:
                        self.assertLess(
                            positions[str(dep)], positions[unit_id],
                            f"{label}: {unit_id} merges before its dependency {dep}",
                        )
                # 4. A blocked unit has an unsuccessful direct dependency and
                #    names what it blocked on; success never blocks anyone.
                for unit_id, entry in entries.items():
                    if entry["status"] == "blocked_by_dependency":
                        deps = units_by_id[unit_id].get("depends_on", []) or []
                        self.assertTrue(deps, f"{label}: {unit_id} blocked with no deps")
                        self.assertTrue(
                            any(not entries[str(dep)].get("process_succeeded") for dep in deps),
                            f"{label}: {unit_id} blocked but every direct dep succeeded",
                        )
                        self.assertTrue(
                            entry.get("blocked_on"),
                            f"{label}: {unit_id} blocked without naming a dependency",
                        )
                # 5. An executed verdict means executed exactly once — this
                #    catches both a phantom result for a never-dispatched unit
                #    and a genuine second spawn, which worktree collisions
                #    would otherwise mask as a different failure.
                for unit_id, entry in entries.items():
                    if entry["status"] in {"completed", "failed"}:
                        self.assertEqual(
                            dispatch_counts.get(unit_id, 0), 1,
                            f"{label}: {unit_id} reports {entry['status']} with "
                            f"{dispatch_counts.get(unit_id, 0)} dispatches",
                        )
                # 6. process_succeeded tracks the injected outcome exactly for
                #    units that actually ran.
                for unit_id, count in dispatch_counts.items():
                    expected = outcomes[unit_id] == "ok"
                    self.assertEqual(
                        bool(entries[unit_id].get("process_succeeded")), expected,
                        f"{label}: {unit_id} outcome {outcomes[unit_id]} vs entry",
                    )
                # 7. No interrupt was injected, so none may be reported.
                self.assertNotIn("interrupted", summary, label)
                for entry in entries.values():
                    self.assertNotEqual(entry.get("status"), "interrupted", label)

    def test_a_ready_dependent_starts_before_an_unrelated_slow_sibling_finishes(self) -> None:
        # The frontier property itself — the reason the wave barrier was
        # removed. Under the barrier, u1 (dependent on fast u0) could not
        # start until the unrelated slow sibling drained the wave; under
        # frontier admission it starts while `slow` is still running. This
        # deterministic shape needs a real sleep, which the seeded bench
        # deliberately avoids — hence a separate test.
        import threading as _threading
        import time as _time

        units = [
            {"unit_id": "slow", "title": "Slow sibling", "owner": "codex", "file_scope": ["src/slow/"]},
            {"unit_id": "u0", "title": "Fast base", "owner": "codex", "file_scope": ["src/u0/"]},
            {"unit_id": "u1", "title": "Dependent", "owner": "codex", "file_scope": ["src/u1/"], "depends_on": ["u0"]},
        ]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = OmhPaths(omh_home=root / ".omh", hermes_home=root / ".hermes")
            repo, sha = _make_repo(root)
            contract = write_fanout_contract(paths, build_fanout_contract(_GOAL, units))
            slow_done = _threading.Event()
            events: list[tuple[str, str, bool]] = []
            events_lock = _threading.Lock()

            def runner(argv, **kwargs):
                if argv[0] == "git":
                    return subprocess.run(argv, **kwargs)
                cwd = str(kwargs.get("cwd", ""))
                unit_id = cwd.rsplit("-fanout-", 1)[-1]
                with events_lock:
                    events.append(("start", unit_id, slow_done.is_set()))
                if unit_id == "slow":
                    _time.sleep(1.0)
                    slow_done.set()
                with events_lock:
                    events.append(("end", unit_id, slow_done.is_set()))
                return _FakeCompleted(0, f"unit {unit_id} done")

            summary = dispatch_fanout(
                paths,
                contract,
                goal_text=_GOAL,
                repo_root=repo,
                base_sha=sha,
                concurrency=3,
                runner=runner,
                readiness=_ready,
            )

            statuses = {entry["unit_id"]: entry["status"] for entry in summary["units"]}
            self.assertEqual(statuses["u1"], "completed", statuses)
            # u1 was admitted while the unrelated slow sibling was still
            # running — the wave barrier would have recorded True here.
            self.assertIn(("start", "u1", False), events, events)


if __name__ == "__main__":
    unittest.main()
