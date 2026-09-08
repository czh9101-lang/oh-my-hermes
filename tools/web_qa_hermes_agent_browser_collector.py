#!/usr/bin/env python3
"""Bounded host-owned native ``agent-browser`` web-QA collection.

The browser remains wholly host-owned.  OMH's pure plan/receipt validators are
used only to close the input/output boundary; no browser operation is moved
into OMH core. Route URLs are transient and never cached or emitted.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import time
from typing import Iterator
from urllib.parse import urlsplit
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from omh.workflows.web_qa_observation import WebQaObservationError, build_web_qa_observation
from omh.workflows.web_qa_observation_plan import WebQaObservationPlanError, parse_normalized_web_qa_observation_plan

REQUEST_SCHEMA = "host_web_qa_collector_request/v1"
RECEIPT_SCHEMA = "host_web_qa_adapter_receipt/v1"
CHANNELS = ("screenshot", "console", "network", "critical_flow", "accessibility", "keyboard", "performance")
MAX_INPUT_BYTES = 262_144
MAX_RECEIPT_BYTES = 262_144
MAX_COMMAND_OUTPUT_BYTES = 131_072
MAX_COMMAND_SECONDS = 60
MAX_ARTIFACT_BYTES = 50_000_000
PNG_CONTAINER_OVERHEAD_BYTES = 65_536
_VERSION = re.compile(r"(?:Headless)?Chrome/(\d+(?:\.\d+){1,3})")


class CollectorError(ValueError):
    pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=90)
    args = parser.parse_args()
    try:
        request = _read_bounded_json()
        receipt = collect(request, Path(args.output_dir), args.timeout_seconds)
        encoded = _canonical(receipt)
        if len(encoded) > MAX_RECEIPT_BYTES:
            raise CollectorError("receipt_size_cap_exhausted")
        sys.stdout.buffer.write(encoded + b"\n")
        return 0
    except (CollectorError, OSError, json.JSONDecodeError) as exc:
        sys.stdout.write(json.dumps({"schema_version": RECEIPT_SCHEMA, "release_status": "BLOCK", "blocker_id": str(exc)}, separators=(",", ":")) + "\n")
        return 2


def collect(request: dict[str, object], output_dir: Path, timeout_seconds: int) -> dict[str, object]:
    """Collect a frozen request once, or return its verified completed cache.

    Cache reads do not create/chmod directories or take a lock. A lock is only
    created for an incomplete identity, then the cache is rechecked under it.
    """
    plan = _validate_request(request, timeout_seconds)
    output_dir = _absolute_nonsymlink_path(output_dir)
    if os.name != "posix":
        raise CollectorError("unsupported_host_platform_posix_file_lock_required")
    key = _sha(_canonical(request))
    cached = _read_completed_cache(output_dir, plan, key)
    if cached is not None:
        return cached

    _create_private_directory(output_dir)
    cache_dir = output_dir / ".web-qa-collector-cache-v1"
    _create_private_directory(cache_dir)
    with _cache_lock(cache_dir / f".{plan['run_id']}.lock"):
        cached = _read_completed_cache(output_dir, plan, key)
        if cached is not None:
            return cached
        conflicting = list(cache_dir.glob(f"{plan['run_id']}-*.json"))
        if conflicting:
            raise CollectorError("completed_run_identity_conflicts_with_request")
        receipt = _collect_uncached(request, plan, output_dir, timeout_seconds)
        _verify_receipt_and_captures(plan, receipt, output_dir)
        cache_path = cache_dir / f"{plan['run_id']}-{key}.json"
        _write_private_json(cache_path, {"request_digest": key, "receipt": receipt})
        return receipt


def _collect_uncached(request: dict[str, object], plan: dict[str, object], output_dir: Path, timeout_seconds: int) -> dict[str, object]:
    started_at = _utc_now()
    limits = _object(plan["limits"], "plan limits")
    deadline = time.monotonic() + min(timeout_seconds, _integer(limits["max_run_seconds"], "max run seconds"))
    commands: list[dict[str, object]] = []
    captures: list[Path] = []
    session_ids: list[str] = []
    cells: list[dict[str, object]] = []
    for cell in _objects(plan["matrix"], "plan matrix"):
        observed, capture, cell_commands, session_id = _collect_cell(request, plan, cell, output_dir, deadline, sum(path.stat().st_size for path in captures if path.exists()))
        cells.append(observed); commands.extend(cell_commands)
        if capture is not None:
            captures.append(capture)
        if session_id is not None:
            session_ids.append(session_id)
    if not commands:
        # A host-only preflight projection makes an all-blocked matrix
        # admissible without implying a browser session was started.
        first = _objects(plan["matrix"], "plan matrix")[0]
        commands.append(_projection(_text(first["cell_id"], "cell id"), "capability_preflight", True, 0, 0, b"", b""))
    artifact_bytes = sum(path.stat().st_size for path in captures if path.is_file())
    cap = _integer(limits["max_artifact_bytes"], "max artifact bytes")
    blockers = ["artifact_cap_exhausted"] if artifact_bytes > cap else []
    if any(not item["success"] for item in commands if item["operation"] == "close_session"):
        blockers.append("session_cleanup_not_observed")
    return {
        "schema_version": RECEIPT_SCHEMA, "receipt_version": 1,
        "run_id": plan["run_id"], "subject_digest": plan["subject_digest"], "condition_digest": plan["condition_digest"],
        "adapter": {"adapter_id": "hermes-agent-browser/native-v1", "session_id_digest": _sha(_canonical(sorted(session_ids)))},
        "execution": {"command_results": commands, "artifact_bytes": artifact_bytes, "session_close_observed": not any(not item["success"] for item in commands if item["operation"] == "close_session"), "attempts": 1, "cost_units": 0, "peak_concurrency": 1, "started_at": started_at, "ended_at": _utc_after(started_at)},
        "cells": cells, "release_status": "COLLECTED" if not blockers else "BLOCK", "release_blockers": blockers,
        "redaction": {"status": "redacted_before_persistence", "forbidden": ["headers", "cookies", "credentials", "query_values", "request_bodies", "response_bodies"]},
    }


def _collect_cell(request: dict[str, object], plan: dict[str, object], cell: dict[str, object], output_dir: Path, deadline: float, artifact_bytes: int) -> tuple[dict[str, object], Path | None, list[dict[str, object]], str | None]:
    condition = _object(plan["condition"], "condition")
    cell_id = _text(cell["cell_id"], "cell id")
    route = next(item for item in _objects(condition["routes"], "routes") if item["route_digest"] == cell["route_digest"])
    viewport = next(item for item in _objects(condition["viewports"], "viewports") if item["viewport_id"] == cell["viewport_id"])
    browser = next(item for item in _objects(condition["browsers"], "browsers") if item["browser_id"] == cell["browser_id"])
    preflight = _preflight_blocker(condition, cell, browser)
    if preflight:
        return _blocked_cell(cell, preflight), None, [], None
    cap = _integer(_object(plan["limits"], "limits")["max_artifact_bytes"], "max artifact bytes")
    reserve = _png_worst_case_bytes(viewport)
    if cap - artifact_bytes < reserve:
        return _blocked_cell(cell, "screenshot_storage_reserve_unavailable"), None, [], None
    candidate = output_dir / f"{cell_id}.png"
    reservation = _reserve_capture_path(candidate)
    if reservation is None:
        return _blocked_cell(cell, "capture_path_already_exists_not_owned"), None, [], None

    session = f"omh-qa-{uuid.uuid4().hex[:20]}"
    commands: list[dict[str, object]] = []
    launched = False
    capture_path: Path | None = None
    try:
        def run(operation: str, argv: list[str]) -> dict[str, object]:
            nonlocal launched
            projection, payload, started = _run(session, cell_id, operation, argv, deadline, _integer(_object(plan["limits"], "limits")["max_step_seconds"], "max step seconds"))
            commands.append(projection)
            launched = launched or started
            return payload

        setup = run("set_viewport", ["set", "viewport", str(viewport["width"]), str(viewport["height"]), str(viewport["dpr"])])
        if setup.get("success") is not True:
            return _blocked_cell(cell, "viewport_configuration_not_observed"), None, commands, session
        navigation = run("navigate", ["open", _text(_object(request["route_urls"], "route URLs")[cell_id], "route URL")])
        before = run("focus_before", ["eval", _focus_script()])
        screenshot = run("screenshot", ["screenshot", str(candidate)])
        if screenshot.get("success") is True and candidate.exists() and not candidate.is_symlink():
            # The browser creates this owned path; make it private before it is
            # hashed, cached, or exposed to the importer.
            candidate.chmod(0o600)
        capture_sha, capture_size = _owned_capture(candidate, cap - artifact_bytes) if screenshot.get("success") is True else ("", 0)
        if capture_sha:
            capture_path = candidate
        console = run("console", ["console"])
        errors = run("errors", ["errors"])
        timing = run("network_timing_probe", ["eval", _timing_script()])
        network = run("network_requests", ["network", "requests"])
        a11y = run("accessibility_audit", ["a11y"])
        vitals = run("lab_vitals", ["vitals"])
        keypress = run("keyboard_tab", ["press", "Tab"])
        after = run("focus_after", ["eval", _focus_script()])
        environment = run("browser_environment", ["eval", _environment_script()])

        by_operation = {str(item["operation"]): item for item in commands}
        digests = {name: _command_digest(value) for name, value in by_operation.items()}
        observed = _environment(environment)
        actual_url = _payload(navigation, "data", "url")
        actual_terminal = _terminal_state(navigation, observed, route, _text(cell["expected_terminal"], "expected terminal"))
        actual = {"origin": _canonical_origin(actual_url), "navigation_digest": _navigation_digest(str(actual_url or "")), "viewport_id": viewport["viewport_id"], "width": observed["width"], "height": observed["height"], "dpr": observed["dpr"], "browser_id": browser["browser_id"], "engine": observed["engine"], "version": observed["version"], "locale": observed["locale"], "timezone": observed["timezone"], "profiles": condition["profiles"], "auth_fixture_ref": condition["auth_fixture_ref"], "terminal_state": actual_terminal}
        fixture_observed = _fixture_is_anonymous(environment)
        matches = actual_terminal == cell["expected_terminal"] and fixture_observed and _actual_matches(actual, route, viewport, browser, condition)
        terminal = "" if matches else _terminal_blocker(navigation, actual, route, viewport, browser, condition, fixture_observed)
        channels = _channels(capture_sha, capture_size, console, errors, timing, network, a11y, vitals, keypress, before, after, navigation, route, cell_id, digests)
        return {"cell_id": cell_id, "route_id": cell["route_id"], "state_id": cell["state_id"], "terminal_status": "observed" if matches else "blocked", "terminal_blocker_id": terminal, "actual": actual if matches else None, "channels": channels}, capture_path, commands, session
    finally:
        if launched:
            # This independent cleanup deadline is deliberately not constrained
            # by an expired run deadline; it cannot start an unlaunched session.
            projection, _, _ = _run(session, cell_id, "close_session", ["close"], time.monotonic() + 5, 5)
            commands.append(projection)
        if reservation.exists() and not reservation.is_symlink():
            reservation.unlink()


def _channels(capture_sha: str, capture_size: int, console: dict[str, object], errors: dict[str, object], timing: dict[str, object], network: dict[str, object], a11y: dict[str, object], vitals: dict[str, object], keypress: dict[str, object], before: dict[str, object], after: dict[str, object], navigation: dict[str, object], route: dict[str, object], cell_id: str, digests: dict[str, str]) -> dict[str, object]:
    try:
        exceptions = _project_console(_payload_list(console, "data", "messages"), _payload_list(errors, "data", "errors"))
        console_channel = _channel(True, "", {"exceptions": exceptions, "operation_digests": [digests["console"], digests["errors"]]}) if console.get("success") is True and errors.get("success") is True else _channel(False, "console_or_errors_not_observed", {})
    except CollectorError:
        console_channel = _channel(False, "console_payload_invalid", {})
    try:
        requests = _project_network(_payload_list(network, "data", "requests"), _timings(timing))
        network_channel = _channel(True, "", {"requests": requests, "operation_digests": [digests["network_requests"]]}) if network.get("success") is True and timing.get("success") is True else _channel(False, "network_duration_or_requests_not_observed", {})
    except CollectorError:
        network_channel = _channel(False, "network_payload_invalid_or_duration_missing", {})
    try:
        findings = _project_a11y(_payload_list(a11y, "data", "violations"))
        a11y_channel = _channel(True, "", {"findings": findings, "operation_digests": [digests["accessibility_audit"]]}) if a11y.get("success") is True else _channel(False, "accessibility_not_observed", {})
    except CollectorError:
        a11y_channel = _channel(False, "accessibility_payload_invalid", {})
    focus_before = _focus(before); focus_after = _focus(after)
    return {
        "screenshot": _channel(bool(capture_sha), "screenshot_not_observed_or_storage_cap", {"capture_sha256": capture_sha, "byte_size": capture_size, "captured_at": _utc_now(), "review": None, "operation_digests": [digests["screenshot"]]} if capture_sha else {}),
        "console": console_channel,
        "network": network_channel,
        "critical_flow": _channel(navigation.get("success") is True, "navigation_not_observed", {"steps": [{"step_id": "navigate", "action": "navigate", "locator": {"strategy": "route", "value_digest": route["navigation_digest"]}, "attempts": [{"query_identity": _sha((cell_id + _text(route["navigation_digest"], "navigation digest")).encode()), "operation_digest": digests["navigate"], "outcome": "success", "failure_class": "success"}]}], "trace_reference": None, "operation_digests": [digests["navigate"]]} if navigation.get("success") is True else {}),
        "accessibility": a11y_channel,
        "keyboard": _channel(keypress.get("success") is True and before.get("success") is True and after.get("success") is True, "keyboard_focus_not_observed", {"action": "Tab", "focus_before": focus_before, "focus_after": focus_after, "focus_changed": focus_before != focus_after, "operation_digests": [digests["keyboard_tab"], digests["focus_before"], digests["focus_after"]]} if keypress.get("success") is True and before.get("success") is True and after.get("success") is True else {}),
        "performance": _channel(vitals.get("success") is True, "lab_vitals_not_observed", {"evidence_class": "lab", "lab": _project_vitals(vitals), "field": None, "operation_digests": [digests["lab_vitals"]]} if vitals.get("success") is True else {}),
    }


def _run(session: str, cell_id: str, operation: str, command: list[str], deadline: float, max_step_seconds: int) -> tuple[dict[str, object], dict[str, object], bool]:
    remaining = min(MAX_COMMAND_SECONDS, max_step_seconds, deadline - time.monotonic())
    if remaining <= 0:
        return _projection(cell_id, operation, False, 124, 0, b"", b""), {}, False
    started = time.monotonic()
    process = subprocess.Popen(["agent-browser", "--session", session, "--json", "--max-output", "65536", *command], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr, overflow = _read_process_bounded(process, remaining)
    duration = round((time.monotonic() - started) * 1000)
    code = process.returncode if process.returncode is not None else 124
    payload = _json(stdout)
    success = not overflow and duration <= max_step_seconds * 1000 and code == 0 and isinstance(payload, dict) and payload.get("success") is True
    return _projection(cell_id, operation, success, code, duration, stdout, stderr), payload if isinstance(payload, dict) else {}, True


def _validate_request(value: dict[str, object], timeout: int) -> dict[str, object]:
    if set(value) != {"schema_version", "plan", "route_urls", "fixture_assignments"} or value.get("schema_version") != REQUEST_SCHEMA:
        raise CollectorError("request_schema_invalid")
    try:
        plan = parse_normalized_web_qa_observation_plan(value["plan"])
    except WebQaObservationPlanError as exc:
        raise CollectorError(f"normalized_plan_invalid:{exc}") from exc
    if plan["authorization"] != {"intent": "read_only", "staging_test_authorization_ref": ""} or _object(plan["condition"], "condition")["interaction"] != "read_only":
        raise CollectorError("read_only_bounded_execution_required")
    limits = _object(plan["limits"], "limits")
    if type(timeout) is not int or timeout < 1 or timeout > _integer(limits["max_run_seconds"], "max run seconds"):
        raise CollectorError("timeout_outside_plan_cap")
    urls = _object(value["route_urls"], "route URLs"); fixtures = _object(value["fixture_assignments"], "fixture assignments")
    cells = _objects(plan["matrix"], "matrix")
    ids = {str(item["cell_id"]) for item in cells}
    if len(ids) != len(cells) or set(urls) != ids:
        raise CollectorError("route_mapping_must_cover_each_unique_planned_cell")
    condition = _object(plan["condition"], "condition")
    fixture_ref = _text(condition["auth_fixture_ref"], "fixture ref")
    if set(fixtures) != {fixture_ref} or fixtures.get(fixture_ref) != {"mode": "anonymous"}:
        raise CollectorError("explicit_anonymous_fixture_assignment_required")
    routes = {str(item["route_digest"]): item for item in _objects(condition["routes"], "routes")}
    for cell in cells:
        route = routes.get(str(cell["route_digest"]))
        url = urls.get(str(cell["cell_id"]))
        if route is None or not isinstance(url, str) or _canonical_origin(url) != route["origin"] or _navigation_digest(url) != route["navigation_digest"]:
            raise CollectorError("route_mapping_navigation_digest_mismatch")
    return plan


def _preflight_blocker(condition: dict[str, object], cell: dict[str, object], browser: dict[str, object]) -> str:
    if browser["engine"] != "chromium":
        return f"unsupported_browser_engine_{browser['engine']}"
    if cell["expected_terminal"] == "authenticated_view":
        return "unsupported_authenticated_view"
    profiles = _object(condition["profiles"], "profiles")
    supported = {"cache": "cold", "load": "normal", "device": "desktop", "cpu": "normal", "network": "online"}
    for name, expected in supported.items():
        if profiles.get(name) != expected:
            return f"unsupported_{name}_profile_{profiles.get(name, 'missing')}"
    if condition["feature_flags"] != []:
        return "unsupported_feature_flag_assignment"
    return ""


def _read_completed_cache(output_dir: Path, plan: dict[str, object], key: str) -> dict[str, object] | None:
    if not output_dir.exists():
        return None
    _safe_directory(output_dir)
    cache_dir = output_dir / ".web-qa-collector-cache-v1"
    if not cache_dir.exists():
        return None
    _safe_directory(cache_dir)
    run_id = _text(plan["run_id"], "run id")
    path = cache_dir / f"{run_id}-{key}.json"
    conflicts = list(cache_dir.glob(f"{run_id}-*.json"))
    if conflicts and path not in conflicts:
        raise CollectorError("completed_run_identity_conflicts_with_request")
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RECEIPT_BYTES:
        raise CollectorError("cache_entry_unsafe")
    try:
        cached = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectorError("cache_entry_invalid") from exc
    if not isinstance(cached, dict) or cached.get("request_digest") != key or not isinstance(cached.get("receipt"), dict):
        raise CollectorError("cache_entry_conflicts_or_invalid")
    receipt = cached["receipt"]
    _verify_receipt_and_captures(plan, receipt, output_dir)
    return receipt


def _verify_receipt_and_captures(plan: dict[str, object], receipt: object, output_dir: Path) -> None:
    try:
        build_web_qa_observation(plan, receipt)
    except (WebQaObservationError, TypeError, ValueError) as exc:
        raise CollectorError(f"cached_receipt_invalid:{exc}") from exc
    if not isinstance(receipt, dict) or receipt.get("run_id") != plan["run_id"]:
        raise CollectorError("cached_receipt_identity_invalid")
    for cell in _objects(receipt.get("cells"), "receipt cells"):
        channels = _object(cell["channels"], "receipt channels"); screenshot = _object(channels["screenshot"], "screenshot")
        if screenshot["status"] != "observed":
            continue
        evidence = _object(screenshot["evidence"], "screenshot evidence")
        name = _text(cell["cell_id"], "cell id") + ".png"
        path = output_dir / name
        digest, size = _owned_capture(path, MAX_ARTIFACT_BYTES, delete_oversize=False)
        if digest != evidence["capture_sha256"] or size != evidence["byte_size"]:
            raise CollectorError("cached_capture_missing_or_digest_mismatch")


@contextmanager
def _cache_lock(path: Path) -> Iterator[None]:
    import fcntl
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)


def _absolute_nonsymlink_path(path: Path) -> Path:
    absolute = path.expanduser()
    if not absolute.is_absolute():
        absolute = Path.cwd() / absolute
    # Resolve every existing component before handing the path to the browser
    # daemon. This prevents a relative daemon cwd or a symlinked parent from
    # redirecting the screenshot outside the authorized canonical target.
    return absolute.resolve(strict=False)


def _create_private_directory(path: Path) -> None:
    if path.exists():
        _safe_directory(path)
        return
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError:
        # A competing collector may have created the private cache directory;
        # validate it before taking the run lock rather than widening the race.
        _safe_directory(path)


def _safe_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir() or path.stat().st_mode & 0o077:
        raise CollectorError("private_output_directory_unsafe")


def _png_worst_case_bytes(viewport: dict[str, object]) -> int:
    """Reserve uncompressed RGBA scanlines for the requested native pixels."""
    width = _integer(viewport["width"], "viewport width")
    height = _integer(viewport["height"], "viewport height")
    dpr = viewport["dpr"]
    if isinstance(dpr, bool) or not isinstance(dpr, (int, float)) or dpr <= 0:
        raise CollectorError("viewport_dpr_invalid")
    pixels_w = math.ceil(width * dpr); pixels_h = math.ceil(height * dpr)
    return pixels_w * pixels_h * 4 + pixels_h + PNG_CONTAINER_OVERHEAD_BYTES


def _reserve_capture_path(candidate: Path) -> Path | None:
    if candidate.exists() or candidate.is_symlink():
        return None
    reservation = candidate.with_name(f".{candidate.name}.reserve")
    try:
        fd = os.open(reservation, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return None
    os.close(fd)
    return reservation


def _owned_capture(path: Path, maximum: int, *, delete_oversize: bool = True) -> tuple[str, int]:
    if path.is_symlink() or not path.is_file():
        return "", 0
    size = path.stat().st_size
    if size < 1 or size > maximum:
        if delete_oversize:
            path.unlink()
        return "", 0
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65_536), b""):
            digest.update(block)
    return digest.hexdigest(), size


def _project_console(messages: object, errors: object) -> list[dict[str, object]]:
    source: list[object] = []
    for payload in (messages, errors):
        if type(payload) is not list or len(payload) > 128:
            raise CollectorError("console_payload_invalid")
        source.extend(payload)
    output: list[dict[str, object]] = []
    for item in source:
        if type(item) is not dict:
            raise CollectorError("console_payload_invalid")
        kind = item.get("type", "error")
        text = item.get("text", item.get("message", ""))
        if not isinstance(kind, str) or not isinstance(text, str):
            raise CollectorError("console_payload_invalid")
        if kind.lower() == "error":
            output.append({"exception_id": _sha(_canonical({"type": kind, "text": text[:4096]})), "classification": "exception", "allowlist_id": ""})
    return output


def _project_network(items: object, timings: dict[tuple[str, str], int]) -> list[dict[str, object]]:
    if type(items) is not list or len(items) > 128:
        raise CollectorError("network_payload_invalid")
    output: list[dict[str, object]] = []
    for item in items:
        if type(item) is not dict or not isinstance(item.get("url"), str) or not isinstance(item.get("method"), str) or type(item.get("status")) is not int:
            raise CollectorError("network_payload_invalid")
        url = item["url"]; origin = _network_origin(url)
        if not origin:
            continue
        path = _path_digest(url); key = (origin, path)
        if key not in timings:
            raise CollectorError("network_duration_missing")
        method = item["method"].upper(); status = item["status"]
        if method not in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE") or status < 0 or status > 599:
            raise CollectorError("network_payload_invalid")
        output.append({"origin": origin, "path_digest": path, "method": method, "status": status, "duration_ms": timings[key], "classification": "transport_failure" if status == 0 else "failure" if status >= 400 else "success", "allowlist_id": ""})
    return output


def _project_a11y(items: object) -> list[dict[str, object]]:
    if type(items) is not list or len(items) > 128:
        raise CollectorError("accessibility_payload_invalid")
    output: list[dict[str, object]] = []
    for item in items:
        if type(item) is not dict or not isinstance(item.get("id"), str) or item.get("impact") not in (None, "minor", "moderate", "serious", "critical") or type(item.get("nodeCount")) is not int or item["nodeCount"] < 0:
            raise CollectorError("accessibility_payload_invalid")
        output.append({"rule_id": item["id"], "impact": item["impact"] or "none", "node_count": item["nodeCount"]})
    return output


def _timings(value: dict[str, object]) -> dict[tuple[str, str], int]:
    raw = _eval_json(value)
    if type(raw) is not list or len(raw) > 256:
        raise CollectorError("network_timing_payload_invalid")
    output: dict[tuple[str, str], int] = {}
    for item in raw:
        if type(item) is not dict or not isinstance(item.get("name"), str) or isinstance(item.get("duration"), bool) or not isinstance(item.get("duration"), (int, float)) or item["duration"] < 0:
            raise CollectorError("network_timing_payload_invalid")
        origin = _network_origin(item["name"])
        if origin:
            output[(origin, _path_digest(item["name"]))] = round(item["duration"])
    return output


def _environment(value: dict[str, object]) -> dict[str, object]:
    raw = _eval_json(value)
    if type(raw) is not dict:
        return {"width": -1, "height": -1, "dpr": -1, "engine": "", "version": "", "locale": "", "timezone": "", "ready_state": ""}
    ua = raw.get("ua"); match = _VERSION.search(ua) if isinstance(ua, str) else None
    return {"width": raw.get("width"), "height": raw.get("height"), "dpr": raw.get("dpr"), "engine": "chromium" if match else "", "version": match.group(1) if match else "", "locale": raw.get("locale"), "timezone": raw.get("timezone"), "ready_state": raw.get("readyState")}


def _terminal_state(navigation: dict[str, object], environment: dict[str, object], route: dict[str, object], expected: str) -> str:
    if navigation.get("success") is not True:
        return ""
    actual_url = _payload(navigation, "data", "url")
    if expected == "same_origin" and _canonical_origin(actual_url) == route["origin"]:
        return "same_origin"
    if expected == "document_ready" and environment["ready_state"] == "complete":
        return "document_ready"
    return ""


def _actual_matches(actual: dict[str, object], route: dict[str, object], viewport: dict[str, object], browser: dict[str, object], condition: dict[str, object]) -> bool:
    return actual == {"origin": route["origin"], "navigation_digest": route["navigation_digest"], "viewport_id": viewport["viewport_id"], "width": viewport["width"], "height": viewport["height"], "dpr": viewport["dpr"], "browser_id": browser["browser_id"], "engine": browser["engine"], "version": browser["version"], "locale": condition["locale"], "timezone": condition["timezone"], "profiles": condition["profiles"], "auth_fixture_ref": condition["auth_fixture_ref"], "terminal_state": actual["terminal_state"]}


def _terminal_blocker(navigation: dict[str, object], actual: dict[str, object], route: dict[str, object], viewport: dict[str, object], browser: dict[str, object], condition: dict[str, object], fixture_observed: bool) -> str:
    if navigation.get("success") is not True:
        return "navigation_not_observed"
    for field, expected in (("origin", route["origin"]), ("navigation_digest", route["navigation_digest"]), ("width", viewport["width"]), ("height", viewport["height"]), ("dpr", viewport["dpr"]), ("engine", browser["engine"]), ("version", browser["version"]), ("locale", condition["locale"]), ("timezone", condition["timezone"])):
        if actual[field] != expected:
            return f"actual_{field}_mismatch"
    return "fixture_assignment_not_observed" if not fixture_observed else "terminal_state_not_observed"


def _channel(observed: bool, blocker: str, evidence: dict[str, object]) -> dict[str, object]:
    return {"status": "observed" if observed else "blocked", "blocker_id": "" if observed else blocker, "evidence": evidence if observed else {}}


def _blocked_cell(cell: dict[str, object], blocker: str) -> dict[str, object]:
    return {"cell_id": cell["cell_id"], "route_id": cell["route_id"], "state_id": cell["state_id"], "terminal_status": "blocked", "terminal_blocker_id": blocker, "actual": None, "channels": {name: _channel(False, blocker, {}) for name in CHANNELS}}


def _fixture_is_anonymous(value: dict[str, object]) -> bool:
    raw = _eval_json(value)
    return type(raw) is dict and raw.get("cookies") == 0 and raw.get("local") == 0 and raw.get("session") == 0


def _focus(value: dict[str, object]) -> dict[str, object]:
    raw = _eval_json(value)
    item = raw if type(raw) is dict else {}
    return {"strategy": "attribute", "value_digest": _sha(_canonical({"tag": str(item.get("tag", "")), "id": str(item.get("id", ""))}))}


def _project_vitals(value: dict[str, object]) -> dict[str, int | float | None]:
    data = _object(value.get("data"), "vitals data"); lcp = _object(data.get("lcp"), "lcp"); cls = _object(data.get("cls"), "cls")
    return {"lcp_ms": _metric(lcp.get("startTime")), "inp_ms": _metric(data.get("inp")), "cls": _metric(cls.get("score"))}


def _metric(value: object) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def _focus_script() -> str:
    return "JSON.stringify({tag:document.activeElement&&document.activeElement.tagName||'',id:document.activeElement&&document.activeElement.id||''})"


def _timing_script() -> str:
    return "JSON.stringify(performance.getEntriesByType('navigation').concat(performance.getEntriesByType('resource')).slice(-256).map(e=>({name:e.name,duration:e.duration})))"


def _environment_script() -> str:
    return "JSON.stringify({ua:navigator.userAgent,dpr:devicePixelRatio,locale:navigator.language,timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,width:innerWidth,height:innerHeight,readyState:document.readyState,cookies:document.cookie?1:0,local:localStorage.length,session:sessionStorage.length})"


def _read_process_bounded(process: subprocess.Popen[bytes], timeout: float) -> tuple[bytes, bytes, bool]:
    assert process.stdout and process.stderr
    selector = selectors.DefaultSelector(); selector.register(process.stdout, selectors.EVENT_READ); selector.register(process.stderr, selectors.EVENT_READ)
    fds = (process.stdout.fileno(), process.stderr.fileno()); chunks = {fd: bytearray() for fd in fds}; expires = time.monotonic() + timeout; overflow = False
    while selector.get_map():
        remaining = expires - time.monotonic()
        if remaining <= 0:
            process.kill(); break
        for key, _ in selector.select(remaining):
            data = os.read(key.fd, 8192)
            if not data:
                selector.unregister(key.fileobj); continue
            chunks[key.fd].extend(data)
            if len(chunks[key.fd]) > MAX_COMMAND_OUTPUT_BYTES:
                overflow = True; process.kill(); selector.unregister(key.fileobj)
    process.wait(); selector.close(); process.stdout.close(); process.stderr.close()
    return bytes(chunks[fds[0]][:MAX_COMMAND_OUTPUT_BYTES]), bytes(chunks[fds[1]][:MAX_COMMAND_OUTPUT_BYTES]), overflow


def _projection(cell_id: str, operation: str, success: bool, returncode: int, duration_ms: int, stdout: bytes, stderr: bytes) -> dict[str, object]:
    return {"cell_id": cell_id, "operation": operation, "success": success, "returncode": returncode, "duration_ms": duration_ms, "stdout_sha256": _sha(stdout), "stderr_sha256": _sha(stderr), "stdout_bytes": len(stdout), "stderr_bytes": len(stderr)}


def _payload(value: dict[str, object], *keys: str) -> object:
    current: object = value
    for key in keys:
        if type(current) is not dict:
            return None
        current = current.get(key)
    return current


def _payload_list(value: dict[str, object], *keys: str) -> object:
    return _payload(value, *keys)


def _eval_json(value: dict[str, object]) -> object:
    raw = _payload(value, "data", "result")
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return None


def _canonical_origin(value: object) -> str:
    try:
        parsed = urlsplit(str(value or ""))
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            return ""
        host = parsed.hostname.encode("idna").decode("ascii").lower(); port = parsed.port
    except (ValueError, UnicodeError):
        return ""
    return f"{parsed.scheme.lower()}://{host}" + ("" if port is None or (parsed.scheme.lower(), port) in (("http", 80), ("https", 443)) else f":{port}")


def _network_origin(value: object) -> str:
    try:
        parsed = urlsplit(str(value or ""))
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return ""
        host = parsed.hostname.encode("idna").decode("ascii").lower(); port = parsed.port
    except (ValueError, UnicodeError):
        return ""
    return f"{parsed.scheme.lower()}://{host}" + ("" if port is None or (parsed.scheme.lower(), port) in (("http", 80), ("https", 443)) else f":{port}")


def _path_digest(url: str) -> str:
    return _sha(_canonical({"path": urlsplit(url).path or "/"}))


def _navigation_digest(url: str) -> str:
    parsed = urlsplit(url)
    return _sha(_canonical({"path": parsed.path or "/", "query": parsed.query, "fragment": parsed.fragment}))


def _read_bounded_json() -> dict[str, object]:
    payload = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(payload) > MAX_INPUT_BYTES:
        raise CollectorError("request_size_cap_exhausted")
    value = json.loads(payload)
    return _object(value, "request")


def _write_private_json(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(_canonical(value)); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _object(value: object, field: str) -> dict[str, object]:
    if type(value) is not dict:
        raise CollectorError(f"{field}_must_be_object")
    return value


def _objects(value: object, field: str) -> list[dict[str, object]]:
    if type(value) is not list or any(type(item) is not dict for item in value):
        raise CollectorError(f"{field}_must_be_object_list")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise CollectorError(f"{field}_must_be_text")
    return value


def _integer(value: object, field: str) -> int:
    if type(value) is not int:
        raise CollectorError(f"{field}_must_be_integer")
    return value


def _command_digest(command: dict[str, object]) -> str:
    return _sha(_canonical(command))


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json(value: bytes) -> object:
    try:
        return json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _utc_after(started_at: str) -> str:
    started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    ended = datetime.now(UTC)
    # Receipt timestamps are millisecond-precision, so preserve at least one
    # whole millisecond after the serialized start value.
    minimum = started + timedelta(milliseconds=1)
    if ended < minimum:
        ended = minimum
    return ended.isoformat(timespec="milliseconds").replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
