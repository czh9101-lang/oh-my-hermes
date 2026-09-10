"""Installed loop bridge tests. Host schemas/results below are attributed fixtures."""
from __future__ import annotations

from collections.abc import Mapping
import importlib
import importlib.util
import json
from pathlib import Path
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from tempfile import TemporaryDirectory
from typing import Protocol, runtime_checkable
import unittest

from _local_package import load_local_package

load_local_package()

from omh.workflows.agent_board import AgentBoardRequest, HostIdentity, NativeAction


class Bridge(Protocol):
    def prepare(self, payload: Mapping[str, object], *, host: HostIdentity | None,
                schemas: Mapping[str, object], hooks: frozenset[str]) -> AgentBoardRequest: ...
    def status(self, board: str, request_id: str) -> AgentBoardRequest: ...
    def pre(self, *, host: HostIdentity, tool_name: str,
            arguments: Mapping[str, object], schemas: Mapping[str, object],
            hooks: frozenset[str]) -> dict[str, object] | None: ...
    def post(self, *, host: HostIdentity, tool_name: str,
             arguments: Mapping[str, object], result: object) -> dict[str, object] | None: ...


@runtime_checkable
class BridgeModule(Protocol):
    def AgentBoardBridge(self, home: Path, *, root_identity: str | None) -> Bridge: ...
    def installed_bridge(self, board: str) -> Bridge: ...
    def pre_agent_board(self, kwargs: Mapping[str, object]) -> dict[str, object] | None: ...
    def post_agent_board(self, kwargs: Mapping[str, object]) -> None: ...
    def handler_identity(self, args: Mapping[str, object], kwargs: Mapping[str, object]) -> HostIdentity | None: ...


def bridge_api() -> BridgeModule:
    name = "omh.plugin_bundle.omh.agent_board_bridge"
    if importlib.util.find_spec(name) is None:
        raise AssertionError("missing durable installed normal-loop bridge")
    module = importlib.import_module(name)
    if not isinstance(module, BridgeModule):
        raise AssertionError("missing AgentBoardBridge API")
    return module


HOOKS = frozenset({"pre_tool_call", "post_tool_call"})


def native_action(prepared: AgentBoardRequest) -> NativeAction:
    action = prepared["native_action"]
    assert action is not None, prepared
    return action


def host(call: str = "prepare") -> HostIdentity:
    return HostIdentity("session", "host-task", call)


def create_contender(home: str, signal: object) -> bool:
    # Spawned processes share an exact bounded barrier, not scheduling sleeps.
    from five_issue_cases.kanban import request, supplied_schemas
    if not isinstance(signal, BarrierSignal):
        raise AssertionError("missing barrier")
    bridge = bridge_api().AgentBoardBridge(Path(home), root_identity="fixture-root")
    prepared = bridge.prepare(request(), host=host(), schemas=supplied_schemas(), hooks=HOOKS)
    action = native_action(prepared)
    _ = signal.wait(timeout=10)
    return bridge.pre(host=host("contender"), schemas=supplied_schemas(), hooks=HOOKS,
                      **action) is None


@runtime_checkable
class BarrierSignal(Protocol):
    def wait(self, timeout: float) -> object: ...


