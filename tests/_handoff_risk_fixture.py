"""Shared real CLI and repository fixtures for the advisory scan contract."""
from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path
import subprocess
import tempfile

from _cli_harness import run_cli
from omh.quality.handoff_risk_model import ScanReport

# The CLI owns this typed JSON output; tests assert its machine fields below.
decode_report: Callable[[str], ScanReport] = json.loads


def invoke(args: list[str], stdin: str = "") -> tuple[int, ScanReport]:
    code, output, _ = run_cli(["handoff-risk-scan", *args, "--json"], stdin_text=stdin)
    return code, decode_report(output)


def scan(brief: str, options: list[str] | None = None) -> ScanReport:
    with tempfile.TemporaryDirectory() as root:
        path = Path(root) / "brief.txt"
        _ = path.write_text(brief, encoding="utf-8")
        return invoke(["--brief-file", str(path), *(options or [])])[1]


def git(repo: Path, *args: str) -> bytes:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.run(
        ["git", "-C", str(repo), *args], env=env, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
    ).stdout


def repository(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _ = git(repo, "init", "-b", "main")
    _ = git(repo, "config", "user.name", "Fixture")
    _ = git(repo, "config", "user.email", "fixture@example.invalid")
    _ = (repo / "tracked.txt").write_text("original", encoding="utf-8")
    _ = git(repo, "add", "tracked.txt")
    _ = git(repo, "commit", "-m", "fixture")
    return repo
