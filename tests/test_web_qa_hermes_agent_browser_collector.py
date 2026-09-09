from __future__ import annotations

from collections.abc import Callable
import importlib.util
import json
import subprocess
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Protocol
from unittest import mock

from _local_package import load_local_package

load_local_package()
from omh.workflows.web_qa_observation import build_web_qa_observation
from omh.workflows.web_qa_observation_plan import build_web_qa_observation_plan
from test_web_qa_observation import object_list, object_value

if TYPE_CHECKING:
    import web_qa_hermes_agent_browser_collector as collector
else:
    ROOT = Path(__file__).parents[1]
    SPEC = importlib.util.spec_from_file_location("web_qa_collector", ROOT / "tools" / "web_qa_hermes_agent_browser_collector.py")
    assert SPEC and SPEC.loader
    collector = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(collector)


class CollectorPorts(Protocol):
    """White-box ports structurally checked against the real collector module."""

    _navigation_digest: Callable[[str], str]
    _terminal_blocker: Callable[[dict[str, object], dict[str, object], dict[str, object], dict[str, object], dict[str, object], dict[str, object], bool], str]
    _collect_cell: Callable[[dict[str, object], dict[str, object], dict[str, object], Path, float, int], tuple[dict[str, object], Path | None, list[dict[str, object]], str | None]]
    _png_worst_case_bytes: Callable[[dict[str, object]], int]
    _reserve_capture_path: Callable[[Path], Path | None]
    _project_network: Callable[[object, dict[tuple[str, str], int]], list[dict[str, object]]]
    _project_console: Callable[[object, object], list[dict[str, object]]]
    _project_a11y: Callable[[object], list[dict[str, object]]]
    _validate_request: Callable[[dict[str, object], int], dict[str, object]]
    _sha: Callable[[bytes], str]
    _canonical: Callable[[object], bytes]
    @property
    def os(self) -> HostPlatform: ...


class HostPlatform(Protocol):
    @property
    def name(self) -> str: ...


class CollectorAccess(CollectorPorts, Protocol):
    @staticmethod
    def bind(module: CollectorPorts):
        return (
            module._navigation_digest, module._terminal_blocker, module._collect_cell,
            module._png_worst_case_bytes, module._reserve_capture_path,
            module._project_network, module._project_console, module._project_a11y,
            module._validate_request, module._sha, module._canonical,
        )


collector_ports: CollectorPorts = collector
(
    navigation_digest, terminal_blocker, collect_cell, png_worst_case_bytes,
    reserve_capture_path, project_network, project_console, project_a11y,
    validate_request, sha, canonical,
) = CollectorAccess.bind(collector)


def normalized_plan(url: str, *, engine: str = "chromium") -> dict[str, object]:
    return build_web_qa_observation_plan({
        "mode": "matrix",
        "subject": {"repository": "https://github.com/acme/collector", "revision": "a" * 40, "observed_deploy_ref": "", "deployment": None},
        "condition": {
            "routes": [{"route_id": "home", "state_id": "anonymous", "expected_terminal": "document_ready", "url": url}],
            "viewports": [{"viewport_id": "desktop", "width": 800, "height": 600, "dpr": 1}],
            "browsers": [{"browser_id": "native", "engine": engine, "version": "152.0.0.0"}],
            "locale": "en-US", "timezone": "UTC", "auth_fixture_ref": "fixture-anonymous-v1",
            "profiles": {"cache": "cold", "load": "normal", "device": "desktop", "cpu": "normal", "network": "online"}, "feature_flags": [],
            "budgets": {"visual": {"minimum_score": 90}, "functional": {"maximum_terminal_failures": 0}, "accessibility": {"maximum_serious_or_critical_findings": 0}, "console": {"maximum_unallowlisted_exceptions": 0}, "network": {"maximum_first_party_failures": 0, "slow_request_ms": 1000}, "performance": {"field_gate": "not_required", "lab_evidence": "diagnostic", "field_p75": {"lcp_ms_lt": 2500, "inp_ms_lt": 200, "cls_lt": 0.1}, "relative_tolerances": {"lcp_percent": 5, "inp_percent": 5, "cls_absolute": 0.01}}},
            "expected_noise_allowlist": [], "environment": "test", "interaction": "read_only",
        },
        "authorization": {"intent": "read_only", "staging_test_authorization_ref": ""},
        "limits": {"max_routes": 1, "max_viewports": 1, "max_browsers": 1, "max_cells": 1, "max_concurrency": 1, "max_step_seconds": 60, "max_run_seconds": 90, "max_rounds": 1, "max_attempts_per_read": 1, "max_artifact_bytes": 8_000_000, "max_cost_units": 1},
        "round": {"round_id": "collector-tests", "ordinal": 1},
    })


