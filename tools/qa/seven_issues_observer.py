#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# How to run: install uv, then from the checkout:
# uv run python tools/qa/seven_issues_observer.py --scenario normalized-lifecycle
# Or chmod +x tools/qa/seven_issues_observer.py and run directly.
"""Real local engine/CLI and installed-host registry scenarios, never a live agent."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import TypeAlias

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
JSON: TypeAlias = str | int | bool | None | list["JSON"] | dict[str, "JSON"]


def cli(arguments: list[str], homes: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, OMH_HOME=str(homes / "omh"), HERMES_HOME=str(homes / "hermes"), PYTHONPATH=str(ROOT))
    return subprocess.run([sys.executable, "-P", "-m", "omh.cli", *arguments],
                          env=env, cwd=homes, text=True, capture_output=True, timeout=60)


def normalized(homes: Path) -> dict[str, JSON]:
    from omh.plugin_bundle.omh.activity_observer import ActivityObserver, opaque_ref
    from omh.system.paths import OmhPaths
    from omh.workflows.session_activity_receipts import SESSION_ACTIVITY_CONSUMERS, session_activity_evidence
    paths = OmhPaths(homes / "omh", homes / "hermes")
    ref = lambda identity: opaque_ref(b"isolated-qa-profile-key", identity)
    observer = ActivityObserver(paths, ref("profile"))
    kinds = ("session_start", "member_start", "tool_call", "tool_call", "tool_error", "compaction", "member_complete", "session_end")
    try:
        for _ in range(2):  # A complete replay must not create another final.
            for sequence, kind in enumerate(kinds):
                boundary = kind in {"session_start", "session_end"}
                assert observer.enqueue({"schema": "omh_group_activity_event/v1", "profile_ref": ref("profile"),
                    "session_ref": ref("session"), "room_ref": ref("room"),
                    "member_ref": None if boundary else ref("member"), "turn_ref": None if boundary else ref("turn"),
                    "kind": kind, "event_ref": ref(str(sequence)), "sequence": sequence,
                    "observed_at": f"2026-09-12T00:00:0{sequence}Z"})
            assert observer.flush()
        listing = cli(["runtime", "session-receipt", "list", "--json"], homes)
        assert listing.returncode == 0, listing.stderr
        payload = json.loads(listing.stdout)
        assert len(payload["receipts"]) == 1
        receipt = payload["receipts"][0]
        assert receipt["boundary"] == {"kind": "session_end", "final": True, "sequence": 7}
        expected = {"tool_calls": 2, "tool_errors": 1, "compaction_boundaries": 1}
        assert {name: receipt["metrics"][name]["value"] for name in expected} == expected
        assert all(reading["value"] is None for name, reading in receipt["metrics"].items() if name not in expected)
        projections = [session_activity_evidence(receipt, consumer) for consumer in SESSION_ACTIVITY_CONSUMERS]
        assert len(projections) == 6 and all(p["terminal"] for p in projections)
        # Exercise manual admission through the same actual CLI, not a separate writer.
        fixture = homes / "receipt.json"
        fixture.write_text(json.dumps(receipt), encoding="utf-8")
        manual = cli(["runtime", "session-receipt", "ingest", "--input", str(fixture)], homes)
        assert manual.returncode == 0 and json.loads(manual.stdout)["outcome"] == "already_recorded"
        doctor = cli(["doctor", "--json"], homes)
        doctor_payload = json.loads(doctor.stdout)
        check = next(c for c in doctor_payload["checks"] if c["name"] == "group_chat_activity")
        assert check["ok"] and not check["observed"]
        return {"scenario": "normalized-lifecycle", "host_collection": "not_observed",
                "cli_command": "python -P -m omh.cli runtime session-receipt list --json",
                "cli_exit": listing.returncode, "cli_output": payload,
                "manual_ingest_exit": manual.returncode, "manual_outcome": json.loads(manual.stdout)["outcome"],
                "doctor_exit": doctor.returncode, "doctor_check": check,
                "doctor_stdout_sha256": hashlib.sha256(doctor.stdout.encode()).hexdigest(),
                "observer": {"last_outcome": observer.status()["last_outcome"],
                             "dropped": observer.status()["dropped"], "write_failed": observer.status()["write_failed"]},
                "consumer_count": len(projections)}
    finally:
        assert observer.close(), "observer drain failed"


def host_child(host: Path, homes: Path, enabled: bool) -> dict[str, JSON]:
    # Environment is set before importing host modules. No live profile is consulted.
    os.environ["HERMES_HOME"] = str(homes / "hermes")
    os.environ["OMH_HOME"] = str(homes / "omh")
    os.environ["HOME"] = str(homes)
    (homes / "hermes").mkdir()
    (homes / "hermes" / "config.yaml").write_text(
        "plugins:\n  entries:\n    omh:\n      settings:\n        group_chat_activity:\n          enabled: "
        + ("true" if enabled else "false") + "\n", encoding="utf-8")
    sys.path.insert(0, str(host))
    # Host modules are optional and exist only in the explicitly named host interpreter.
    host_plugins = importlib.import_module("hermes_cli.plugins")
    registry = importlib.import_module("tools.registry").registry
    bundle = ROOT / "src/plugin_bundle/omh"
    spec = importlib.util.spec_from_file_location("_qa_omh_bundle", bundle / "__init__.py", submodule_search_locations=[str(bundle)])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    manager = host_plugins.PluginManager(scope_key=str(homes / "hermes"))
    ctx = host_plugins.PluginContext(host_plugins.PluginManifest(name="omh", path=bundle), manager)
    try:
        module.register(ctx)
        assert ctx.get_config("group_chat_activity", None) == {"enabled": enabled}
        hooks = sorted(manager._hooks)
        tools = sorted(manager._plugin_tool_names)
        assert "on_session_end" in hooks and "omh_status" in tools and "omh_memory" in tools
        assert not any("member" in name for name in hooks)
        assert ctx._memory_provider.name == "omh"
        status_entry = registry.get_entry("omh_status", scope=manager.scope_key)
        assert status_entry is not None
        status = json.loads(status_entry.handler({}))["group_chat_activity"]
        assert status["readiness"] == ("unavailable" if enabled else "disabled")
        assert status["compatibility"] == ("member_activity_contract_unsupported" if enabled else "not_observed")
        assert not any(name.startswith(spec.name + ".activity_observer") for name in sys.modules)
        hook_results = [callback(omh_home=str(homes / "omh"), host="hermes-agent", session_id="qa-synthetic-session")
                        for callback in manager._hooks["on_session_end"]]
        assert not list((homes / "omh").rglob("*activity*"))
        return {"enabled": enabled, "status": status, "registered_hooks": hooks,
                "registered_tools": tools, "provider": ctx._memory_provider.name,
                "on_session_end_results": [result.get("status") if result else None for result in hook_results],
                "valid_member_hooks": sorted(name for name in host_plugins.VALID_HOOKS if "member" in name),
                "receipt_count": 0, "host_collection": "not_observed"}
    finally:
        ctx._memory_provider.shutdown()
        manager.unload("omh")
        assert not any(handle.active for handle in manager._registration_order)


def installed_host(host: Path) -> dict[str, JSON]:
    python = host / "venv/bin/python"
    if not python.is_file():
        return {"unavailable": "host_interpreter_missing", "exit": 3}
    results: list[JSON] = []
    for enabled in (False, True):
        command = [str(python), str(Path(__file__).resolve()), "--scenario", "installed-host-compatibility",
                   "--host-source", str(host), "--host-child", "enabled" if enabled else "disabled"]
        result = subprocess.run(command, cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1"),
                                text=True, capture_output=True, timeout=60)
        assert result.returncode == 0, result.stderr
        results.append(json.loads(result.stdout))
    revision = subprocess.run(["git", "-C", str(host), "rev-parse", "HEAD"],
                              text=True, capture_output=True, check=True, timeout=10).stdout.strip()
    return {"scenario": "installed-host-compatibility", "host_revision": revision,
            "host_interpreter": str(python), "results": results, "live_gate": "unavailable", "exit": 0}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True, choices=("normalized-lifecycle", "installed-host-compatibility", "supported-host-lifecycle"))
    parser.add_argument("--host-source", type=Path)
    parser.add_argument("--host-python", type=Path, help="Interpreter with host dependencies for a disposable upstream source fixture.")
    parser.add_argument("--host-child", choices=("enabled", "disabled"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.scenario == "supported-host-lifecycle":
        if args.host_source is None:
            print(json.dumps({"scenario": args.scenario, "exit": 3, "reason": "host_source_required",
                              "host_collection": "not_observed", "cleanup": {"verified_absent": True}}))
            return 3
        python = args.host_python or args.host_source / "venv/bin/python"
        result = subprocess.run([sys.executable, str(ROOT / "tools/qa/seven_issues_observer_native.py"),
                                 "--host-source", str(args.host_source), "--host-python", str(python)],
                                text=True, capture_output=True, timeout=120)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.returncode
    with TemporaryDirectory(prefix="omh-observer-qa-") as tmp:
        homes = Path(tmp)
        if args.host_child:
            assert args.host_source is not None
            result = host_child(args.host_source.resolve(), homes, args.host_child == "enabled")
        elif args.scenario == "normalized-lifecycle":
            result = normalized(homes)
        else:
            assert args.host_source is not None, "--host-source is required"
            result = installed_host(args.host_source.resolve())
    assert not homes.exists(), "invocation-owned scratch cleanup failed"
    result["cleanup"] = {"verified_absent": True, "workers_reaped": True, "registrations_disposed": True}
    print(json.dumps(result, sort_keys=True))
    return 3 if result.get("exit") == 3 else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"exit": 1, "failure": type(exc).__name__}), file=sys.stderr)
        raise
