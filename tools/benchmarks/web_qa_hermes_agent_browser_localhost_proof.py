#!/usr/bin/env python3
"""Live localhost proof for the native web-QA collector.

This intentionally has no visual reviewer.  It proves a real, consumer-admissible
receipt with a named missing-review BLOCK rather than fabricating a score.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import uuid

ROOT = Path(__file__).parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "plugin_bundle")]
from omh.workflows.web_qa_observation import build_web_qa_observation
from omh.workflows.web_qa_observation_plan import build_web_qa_observation_plan


def _native_environment(url: str) -> dict[str, object]:
    """Read the installed browser's actual environment, then close it."""
    session = f"omh-qa-proof-probe-{uuid.uuid4().hex}"
    script = "JSON.stringify({ua:navigator.userAgent,dpr:devicePixelRatio,locale:navigator.language,timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,width:innerWidth,height:innerHeight})"
    try:
        opened = subprocess.run(["agent-browser", "--session", session, "--json", "set", "viewport", "800", "600", "1"], capture_output=True, text=True, check=False, timeout=30)
        assert opened.returncode == 0 and json.loads(opened.stdout)["success"] is True
        opened = subprocess.run(["agent-browser", "--session", session, "--json", "open", url], capture_output=True, text=True, check=False, timeout=30)
        assert opened.returncode == 0 and json.loads(opened.stdout)["success"] is True
        result = subprocess.run(["agent-browser", "--session", session, "--json", "eval", script], capture_output=True, text=True, check=False, timeout=30)
        assert result.returncode == 0 and json.loads(result.stdout)["success"] is True
        observed = json.loads(json.loads(result.stdout)["data"]["result"])
        ua = str(observed["ua"])
        marker = "HeadlessChrome/" if "HeadlessChrome/" in ua else "Chrome/"
        version = ua.split(marker, 1)[1].split(" ", 1)[0]
        return {"version": version, "locale": observed["locale"], "timezone": observed["timezone"]}
    finally:
        subprocess.run(["agent-browser", "--session", session, "--json", "close"], capture_output=True, text=True, check=False, timeout=30)


def _raw_plan(url: str, environment: dict[str, object], revision: str, *, expected_terminal: str = "same_origin") -> dict[str, object]:
    return {
        "mode": "matrix",
        "subject": {"repository": url.rsplit("/", 1)[0], "revision": revision, "observed_deploy_ref": "", "deployment": None},
        "condition": {
            "routes": [{"route_id": "localhost", "state_id": "anonymous", "expected_terminal": expected_terminal, "url": url}],
            "viewports": [{"viewport_id": "desktop", "width": 800, "height": 600, "dpr": 1}],
            "browsers": [{"browser_id": "native-chromium", "engine": "chromium", "version": environment["version"]}],
            "locale": environment["locale"], "timezone": environment["timezone"], "auth_fixture_ref": "fixture-anonymous-v1",
            "profiles": {"cache": "cold", "load": "normal", "device": "desktop", "cpu": "normal", "network": "online"},
            "feature_flags": [],
            "budgets": {
                "visual": {"minimum_score": 90}, "functional": {"maximum_terminal_failures": 0},
                "accessibility": {"maximum_serious_or_critical_findings": 0}, "console": {"maximum_unallowlisted_exceptions": 0},
                "network": {"maximum_first_party_failures": 0, "slow_request_ms": 10_000},
                "performance": {"field_gate": "not_required", "lab_evidence": "diagnostic", "field_p75": {"lcp_ms_lt": 2500, "inp_ms_lt": 200, "cls_lt": 0.1}, "relative_tolerances": {"lcp_percent": 5, "inp_percent": 5, "cls_absolute": 0.01}},
            },
            "expected_noise_allowlist": [], "environment": "test", "interaction": "read_only",
        },
        "authorization": {"intent": "read_only", "staging_test_authorization_ref": ""},
        "limits": {"max_routes": 1, "max_viewports": 1, "max_browsers": 1, "max_cells": 1, "max_concurrency": 1, "max_step_seconds": 60, "max_run_seconds": 90, "max_rounds": 1, "max_attempts_per_read": 1, "max_artifact_bytes": 20_000_000, "max_cost_units": 1},
        "round": {"round_id": "proof-round", "ordinal": 1},
    }