def request(url: str, *, engine: str = "chromium") -> dict[str, object]:
    plan = normalized_plan(url, engine=engine)
    cell_id = object_list(plan["matrix"])[0]["cell_id"]
    assert isinstance(cell_id, str)
    return {"schema_version": collector.REQUEST_SCHEMA, "plan": plan, "route_urls": {cell_id: url}, "fixture_assignments": {"fixture-anonymous-v1": {"mode": "anonymous"}}}


class HostCollectorContractTests(unittest.TestCase):
    def test_rejects_forged_normalized_plan_before_output_directory_exists(self) -> None:
        item = request("http://127.0.0.1:8123/index.html")
        object_value(object_value(item["plan"])["limits"])["max_run_seconds"] = 999_999
        for host, platform in (("native", nullcontext()), ("unsupported", mock.patch.object(collector, "os", SimpleNamespace(name="nt")))):
            with self.subTest(host=host), platform, tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "never-created"
                with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("browser must not launch")) as spawn:
                    with self.assertRaisesRegex(collector.CollectorError, "normalized_plan_invalid"):
                        _ = collector.collect(item, output, 30)
                spawn.assert_not_called()
                self.assertFalse(output.exists())
                self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_actual_redirect_path_is_not_the_requested_navigation_digest(self) -> None:
        requested = "http://127.0.0.1:8123/index.html"
        redirected = "http://127.0.0.1:8123/login.html"
        self.assertNotEqual(navigation_digest(requested), navigation_digest(redirected))
        actual: dict[str, object] = {"origin": "http://127.0.0.1:8123", "navigation_digest": navigation_digest(redirected), "width": 800, "height": 600, "dpr": 1, "engine": "chromium", "version": "152.0.0.0", "locale": "en-US", "timezone": "UTC"}
        self.assertEqual(terminal_blocker({"success": True}, actual, {"origin": actual["origin"], "navigation_digest": navigation_digest(requested)}, {"width": 800, "height": 600, "dpr": 1}, {"engine": "chromium", "version": "152.0.0.0"}, {"locale": "en-US", "timezone": "UTC"}, True), "actual_navigation_digest_mismatch")

    def test_unsupported_engine_and_authenticated_terminal_never_launch(self) -> None:
        item = request("http://127.0.0.1:8123/index.html")
        plan = object_value(item["plan"])
        condition = object_value(plan["condition"])
        browser = object_list(condition["browsers"])[0]
        cell = object_list(plan["matrix"])[0]
        browser["engine"] = "firefox"
        result, capture, commands, session = collect_cell(item, plan, cell, Path(tempfile.mkdtemp()), 1_000_000, 0)
        self.assertIsNone(capture); self.assertEqual(commands, []); self.assertIsNone(session); self.assertEqual(result["terminal_blocker_id"], "unsupported_browser_engine_firefox")
        browser["engine"] = "chromium"; cell["expected_terminal"] = "authenticated_view"
        result, capture, commands, session = collect_cell(item, plan, cell, Path(tempfile.mkdtemp()), 1_000_000, 0)
        self.assertIsNone(capture); self.assertEqual(commands, []); self.assertIsNone(session); self.assertEqual(result["terminal_blocker_id"], "unsupported_authenticated_view")

    def test_all_unsupported_cells_admit_as_named_block_without_browser_commands(self) -> None:
        item = request("http://127.0.0.1:8123/index.html", engine="firefox")
        for host, platform in (("native", nullcontext()), ("unsupported", mock.patch.object(collector, "os", SimpleNamespace(name="nt")))):
            with self.subTest(host=host), platform, tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "collection"
                with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("browser must not launch")) as spawn:
                    if collector_ports.os.name == "posix":
                        receipt = collector.collect(item, output, 30)
                        self.assertEqual([command["operation"] for command in object_list(object_value(receipt["execution"])["command_results"])], ["capability_preflight"])
                        self.assertEqual(object_list(receipt["cells"])[0]["terminal_blocker_id"], "unsupported_browser_engine_firefox")
                        self.assertEqual(build_web_qa_observation(item["plan"], receipt)["verdict"], "BLOCK")
                    else:
                        with self.assertRaisesRegex(collector.CollectorError, "^unsupported_host_platform_posix_file_lock_required$"):
                            _ = collector.collect(item, output, 30)
                        self.assertFalse(output.exists())
                        self.assertEqual(list(Path(temporary).iterdir()), [])
                spawn.assert_not_called()

    def test_unsupported_host_refuses_chromium_without_output_or_browser_commands(self) -> None:
        item = request("http://127.0.0.1:8123/index.html")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "never-created"
            with mock.patch.object(collector, "os", SimpleNamespace(name="nt")), mock.patch.object(subprocess, "Popen", side_effect=AssertionError("browser must not launch")) as spawn:
                with self.assertRaisesRegex(collector.CollectorError, "^unsupported_host_platform_posix_file_lock_required$"):
                    _ = collector.collect(item, output, 30)
            spawn.assert_not_called()
            self.assertFalse(output.exists())
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_worst_case_png_reservation_scales_with_viewport_and_dpr(self) -> None:
        self.assertGreater(png_worst_case_bytes({"width": 7680, "height": 4320, "dpr": 4}), 2_000_000_000)
        self.assertGreater(png_worst_case_bytes({"width": 800, "height": 600, "dpr": 2}), png_worst_case_bytes({"width": 800, "height": 600, "dpr": 1}))

    def test_preexisting_capture_is_not_reserved_or_modified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary) / "cell.png"
            _ = candidate.write_bytes(b"unrelated")
            self.assertIsNone(reserve_capture_path(candidate))
            self.assertEqual(candidate.read_bytes(), b"unrelated")

    def test_malformed_or_missing_network_evidence_is_blocked_not_silently_dropped(self) -> None:
        with self.assertRaisesRegex(collector.CollectorError, "duration"):
            _ = project_network([{"url": "https://example.test/a", "status": 500, "method": "GET"}], {})
        with self.assertRaisesRegex(collector.CollectorError, "network_payload"):
            _ = project_network([{"url": "https://example.test/a", "status": "500", "method": "GET"}], {})

    def test_console_error_messages_are_evidence_and_malformed_a11y_is_rejected(self) -> None:
        evidence = project_console([{"type": "error", "text": "secret"}], [{"message": "uncaught"}])
        self.assertEqual(len(evidence), 2)
        with self.assertRaisesRegex(collector.CollectorError, "accessibility_payload"):
            _ = project_a11y([{"id": "x", "impact": "serious"}])

    def test_corrupt_cache_fails_closed_without_collecting(self) -> None:
        item = request("http://127.0.0.1:8123/index.html")
        plan = validate_request(item, 30)
        for host, platform in (("native", nullcontext()), ("unsupported", mock.patch.object(collector, "os", SimpleNamespace(name="nt")))):
            with self.subTest(host=host), platform, tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); cache = root / ".web-qa-collector-cache-v1"; cache.mkdir(mode=0o700)
                key = sha(canonical(item))
                entry = cache / f"{plan['run_id']}-{key}.json"
                _ = entry.write_text(json.dumps({"request_digest": key, "receipt": {"run_id": plan["run_id"], "release_status": "COLLECTED"}}), encoding="utf-8")
                original = entry.read_bytes()
                metadata = [(path.stat().st_mode, path.stat().st_mtime_ns) for path in (root, cache, entry)]
                blocker = "cached_receipt_invalid" if collector_ports.os.name == "posix" else "^unsupported_host_platform_posix_file_lock_required$"
                with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("browser must not launch")) as spawn:
                    with self.assertRaisesRegex(collector.CollectorError, blocker):
                        _ = collector.collect(item, root, 30)
                spawn.assert_not_called()
                self.assertEqual(list(root.iterdir()), [cache])
                self.assertEqual(list(cache.iterdir()), [entry])
                self.assertEqual(entry.read_bytes(), original)
                self.assertEqual([(path.stat().st_mode, path.stat().st_mtime_ns) for path in (root, cache, entry)], metadata)


if __name__ == "__main__":
    _ = unittest.main()
