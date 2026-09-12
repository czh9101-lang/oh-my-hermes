#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# Run from the checkout with an invocation-owned official Hermes source fixture:
# uv run python tools/qa/seven_issues_observer_native.py --host-source PATH --host-python PATH
"""Exercise the original upstream member projector and real per-consumer queue."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from seven_issues_observer import JSON, cli


def exercise(host: Path, homes: Path) -> dict[str, JSON]:
    os.environ.update(HERMES_HOME=str(homes / "hermes"), OMH_HOME=str(homes / "omh"), HOME=str(homes))
    (homes / "hermes").mkdir()
    (homes / "hermes" / "config.yaml").write_text(
        "plugins:\n  entries:\n    omh:\n      settings:\n        group_chat_activity:\n          enabled: false\n", encoding="utf-8")
    sys.path.insert(0, str(host))
    plugins = importlib.import_module("hermes_cli.plugins")
    stream = importlib.import_module("agent.plugin_stream_hooks")
    producer = importlib.import_module("tui_gateway.hosted_room_member_activity")
    registry = importlib.import_module("tools.registry").registry
    assert "on_room_member_activity" in plugins.VALID_HOOKS
    bundle = ROOT / "src/plugin_bundle/omh"
    spec = importlib.util.spec_from_file_location("_qa_omh_native", bundle / "__init__.py", submodule_search_locations=[str(bundle)])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    manager = plugins.get_plugin_manager()
    ctx = plugins.PluginContext(plugins.PluginManifest(name="omh", path=bundle), manager)
    coordinates: dict[str, str | int] = {field: f"RAW_{field.upper()}_SENTINEL" for field in
                   ("room_id", "thread_id", "member_id", "turn_id", "task_id")}
    coordinates["execution_generation"] = 1
    sessions = {"hidden-fixture": {"_hosted_room_task": coordinates}}
    frame = {"method": "event", "params": {"type": "tool.start", "session_id": "hidden-fixture", "seq": 1,
             "payload": {"args": {"token": "RAW_PAYLOAD_SENTINEL"}, "text": "RAW_PAYLOAD_SENTINEL"}}}
    release, entered = Event(), Event()
    dispatchers = []
    adapter = None
    try:
        module.register(ctx)
        assert not manager.iter_hook_callbacks("on_room_member_activity")
        assert not producer.project_room_member_activity(frame, sessions)
        assert not list((homes / "omh").rglob("*activity*"))
        assert spec.name + ".native_activity_observer" not in sys.modules
        ctx._memory_provider.shutdown()
        manager.unload("omh")
        # Enable through the real profile-local settings API, never the installed profile.
        ctx.set_config("group_chat_activity", {"enabled": True})
        module.register(ctx)
        callbacks = manager.iter_hook_callbacks("on_room_member_activity")
        if not callbacks:
            queued = producer.project_room_member_activity(frame, sessions)
            print(json.dumps({"native_callback_count": 0, "producer_enqueued": queued}), flush=True)
            assert queued, "original native producer did not enqueue OMH activity"
        assert len(callbacks) == 1
        adapter = callbacks[0].__self__
        assert adapter.engine.flush()
        engine_module = importlib.import_module(spec.name + ".activity_observer")
        write = engine_module.atomic_write_json

        def held_write(*args, **kwargs):
            entered.set()
            if not release.wait(30):
                raise TimeoutError("native fixture writer release missing")
            return write(*args, **kwargs)

        with patch.object(engine_module, "atomic_write_json", held_write):
            try:
                # Exact upstream enqueue happens before the bounded queue-completion wait.
                assert producer.project_room_member_activity(frame, sessions)
                assert entered.wait(10), "OMH aggregation did not reach storage"
                # Replay the original frame; payload changes carry no identity or metrics.
                assert producer.project_room_member_activity(frame, sessions)
                for seq, kind in ((2, "tool.complete"), (4, "tool.start"), (5, "error"),
                                  (6, "approval.request"), (7, "message.delta"), (8, "reasoning.delta"),
                                  (9, "tool.output_risk"), (10, "message.interim")):
                    event = {"method": "event", "params": dict(frame["params"], seq=seq, type=kind)}
                    assert producer.project_room_member_activity(event, sessions)
                dispatchers = list(stream._dispatchers.values())
                assert len(dispatchers) == 1
                for dispatcher in dispatchers:
                    with dispatcher.events.all_tasks_done:
                        assert dispatcher.events.all_tasks_done.wait_for(
                            lambda: dispatcher.events.unfinished_tasks == 0, timeout=10), "native callback blocked on OMH storage"
                # The host queue has completed callbacks while OMH's writer is still held.
                assert not release.is_set()
                assert "RAW_" not in repr(adapter.engine._pending)
            finally:
                release.set()
            assert adapter.engine.flush()
        tool = registry.get_entry("omh_status", scope=manager.scope_key)
        assert tool is not None
        status = json.loads(tool.handler({}))["group_chat_activity"]
        assert status["compatibility"] == "on_room_member_activity/v1"
        assert status["readiness"] == "ready"
        assert status["gapped"] >= 1
        assert status["rejected"] == 0
        # Existing turn-finalization hook is not a room-terminal fact.
        for callback in manager.iter_hook_callbacks("on_session_end"):
            callback(session_id="unrelated-session", task_id=coordinates["task_id"], completed=True)
        before = cli(["runtime", "session-receipt", "list", "--json"], homes)
        assert before.returncode == 0 and json.loads(before.stdout)["receipts"] == []
        ctx._memory_provider.shutdown()
        manager.unload("omh")
        assert adapter.engine.close(), "native unload did not drain"
        listing = cli(["runtime", "session-receipt", "list", "--json"], homes)
        assert listing.returncode == 0, listing.stderr
        receipts = json.loads(listing.stdout)["receipts"]
        assert len(receipts) == 1
        receipt = receipts[0]
        assert receipt["boundary"]["kind"] == "process_exit" and not receipt["boundary"]["final"]
        assert receipt["observed_interval"]["coverage"] != "full_session"
        assert receipt["metrics"]["tool_calls"] == {"availability": "observed", "measurement": "floor", "value": 2}
        assert receipt["metrics"]["tool_errors"]["value"] is None
        for path in (homes / "omh").rglob("*"):
            if path.is_file():
                assert b"RAW_" not in path.read_bytes(), "raw metadata reached OMH state"
        doctor = cli(["doctor", "--json"], homes)
        check = next(c for c in json.loads(doctor.stdout)["checks"] if c["name"] == "group_chat_activity")
        return {"scenario": "supported-host-lifecycle", "native_collection": "observed_fixture_fire_path",
                "disabled_native_callback_count": 0, "disabled_producer_enqueued": False, "disabled_observer_state": False,
                "native_terminal": "unsupported", "producer_sha256": hashlib.sha256(Path(producer.__file__).read_bytes()).hexdigest(),
                "native_hook": producer.HOOK_NAME, "status_before_unload": status, "cli_exit": listing.returncode,
                "cli_output": json.loads(listing.stdout), "doctor_exit": doctor.returncode, "doctor_check": check,
                "native_callbacks_completed_while_storage_held": True, "raw_state_retained": False}
    finally:
        release.set()
        manager.unload("omh")
        stream.shutdown_plugin_stream_hook_dispatcher(timeout=10)
        assert all(d.thread is None or not d.thread.is_alive() for d in dispatchers)
        if adapter is not None:
            assert adapter.engine.close()
        assert not any(handle.active for handle in manager._registration_order)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-source", required=True, type=Path)
    parser.add_argument("--host-python", type=Path)
    parser.add_argument("--child", action="store_true")
    args = parser.parse_args()
    if not (args.host_source / "tui_gateway/hosted_room_member_activity.py").is_file():
        print(json.dumps({"exit": 3, "reason": "member_activity_contract_unsupported", "cleanup": {"verified_absent": True}}))
        return 3
    if not args.child:
        assert args.host_python is not None
        if not args.host_python.is_file():
            print(json.dumps({"exit": 3, "reason": "host_interpreter_missing", "cleanup": {"verified_absent": True}}))
            return 3
        process = subprocess.run([str(args.host_python), str(Path(__file__).resolve()), "--host-source", str(args.host_source), "--child"],
            env=dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1"), text=True, capture_output=True, timeout=90)
        sys.stdout.write(process.stdout)
        sys.stderr.write(process.stderr)
        return process.returncode
    with TemporaryDirectory(prefix="omh-native-qa-") as tmp:
        homes = Path(tmp)
        result = exercise(args.host_source.resolve(), homes)
    assert not homes.exists()
    result["cleanup"] = {"verified_absent": True, "workers_reaped": True, "registrations_disposed": True}
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
