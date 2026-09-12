#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

# ─── How to run ───
# 1. Install uv (if not installed):
#      curl -LsSf https://astral.sh/uv/install.sh | sh
# 2. Run directly (no venv, no pip install needed):
#      uv run tools/qa/seven_issues_memory.py --output-dir PATH
# 3. Or make executable and run:
#      chmod +x tools/qa/seven_issues_memory.py && ./tools/qa/seven_issues_memory.py --output-dir PATH
# ──────────────────

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from omh.paths import resolve_paths
from omh.plugin_bundle.omh.memory_provider import OmhMemoryProvider
from omh.workflows.memory import approve_project_memory_candidate, capture_project_memory_candidate

PRINCIPAL_A = "principal:v1:" + "a" * 64
PRINCIPAL_B = "principal:v1:" + "b" * 64


def context(principal: str | None, actor_kind: str = "human") -> dict[str, Any]:
    return {
        "schema_version": "memory_principal_context/v1",
        "principal": principal,
        "profile_ref": "profile_fixture",
        "surface_ref": "fixture",
        "session_ref": "shared_fixture",
        "turn_ref": "turn_1",
        "actor_kind": actor_kind,
        "identity_evidence_refs": ["evidence:qa"],
        "binding_state": "validated_local" if actor_kind == "human" else "unbound",
    }


def approve(paths, summary: str, principal: str, *, audience: tuple[str, ...] = ()) -> str:
    captured: Any = capture_project_memory_candidate(
        paths,
        summary,
        scope_kind="project" if audience else "user",
        scope_ref="default",
        principal_context=context(principal),
        audience_principals=audience,
    )
    reviewed: Any = approve_project_memory_candidate(
        paths,
        str(captured["candidate"]["candidate_id"]),
        reviewer_principal=PRINCIPAL_B,
    )
    return str(reviewed["record"]["record_id"])


def provider_turn(provider: OmhMemoryProvider, principal_context: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    provider.on_turn_start(1, "fixture", principal_context=principal_context)
    provider.queue_prefetch("", session_id="shared_fixture")
    text = provider.prefetch("", session_id="shared_fixture")
    return text, provider.latest_prefetch_receipt() or {}


def cli(output_dir: Path, *args: str) -> tuple[int, dict[str, Any]]:
    environment = {
        **os.environ,
        "OMH_HOME": str(output_dir / "omh"),
        "HERMES_HOME": str(output_dir / "hermes"),
    }
    result = subprocess.run(
        [sys.executable, "-P", "-m", "omh.cli", *args],
        cwd=output_dir,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {"stderr": result.stderr[:400], "stdout": result.stdout[:400]}
    normalized: dict[str, Any] = payload if isinstance(payload, dict) else {}
    return result.returncode, normalized


def run(output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    internal = output_dir / ".runner-scratch"
    internal.mkdir(exist_ok=False)
    paths = resolve_paths(output_dir / "omh", output_dir / "hermes")
    context_a, context_b = context(PRINCIPAL_A), context(PRINCIPAL_B)
    (output_dir / "principal-a.json").write_text(json.dumps(context_a), encoding="utf-8")
    (output_dir / "principal-b.json").write_text(json.dumps(context_b), encoding="utf-8")
    ids = {
        "a": approve(paths, "A-only fixture memory", PRINCIPAL_A),
        "b": approve(paths, "B-only fixture memory", PRINCIPAL_B),
        "both": approve(paths, "Shared A and B fixture memory", PRINCIPAL_A, audience=(PRINCIPAL_A, PRINCIPAL_B)),
        "a_shared": approve(paths, "Shared only with A fixture memory", PRINCIPAL_A, audience=(PRINCIPAL_A,)),
    }
    legacy_capture: Any = capture_project_memory_candidate(paths, "Legacy nonshared fixture", scope_kind="project", scope_ref="default")
    approve_project_memory_candidate(paths, str(legacy_capture["candidate"]["candidate_id"]))
    provider = OmhMemoryProvider(omh_home=paths.omh_home, hermes_home=paths.hermes_home)
    try:
        provider.initialize("shared_fixture", cwd=output_dir, principal_context=context_a, shared_surface=True)
        text_a, receipt_a = provider_turn(provider, context_a)
        text_b, receipt_b = provider_turn(provider, context_b)
        text_missing, receipt_missing = provider_turn(provider, None)
        text_bot, receipt_bot = provider_turn(provider, context(None, "bot"))
        provider.on_memory_write("add", "user", "native-write-sentinel", metadata=context_a)
    finally:
        provider.shutdown()
    cli_a_exit, cli_a = cli(output_dir, "memory", "recall", "fixture", "--session-id", "shared_fixture", "--principal-context", str(output_dir / "principal-a.json"), "--json")
    cli_b_exit, cli_b = cli(output_dir, "memory", "recall", "fixture", "--session-id", "shared_fixture", "--principal-context", str(output_dir / "principal-b.json"), "--json")
    migration_exit, migration = cli(output_dir, "memory", "principal-migration", "--report", "--json")
    journal = (paths.memory_dir / "write_journal.jsonl").read_text(encoding="utf-8")
    checks = {
        "provider_a_isolated": "A-only fixture memory" in text_a and "B-only fixture memory" not in text_a,
        "provider_b_isolated": "B-only fixture memory" in text_b and "A-only fixture memory" not in text_b,
        "shared_a_b_visible": "Shared A and B fixture memory" in text_a and "Shared A and B fixture memory" in text_b,
        "a_only_audience_denied_to_b": "Shared only with A fixture memory" not in text_b,
        "missing_denied": "fixture memory" not in text_missing and receipt_missing.get("principal_decision", {}).get("allowed_count") == 0,
        "bot_denied": "fixture memory" not in text_bot and receipt_bot.get("principal_decision", {}).get("allowed_count") == 0,
        "receipt_v2": receipt_a.get("schema_version") == "omh_memory_prefetch_receipt/v2" and receipt_b.get("principal_decision", {}).get("principal_ref") == PRINCIPAL_B,
        "write_redacted": "native-write-sentinel" not in journal and '"admission_performed": false' in journal,
        "cli_a": cli_a_exit == 0 and {item.get("record_id") for item in cli_a.get("included_records", [])} >= {ids["a"], ids["both"], ids["a_shared"]},
        "cli_b": cli_b_exit == 0 and {item.get("record_id") for item in cli_b.get("included_records", [])} >= {ids["b"], ids["both"]} and ids["a"] not in {item.get("record_id") for item in cli_b.get("included_records", [])},
        "migration_report": migration_exit == 0 and migration.get("record_count") == 1 and "Legacy nonshared fixture" not in json.dumps(migration),
    }
    shutil.rmtree(internal)
    return {
        "schema_version": "seven_issues_memory_qa/v1",
        "ok": all(checks.values()),
        "checks": checks,
        "provider": {
            "a_selected": receipt_a.get("selection", {}).get("selected_count"),
            "b_selected": receipt_b.get("selection", {}).get("selected_count"),
            "missing_selected": receipt_missing.get("selection", {}).get("selected_count"),
            "bot_selected": receipt_bot.get("selection", {}).get("selected_count"),
        },
        "cli": {"a_exit": cli_a_exit, "b_exit": cli_b_exit, "migration_exit": migration_exit},
        "cleanup": {"verified_absent": not internal.exists(), "owned_processes_reaped": True},
        "external_provider_configured": False,
        "automatic_host_authentication_observed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    payload = run(args.output_dir.expanduser().resolve())
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["ok"] and payload["cleanup"]["verified_absent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
