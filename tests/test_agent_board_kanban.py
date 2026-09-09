"""Kanban foundation proofs; supplied schemas/callbacks are not host authority."""
from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
import copy
import importlib
import importlib.util
import json
from pathlib import Path
from threading import Barrier
import unittest
from typing import TYPE_CHECKING, Protocol, TypedDict, Unpack, runtime_checkable

if TYPE_CHECKING:
    from omh.workflows.agent_board import AgentBoard, AgentBoardRequest, HostIdentity, NativeAction

from _local_package import load_local_package

load_local_package()

# Frozen supported subset of the researched native schemas, not a fake executor.
FIELDS = {
    "create": ("title assignee", "parents idempotency_key initial_status model provider completion_contract"),
    "link": ("parent_id child_id", ""),
    "comment": ("task_id body", ""),
    "heartbeat": ("", "task_id note"),
    "request_review": ("summary", "task_id reviewer metadata"),
    "request_changes": ("reason", "task_id"),
    "block": ("reason", "task_id kind"),
    "unblock": ("task_id", ""),
    "complete": ("", "task_id summary result metadata created_cards"),
    "show": ("", "task_id"),
    "list": ("", "assignee status tenant include_archived limit"),
    "attachments": ("", "task_id"),
}


class SuppliedSchema(TypedDict):
    type: str
    properties: dict[str, dict[str, str]]
    required: list[str]


class PrepareOptions(TypedDict, total=False):
    host: HostIdentity | None
    schemas: Mapping[str, object]
    hooks: frozenset[str]
    board_is_safe: bool


class ObserveOptions(TypedDict, total=False):
    host: HostIdentity
    tool_name: str
    arguments: Mapping[str, object]
    result: object


@runtime_checkable
class BoardModule(Protocol):
    AgentBoard: type[AgentBoard]
    HostIdentity: type[HostIdentity]

    def board_reference(self, root_identity: str, board: str) -> str: ...


@runtime_checkable
class QaModule(Protocol):
    def run_case(self, case_id: str) -> Mapping[str, object]: ...


def supplied_schemas() -> dict[str, SuppliedSchema]:
    schemas: dict[str, SuppliedSchema] = {}
    for operation, (required, optional) in FIELDS.items():
        fields = (required + " " + optional + " board").split()
        properties = {name: {"type": "string"} for name in fields}
        for name in ("parents", "created_cards"):
            if name in properties:
                properties[name] = {"type": "array"}
        if "metadata" in properties:
            properties["metadata"] = {"type": "object"}
        if "limit" in properties:
            properties["limit"] = {"type": "integer"}
        if "include_archived" in properties:
            properties["include_archived"] = {"type": "boolean"}
        schemas["kanban_" + operation] = {
            "type": "object", "properties": properties, "required": required.split(),
        }
    schemas["delegate_task"] = {
        "type": "object", "properties": {"tasks": {"type": "array"}}, "required": ["tasks"],
    }
    return schemas


def request(operation: str = "create", request_id: str = "qa-create-1",
            arguments: dict[str, object] | None = None, **extra: object) -> dict[str, object]:
    if arguments is None:
        arguments = {"title": "qa-task", "assignee": "qa-profile"}
    return {"action": "prepare", "request_id": request_id, "coordination": "durable",
            "operation": operation, "board": "qa-board", "profile": "qa-profile",
            "arguments": arguments, **extra}


