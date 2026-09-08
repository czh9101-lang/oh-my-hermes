#!/usr/bin/env python3
"""Measure clean and dirty working-tree fingerprint collection at fixed fixture sizes."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter_ns

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RATCHETS_PATH = REPOSITORY_ROOT / "benchmarks" / "working-tree-fingerprint" / "ratchets.json"
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from omh.quality.working_tree_fingerprint import working_tree_content_fingerprint

SIZES = (1_000, 10_000, 100_000)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _fixture(root: Path, count: int) -> None:
    _git(root, "init")
    _git(root, "config", "user.email", "bench@example.test")
    _git(root, "config", "user.name", "Benchmark")
    for index in range(count):
        directory = root / f"d{index // 1_000:03d}"
        directory.mkdir(exist_ok=True)
        (directory / f"f{index:06d}").write_bytes(b"x\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture")


def _measure(root: Path) -> dict[str, int | str]:
    started = perf_counter_ns()
    result = working_tree_content_fingerprint(root)
    elapsed = (perf_counter_ns() - started) / 1_000_000
    return {
        "state": result.state,
        "git_calls": result.git_calls,
        "wall_ms": round(elapsed, 3),
        "identity_length": len(result.fingerprint or ""),
    }


def main() -> int:
    rows: dict[str, dict[str, dict[str, int | str]]] = {}
    for count in SIZES:
        with TemporaryDirectory(prefix="omh-fingerprint-benchmark-") as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            _fixture(root, count)
            clean = _measure(root)
            (root / "d000" / "f000000").write_bytes(b"dirty\n")
            dirty = _measure(root)
        rows[str(count)] = {"clean": clean, "dirty": dirty}
    ratchets = json.loads(RATCHETS_PATH.read_text(encoding="utf-8"))
    passed = _passes_ratchets(rows, ratchets)
    print(json.dumps({"schema_version": "working_tree_fingerprint_benchmark/v1", "fixtures": rows, "ratchets_pass": passed}, sort_keys=True))
    return 0 if passed else 1


def _passes_ratchets(rows: dict[str, dict[str, dict[str, int | str]]], ratchets: dict[str, object]) -> bool:
    call_limits = ratchets["max_git_calls"]
    wall_limits = ratchets["max_wall_ms"]
    if not isinstance(call_limits, dict) or not isinstance(wall_limits, dict):
        return False
    for size, variants in rows.items():
        limits = wall_limits.get(size)
        if not isinstance(limits, dict):
            return False
        for variant, result in variants.items():
            if result["state"] != variant or result["identity_length"] != 64:
                return False
            if result["git_calls"] != call_limits.get(variant) or result["wall_ms"] > limits.get(variant, 0):
                return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