def _fixture_revision(root: Path) -> str:
    for command in (("git", "init", "-q"), ("git", "config", "user.email", "proof@example.test"), ("git", "config", "user.name", "localhost-proof"), ("git", "add", "index.html", "negative.html"), ("git", "commit", "-qm", "fixture")):
        subprocess.run(command, cwd=root, check=True, capture_output=True, text=True)
    return subprocess.run(("git", "rev-parse", "HEAD"), cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def _persist(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    path.chmod(0o600)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", help="private directory that retains the reviewable PNG and receipt cache")
    args = parser.parse_args()
    collector = ROOT / "tools" / "web_qa_hermes_agent_browser_collector.py"
    output_dir = (Path(args.output_dir).expanduser().absolute() if args.output_dir else Path(tempfile.mkdtemp(prefix="omh-web-qa-proof-")).absolute())
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "index.html").write_text("<!doctype html><html lang='en'><head><title>QA</title><link rel='icon' href='data:,'></head><body><main><h1>QA</h1><button id='continue'>Continue</button></main><script>console.log('localhost-proof')</script></body></html>", encoding="utf-8")
        (root / "negative.html").write_text("<!doctype html><html lang='en'><head><title>Negative QA</title><link rel='icon' href='data:,'></head><body><main><img src='/missing.png'><button id='continue'>Continue</button></main><script>console.error('localhost-console-error');throw new Error('localhost-uncaught-error')</script></body></html>", encoding="utf-8")
        revision = _fixture_revision(root)
        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, format: str, *values: object) -> None:
                return
        server = ThreadingHTTPServer(("127.0.0.1", 0), lambda *args: QuietHandler(*args, directory=root))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/index.html"
            plan = build_web_qa_observation_plan(_raw_plan(url, _native_environment(url), revision))
            cell_id = plan["matrix"][0]["cell_id"]
            request = {"schema_version": "host_web_qa_collector_request/v1", "plan": plan, "route_urls": {cell_id: url}, "fixture_assignments": {"fixture-anonymous-v1": {"mode": "anonymous"}}}
            output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            plan_path = output_dir / "normalized-plan.json"
            _persist(plan_path, plan)
            started = time.perf_counter()
            first = subprocess.run([sys.executable, str(collector), "--output-dir", str(output_dir), "--timeout-seconds", "90"], input=json.dumps(request), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=100, check=False)
            adapter_wall_ms = round((time.perf_counter() - started) * 1000)
            assert first.returncode == 0, first.stderr
            receipt = json.loads(first.stdout)
            receipt_path = output_dir / "redacted-receipt.json"
            _persist(receipt_path, receipt)
            core_started = time.perf_counter()
            observation = build_web_qa_observation(plan, receipt)
            pure_core_ms = round((time.perf_counter() - core_started) * 1000)
            cell = receipt["cells"][0]
            capture = cell["channels"]["screenshot"]["evidence"]
            capture_path = output_dir / f"{cell_id}.png"
            assert capture_path.is_file() and hashlib.sha256(capture_path.read_bytes()).hexdigest() == capture["capture_sha256"]
            assert all(cell["channels"][name]["status"] == "observed" for name in ("screenshot", "console", "network", "critical_flow", "accessibility", "keyboard", "performance"))
            assert any(item["origin"] == f"http://127.0.0.1:{server.server_port}" for item in cell["channels"]["network"]["evidence"]["requests"])
            assert cell["channels"]["keyboard"]["evidence"]["focus_changed"] is True
            assert receipt["execution"]["session_close_observed"] is True
            assert observation["verdict"] == "BLOCK" and any(blocker.endswith("screenshot_review_missing") for blocker in observation["blockers"]), observation
            assert observation["revision_reasons"] == [], observation
            before = {path: (path.stat().st_mtime_ns, path.stat().st_size) for path in output_dir.rglob("*") if path.is_file()}
            second = subprocess.run([sys.executable, str(collector), "--output-dir", str(output_dir), "--timeout-seconds", "90"], input=json.dumps(request), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False)
            after = {path: (path.stat().st_mtime_ns, path.stat().st_size) for path in output_dir.rglob("*") if path.is_file()}
            assert second.returncode == 0 and second.stdout == first.stdout and before == after
            negative_url = f"http://127.0.0.1:{server.server_port}/negative.html"
            negative_plan = build_web_qa_observation_plan(_raw_plan(negative_url, _native_environment(negative_url), revision))
            negative_cell_id = negative_plan["matrix"][0]["cell_id"]
            negative_request = {"schema_version": "host_web_qa_collector_request/v1", "plan": negative_plan, "route_urls": {negative_cell_id: negative_url}, "fixture_assignments": {"fixture-anonymous-v1": {"mode": "anonymous"}}}
            negative_dir = output_dir / "negative"
            negative = subprocess.run([sys.executable, str(collector), "--output-dir", str(negative_dir), "--timeout-seconds", "90"], input=json.dumps(negative_request), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=100, check=False)
            assert negative.returncode == 0, negative.stderr
            negative_receipt = json.loads(negative.stdout)
            negative_observation = build_web_qa_observation(negative_plan, negative_receipt)
            negative_cell = negative_receipt["cells"][0]
            assert len(negative_cell["channels"]["console"]["evidence"]["exceptions"]) >= 2
            assert any(item["status"] == 404 and item["classification"] == "failure" for item in negative_cell["channels"]["network"]["evidence"]["requests"])
            assert any(reason.endswith("unallowlisted_console_exception") for reason in negative_observation["revision_reasons"])
            assert any(reason.endswith("first_party_request_failure") for reason in negative_observation["revision_reasons"])
            negative_plan_path = negative_dir / "normalized-plan.json"; negative_receipt_path = negative_dir / "redacted-receipt.json"
            _persist(negative_plan_path, negative_plan); _persist(negative_receipt_path, negative_receipt)
            negative_capture = negative_dir / f"{negative_cell_id}.png"
            print(json.dumps({"status": "PASS", "observation_verdict": observation["verdict"], "intentional_blocker": "screenshot_review_missing", "capture": {"path": str(capture_path), "sha256": capture["capture_sha256"], "byte_size": capture["byte_size"]}, "normalized_plan_path": str(plan_path), "redacted_receipt_path": str(receipt_path), "negative": {"capture_path": str(negative_capture), "normalized_plan_path": str(negative_plan_path), "redacted_receipt_path": str(negative_receipt_path), "revision_reasons": negative_observation["revision_reasons"]}, "benchmark": {"adapter_wall_ms": adapter_wall_ms, "pure_core_ms": pure_core_ms, "per_command_ms": {item["operation"]: item["duration_ms"] for item in receipt["execution"]["command_results"]}, "attempts": receipt["execution"]["attempts"], "artifact_bytes": receipt["execution"]["artifact_bytes"]}, "repeat": {"new_browser_commands": 0, "new_captures": 0, "new_writes": 0}}))
            return 0
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