class AgentBoardFoundation(unittest.TestCase):
    def api(self) -> BoardModule:
        name = "omh.workflows.agent_board"
        spec = importlib.util.find_spec(name)
        self.assertIsNotNone(spec, "missing bounded Kanban action/receipt state machine")
        module = importlib.import_module(name)
        self.assertTrue(isinstance(module, BoardModule), "missing AgentBoard state machine")
        assert isinstance(module, BoardModule)
        return module

    def board(self) -> AgentBoard:
        api = self.api()
        return api.AgentBoard("qa-board", api.board_reference("host-root-identity", "qa-board"))

    def host(self, call: str = "prepare-1", session: str = "session-1",
             task: str = "host-task-1") -> HostIdentity:
        return self.api().HostIdentity(session, task, call)

    def prepare(self, board: AgentBoard, payload: Mapping[str, object] | None = None,
                **overrides: Unpack[PrepareOptions]) -> AgentBoardRequest:
        return board.prepare(payload or request(), host=overrides.get("host", self.host()),
                             schemas=overrides.get("schemas", supplied_schemas()),
                             hooks=overrides.get("hooks", frozenset({"pre_tool_call", "post_tool_call"})),
                             board_is_safe=overrides.get("board_is_safe", True))

    def action(self, prepared: AgentBoardRequest) -> NativeAction:
        action = prepared["native_action"]
        self.assertIsNotNone(action)
        assert action is not None
        return action

    def begin(self, board: AgentBoard, prepared: AgentBoardRequest, call: str = "native-1") -> bool:
        action = self.action(prepared)
        return board.begin(prepared["request_id"], host=self.host(call),
                           tool_name=action["tool_name"], arguments=action["arguments"])

    def observe(self, board: AgentBoard, prepared: AgentBoardRequest, native_result: object,
                call: str = "native-1", **overrides: Unpack[ObserveOptions]) -> dict[str, object] | None:
        action = self.action(prepared)
        return board.observe(prepared["request_id"], host=overrides.get("host", self.host(call)),
                             tool_name=overrides.get("tool_name", action["tool_name"]),
                             arguments=overrides.get("arguments", action["arguments"]),
                             result=overrides.get("result", json.dumps(native_result)))

    def test_k1_foundation_typed_actions_all_native_operations(self):
        cases: dict[str, tuple[dict[str, object], dict[str, str]]] = {
            "create": ({"title": "qa-task", "assignee": "qa-profile"}, {}),
            "link": ({"parent_id": "T1", "child_id": "T2"}, {}),
            "comment": ({"body": "ephemeral-body"}, {"task_id": "T1"}),
            "heartbeat": ({}, {"task_id": "T1"}),
            "request_review": ({"summary": "ephemeral-summary"}, {"task_id": "T1"}),
            "request_changes": ({"reason": "ephemeral-reason"}, {"task_id": "T1"}),
            "block": ({"reason": "ephemeral-reason"}, {"task_id": "T1"}),
            "unblock": ({}, {"task_id": "T1"}),
            "complete": ({"result": "ephemeral-result"}, {"task_id": "T1"}),
            "show": ({}, {"task_id": "T1"}),
            "list": ({"limit": 1}, {}),
            "attachments": ({}, {"task_id": "T1"}),
        }
        for operation, (arguments, extra) in cases.items():
            with self.subTest(operation=operation):
                board = self.board()
                prepared = self.prepare(board, request(operation, arguments=arguments, **extra))
                self.assertEqual(prepared["state"], "prepared")
                self.assertEqual(prepared["schema_version"], "agent_board_request/v1")
                self.assertEqual(self.action(prepared)["tool_name"], "kanban_" + operation)
                self.assertEqual(self.action(prepared)["arguments"]["board"], "qa-board")
                self.assertEqual(prepared["observed_receipts"], [])
                self.assertIsNone(board.status("qa-create-1")["native_action"])
                self.assertNotIn("ephemeral", json.dumps(board.snapshot()))

    def test_k2_foundation_capability_schema_hooks_identity_unavailable(self):
        cases: list[tuple[PrepareOptions, str]] = [({"schemas": {}}, "kanban_create"),
                                  ({"hooks": frozenset()}, "pre_tool_call"),
                                  ({"hooks": frozenset({"pre_tool_call"})}, "post_tool_call"),
                                  ({"host": None}, "host_identity"),
                                  ({"board_is_safe": False}, "board_binding")]
        for override, missing in cases:
            with self.subTest(missing=missing):
                result = self.prepare(self.board(), **override)
                self.assertEqual(result["state"], "unavailable")
                self.assertIn(missing, result["missing_capabilities"])
                self.assertIsNone(result["native_action"])
        schemas = supplied_schemas()
        _ = schemas["kanban_create"]["properties"].pop("board")
        result = self.prepare(self.board(), schemas=schemas)
        self.assertIn("schema:kanban_create", result["missing_capabilities"])
        schemas = supplied_schemas()
        schemas["kanban_create"]["required"].append("unsupported_native_required")
        self.assertEqual(self.prepare(self.board(), schemas=schemas)["state"], "unavailable")

    def test_k3_foundation_explicit_route_no_substitution(self):
        board = self.board()
        durable = self.prepare(board)
        self.assertEqual(durable["route"], "kanban")
        task = {"goal": "bounded", "context": "parent"}
        research = request("research", "research-1", {"tasks": [task]},
                           coordination="bounded_research")
        prepared = self.prepare(board, research)
        self.assertEqual(prepared["route"], "delegation")
        self.assertEqual(prepared["native_action"], {"tool_name": "delegate_task", "arguments": research["arguments"]})
        self.assertEqual(self.prepare(self.board(), research, schemas={})["state"], "unavailable")
        self.assertEqual(self.prepare(self.board(), schemas={})["route"], "kanban")
        task["model"] = "forbidden"
        with self.assertRaises(ValueError):
            _ = self.prepare(self.board(), research)

    def test_k4_foundation_digest_freeze_sequential_identity(self):
        board = self.board()
        prepared = self.prepare(board)
        self.assertEqual(self.action(prepared)["arguments"]["idempotency_key"], "qa-create-1")
        self.assertTrue(self.begin(board, prepared))
        receipt = self.observe(board, prepared, {"ok": True, "task_id": "T-native", "status": "ready"})
        assert receipt is not None
        self.assertEqual(receipt["task_id"], "T-native")
        retry = self.prepare(board, request(arguments={"assignee": "qa-profile", "title": "qa-task"},
                                            profile="second-profile"))
        self.assertEqual(retry["argument_digest"], prepared["argument_digest"])
        self.assertEqual(retry["request_ref"], prepared["request_ref"])
        self.assertEqual(retry["state"], "observed")
        self.assertIsNone(retry["native_action"])
        self.assertEqual(retry["observed_receipts"][0]["task_id"], "T-native")
        changed = request(arguments={"title": "changed", "assignee": "qa-profile"})
        refused = self.prepare(board, changed)
        self.assertEqual((refused["state"], refused["reason"]), ("denied", "request_digest_changed"))
        self.assertEqual(board.status("qa-create-1")["state"], "observed")

    def test_k4_foundation_simultaneous_begin_has_one_winner(self):
        board = self.board()
        prepared = self.prepare(board)
        gate = Barrier(3)
        def contender(call: str) -> tuple[bool, str]:
            _ = gate.wait(timeout=5)
            return self.begin(board, prepared, call), call
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(contender, "native-" + str(i)) for i in range(2)]
            _ = gate.wait(timeout=5)
            results = [future.result(timeout=5) for future in futures]
        self.assertEqual(sum(won for won, _ in results), 1)
        winner = next(call for won, call in results if won)
        _ = self.observe(board, prepared, {"ok": True, "task_id": "T1", "status": "ready"}, winner)
        self.assertEqual(len(board.status("qa-create-1")["observed_receipts"]), 1)

    def test_k5_foundation_closed_inputs_and_explicit_bindings(self):
        bad = [request(authorized=True), request(session_id="forged"), request(action="observe"),
               request(board="foreign"), request("heartbeat", arguments={}),
               request("complete", arguments={}, task_id="T1"),
               request("link", arguments={"parent_id": "T1", "child_id": "T1"}),
               request("show", arguments={"task_id": "T2"}, task_id="T1"),
               request(arguments={"title": "x", "assignee": "p", "board": "foreign"}),
               request(arguments={"title": "x", "assignee": "p", "idempotency_key": "different"}),
               request("list", arguments={"limit": 201}), request("list", arguments={"limit": True}),
               request("list", arguments={"status": "review"}), request("list", arguments={"cursor": "x"}),
               request("complete", arguments={"summary": "ok", "artifacts": []}, task_id="T1")]
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                _ = self.prepare(self.board(), payload)
        result = self.prepare(self.board(), request("show", arguments={"expected_revision": 4}, task_id="T1"))
        self.assertEqual(result["state"], "unavailable")
        self.assertIn("native_compare_and_swap", result["missing_capabilities"])

    def test_k5_foundation_expected_observation_rechecked_before_call(self):
        board = self.board()
        first = self.prepare(board)
        second = self.prepare(board, request("heartbeat", "heartbeat-1", {}, task_id="T1",
                                             expected_observation_ref="observation:0"))
        _ = self.begin(board, first)
        _ = self.observe(board, first, {"ok": True, "task_id": "T1", "status": "ready"})
        self.assertFalse(self.begin(board, second, "native-2"))
        stale = self.prepare(board, request("show", "show-1", {}, task_id="T1",
                                            expected_observation_ref="observation:0"))
        self.assertEqual((stale["state"], stale["reason"]), ("denied", "stale_observation"))

    def test_k5_foundation_foreign_replay_and_mutated_callbacks_do_not_bind(self):
        board = self.board()
        prepared = self.prepare(board)
        success = {"ok": True, "task_id": "T1", "status": "ready"}
        self.assertIsNone(self.observe(board, prepared, success))  # no pre
        self.assertFalse(board.begin("qa-create-1", host=self.host("native-1", session="foreign"),
                                     **self.action(prepared)))
        self.assertTrue(self.begin(board, prepared))
        before = board.snapshot()
        self.assertIsNone(self.observe(board, prepared, success, "foreign-call"))
        self.assertIsNone(self.observe(board, prepared, success, host=self.host("native-1", task="foreign")))
        self.assertEqual(board.snapshot(), before)
        receipt = self.observe(board, prepared, success, arguments={"board": "foreign"})
        assert receipt is not None
        self.assertEqual((receipt["state"], receipt["reason"]), ("failed", "arguments_changed"))
        self.assertNotIn("task_id", receipt)
        self.assertIsNone(self.observe(board, prepared, success))
        self.assertEqual(board.status("qa-create-1")["state"], "failed")

    def test_k5_foundation_result_identity_and_shape_validation(self):
        cases: list[tuple[dict[str, object], str, dict[str, object]]] = [({"ok": True, "task_id": "T2"}, "heartbeat", {}),
                 ({"ok": True, "parent_id": "T1", "child_id": "T3"}, "link", {"parent_id": "T1", "child_id": "T2"}),
                 ({"ok": True, "task_id": "T1", "board": "foreign"}, "heartbeat", {}),
                 ({"ok": "true", "task_id": "T1"}, "heartbeat", {}),
                 ({"ok": True, "task_id": "T1", "error": "private"}, "heartbeat", {}),
                 ({"ok": True, "task_id": "T1"}, "request_review", {"summary": "s"})]
        for result, operation, arguments in cases:
            with self.subTest(operation=operation, result=result):
                board = self.board()
                extra = {} if operation == "link" else {"task_id": "T1"}
                prepared = self.prepare(board, request(operation, arguments=arguments, **extra))
                _ = self.begin(board, prepared)
                receipt = self.observe(board, prepared, result)
                assert receipt is not None
                self.assertEqual(receipt["state"], "failed")
                self.assertTrue(receipt["requires_reconciliation"])
                self.assertNotIn("task_id", receipt)
                self.assertNotIn("private", json.dumps(board.snapshot()))

    def test_k6_foundation_separate_landed_operation_facts(self):
        cases: list[tuple[str, dict[str, object], dict[str, object], str]] = [
            ("create", {"title": "t", "assignee": "p"}, {"ok": True, "task_id": "T1", "status": "ready"}, "create"),
            ("link", {"parent_id": "T1", "child_id": "T2"}, {"ok": True, "parent_id": "T1", "child_id": "T2"}, "link"),
            ("comment", {"body": "body"}, {"ok": True, "task_id": "T1", "comment_id": 1}, "comment"),
            ("heartbeat", {}, {"ok": True, "task_id": "T1"}, "heartbeat"),
            ("block", {"reason": "r"}, {"ok": True, "task_id": "T1", "run_id": 1, "status": "blocked", "block_kind": "dependency"}, "block"),
            ("unblock", {}, {"ok": True, "task_id": "T1", "status": "ready"}, "unblock"),
            ("request_review", {"summary": "s"}, {"ok": True, "task_id": "T1", "run_id": 1, "status": "review"}, "review_requested"),
            ("request_changes", {"reason": "r"}, {"ok": True, "task_id": "T1", "run_id": 2, "status": "ready", "implementer": "p"}, "changes_requested"),
            ("complete", {"summary": "s"}, {"ok": True, "task_id": "T1", "run_id": 3}, "complete"),
        ]
        board = self.board()
        for n, (operation, arguments, result, fact) in enumerate(cases, 1):
            extra = {} if operation in ("create", "link") else {"task_id": "T1"}
            prepared = self.prepare(board, request(operation, f"request-{n}", arguments, **extra))
            self.assertTrue(self.begin(board, prepared, f"call-{n}"))
            receipt = self.observe(board, prepared, result, f"call-{n}")
            assert receipt is not None
            self.assertEqual(receipt["schema_version"], "agent_board_receipt/v1")
            self.assertEqual(receipt["state"], "observed")
            self.assertEqual(receipt["fact"], fact)
            self.assertEqual(receipt["observation_ref"], f"observation:{n}")
            self.assertEqual(receipt["argument_digest"], prepared["argument_digest"])
        snapshot = json.dumps(board.snapshot())
        for inferred in ('"dispatch"', '"approved"', '"ci"', '"merge"'):
            self.assertNotIn(inferred, snapshot)
        unavailable = self.prepare(board, request("dispatch", "dispatch-1", {}, task_id="T1"))
        self.assertEqual(unavailable["state"], "unavailable")

    def test_k6_foundation_show_list_and_attachment_metadata_only(self):
        board = self.board()
        show = self.prepare(board, request("show", "show-1", {}, task_id="T1"))
        _ = self.begin(board, show)
        receipt = self.observe(board, show, {"task": {"id": "T1", "status": "running", "current_run_id": 1, "description": "private-body"},
            "parents": [], "children": [], "comments": [{"body": "private-body"}], "events": [], "runs": [], "worker_context": "private-body"})
        assert receipt is not None
        self.assertEqual((receipt["task_id"], receipt["landed_status"], receipt["run_id"]), ("T1", "running", 1))
        self.assertEqual(receipt["fact"], "show")  # running/claim is not dispatch
        listing = self.prepare(board, request("list", "list-1", {"limit": 200}))
        self.assertIn("native_mutation_guard", listing["required_capabilities"])
        _ = self.begin(board, listing, "call-list")
        receipt = self.observe(board, listing, {"tasks": [{"id": f"T{i}", "title": "private-body"} for i in range(201)],
            "count": 201, "limit": 200, "truncated": True, "next_limit": 200, "promoted": 0}, "call-list")
        assert receipt is not None
        self.assertEqual(receipt["task_ids"], [f"T{i}" for i in range(200)])
        self.assertTrue(receipt["truncated"])
        attachments = self.prepare(board, request("attachments", "attach-1", {}, task_id="T1"))
        _ = self.begin(board, attachments, "call-attachments")
        receipt = self.observe(board, attachments, {"ok": True, "task_id": "T1", "attachments": [
            {"id": i, "size": 2, "content_type": "text/plain", "filename": "private-body", "stored_path": "/private-body", "uploaded_by": "private-body"} for i in range(1, 34)]}, "call-attachments")
        assert receipt is not None
        self.assertEqual(receipt["attachment_refs"], [{"board_ref": board.board_ref, "task_id": "T1", "attachment_id": i, "size": 2, "content_type": "text/plain"} for i in range(1, 33)])
        self.assertTrue(receipt["truncated"])
        self.assertNotIn("private-body", json.dumps(board.snapshot()))

    def test_k8_foundation_ambiguous_failed_and_missing_post_never_retry(self):
        for result in [None, {"error": "private-error"}, {"ok": False}, {"ok": True}]:
            with self.subTest(result=result):
                board = self.board()
                prepared = self.prepare(board)
                _ = self.begin(board, prepared)
                if result is not None:
                    _ = self.observe(board, prepared, result)
                retry = self.prepare(board)
                self.assertIsNone(retry["native_action"])
                self.assertNotEqual(retry["state"], "observed")
                self.assertTrue(retry["requires_reconciliation"])
                self.assertFalse(self.begin(board, prepared, "retry-call"))

    def test_k8_foundation_intake_bounds_and_malformed_attachments(self):
        success = {"ok": True, "task_id": "T1", "status": "ready"}
        oversized = [json.dumps(dict(success, extra=text), ensure_ascii=False)
                     for text in ("x" * (256 * 1024), chr(0x3b1) * 150000)]
        nested = '{"ok":true,"task_id":"T1","status":"ready","extra":' + "[" * 40 + "0" + "]" * 40 + "}"
        duplicate = '{"ok":true,"task_id":"T1","task_id":"T2","status":"ready"}'
        for raw in ['{"ok":true,', "[]", duplicate, nested, *oversized]:
            with self.subTest(length=len(raw)):
                board = self.board()
                prepared = self.prepare(board)
                _ = self.begin(board, prepared)
                receipt = self.observe(board, prepared, {}, result=raw)
                assert receipt is not None
                self.assertEqual(receipt["state"], "failed")
                if raw in oversized:
                    self.assertEqual(receipt["reason"], "result_intake_exceeded")
                elif raw == nested:
                    self.assertEqual(receipt["reason"], "json_depth_exceeded")
                elif raw == duplicate:
                    self.assertEqual(receipt["reason"], "duplicate_result_key")
        for attachment in [{"id": True, "size": 1, "content_type": "text/plain"},
                           {"id": 1, "size": -1, "content_type": "text/plain"},
                           {"id": 1, "size": 2, "content_type": "https://private-url"},
                           {"id": 1, "size": 2, "content_type": "text/plain", "task_id": "foreign"}]:
            with self.subTest(attachment=attachment):
                board = self.board()
                prepared = self.prepare(board, request("attachments", arguments={}, task_id="T1"))
                _ = self.begin(board, prepared)
                receipt = self.observe(board, prepared, {"ok": True, "task_id": "T1", "attachments": [attachment]})
                assert receipt is not None
                self.assertEqual(receipt["state"], "failed")

    def test_k8_foundation_argument_copy_digest_and_board_identity(self):
        api = self.api()
        self.assertEqual(api.board_reference("root", "board"), api.board_reference("root", "board"))
        self.assertNotEqual(api.board_reference("root", "board"), api.board_reference("other-root", "board"))
        board = self.board()
        arguments: dict[str, object] = {"title": "qa-task", "assignee": "qa-profile"}
        payload = request(arguments=arguments)
        prepared = self.prepare(board, payload)
        pristine = copy.deepcopy(prepared)
        arguments["title"] = "mutated"
        self.action(prepared)["arguments"]["title"] = "mutated"
        self.assertFalse(self.begin(board, prepared))
        self.assertTrue(self.begin(board, pristine))
        raw = json.dumps(board.snapshot())
        self.assertNotIn("host-root-identity", raw)
        self.assertNotIn("session-1", raw)
        self.assertNotIn("host-task-1", raw)
        self.assertNotIn("qa-task", raw)

    def test_k8_foundation_status_closed_and_projection_fact_cap(self):
        board = self.board()
        with self.assertRaises(ValueError):
            _ = board.prepare({"action": "status", "request_id": "unknown", "authorized": True})
        self.assertEqual(board.prepare({"action": "status", "request_id": "unknown"})["state"], "unavailable")
        for i in range(34):
            prepared = self.prepare(board, request("heartbeat", f"beat-{i}", {}, task_id="T1"))
            _ = self.begin(board, prepared, f"call-{i}")
            _ = self.observe(board, prepared, {"ok": True, "task_id": "T1"}, f"call-{i}")
        snapshot = board.snapshot()
        self.assertEqual(len(snapshot["operation_facts"]), 32)
        self.assertTrue(snapshot["truncated"])
        self.assertEqual(snapshot["observation_ref"], "observation:34")
        for i in range(34, 200):
            result = self.prepare(board, request("heartbeat", f"beat-{i}", {}, task_id="T1"))
            self.assertEqual(result["state"], "prepared")
        overflow = self.prepare(board, request("heartbeat", "beat-overflow", {}, task_id="T1"))
        self.assertEqual((overflow["state"], overflow["reason"]), ("unavailable", "request_capacity"))
        self.assertEqual(len(board.snapshot()["requests"]), 200)
        self.assertEqual(board.status("beat-0")["state"], "observed")

    def test_k6_foundation_native_integer_runs_and_string_edge_ids(self):
        board = self.board()
        prepared = self.prepare(board, request("show", "show-native", {}, task_id="T1"))
        _ = self.begin(board, prepared)
        receipt = self.observe(board, prepared, {
            "task": {"id": "T1", "status": "running", "current_run_id": 7},
            "parents": ["P1"], "children": ["C1"], "comments": [], "events": [],
            "runs": [{"id": 7, "pid": 99}], "worker_context": "private-context"})
        assert receipt is not None
        self.assertEqual(receipt["state"], "observed")
        self.assertEqual((receipt["run_id"], receipt["parent_ids"], receipt["child_ids"]), (7, ["P1"], ["C1"]))
        self.assertNotIn("dispatch", receipt.values())
        review = self.prepare(board, request("request_review", "review-native", {"summary": "s"}, task_id="T1"))
        _ = self.begin(board, review, "review-call")
        receipt = self.observe(board, review, {"ok": True, "task_id": "T1", "run_id": 7, "status": "review"}, "review-call")
        assert receipt is not None
        self.assertEqual((receipt["state"], receipt["run_id"]), ("observed", 7))

    def test_k8_foundation_unsolicited_lifecycle_fields_are_not_evidence(self):
        board = self.board()
        prepared = self.prepare(board, request("heartbeat", arguments={}, task_id="T1"))
        _ = self.begin(board, prepared)
        receipt = self.observe(board, prepared, {"ok": True, "task_id": "T1", "status": "done",
                                               "run_id": 99, "comment_id": 22})
        assert receipt is not None
        self.assertEqual(receipt["state"], "observed")
        self.assertEqual(receipt["fact"], "heartbeat")
        self.assertTrue({"landed_status", "run_id", "comment_id"}.isdisjoint(receipt))

    def test_k8_foundation_failed_task_needs_fresh_show_before_mutation(self):
        board = self.board()
        failed = self.prepare(board, request("heartbeat", "failed-heartbeat", {}, task_id="T1"))
        _ = self.begin(board, failed)
        _ = self.observe(board, failed, {"error": "claim touched but heartbeat failed"})
        waiting = self.prepare(board, request("complete", "complete-after-failure", {"summary": "s"}, task_id="T1"))
        self.assertEqual((waiting["state"], waiting["reason"]), ("denied", "reconciliation_required"))
        show = self.prepare(board, request("show", "reconcile-1", {}, task_id="T1"))
        self.assertTrue(self.begin(board, show, "show-call"))
        _ = self.observe(board, show, {"task": {"id": "T1", "status": "running", "current_run_id": 2},
            "parents": [], "children": [], "comments": [], "events": [], "runs": [], "worker_context": ""}, "show-call")
        ready = self.prepare(board, request("complete", "complete-after-failure", {"summary": "s"}, task_id="T1"))
        self.assertEqual(ready["state"], "prepared")
        self.assertEqual(board.status("failed-heartbeat")["state"], "failed")
        self.assertTrue(board.status("failed-heartbeat")["requires_reconciliation"])

    def test_k7_foundation_shipped_skill_characterization(self):
        from omh.skill_pack import builtin_skill_templates, builtin_skill_reference_templates
        from omh.skills.catalog_types import omh_skill_display_name

        root = Path(__file__).resolve().parents[1]
        templates = [template for template in builtin_skill_templates()
                     if omh_skill_display_name(template.name) == "omh-agent-board"]
        self.assertEqual(len(templates), 1)
        for template in templates:
            shipped = root / "skills" / omh_skill_display_name(template.name) / "SKILL.md"
            self.assertTrue(shipped.read_bytes() == template.content.encode("utf-8"), "shipped_skill_bytes_differ")
        for reference in builtin_skill_reference_templates():
            if omh_skill_display_name(reference.skill_name) == "omh-agent-board":
                shipped = root / "skills" / "omh-agent-board" / reference.relative_path
                self.assertTrue(shipped.read_bytes() == reference.content.encode("utf-8"), "shipped_reference_bytes_differ")

    def test_k8_foundation_qa_does_not_claim_integrated_pass(self):
        name = "five_issue_cases.kanban"
        self.assertIsNotNone(importlib.util.find_spec(name), "missing foundation-only QA producer")
        from five_issue_cases import kanban as qa
        assert isinstance(qa, QaModule)
        for case in ("K1", "K2", "K3", "K4", "K5", "K6", "K7", "K8"):
            result = qa.run_case(case)
            self.assertFalse(result["pass"])
            self.assertEqual(result["provenance"], {"kind": "local", "scope": "foundation",
                "native_required": case in {"K1", "K4", "K6", "K8"}, "native_available": False})
            self.assertEqual(result["cleanup"], {"owned_resources": [], "terminated_processes": [],
                "removed_paths": [], "verified_absent": True, "errors": []})
            self.assertTrue(result["blocked_reason"])


if __name__ == "__main__":
    _ = unittest.main()
