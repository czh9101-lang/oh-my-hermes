#!/usr/bin/env python3
"""Isolated real-host benchmark for promoted browser workflow resources.

The promoted arm reads and validates the installed entry, manifest, procedure,
and trace before translating the trace's exact semantic contract into browser
commands.  The ordinary arm receives only the test's declared contract and a
fresh native DOM snapshot; it never reads promoted resources.
"""
from __future__ import annotations

import hashlib
from importlib import import_module
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "benchmarks" / "browser-skill-promotion" / "result.json"
CORPUS = ("stable", "missing_locator", "ambiguous_locator", "schema_change")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _run(command: list[str], *, timeout: int = 30) -> dict[str, Any]:
    started = time.perf_counter()
    completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout)
    try:
        payload: object = json.loads(completed.stdout) if completed.stdout else None
    except json.JSONDecodeError:
        payload = None
    return {
        "argv": command[4:] if len(command) >= 4 and command[:2] == ["agent-browser", "--session"] and command[3] == "--json" else command[1:],
        "returncode": completed.returncode,
        "success": isinstance(payload, dict) and payload.get("success") is True,
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        "payload": payload,
        "stderr": completed.stderr[-512:],
    }


def _browser_session(url: str, contract: dict[str, Any], *, label: str) -> dict[str, Any]:
    """Use one snapshot then at most one exact-id action; close is measured too."""
    session = f"omh-{uuid4().hex[:16]}"
    commands: list[dict[str, Any]] = []
    target = contract["locator"]
    _require(isinstance(target, dict) and target.get("kind") == "role", "benchmark supports only role locators")
    role, name = target.get("role"), target.get("name")
    _require(isinstance(role, str) and isinstance(name, str), "invalid role locator")
    snapshot_js = (
        "JSON.stringify(Array.from(document.querySelectorAll('button,[role=button]')).map((el,index)=>"
        "({index,role:el.getAttribute('role')||el.tagName.toLowerCase(),name:(el.getAttribute('aria-label')||el.textContent||'').trim(),id:el.id})))"
    )
    started = time.perf_counter()
    outcome: dict[str, Any] | None = None
    try:
        opened = _run(["agent-browser", "--session", session, "--json", "open", url]); commands.append(opened)
        if not opened["success"]:
            outcome = _outcome(commands, started, "browser_open_failed", False, None, contract)
            return outcome
        snapshot = _run(["agent-browser", "--session", session, "--json", "eval", snapshot_js]); commands.append(snapshot)
        if not snapshot["success"]:
            outcome = _outcome(commands, started, "native_snapshot_failed", False, None, contract)
            return outcome
        data = snapshot["payload"]
        result = data.get("data", {}).get("result") if isinstance(data, dict) and isinstance(data.get("data"), dict) else None
        try:
            nodes = json.loads(result) if isinstance(result, str) else None
        except json.JSONDecodeError:
            nodes = None
        if not isinstance(nodes, list):
            outcome = _outcome(commands, started, "native_snapshot_invalid", False, None, contract)
            return outcome
        matches = [node for node in nodes if isinstance(node, dict) and node.get("role") == role and node.get("name") == name and isinstance(node.get("id"), str) and node["id"]]
        if len(matches) != 1:
            outcome = _outcome(commands, started, "zero_locator" if not matches else "ambiguous_locator", False, None, contract)
            return outcome
        identifier = matches[0]["id"]
        # identifier came only from the current snapshot after exact role/name matching.
        action_js = "const el=document.getElementById(" + json.dumps(identifier) + ");if(!el)throw new Error('snapshot_target_missing');el.click();JSON.stringify(window.__omhOutput||null)"
        action = _run(["agent-browser", "--session", session, "--json", "eval", action_js]); commands.append(action)
        if not action["success"]:
            outcome = _outcome(commands, started, "browser_action_failed", False, None, contract)
            return outcome
        data = action["payload"]
        output_raw = data.get("data", {}).get("result") if isinstance(data, dict) and isinstance(data.get("data"), dict) else None
        try:
            output = json.loads(output_raw) if isinstance(output_raw, str) else None
        except json.JSONDecodeError:
            output = None
        expected_fields = contract["output_fields"]
        valid_output = isinstance(output, dict) and sorted(output) == expected_fields
        outcome = _outcome(commands, started, "matched" if valid_output else "schema_mismatch", valid_output, output, contract)
        return outcome
    finally:
        closed = _run(["agent-browser", "--session", session, "--json", "close"])
        commands.append(closed)
        _require(bool(closed["success"]), f"agent-browser session close was not observed: {closed!r}")
        if outcome is not None:
            outcome["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)


def _outcome(commands: list[dict[str, Any]], started: float, reason: str, correct: bool, output: object, contract: dict[str, Any]) -> dict[str, Any]:
    # close is appended by finally; its count is filled after the finally return.
    return {"reason": reason, "correct": correct, "output": output, "contract": contract, "latency_ms": round((time.perf_counter() - started) * 1000, 3), "commands": commands}


def _finalize_session(result: dict[str, Any]) -> dict[str, Any]:
    commands = result["commands"]
    if not isinstance(commands, list) or not commands or not isinstance(commands[-1], dict) or commands[-1].get("argv") != ["close"]:
        raise RuntimeError("session close was not included in command evidence")
    snapshot_bytes = 0
    for command in commands:
        argv = command.get("argv", [])
        if len(argv) == 2 and argv[0] == "eval" and "document.querySelectorAll" in argv[1]:
            payload = command.get("payload")
            if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                snapshot = payload["data"].get("result")
                if isinstance(snapshot, str):
                    snapshot_bytes += len(snapshot.encode("utf-8"))
    return {
        **result,
        "browser_commands": len(commands),
        "command_successes": sum(item.get("success") is True for item in commands if isinstance(item, dict)),
        "supplied_context_utf8_bytes": len(_canonical(result["contract"])) + snapshot_bytes,
        "context_basis": "Declared semantic contract plus native DOM snapshot; not model tokens.",
    }


def _fixture(identifier: str, kind: str, nodes: list[dict[str, str]], output_fields: list[str], origin: str) -> dict[str, Any]:
    value: dict[str, Any] = {"fixture_id": identifier, "kind": kind, "origin": origin, "nodes": nodes, "output_fields": output_fields}
    value["digest"] = _sha(value)
    return value


def _trace(origin: str, source: dict[str, Any], *, action: str = "click") -> dict[str, Any]:
    target = [{"role": "button", "name": "Continue"}]
    fixtures = [
        _fixture("ambiguous", "negative", [*target, *target], ["confirmation"], origin),
        _fixture("missing", "negative", [], ["confirmation"], origin),
        _fixture("positive", "positive", target, ["confirmation"], origin),
        _fixture("schema", "negative", target, ["changed"], origin),
        _fixture("transient", "transient", target, ["confirmation"], origin),
    ]
    return {
        "schema_version": "browser_workflow_trace/v1", "project": {"identity": "0" * 64}, "origins": [origin],
        "adapter_version": source["binding"]["adapter_version"], "parser_version": "benchmark-parser/v1", "source": source,
        "steps": [{"action": action, "locators": [{"kind": "role", "role": "button", "name": "Continue"}]}],
        "output_schema": {"kind": "object", "fields": ["confirmation"]}, "fixtures": fixtures, "metadata": {},
    }


def _parse_entry(entry: str) -> dict[str, Any]:
    line = next((line for line in entry.splitlines() if line.startswith("omh_browser_promotion: ")), None)
    if line is None:
        raise RuntimeError("installed SKILL.md has no promotion metadata")
    value = json.loads(line.removeprefix("omh_browser_promotion: "))
    _require(isinstance(value, dict) and value.get("schema_version") == "browser_skill_entry/v1", "installed metadata is invalid")
    return value


def _load_promoted_contract(api: Any, project: Path, skill: str) -> tuple[dict[str, Any], dict[str, Any]]:
    status = api.browser_skill_promotion_status(project, skill)
    _require(status.get("status") == "active", f"{skill} is not active before live use")
    target = project / ".hermes" / "skills" / skill
    package_reader: Any = import_module("omh.workflows.browser_skill_promotion_plan").read_browser_skill_package
    package = package_reader(target)
    _require(isinstance(package, dict) and isinstance(package.get("SKILL.md"), str), "installed package is unreadable")
    entry = package["SKILL.md"]
    metadata = _parse_entry(entry)
    resources = metadata.get("resources")
    if not isinstance(resources, dict) or set(resources) != {"manifest", "procedure", "trace"}:
        raise RuntimeError("installed resource pointers are invalid")
    manifest_path, procedure_path, trace_path = (resources["manifest"], resources["procedure"], resources["trace"])
    if not all(isinstance(name, str) and name in package for name in (manifest_path, procedure_path, trace_path)):
        raise RuntimeError("installed immutable resource is missing")
    manifest = json.loads(package[manifest_path])
    _require(isinstance(manifest, dict) and manifest.get("schema_version") == "browser_skill_resource_manifest/v1", "installed manifest is invalid")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != {f"resources/{metadata['generation']}/entry.md", procedure_path, trace_path}:
        raise RuntimeError("installed manifest file set is invalid")
    for relative, digest in files.items():
        _require(isinstance(relative, str) and isinstance(digest, str) and _DIGEST.fullmatch(digest) is not None and relative in package and _sha_bytes(package[relative].encode("utf-8")) == digest, "installed immutable resource digest mismatch")
    procedure = package[procedure_path]
    trace = json.loads(package[trace_path])
    _require(isinstance(trace, dict) and trace.get("digest") == metadata.get("trace_digest") and trace.get("trace_id") == metadata.get("trace_id"), "installed trace does not bind entry metadata")
    schema = trace.get("output_schema")
    if not isinstance(schema, dict) or _sha(schema) != metadata.get("output_schema_digest"):
        raise RuntimeError("installed output contract does not bind entry metadata")
    _require(isinstance(procedure, str) and str(metadata["trace_digest"]) in procedure and json.dumps(schema, sort_keys=True) in procedure, "installed procedure does not bind trace contract")
    steps = trace.get("steps")
    if not isinstance(steps, list) or len(steps) != 1 or not isinstance(steps[0], dict):
        raise RuntimeError("benchmark requires one installed procedure step")
    step = steps[0]
    locators = step.get("locators")
    if step.get("action") != "click" or not isinstance(locators, list) or len(locators) != 1 or not isinstance(locators[0], dict):
        raise RuntimeError("benchmark requires one exact installed role click")
    fields = schema.get("fields")
    _require(isinstance(fields, list) and all(isinstance(field, str) for field in fields) and fields == sorted(fields), "installed output schema is invalid")
    loaded = {"SKILL.md": len(entry.encode("utf-8")), str(manifest_path): len(package[manifest_path].encode("utf-8")), str(procedure_path): len(procedure.encode("utf-8")), str(trace_path): len(package[trace_path].encode("utf-8"))}
    return {"action": "click", "locator": locators[0], "output_fields": fields}, {"status": status["status"], "generation": metadata["generation"], "loaded_utf8_bytes": loaded, "total_utf8_bytes": sum(loaded.values()), "trace_id": metadata["trace_id"]}


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _approve(api: Any, root: Path, trace_id: str, skill: str) -> tuple[dict[str, Any], dict[str, Any]]:
    review = api.review_browser_skill_lifecycle(root, trace_id, skill)
    preflight = review.get("native_preflight")
    _require(isinstance(preflight, dict), "native preflight was absent")
    policy = preflight.get("policy")
    _require(preflight.get("trusted") is True and preflight.get("structure_error") is None and preflight.get("lint_errors") in ([], ()) and preflight.get("security_verdict") == "safe", f"native trust, scan, or lint preflight failed: {preflight!r}")
    _require(isinstance(policy, dict) and policy.get("requirement") == "not_required" and policy.get("approval") == "not_applicable" and policy.get("support") == "available", "native write_approval=false was not observed")
    receipt = api.approve_browser_skill_lifecycle(root, trace_id, skill, reviewed_diff_digest=review["plan"]["diff_digest"], reviewer_identity="localhost-benchmark")
    return receipt, preflight


def _native_source(url: str, git_revision: str, adapter_revision: str) -> tuple[dict[str, Any], dict[str, Any]]:
    probe = _finalize_session(_browser_session(url, {"locator": {"kind": "role", "role": "button", "name": "Continue"}, "output_fields": ["confirmation"]}, label="source"))
    _require(probe["correct"] is True, "successful native host output could not be captured")
    environment = {"python": sys.version.split()[0], "platform": sys.platform, "url": url, "browser": adapter_revision}
    evidence = {"commands": probe["commands"], "outcome": probe["reason"], "output": probe["output"]}
    source = {
        "run_ref": f"native-{_sha(evidence)[:20]}", "evidence_ref": "localhost-" + _sha({"url": url, "revision": git_revision})[:20], "selected": True,
        "success": {"state": "success", "evidence_digest": _sha(evidence)},
        "binding": {"project_identity": "0" * 64, "origin": url.rsplit("/", 1)[0], "adapter_version": adapter_revision},
        "lineage": {"source_digest": _sha({"git_revision": git_revision, "evidence": evidence}), "environment_digest": _sha(environment), "adapter_digest": _sha({"agent_browser_version": adapter_revision})},
    }
    return source, {"git_revision": git_revision, "adapter_revision": adapter_revision, "environment": environment, "capture": probe}


def main() -> int:
    # These isolated homes are established before importing OMH or Hermes.
    with tempfile.TemporaryDirectory(prefix="omh-browser-promotion-") as raw:
        temp = Path(raw); project = temp / "project"; home = temp / "hermes-home"; managed = temp / "managed-home"
        project.mkdir(); home.mkdir(); managed.mkdir()
        os.environ["HERMES_HOME"] = str(home); os.environ["HERMES_MANAGED_DIR"] = str(managed); os.environ.pop("TERMINAL_CWD", None)
        (home / "config.yaml").write_text("skills:\n  project_discovery: true\n  trusted_project_dirs:\n    - " + json.dumps(str(project)) + "\n  write_approval: false\n", encoding="utf-8")
        pages = {
            "stable.html": "<button id='continue' onclick=\"window.__omhOutput={confirmation:'confirmed'}\">Continue</button>",
            "missing_locator.html": "<button id='different' onclick=\"window.__omhOutput={confirmation:'wrong'}\">Different target</button>",
            "ambiguous_locator.html": "<button id='one'>Continue</button><button id='two'>Continue</button>",
            "schema_change.html": "<button id='continue' onclick=\"window.__omhOutput={changed:'confirmed'}\">Continue</button>",
        }
        for name, html in pages.items(): (project / name).write_text("<!doctype html>" + html, encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=project, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.email=proof@example.test", "-c", "user.name=proof", "commit", "-qm", "localhost-fixtures"], cwd=project, check=True, capture_output=True)
        git_revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project, check=True, capture_output=True, text=True).stdout.strip()
        adapter_version = subprocess.run(["agent-browser", "--version"], check=True, capture_output=True, text=True).stdout.strip()
        _require(bool(adapter_version), "agent-browser did not report a version")
        adapter_revision = f"agent-browser/v{adapter_version.removeprefix('agent-browser ').strip()}"
        sys.path.insert(0, str(ROOT / "src"))
        api: Any = import_module("omh.workflows.browser_skill_promotion")
        trace_store: Any = import_module("omh.workflows.browser_workflow_learning_store")
        class Quiet(SimpleHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None: return
        server = ThreadingHTTPServer(("127.0.0.1", 0), lambda *args: Quiet(*args, directory=project)); thread = Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            origin = f"http://127.0.0.1:{server.server_port}"
            ordinary_contract = {"action": "click", "locator": {"kind": "role", "role": "button", "name": "Continue"}, "output_fields": ["confirmation"]}
            expected = {"stable": "matched", "missing_locator": "zero_locator", "ambiguous_locator": "ambiguous_locator", "schema_change": "schema_mismatch"}
            cases: dict[str, dict[str, Any]] = {}; native_preflights: dict[str, Any] = {}; activations: dict[str, dict[str, Any]] = {}; host_sources: dict[str, Any] = {}
            for case in CORPUS:
                source, source_evidence = _native_source(origin + "/stable.html", git_revision, adapter_revision)
                host_sources[case] = source_evidence
                trace = trace_store.write_browser_workflow_trace(_trace(origin, source), project)
                trace_store.approve_browser_workflow_trace(project, str(trace["trace_id"]), str(trace["digest"]))
                replay = trace_store.replay_stored_browser_workflow_trace(project, str(trace["trace_id"]), {"fixture_id": "positive"})
                _require(replay["status"] == "replayed", f"{case} trace did not receive an offline positive replay")
                skill = f"localhost-{case.replace('_', '-')}-workflow"
                receipt, preflight = _approve(api, project, str(trace["trace_id"]), skill)
                activation = api.promote_approved_browser_skill(project, str(receipt["receipt_id"]))
                _require(activation.get("status") == "active", f"{case} promotion was not active")
                load_started = time.perf_counter()
                contract, loaded = _load_promoted_contract(api, project, skill)
                resource_load_ms = (time.perf_counter() - load_started) * 1000
                ordinary = _finalize_session(_browser_session(origin + f"/{case}.html", ordinary_contract, label=f"ordinary-{case}"))
                promoted = _finalize_session(_browser_session(origin + f"/{case}.html", contract, label=f"promoted-{case}"))
                promoted["resource_load_ms"] = round(resource_load_ms, 3)
                promoted["end_to_end_latency_ms"] = round(resource_load_ms + promoted["latency_ms"], 3)
                promoted["supplied_context_utf8_bytes"] += loaded["total_utf8_bytes"]
                promoted["context_basis"] = "Installed resource bytes, derived semantic contract and native DOM snapshot; not model tokens."
                _require(ordinary["reason"] == expected[case] and promoted["reason"] == expected[case], f"{case} safety outcome differed from its declared corpus result")
                _require(ordinary["correct"] is promoted["correct"], f"{case} arms disagreed on correctness")
                cases[case] = {"expected_outcome": expected[case], "ordinary_fallback": ordinary, "promoted_resource_path": {**promoted, "installed": loaded}, "trace": {"trace_id": trace["trace_id"], "trace_digest": trace["digest"], "replay": replay["status"]}, "receipt_id": receipt["receipt_id"]}
                native_preflights[case] = preflight; activations[case] = activation
            stable_case = cases["stable"]
            repeat = api.promote_approved_browser_skill(project, str(stable_case["receipt_id"]))
            _require(repeat.get("reused") is True, "repeat promotion was not reused")
            drift_source, drift_source_evidence = _native_source(origin + "/stable.html", git_revision, adapter_revision)
            host_sources["drift"] = drift_source_evidence
            drift_trace = trace_store.write_browser_workflow_trace(_trace(origin, drift_source), project)
            trace_store.approve_browser_workflow_trace(project, str(drift_trace["trace_id"]), str(drift_trace["digest"]))
            trace_store.replay_stored_browser_workflow_trace(project, str(drift_trace["trace_id"]), {"fixture_id": "positive"})
            drift_receipt, drift_preflight = _approve(api, project, str(drift_trace["trace_id"]), "localhost-drift-workflow")
            api.promote_approved_browser_skill(project, str(drift_receipt["receipt_id"]))
            drift_replay = trace_store.replay_stored_browser_workflow_trace(project, str(drift_trace["trace_id"]), {"fixture_id": "missing"})
            drift_status = api.browser_skill_promotion_status(project, "localhost-drift-workflow")
            _require(drift_replay["status"] == "stale" and drift_status.get("status") == "stale", "trace drift did not deactivate its independent skill")
            first_activation = activations["stable"]
            rollback_review = api.review_browser_skill_rollback(project, "localhost-stable-workflow", str(first_activation["generation"]))
            rollback_receipt = api.approve_browser_skill_rollback(project, "localhost-stable-workflow", str(first_activation["generation"]), reviewed_diff_digest=rollback_review["plan"]["diff_digest"], reviewer_identity="localhost-benchmark")
            rollback = api.promote_approved_browser_skill(project, str(rollback_receipt["receipt_id"]))
            _require(rollback.get("status") == "rolled_back", "approved rollback did not complete")
            removal_review = api.review_browser_skill_removal(project, "localhost-stable-workflow")
            removal_receipt = api.approve_browser_skill_removal(project, "localhost-stable-workflow", reviewed_diff_digest=removal_review["plan"]["diff_digest"], reviewer_identity="localhost-benchmark")
            removal = api.promote_approved_browser_skill(project, str(removal_receipt["receipt_id"]))
            _require(removal.get("status") == "removed", "approved removal did not complete")
            correct = sum(value["promoted_resource_path"]["reason"] == value["expected_outcome"] for value in cases.values())
            live_effect_matches = sum(bool(value["promoted_resource_path"]["correct"]) for value in cases.values())
            false_successes = sum(value["expected_outcome"] != "matched" and value["promoted_resource_path"]["correct"] is True for value in cases.values())
            result = {"schema_version": "browser_skill_promotion_benchmark/v2", "status": "PASS", "scope": "isolated localhost benchmark; no model invocation or production-site claim", "corpus": list(CORPUS), "host_sources": host_sources, "native_preflights": native_preflights, "cases": cases, "metrics": {"correct_promoted_cases": correct, "live_effect_matches": live_effect_matches, "total_cases": len(CORPUS), "false_successes": false_successes, "model_context_utf8_bytes": None, "model_context_null_reason": "no model prompt or context surface was invoked"}, "lifecycle": {"repeat_reused": repeat["reused"], "rollback": rollback["status"], "removal": removal["status"], "drift_replay": drift_replay["status"], "drift_status": drift_status["status"], "drift_native_preflight": drift_preflight}}
            _require(correct == len(CORPUS) and live_effect_matches == 1 and false_successes == 0, "corpus correctness aggregate failed")
            OUT.parent.mkdir(parents=True, exist_ok=True); OUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(json.dumps(result, sort_keys=True)); return 0
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
