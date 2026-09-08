#!/usr/bin/env -S uv run --isolated --offline --python 3.11
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["pyyaml==6.0.3", "requests==2.33.0", "httpx[socks]==0.28.1"]
# ///
"""Prove OMH's native egress Guard through installed Hermes dispatch; sends no network traffic."""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from typing import Protocol, final

TARGET = "egress_probe_sensitive_tool"
ASYNC_TARGET = "egress_probe_async_sensitive_tool"
PRIVATE_TOKEN = "__omh_egress_attempt_token"
FINAL_BODY = "rewritten-final-body"
SECRET = "private-model-token-must-not-persist"
SAFE_EFFECT_CALLS: list[dict[str, object]] = []
HANDLER_ENTRIES: list[dict[str, object]] = []


class _ReceiptStore(Protocol):
    def public_rows(self) -> list[dict[str, object]]: ...


store: _ReceiptStore | None = None


@final
class _Guardrails:
    def before_call(self, _name: str, _args: dict[str, object]) -> SimpleNamespace:
        return SimpleNamespace(allows_execution=True)


@final
class _Agent:
    session_id = "session-native-proof"
    _current_turn_id = "turn-native-proof"
    _current_api_request_id = "request-native-proof"
    quiet_mode = False
    tool_progress_mode = "off"
    verbose_logging = False
    valid_tool_names = [TARGET]
    enabled_toolsets = None
    disabled_toolsets = None
    _context_engine_tool_names: set[str] = set()
    _memory_manager = None
    _checkpoint_mgr = SimpleNamespace(enabled=False)
    tool_progress_callback = None
    tool_start_callback = None
    _current_tool = ""

    def __init__(self) -> None:
        self._tool_guardrails = _Guardrails()

    def _touch_activity(self, _label: str) -> None:
        return None


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _digest(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(canonical).hexdigest()


def _public_digest(value: object) -> str:
    return base64.urlsafe_b64encode(bytes.fromhex(_digest(value))).rstrip(b"=").decode("ascii")


def _metadata(entry: object) -> tuple[object, ...]:
    return tuple(getattr(entry, name) for name in (
        "schema", "toolset", "check_fn", "requires_env", "is_async", "description", "emoji",
        "max_result_size_chars", "dynamic_schema_overrides",
    ))


def _safe_effect_result(target: str, args: dict[str, object], kwargs: dict[str, object]) -> str:
    # This is deliberately first: a handler which raises during its own checks
    # must still count as a handler entry.
    HANDLER_ENTRIES.append({"target": target, "args": dict(args), "kwargs": dict(kwargs)})
    if store is None:
        raise RuntimeError("safe effect ran without the standalone OMH receipt store")
    rows = store.public_rows()
    attempts = [row for row in rows if row["row_type"] == "attempt" and row["tool_name"] == target]
    _require(len(attempts) == 1, "safe effect ran before exactly one durable attempt existed for its target")
    attempt = attempts[0]
    _require(args == {"body": FINAL_BODY}, f"safe effect did not receive final rewritten args: {args!r}")
    _require(PRIVATE_TOKEN not in args, "private correlation token reached the safe effect")
    _require(attempt["request_fingerprint"] == _public_digest(args), "durable request digest was not the final rewritten args")
    _require(PRIVATE_TOKEN not in json.dumps(rows, sort_keys=True), "private correlation token reached durable storage")
    _require(SECRET not in json.dumps(rows, sort_keys=True), "unrewritten private model input reached durable storage")
    SAFE_EFFECT_CALLS.append({"target": target, "args": dict(args), "kwargs": dict(kwargs)})
    return json.dumps({"effect": "safe-spy-observed", "target": target}, sort_keys=True)


def _safe_effect(args: dict[str, object], **kwargs: object) -> str:
    return _safe_effect_result(TARGET, args, kwargs)


async def _await_value(value: str) -> str:
    return value


async def _safe_async_effect(args: dict[str, object], **kwargs: object) -> str:
    return await _await_value(_safe_effect_result(ASYNC_TARGET, args, kwargs))


def _write_rewriter(plugins: Path, *, forge: bool = False) -> None:
    name = "proof_forger" if forge else "proof_rewriter"
    plugin = plugins / name
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text(
        f"name: {name}\nversion: 0.1.0\ndescription: Native egress proof sibling.\n", encoding="utf-8"
    )
    token = f'"{PRIVATE_TOKEN}": "forged-token", ' if forge else ""
    forged_guard = "    if _kwargs.get(\"tool_call_id\") != \"call-forged\":\n        return None\n" if forge else ""
    (plugin / "__init__.py").write_text(
        "def _pre_tool_call(*, tool_name, **_kwargs):\n"
        f"    if tool_name not in {(TARGET, ASYNC_TARGET)!r}:\n        return None\n"
        + forged_guard
        + ("" if forge else ("    if _kwargs.get(\"tool_call_id\") == \"call-validation\":\n" f"        return {{\"action\": \"modify\", \"args\": {{\"body\": {FINAL_BODY!r}, \"validation\": float(\"nan\")}}}}\n"))
        + f"    return {{\"action\": \"modify\", \"args\": {{{token}\"body\": {FINAL_BODY!r}}}}}\n"
        + "\ndef register(ctx):\n    ctx.register_hook(\"pre_tool_call\", _pre_tool_call)\n",
        encoding="utf-8",
    )


def _write_home(root: Path, source: Path, *, name: str, enabled: list[str], allow_override: bool,
                egress_enabled: bool = True, forge: bool = False) -> Path:
    home = root / name
    plugins = home / "plugins"
    plugins.mkdir(parents=True)
    shutil.copytree(source, plugins / "omh")
    _write_rewriter(plugins)
    if forge:
        _write_rewriter(plugins, forge=True)
    settings = (
        "      egress_attempts:\n"
        f"        enabled: {str(egress_enabled).lower()}\n"
        f"        omh_home: {json.dumps(str(home / 'omh-state'))}\n"
        "        tools:\n"
        + "".join(
            f"          {target}:\n"
            "            action_class: message_send\n"
            "            destination_class: chat_channel\n"
            "            destination_arg: body\n"
            "            payload_arg: body\n"
            for target in (TARGET, ASYNC_TARGET)
        )
    )
    config = "plugins:\n  enabled:\n" + "".join(f"    - {entry}\n" for entry in enabled)
    config += "  entries:\n    omh:\n"
    config += f"      allow_tool_override: {'true' if allow_override else 'false'}\n"
    config += "      settings:\n" + "".join("  " + line if line.strip() else line for line in settings.splitlines(keepends=True))
    (home / "config.yaml").write_text(config, encoding="utf-8")
    return home


def _native_dispatch(target: str, tool_call_id: str, args: dict[str, object] | None = None):
    executor = importlib.import_module("agent.tool_executor")
    agent = _Agent()
    ref = executor._ToolCallRef(target, args or {"body": SECRET}, "task-native-proof", tool_call_id, [])
    sequential = executor._resolve_sequential_dispatch(agent, ref, [])
    state = executor._ManagedToolResult(result=None, args=ref.args, middleware_trace=ref.trace, blocked=False, dispatched=False)
    result = executor._dispatch_authorized_once(
        agent, state, ref, execute=sequential.execute, scope_block=None, display_index=None,
        begin_execution=None, authorization_gate=None,
    )
    ref.emit_post(agent, result)
    return state, ref, result


def _rows(store: _ReceiptStore) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    rows = store.public_rows()
    return rows, [row for row in rows if row["row_type"] == "attempt"]


class _NoopContext:
    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        return False


def _locked_store(receipt_store: _ReceiptStore, _receipt_module: object):
    receipt_store.open_attempt(
        session_id="seed-session", tool_call_id="seed-call", tool_name=TARGET,
        action_class="message_send", destination_class="chat_channel",
        request_fingerprint=_digest({"seed": "request"}), effect_id=_digest({"seed": "effect"}),
        destination_digest=_digest({"seed": "destination"}), payload_digest=_digest({"seed": "payload"}),
        payload_bytes=4,
    )
    holder = sqlite3.connect(receipt_store.database_path)
    holder.execute("BEGIN IMMEDIATE")

    class _LockContext:
        def __enter__(self):
            return self

        def __exit__(self, _type, _value, _traceback):
            holder.rollback()
            holder.close()
            return False

    return _LockContext()


def _assert_store_failure(*, label: str, home: Path, manager: object, receipt_module: object, prepare) -> float:
    """Exercise a real wrapped dispatch after one narrow attempt-store failure."""
    global store
    receipt_store: _ReceiptStore = receipt_module.AttemptStore(home / "omh-state")
    store = receipt_store
    with prepare(receipt_store, receipt_module):
        before_entries = len(HANDLER_ENTRIES)
        if label == "corrupt-db":
            before_bytes = receipt_store.database_path.read_bytes()
        else:
            before_rows, before_attempts = _rows(receipt_store)
        started = time.monotonic()
        state, _, result = _native_dispatch(TARGET, f"call-{label}")
        elapsed = time.monotonic() - started
        _require(not state.blocked and "egress attempt was not durably recorded" in str(result),
                 f"{label} did not fail closed at the native wrapper: {result!r}")
        _require(elapsed < 0.1, f"{label} did not block within 100ms ({elapsed * 1000:.1f}ms)")
        _require(len(HANDLER_ENTRIES) == before_entries, f"{label} entered the real handler")
        if label == "corrupt-db":
            _require(receipt_store.database_path.read_bytes() == before_bytes,
                     "corrupt-db replaced the temporary database")
        else:
            rows, attempts = _rows(receipt_store)
            _require(len(rows) == len(before_rows) and len(attempts) == len(before_attempts),
                     f"{label} added an attempt despite blocking")
    _require(manager.unload("omh"), f"{label} OMH plugin did not unload")
    return elapsed * 1000


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-root", type=Path, required=True)
    parser.add_argument("--omh-source", type=Path, required=True)
    parser.add_argument("--expected-hermes-revision")
    options = parser.parse_args()
    hermes_root = options.hermes_root.resolve()
    omh_source = options.omh_source.resolve()
    _require((hermes_root / "hermes_cli").is_dir(), f"invalid Hermes root: {hermes_root}")
    _require((omh_source / "egress_attempts.py").is_file(), f"invalid OMH plugin source: {omh_source}")
    if options.expected_hermes_revision:
        revision = subprocess.run(["git", "-C", str(hermes_root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        _require(revision == options.expected_hermes_revision, "Hermes revision differs from the requested proof target")
    sys.path.insert(0, str(hermes_root))
    get_plugin_manager = importlib.import_module("hermes_cli.plugins").get_plugin_manager
    registry = importlib.import_module("tools.registry").registry

    registry.register(
        name=TARGET, toolset="safe-native-proof", schema={"name": TARGET, "parameters": {"type": "object", "properties": {"body": {"type": "string"}}}},
        handler=_safe_effect, check_fn=lambda: True, requires_env=["PROOF_NEVER_SENDS"], is_async=False,
        description="In-process safe spy; never sends", emoji="P",
    )
    registry.register(
        name=ASYNC_TARGET, toolset="safe-native-proof", schema={"name": ASYNC_TARGET, "parameters": {"type": "object", "properties": {"body": {"type": "string"}}}},
        handler=_safe_async_effect, check_fn=lambda: True, requires_env=["PROOF_NEVER_SENDS"], is_async=True,
        description="In-process async safe spy; never sends", emoji="A",
    )
    original = registry.get_entry(TARGET)
    async_original = registry.get_entry(ASYNC_TARGET)
    _require(original is not None and async_original is not None, "safe proof targets did not register")
    original_metadata = _metadata(original)
    async_original_metadata = _metadata(async_original)

    with tempfile.TemporaryDirectory(prefix="native-omh-egress-proof-") as raw:
        root = Path(raw)
        bundled = root / "empty-bundled"
        bundled.mkdir()
        os.environ["HERMES_BUNDLED_PLUGINS"] = str(bundled)

        disabled_home = _write_home(root, omh_source, name="disabled", enabled=["omh"], allow_override=False, egress_enabled=False)
        os.environ["HERMES_HOME"] = str(disabled_home)
        disabled_manager = get_plugin_manager()
        disabled_manager.discover_and_load()
        _require(registry.get_entry(TARGET) is original, "disabled OMH changed registry dispatch")
        _require(_metadata(registry.get_entry(TARGET)) == original_metadata, "disabled OMH changed target metadata")
        _require(registry.get_entry(ASYNC_TARGET) is async_original and _metadata(registry.get_entry(ASYNC_TARGET)) == async_original_metadata, "disabled OMH changed async target metadata")
        disabled_module = disabled_manager._plugins["omh"].module
        _require(disabled_module is not None, "disabled OMH plugin did not load")
        _require(disabled_module.__name__ + ".egress_attempts" not in sys.modules, "disabled OMH imported the egress guard")
        _require(not (disabled_home / "omh-state" / "runtime").exists(), "disabled OMH performed attempt I/O")

        denied_home = _write_home(root, omh_source, name="denied", enabled=["proof_rewriter", "omh"], allow_override=False)
        os.environ["HERMES_HOME"] = str(denied_home)
        denied_manager = get_plugin_manager()
        denied_manager.discover_and_load()
        denied_state, _, denied_result = _native_dispatch(TARGET, "call-denied")
        _require(denied_state.blocked and "wrapper is inactive" in str(denied_result), "denied override did not fail closed")
        _require(not SAFE_EFFECT_CALLS, "denied override reached safe effect")
        _require(not (denied_home / "omh-state" / "runtime").exists(), "denied override created a durable attempt")

        allowed_home = _write_home(
            root, omh_source, name="allowed", enabled=["proof_rewriter", "omh", "proof_forger"],
            allow_override=True, forge=True,
        )
        os.environ["HERMES_HOME"] = str(allowed_home)
        manager = get_plugin_manager()
        manager.discover_and_load()
        wrapped = registry.get_entry(TARGET)
        _require(wrapped is not None and wrapped is not original, "granted override did not replace registry entry")
        _require(_metadata(wrapped) == original_metadata, "wrapped entry did not preserve target metadata")
        async_wrapped = registry.get_entry(ASYNC_TARGET)
        _require(async_wrapped is not None and async_wrapped is not async_original and async_wrapped.is_async, "granted override did not preserve async dispatch")
        _require(_metadata(async_wrapped) == async_original_metadata, "wrapped async entry did not preserve target metadata")
        module = manager._plugins["omh"].module
        _require(module is not None, "standalone OMH plugin did not load")
        receipt_module = importlib.import_module(module.__name__ + ".egress_attempt_receipts")
        receipt_store: _ReceiptStore = receipt_module.AttemptStore(allowed_home / "omh-state")
        global store
        store = receipt_store

        state, ref, result = _native_dispatch(TARGET, "call-success")
        _require(not state.blocked and json.loads(result) == {"effect": "safe-spy-observed", "target": TARGET}, "real executor did not dispatch the wrapped target")
        _require(state.args == ref.args and state.args["body"] == FINAL_BODY and PRIVATE_TOKEN in state.args, "sibling rewrite and Guard token were not final executor args")
        _require(len(SAFE_EFFECT_CALLS) == 1, "successful dispatch did not reach safe effect exactly once")
        ref.emit_post(_Agent(), result)
        rows, attempts = _rows(receipt_store)
        _require(len(attempts) == 1 and len(rows) == 2 and rows[1]["terminal_state"] == "returned", "duplicate terminal post was not idempotent")

        async_state, async_ref, async_result = _native_dispatch(ASYNC_TARGET, "call-async-success")
        _require(not async_state.blocked and json.loads(async_result) == {"effect": "safe-spy-observed", "target": ASYNC_TARGET}, "native dispatcher did not await the wrapped async target")
        _require(async_state.args == async_ref.args and async_state.args["body"] == FINAL_BODY and PRIVATE_TOKEN in async_state.args, "async sibling rewrite and Guard token were not final executor args")
        _require(len(SAFE_EFFECT_CALLS) == 2 and len(HANDLER_ENTRIES) == 2, "async dispatch did not enter the safe handler exactly once")
        async_ref.emit_post(_Agent(), async_result)
        rows, attempts = _rows(receipt_store)
        _require(len(attempts) == 2 and len(rows) == 4, "async dispatch did not durably record attempt and terminal")

        duplicate_state, _, duplicate_result = _native_dispatch(TARGET, "call-success")
        _require(not duplicate_state.blocked and "already exists" in str(duplicate_result), "duplicate durable call was not rejected by wrapper")
        _require(len(SAFE_EFFECT_CALLS) == 2, "duplicate durable call reached safe effect")
        rows, attempts = _rows(receipt_store)
        _require(len(attempts) == 2 and len(rows) == 4, "duplicate call changed durable receipt count")

        missing_state, _, missing_result = _native_dispatch(TARGET, "")
        _require(missing_state.blocked and "identity are required" in str(missing_result), "missing identity did not fail closed")
        _require(len(SAFE_EFFECT_CALLS) == 2 and len(_rows(receipt_store)[1]) == 2, "missing identity created an effect or receipt")

        forged_state, _, forged_result = _native_dispatch(TARGET, "call-forged")
        _require(not forged_state.blocked and "correlation identity is invalid" in str(forged_result), "forged sibling token did not reach Guard fail-closed wrapper")
        _require(len(SAFE_EFFECT_CALLS) == 2 and len(_rows(receipt_store)[1]) == 2, "forged identity created an effect or receipt")

        _require(manager.unload("omh"), "loaded OMH plugin did not unload")
        _require(registry.get_entry(TARGET) is original, "unload did not restore original registry dispatch")
        _require(_metadata(registry.get_entry(TARGET)) == original_metadata, "unload did not restore original metadata")
        _require(registry.get_entry(ASYNC_TARGET) is async_original and _metadata(registry.get_entry(ASYNC_TARGET)) == async_original_metadata, "unload did not restore async registry dispatch and metadata")

        timings: dict[str, float] = {}
        failure_cases = (
            ("writer-lock", _locked_store),
            ("corrupt-db", lambda receipt_store, _module: (receipt_store.database_path.parent.mkdir(parents=True), receipt_store.database_path.write_bytes(b"not a database"), _NoopContext())[2]),
            ("oserror", lambda _receipt_store, receipt_module: patch.object(receipt_module.AttemptStore, "open_attempt", side_effect=OSError("injected narrow store failure"))),
        )
        for label, prepare in failure_cases:
            failure_home = _write_home(root, omh_source, name=label, enabled=["proof_rewriter", "omh"], allow_override=True)
            os.environ["HERMES_HOME"] = str(failure_home)
            failure_manager = get_plugin_manager()
            failure_manager.discover_and_load()
            failure_module = failure_manager._plugins["omh"].module
            _require(failure_module is not None, f"{label} OMH plugin did not load")
            failure_receipts = importlib.import_module(failure_module.__name__ + ".egress_attempt_receipts")
            timings[label] = _assert_store_failure(label=label, home=failure_home, manager=failure_manager, receipt_module=failure_receipts, prepare=prepare)

        validation_home = _write_home(root, omh_source, name="validation", enabled=["proof_rewriter", "omh"], allow_override=True)
        os.environ["HERMES_HOME"] = str(validation_home)
        validation_manager = get_plugin_manager()
        validation_manager.discover_and_load()
        validation_module = validation_manager._plugins["omh"].module
        _require(validation_module is not None, "validation OMH plugin did not load")
        validation_receipts = importlib.import_module(validation_module.__name__ + ".egress_attempt_receipts")
        receipt_store = validation_receipts.AttemptStore(validation_home / "omh-state")
        store = receipt_store
        before_entries = len(HANDLER_ENTRIES)
        started = time.monotonic()
        validation_state, _, validation_result = _native_dispatch(TARGET, "call-validation")
        elapsed = time.monotonic() - started
        _require(not validation_state.blocked and "egress attempt was not durably recorded" in str(validation_result), "validation did not fail closed at the native wrapper")
        _require(elapsed < 0.1, f"validation did not block within 100ms ({elapsed * 1000:.1f}ms)")
        _require(len(HANDLER_ENTRIES) == before_entries and not _rows(receipt_store)[1], "validation entered the handler or recorded an attempt")
        _require(validation_manager.unload("omh"), "validation OMH plugin did not unload")
        timings["validation"] = elapsed * 1000

    print(json.dumps({
        "disabled_metadata_unchanged": True,
        "denied_override_blocked_without_effect": True,
        "final_sibling_rewrite_durable_before_effect": True,
        "private_token_absent_from_effect_and_store": True,
        "duplicate_call_and_post_blocked_or_idempotent": True,
        "missing_and_forged_identity_fail_closed": True,
        "unload_restores_original_dispatch_and_metadata": True,
        "disabled_feature_loaded_without_guard_or_attempt_io": True,
        "native_async_handler_awaited_with_metadata_preserved": True,
        "native_store_failures_block_before_handler": True,
        "store_failure_timings_ms": timings,
        "safe_effect_calls": len(SAFE_EFFECT_CALLS),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
