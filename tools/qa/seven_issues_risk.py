#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# How to run: from a lane with `uv sync --group lint` completed:
# uv run python tools/qa/seven_issues_risk.py --output-dir /tmp/owned-risk-fixtures
# Or execute with the project's interpreter; no provider, model or network used.
"""Real local CLI scenarios for #1495, not a unittest wrapper."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from tempfile import TemporaryDirectory
from collections.abc import Callable
from typing import TypeAlias, TypedDict

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
# json.loads' stdlib signature says Any; its actual default JSON value set is closed.
decode_json: Callable[[str], JsonValue] = json.loads


class Options(argparse.Namespace):
    output_dir: Path | None = None


class Scenario(TypedDict):
    name: str
    argv: list[str]
    exit: int
    status: str
    verdict: str | None
    finding_ids: list[str]
    output_sha256: str


def git(repo: Path, args: list[str], env: dict[str, str]) -> None:
    _ = subprocess.run(["git", "-C", str(repo), *args], env=env, check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)


def snapshot(repo: Path) -> str:
    """QA-only synthetic fixture byte snapshot, including refs and index."""
    digest = sha256()
    for path in sorted(repo.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(repo)).encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def scenarios(output: Path, scratch: Path) -> list[Scenario]:
    env = {key: value for key, value in os.environ.items() if not key.startswith(("GIT_", "OMH_", "HERMES_"))}
    env.update(OMH_HOME=str(scratch / "omh"), HERMES_HOME=str(scratch / "hermes"),
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    (scratch / "omh").mkdir()
    (scratch / "hermes").mkdir()
    output.mkdir(parents=True, exist_ok=True)
    for name in ("repo", "clean"):
        repo = output / name
        repo.mkdir()
        git(repo, ["init", "-b", "main"], env)
        git(repo, ["config", "user.name", "Risk fixture"], env)
        git(repo, ["config", "user.email", "risk@example.invalid"], env)
        _ = (repo / "tracked.txt").write_text("original", encoding="utf-8")
        _ = (repo / ".env.example").write_text("template", encoding="utf-8")
        if name == "repo":
            _ = (repo / ".env").write_text("SYNTHETIC_SECRET_SENTINEL", encoding="utf-8")
        git(repo, ["add", "."], env)
        git(repo, ["commit", "-m", "fixture"], env)
        if name == "repo":
            _ = (repo / "tracked.txt").write_text("changed", encoding="utf-8")
            _ = (repo / "untracked.txt").write_text("synthetic", encoding="utf-8")
    promisor = output / "promisor"
    git(output / "clean", ["clone", "--local", "--no-hardlinks", str(output / "clean"), str(promisor)], env)
    git(promisor, ["config", "remote.origin.promisor", "true"], env)
    tree = subprocess.run(["git", "-C", str(promisor), "rev-parse", "HEAD^{tree}"],
                          env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=10).stdout.decode().strip()
    (promisor / ".git" / "objects" / tree[:2] / tree[2:]).unlink()
    fixtures = {
        "high.txt": 'Run git reset --hard; rm -rf "SYNTHETIC_PRIVATE_SENTINEL"; DROP TABLE customers;',
        "negated.txt": "Do not run git reset --hard; inspect status.",
        "large.txt": "a" * 65537,
    }
    for name, value in fixtures.items():
        _ = (output / name).write_text(value, encoding="utf-8")
    _ = (output / "invalid.txt").write_bytes(b"\xff")
    (output / "not-repo").mkdir()
    before = {name: snapshot(output / name) for name in ("repo", "clean", "promisor")}
    cases: tuple[tuple[str, list[str], bytes | None, int, str | None], ...] = (
        ("brief-only", ["--brief-file", str(output / "high.txt")], None, 0, "high_risk"),
        ("repo-only", ["--repo", str(output / "repo")], None, 0, "high_risk"),
        ("both-default", ["--brief-file", str(output / "high.txt"), "--repo", str(output / "repo")], None, 0, "high_risk"),
        ("both-strict", ["--brief-file", str(output / "high.txt"), "--repo", str(output / "repo"), "--strict"], None, 1, "high_risk"),
        ("negated-clean", ["--brief-file", str(output / "negated.txt"), "--repo", str(output / "clean"), "--strict"], None, 0, "clear"),
        ("stdin", ["--brief-stdin", "--strict"], b"git push --force-with-lease origin HEAD:main", 1, "high_risk"),
        ("oversized", ["--brief-file", str(output / "large.txt")], None, 2, None),
        ("stdin-oversized", ["--brief-stdin"], b"a" * 65537, 2, None),
        ("missing", ["--brief-file", str(output / "missing.txt")], None, 2, None),
        ("directory-brief", ["--brief-file", str(output / "repo")], None, 2, None),
        ("non-repo", ["--repo", str(output / "not-repo")], None, 2, None),
        ("missing-repo", ["--repo", str(output / "missing-repo")], None, 2, None),
        ("promisor-missing-tree", ["--repo", str(promisor)], None, 2, None),
        ("invalid-utf8", ["--brief-file", str(output / "invalid.txt")], None, 2, None),
        ("input-required", [], None, 2, None),
        ("protected-override", ["--brief-stdin", "--protected-branch", "production", "--strict"], b"git push origin main", 0, "clear"),
    )
    results: list[Scenario] = []
    for name, options, stdin, expected, verdict in cases:
        argv = [sys.executable, "-P", "-m", "omh.cli", "handoff-risk-scan", *options, "--json"]
        process = subprocess.run(argv, input=stdin, env=env, cwd=scratch,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        assert process.returncode == expected, (name, process.returncode, expected, process.stderr[:300])
        assert len(process.stdout) <= 16384, name
        report = decode_json(process.stdout.decode("utf-8"))
        assert isinstance(report, dict), name
        findings = report["findings"]
        counts = report["summary"]
        inputs = report["input_summary"]
        status = report["status"]
        assert isinstance(findings, list) and isinstance(counts, dict) and isinstance(inputs, dict), name
        assert isinstance(status, str), name
        assert report["schema_version"] == "handoff_risk_scan/v1", name
        assert report["verdict"] == verdict, (name, report)
        assert report["status"] == ("scan_error" if verdict is None else "completed"), name
        assert counts["total"] == len(findings), name
        assert b"SYNTHETIC_PRIVATE_SENTINEL" not in process.stdout, name
        assert b"SYNTHETIC_SECRET_SENTINEL" not in process.stdout, name
        ids: list[str] = []
        for item in findings:
            assert isinstance(item, dict) and isinstance(item["id"], str), name
            ids.append(item["id"])
            assert item["confidence"] == "high", name
            assert item["severity"] == ("medium" if item["id"] in ("dirty_worktree", "untracked_files") else "high"), name
        ids.sort()
        if name == "both-strict":
            assert ids == sorted(("destructive_git", "destructive_filesystem", "destructive_database",
                                  "protected_branch_write", "dirty_worktree", "untracked_files", "tracked_secret_path")), ids
        if name == "repo-only":
            assert inputs["tracked_count"] == 3
            assert inputs["dirty_count"] == 1
            assert inputs["untracked_count"] == 1
        _ = (output / f"{name}.result.json").write_bytes(process.stdout)
        results.append(Scenario(name=name, argv=argv, exit=process.returncode, status=status,
                                verdict=verdict, finding_ids=ids, output_sha256=sha256(process.stdout).hexdigest()))
    assert before == {name: snapshot(output / name) for name in before}, "repository_mutation"
    # Keep snapshot and command receipts alongside caller-owned synthetic fixtures.
    _ = (output / "receipts.json").write_text(json.dumps({"unchanged_repositories": before,
        "commands": [shlex.join(row["argv"]) for row in results]}, indent=2) + "\n", encoding="utf-8")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(namespace=Options())
    records: list[Scenario] = []
    failed: str | None = None
    scratch_path = None
    try:
        with TemporaryDirectory(prefix="omh-risk-qa-") as scratch:
            scratch_path = Path(scratch)
            records = scenarios(args.output_dir or scratch_path / "fixtures", scratch_path)
    except (AssertionError, OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        failed = f"{type(exc).__name__}: {str(exc)[:400]}"
    clean = scratch_path is not None and not scratch_path.exists()
    print(json.dumps({"issue": 1495, "scenarios": records, "scenario_count": len(records),
                      "error": failed, "cleanup": {"verified_absent": clean},
                      "repository_bytes_refs_index_unchanged": failed is None,
                      "live_provider_plugin_github": "not_invoked_not_required"}, separators=(",", ":")))
    return 0 if failed is None and clean and records else 1


if __name__ == "__main__":
    raise SystemExit(main())
