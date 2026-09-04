#!/usr/bin/env python3
# ─── How to run ───
# python tools/test_sharding/run.py --plan plan.json --lane linux-3.12 --shard 0 --out result.json
# python tools/test_sharding/run.py --plan plan.json --lane linux-3.12 --quarantine --out result.json
"""Run exactly one planned shard or quarantine and record its lane-local result."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import sys
import time
import types
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.test_sharding import JsonValue, ShardingError
from tools.test_sharding.aggregate import QUARANTINE_KEY, load_plan

_ExcInfo = tuple[type[BaseException], BaseException, types.TracebackType]


@dataclass(frozen=True, slots=True)
class ShardTarget:
    """The lane and plan entry this process owns."""

    lane: str
    kind: str
    shard: int | None


def flatten_suite(suite: unittest.TestSuite) -> list[unittest.TestCase]:
    """Flatten a unittest suite to its leaf cases without changing ordering."""

    tests: list[unittest.TestCase] = []
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            tests.extend(flatten_suite(test))
        else:
            tests.append(test)
    return tests


class RecordingResult(unittest.TextTestResult):
    """Record outcomes and monotonic durations while unittest runs one suite."""

    def __init__(self, stream: unittest.runner._WritelnDecorator, descriptions: bool, verbosity: int) -> None:
        super().__init__(stream, descriptions, verbosity)
        self.outcomes: dict[str, str] = {}
        self.durations: dict[str, float] = {}
        self._started = 0.0

    def startTest(self, test: unittest.TestCase) -> None:
        self._started = time.monotonic()
        super().startTest(test)

    def stopTest(self, test: unittest.TestCase) -> None:
        self.durations[test.id()] = time.monotonic() - self._started
        super().stopTest(test)

    def addSuccess(self, test: unittest.TestCase) -> None:
        self.outcomes[test.id()] = "passed"
        super().addSuccess(test)

    def addFailure(self, test: unittest.TestCase, err: _ExcInfo) -> None:
        self.outcomes[test.id()] = "failure"
        super().addFailure(test, err)

    def addError(self, test: unittest.TestCase, err: _ExcInfo) -> None:
        self.outcomes[test.id()] = "error"
        super().addError(test, err)

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:
        self.outcomes[test.id()] = "skipped"
        super().addSkip(test, reason)

    def addExpectedFailure(self, test: unittest.TestCase, err: _ExcInfo) -> None:
        self.outcomes[test.id()] = "passed"
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:
        self.outcomes[test.id()] = "failure"
        super().addUnexpectedSuccess(test)


def load_plan_ids(plan_path: Path, shard: int | None) -> tuple[str, list[str]]:
    """Select one fully validated shard list or serial quarantine list."""

    assignments = load_plan(plan_path)
    if shard is None:
        return "quarantine", list(assignments[QUARANTINE_KEY])
    planned = assignments.get(shard)
    if planned is None:
        raise ShardingError(f"plan has no shard {shard}")
    return "shard", list(planned)


def load_suite(test_ids: list[str], start_dir: Path) -> unittest.TestSuite:
    """Import and load only planned IDs, rejecting any mismatch before running."""

    resolved = str(start_dir.resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)
    try:
        suite = unittest.TestLoader().loadTestsFromNames(test_ids)
    except (ImportError, AttributeError, ValueError) as exc:
        raise ShardingError(f"could not load planned tests: {exc}") from exc
    loaded = sorted(test.id() for test in flatten_suite(suite))
    if loaded != sorted(test_ids):
        raise ShardingError(f"planned tests failed to load: {sorted(set(test_ids) - set(loaded))}")
    return suite


def result_payload(target: ShardTarget, planned: list[str], result: RecordingResult) -> dict[str, JsonValue]:
    """Build the lane-identified JSON contract consumed by the aggregate."""

    outcomes = {name: [] for name in ("passed", "skipped", "failure", "error")}
    for test_id, outcome in result.outcomes.items():
        outcomes[outcome].append(test_id)
    durations = {
        test_id: round(seconds, 6)
        for test_id, seconds in result.durations.items()
        if result.outcomes.get(test_id) != "skipped"
    }
    return {
        "version": 1,
        "lane": target.lane,
        "kind": target.kind,
        "shard": target.shard,
        "planned": sorted(planned),
        "executed": sorted(outcomes["passed"]),
        "skipped": sorted(outcomes["skipped"]),
        "failures": sorted(outcomes["failure"]),
        "errors": sorted(outcomes["error"]),
        "durations": dict(sorted(durations.items())),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--lane", required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--shard", type=int)
    target.add_argument("--quarantine", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--start-dir", type=Path, default=Path("tests"))
    return parser


def main(argv: list[str] | None = None) -> int:
    """Execute the selected lane-local test list and fail on test failures."""

    args = _parser().parse_args(argv)
    if not args.lane.strip():
        print("test sharding: lane must be non-empty", file=sys.stderr)
        return 2
    shard = None if args.quarantine else args.shard
    try:
        kind, planned = load_plan_ids(args.plan, shard)
        suite = load_suite(planned, args.start_dir)
    except ShardingError as exc:
        print(f"test sharding: {exc}", file=sys.stderr)
        return 2
    result = unittest.TextTestRunner(resultclass=RecordingResult, verbosity=1).run(suite)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result_payload(ShardTarget(args.lane, kind, shard), planned, result), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if result.failures or result.errors:
        print(f"test sharding: {len(result.failures)} failures, {len(result.errors)} errors", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