class AgentBoardIntegration(unittest.TestCase):
    def test_k1_bridge_installed_prepare_and_normal_hook_pair(self) -> None:
        from five_issue_cases.kanban import request, supplied_schemas
        module = bridge_api()
        with TemporaryDirectory() as tmp:
            bridge = module.AgentBoardBridge(Path(tmp), root_identity="fixture-root")
            prepared = bridge.prepare(request(), host=host(), schemas=supplied_schemas(), hooks=HOOKS)
            action = native_action(prepared)
            self.assertEqual(action["tool_name"], "kanban_create")
            self.assertEqual(prepared["observed_receipts"], [])
            self.assertIsNone(bridge.pre(host=host("create"), schemas=supplied_schemas(), hooks=HOOKS, **action))
            receipt = bridge.post(host=host("create"), **action,
                                  result='{"ok":true,"task_id":"T1","status":"ready"}')
            assert receipt is not None
            self.assertEqual((receipt["state"], receipt["task_id"]), ("observed", "T1"))
            self.assertEqual(bridge.status("qa-board", "qa-create-1")["observed_receipts"], [receipt])

    def test_k2_bridge_absent_capability_and_unsafe_binding(self) -> None:
        from five_issue_cases.kanban import request, supplied_schemas
        module = bridge_api()
        with TemporaryDirectory() as tmp:
            cases: list[tuple[str | None, Mapping[str, object], frozenset[str], str]] = [
                (None, supplied_schemas(), HOOKS, "board_binding"),
                ("fixture-root", {}, HOOKS, "kanban_create"),
                ("fixture-root", supplied_schemas(), frozenset(), "post_tool_call"),
            ]
            for root, schemas, hooks, missing in cases:
                bridge = module.AgentBoardBridge(Path(tmp), root_identity=root)
                result = bridge.prepare(request(), host=host(), schemas=schemas, hooks=hooks)
                self.assertEqual(result["state"], "unavailable")
                self.assertIn(missing, result["missing_capabilities"])
                self.assertIsNone(result["native_action"])
                self.assertEqual(result["observed_receipts"], [])

    def test_k3_bridge_routes_without_substitution(self) -> None:
        from five_issue_cases.kanban import request, supplied_schemas
        module = bridge_api()
        with TemporaryDirectory() as tmp:
            bridge = module.AgentBoardBridge(Path(tmp), root_identity="fixture-root")
            cases: list[tuple[str, str, dict[str, object], str]] = [
                ("durable", "create", {"title": "qa-task", "assignee": "qa-profile"}, "kanban"),
                ("bounded_research", "research", {"tasks": [{"goal": "bounded", "context": "parent"}]}, "delegation"),
            ]
            for coordination, operation, args, route in cases:
                prepared = bridge.prepare(request(operation, operation, args, coordination=coordination),
                                          host=host(), schemas=supplied_schemas(), hooks=HOOKS)
                self.assertEqual(prepared["route"], route)
                self.assertIsNotNone(prepared["native_action"])
                absent = bridge.prepare(request(operation, operation, args, coordination=coordination),
                                        host=host(), schemas={}, hooks=HOOKS)
                self.assertEqual(absent["state"], "unavailable")
                self.assertIsNone(absent["native_action"])

    def test_k4_bridge_restart_and_cross_process_single_admission(self) -> None:
        from five_issue_cases.kanban import request, supplied_schemas
        module = bridge_api()
        with TemporaryDirectory() as tmp:
            context = multiprocessing.get_context("spawn")
            with context.Manager() as manager:
                barrier = manager.Barrier(2)
                with ProcessPoolExecutor(max_workers=2, mp_context=context) as pool:
                    results = [pool.submit(create_contender, tmp, barrier) for _ in range(2)]
                    self.assertEqual(sum(item.result(timeout=20) for item in results), 1)
            bridge = module.AgentBoardBridge(Path(tmp), root_identity="fixture-root")
            interrupted = bridge.prepare(request(), host=host(), schemas=supplied_schemas(), hooks=HOOKS)
            self.assertTrue(interrupted["in_flight"])
            self.assertIsNone(interrupted["native_action"])
            self.assertTrue(interrupted["requires_reconciliation"])
        with TemporaryDirectory() as tmp:
            bridge = module.AgentBoardBridge(Path(tmp), root_identity="fixture-root")
            action = native_action(bridge.prepare(request(), host=host(), schemas=supplied_schemas(), hooks=HOOKS))
            self.assertIsNone(bridge.pre(host=host("call"), schemas=supplied_schemas(), hooks=HOOKS, **action))
            _ = bridge.post(host=host("call"), **action, result='{"ok":true,"task_id":"T1","status":"ready"}')
            restarted = module.AgentBoardBridge(Path(tmp), root_identity="fixture-root")
            repeated = restarted.prepare(request(), host=host(), schemas=supplied_schemas(), hooks=HOOKS)
            self.assertEqual(repeated["observed_receipts"][0]["task_id"], "T1")
            self.assertIsNone(repeated["native_action"])
            changed = restarted.prepare(request(arguments={"title": "changed", "assignee": "qa-profile"}),
                                        host=host(), schemas=supplied_schemas(), hooks=HOOKS)
            self.assertEqual(changed["reason"], "request_digest_changed")

    def test_k5_bridge_foreign_replay_stale_and_reload_privacy(self) -> None:
        from five_issue_cases.kanban import request, supplied_schemas
        module = bridge_api()
        with TemporaryDirectory() as tmp:
            bridge = module.AgentBoardBridge(Path(tmp), root_identity="fixture-root")
            first = native_action(bridge.prepare(request(), host=host(), schemas=supplied_schemas(), hooks=HOOKS))
            second = native_action(bridge.prepare(request("heartbeat", "beat", {}, task_id="T1",
                                                           expected_observation_ref="observation:0"),
                                                   host=host(), schemas=supplied_schemas(), hooks=HOOKS))
            self.assertIsNone(bridge.post(host=host("call"), **first, result='{"ok":true}'))
            self.assertIsNone(bridge.pre(host=host("call"), schemas=supplied_schemas(), hooks=HOOKS, **first))
            self.assertIsNone(bridge.post(host=HostIdentity("foreign", "host-task", "call"), **first, result='{"ok":true}'))
            receipt = bridge.post(host=host("call"), **first,
                                  result='{"ok":true,"task_id":"T1","status":"ready","body":"PRIVATE-SENTINEL"}')
            assert receipt is not None
            self.assertEqual(receipt["state"], "observed")
            self.assertIsNone(bridge.post(host=host("call"), **first, result='{"ok":true}'))
            self.assertIsNotNone(bridge.pre(host=host("beat"), schemas=supplied_schemas(), hooks=HOOKS, **second))
            for path in Path(tmp).rglob("*.json"):
                text = path.read_text()
                for forbidden in ("PRIVATE-SENTINEL", "qa-task", "fixture-root", '"native_action": {'):
                    self.assertNotIn(forbidden, text)
            other = module.AgentBoardBridge(Path(tmp), root_identity="other-root")
            self.assertEqual(other.status("qa-board", "qa-create-1")["state"], "unavailable")

    def test_k6_bridge_separate_operation_readback(self) -> None:
        from five_issue_cases.kanban import request, supplied_schemas
        module = bridge_api()
        with TemporaryDirectory() as tmp:
            bridge = module.AgentBoardBridge(Path(tmp), root_identity="fixture-root")
            cases: list[tuple[str, dict[str, object], dict[str, object]]] = [
                ("heartbeat", {}, {"ok": True, "task_id": "T1"}),
                ("request_review", {"summary": "private"}, {"ok": True, "task_id": "T1", "run_id": 1, "status": "review"}),
                ("complete", {"summary": "private"}, {"ok": True, "task_id": "T1", "run_id": 1}),
                ("show", {}, {"task": {"id": "T1", "status": "done"}, "parents": [], "children": [],
                              "comments": [], "events": [], "runs": [], "worker_context": "private"}),
            ]
            for operation, args, result in cases:
                action = native_action(bridge.prepare(request(operation, operation, args, task_id="T1"),
                                                       host=host(), schemas=supplied_schemas(), hooks=HOOKS))
                self.assertIsNone(bridge.pre(host=host(operation), schemas=supplied_schemas(), hooks=HOOKS, **action))
                receipt = bridge.post(host=host(operation), **action, result=json.dumps(result))
                assert receipt is not None
                self.assertEqual(receipt["operation"], operation)
                self.assertEqual(receipt["state"], "observed")
            restarted = module.AgentBoardBridge(Path(tmp), root_identity="fixture-root")
            self.assertEqual(restarted.status("qa-board", "complete")["observed_receipts"][0]["fact"], "complete")
            self.assertEqual(restarted.status("qa-board", "show")["observed_receipts"][0]["fact"], "show")
            self.assertNotIn('"dispatch"', json.dumps(restarted.status("qa-board", "show")))

    def test_k8_bridge_revocation_missing_post_failure_and_unrelated_tools(self) -> None:
        from five_issue_cases.kanban import request, supplied_schemas
        module = bridge_api()
        with TemporaryDirectory() as tmp:
            bridge = module.AgentBoardBridge(Path(tmp), root_identity="fixture-root")
            action = native_action(bridge.prepare(request(), host=host(), schemas=supplied_schemas(), hooks=HOOKS))
            self.assertIsNotNone(bridge.pre(host=host("denied"), schemas={}, hooks=HOOKS, **action))
            self.assertIsNone(bridge.pre(host=host("unrelated"), tool_name="terminal", arguments={"command": "true"},
                                         schemas={}, hooks=HOOKS))
            self.assertIsNone(bridge.pre(host=host("native-unrelated"), tool_name="kanban_heartbeat",
                                         arguments={"board": "qa-board", "task_id": "unrelated"}, schemas={}, hooks=HOOKS))
            self.assertIsNone(bridge.pre(host=host("call"), schemas=supplied_schemas(), hooks=HOOKS, **action))
            receipt = bridge.post(host=host("call"), **action, result='{"error":"PRIVATE-SENTINEL"}')
            assert receipt is not None
            self.assertEqual(receipt["state"], "failed")
            retry = bridge.prepare(request(), host=host(), schemas=supplied_schemas(), hooks=HOOKS)
            self.assertIsNone(retry["native_action"])
            self.assertTrue(retry["requires_reconciliation"])


    def test_k2_installed_qa_exercises_absent_surface(self) -> None:
        from five_issue_cases.kanban import run_case
        result = run_case("K2")
        self.assertEqual(result["provenance"]["scope"], "surface")
        self.assertTrue(result["pass"])
        self.assertEqual(result["observations"]["native_calls"], 0)
        self.assertEqual(result["observations"]["fixture_result_calls"], 0)
        self.assertTrue(result["cleanup"]["verified_absent"])
        self.assertTrue(result["cleanup"]["removed_paths"])

    def test_k8_bridge_schema_revocation_cannot_arm_stored_request(self) -> None:
        from five_issue_cases.kanban import request, supplied_schemas
        module = bridge_api()
        with TemporaryDirectory() as tmp:
            bridge = module.AgentBoardBridge(Path(tmp), root_identity="fixture-root")
            action = native_action(bridge.prepare(request(), host=host(), schemas=supplied_schemas(), hooks=HOOKS))
            schemas = supplied_schemas()
            _ = schemas["kanban_create"]["properties"].pop("board")
            directive = bridge.pre(host=host("call"), schemas=schemas, hooks=HOOKS, **action)
            self.assertIsNotNone(directive)
            self.assertFalse(bridge.status("qa-board", "qa-create-1")["in_flight"])

    def test_k8_installed_binding_revocation_preserves_unrelated_native(self) -> None:
        from unittest.mock import patch
        bridge_module = bridge_api()
        from five_issue_cases.kanban import request, supplied_schemas
        with TemporaryDirectory() as tmp, patch.object(bridge_module, "default_omh_home", return_value=Path(tmp)), \
                patch.object(bridge_module, "effective_root", return_value="fixture-root") as root, \
                patch.object(bridge_module, "host_capabilities", return_value=(supplied_schemas(), HOOKS)):
            bridge = bridge_module.installed_bridge("qa-board")
            action = native_action(bridge.prepare(request(), host=host(), schemas=supplied_schemas(), hooks=HOOKS))
            root.return_value = None
            directive = bridge_module.pre_agent_board({"session_id": "session", "task_id": "host-task", "tool_call_id": "call",
                                                       "tool_name": action["tool_name"], "args": action["arguments"]})
            self.assertIsNotNone(directive)
            self.assertFalse(bridge.status("qa-board", "qa-create-1")["in_flight"])
            unrelated = bridge_module.pre_agent_board({"session_id": "other", "task_id": "other", "tool_call_id": "other",
                                                       "tool_name": "kanban_heartbeat", "args": {"board": "qa-board", "task_id": "other"}})
            self.assertIsNone(unrelated)


    def test_k5_installed_post_rechecks_effective_binding(self) -> None:
        from unittest.mock import patch
        from five_issue_cases.kanban import request, supplied_schemas
        module = bridge_api()
        with TemporaryDirectory() as tmp, patch.object(module, "default_omh_home", return_value=Path(tmp)), \
                patch.object(module, "effective_root", return_value="fixture-root") as root, \
                patch.object(module, "host_capabilities", return_value=(supplied_schemas(), HOOKS)):
            bridge = module.installed_bridge("qa-board")
            action = native_action(bridge.prepare(request(), host=host(), schemas=supplied_schemas(), hooks=HOOKS))
            kwargs = {"session_id": "session", "task_id": "host-task", "tool_call_id": "call",
                      "tool_name": action["tool_name"], "args": action["arguments"]}
            self.assertIsNone(module.pre_agent_board(kwargs))
            root.return_value = None
            module.post_agent_board({**kwargs, "result": '{"ok":true,"task_id":"FOREIGN","status":"ready"}'})
            status = bridge.status("qa-board", "qa-create-1")
            self.assertEqual(status["state"], "failed")
            self.assertEqual(status["reason"], "board_binding_changed")
            self.assertNotIn("task_id", status["observed_receipts"][0])

    def test_k4_observed_create_readback_survives_new_host_scope(self) -> None:
        from five_issue_cases.kanban import request, supplied_schemas
        module = bridge_api()
        with TemporaryDirectory() as tmp:
            bridge = module.AgentBoardBridge(Path(tmp), root_identity="fixture-root")
            action = native_action(bridge.prepare(request(), host=host(), schemas=supplied_schemas(), hooks=HOOKS))
            self.assertIsNone(bridge.pre(host=host("call"), schemas=supplied_schemas(), hooks=HOOKS, **action))
            _ = bridge.post(host=host("call"), **action, result='{"ok":true,"task_id":"T1","status":"ready"}')
            restarted = module.AgentBoardBridge(Path(tmp), root_identity="fixture-root")
            result = restarted.prepare(request(profile="second-profile"), host=HostIdentity("new-session", "new-task", "new-call"),
                                       schemas=supplied_schemas(), hooks=HOOKS)
            self.assertEqual(result["state"], "observed")
            self.assertIsNone(result["native_action"])
            self.assertEqual(result["observed_receipts"][0]["task_id"], "T1")


    def test_k8_unrelated_native_repeat_is_not_a_replayed_omh_call(self) -> None:
        from five_issue_cases.kanban import request, supplied_schemas
        module = bridge_api()
        with TemporaryDirectory() as tmp:
            bridge = module.AgentBoardBridge(Path(tmp), root_identity="fixture-root")
            action = native_action(bridge.prepare(request("heartbeat", "beat", {}, task_id="T1"),
                                                  host=host(), schemas=supplied_schemas(), hooks=HOOKS))
            self.assertIsNone(bridge.pre(host=host("call"), schemas=supplied_schemas(), hooks=HOOKS, **action))
            _ = bridge.post(host=host("call"), **action, result='{"ok":true,"task_id":"T1"}')
            before = bridge.status("qa-board", "beat")
            # Same call ID is a replay; a later native-only call is not ours.
            self.assertIsNotNone(bridge.pre(host=host("call"), schemas=supplied_schemas(), hooks=HOOKS, **action))
            self.assertIsNone(bridge.pre(host=host("native-only"), schemas=supplied_schemas(), hooks=HOOKS, **action))
            self.assertIsNone(bridge.post(host=host("native-only"), **action, result='{"ok":true,"task_id":"T1"}'))
            self.assertEqual(bridge.status("qa-board", "beat"), before)


    def test_k8_store_corruption_and_symlinks_cannot_reopen_create(self) -> None:
        from five_issue_cases.kanban import request, supplied_schemas
        module = bridge_api()
        with TemporaryDirectory() as tmp:
            bridge = module.AgentBoardBridge(Path(tmp), root_identity="fixture-root")
            _ = bridge.prepare(request(), host=host(), schemas=supplied_schemas(), hooks=HOOKS)
            path = next((Path(tmp) / "runtime" / "agent-board").glob("*.json"))
            original = path.read_text()
            _ = path.write_text(original[:-1] + ',"private_body":"PRIVATE-SENTINEL"}')
            with self.assertRaises(ValueError):
                _ = bridge.prepare(request(), host=host(), schemas=supplied_schemas(), hooks=HOOKS)
            self.assertIn("PRIVATE-SENTINEL", path.read_text())  # no silent overwrite/migration
            path.unlink()
            target = Path(tmp) / "unrelated.json"
            _ = target.write_text(original)
            path.symlink_to(target)
            with self.assertRaises(ValueError):
                _ = bridge.prepare(request(), host=host(), schemas=supplied_schemas(), hooks=HOOKS)
            self.assertEqual(target.read_text(), original)


    def test_k5_create_key_cannot_move_to_a_foreign_board(self) -> None:
        from unittest.mock import patch
        from five_issue_cases.kanban import request, supplied_schemas
        module = bridge_api()
        with TemporaryDirectory() as tmp, patch.object(module, "default_omh_home", return_value=Path(tmp)), \
                patch.object(module, "effective_root", return_value="fixture-root"), \
                patch.object(module, "host_capabilities", return_value=(supplied_schemas(), HOOKS)):
            bridge = module.installed_bridge("qa-board")
            action = native_action(bridge.prepare(request(), host=host(), schemas=supplied_schemas(), hooks=HOOKS))
            directive = module.pre_agent_board({"session_id": "session", "task_id": "host-task", "tool_call_id": "call",
                "tool_name": "kanban_create", "args": {**action["arguments"], "board": "foreign-board"}})
            self.assertIsNotNone(directive)
            self.assertFalse(bridge.status("qa-board", "qa-create-1")["in_flight"])

    def test_k8_untracked_native_board_normalization_remains_host_owned(self) -> None:
        from unittest.mock import patch
        module = bridge_api()
        with TemporaryDirectory() as tmp, patch.object(module, "default_omh_home", return_value=Path(tmp)), \
                patch.object(module, "effective_root", return_value="fixture-root"), \
                patch.object(module, "host_capabilities", return_value=({}, HOOKS)):
            directive = module.pre_agent_board({"session_id": "session", "task_id": "host-task", "tool_call_id": "call",
                "tool_name": "kanban_show", "args": {"task_id": "native-only", "board": "QA-BOARD"}})
            self.assertIsNone(directive)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_k1_handler_identity_survives_hermes_hook_worker_thread(self) -> None:
        # Hermes runs pre_tool_call on invoke_hook's bounded worker thread under
        # a copied context; the handler then runs on the caller's thread.
        import contextvars
        from concurrent.futures import ThreadPoolExecutor
        module = bridge_api()
        args = {"action": "prepare", "request_id": "qa-create-1", "board": "qa-board"}
        kwargs = {"session_id": "session", "task_id": "host-task", "tool_call_id": "call",
                  "tool_name": "omh_agent_board", "args": dict(args)}
        with ThreadPoolExecutor(max_workers=1) as pool:
            _ = pool.submit(contextvars.copy_context().run, module.pre_agent_board, kwargs).result(timeout=10)
        identity = module.handler_identity(args, {"session_id": "session", "task_id": "host-task"})
        self.assertEqual(identity, HostIdentity("session", "host-task", "call"))
        # Consume-once: a second handler call for the same pre gets nothing.
        self.assertIsNone(module.handler_identity(args, {"session_id": "session", "task_id": "host-task"}))

    def test_k8_handler_identity_is_scoped_to_its_own_session_and_digest(self) -> None:
        module = bridge_api()
        handler_identity = module.handler_identity
        args = {"action": "prepare", "request_id": "qa-create-1", "board": "qa-board"}
        _ = module.pre_agent_board({"session_id": "session", "task_id": "host-task", "tool_call_id": "call",
                                    "tool_name": "omh_agent_board", "args": dict(args)})
        _ = module.pre_agent_board({"session_id": "other", "task_id": "other-task", "tool_call_id": "other",
                                    "tool_name": "omh_agent_board", "args": dict(args)})
        # A foreign session cannot consume this session's pre; a changed digest cannot either.
        self.assertIsNone(handler_identity({**args, "board": "foreign"}, {"session_id": "session", "task_id": "host-task"}))
        self.assertIsNone(handler_identity(args, {"session_id": "session", "task_id": "wrong-task"}))
        self.assertEqual(handler_identity(args, {"session_id": "other", "task_id": "other-task"}),
                         HostIdentity("other", "other-task", "other"))
        # The digest mismatch above consumed session's pre; nothing remains.
        self.assertIsNone(handler_identity(args, {"session_id": "session", "task_id": "host-task"}))


if __name__ == "__main__":
    _ = unittest.main()
