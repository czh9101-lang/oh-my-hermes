from __future__ import annotations

import json
from contextvars import copy_context
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from _browser_adapter_support import Adapter, HostContext, host_session, request
from omh.plugin_bundle.omh import register


class BrowserAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "omh"
        self.adapter = Adapter()
        self.enterContext(host_session())
        self.ctx = HostContext(self.home, self.adapter)
        register(self.ctx)
        self.enterContext(getattr(self.ctx, "browser_task")("task"))

    def call(self, args, owner="owner"):
        self.assertIn("omh_browser", self.ctx.tools)
        return json.loads(self.ctx.tools["omh_browser"](args, session_id=owner))

    def acquire(self, **kwargs):
        if "task" in kwargs:
            self.enterContext(getattr(self.ctx, "browser_task")(kwargs["task"]))
        result = self.call(request(**kwargs))
        self.assertEqual(result["status"], "active", result)
        return result

    def action(self, lease, **changes):
        args = {"operation": "act", "lease_id": lease["lease_id"],
                "tab_id": lease["tabs"][0], "revision": lease["page"]["revision"],
                "handle": lease["page"]["elements"][0]["handle"], "action": "read"}
        args.update(changes)
        return args

    def test_existing_registered_hook_blocks_foreign_browser_action(self):
        with patch.dict("os.environ", {"OMH_HOME": str(self.home), "HERMES_HOME": str(self.home / "hermes")}):
            results = [hook(tool_name="browser_click", args={"lease_id": "foreign", "index": 1},
                            session_id="attacker", omh_home=str(self.home))
                       for hook in self.ctx.hooks["pre_tool_call"]]
        self.assertTrue(any(isinstance(r, dict) and r.get("action") == "block" for r in results), results)

    def test_acquire_reuses_completed_result_without_callbacks_or_writes(self):
        lease = self.acquire()
        counts = self.adapter.calls.copy()
        files = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.home.rglob("*") if p.is_file()}
        self.assertEqual(self.call(request()), lease)
        self.assertEqual(self.adapter.calls, counts)
        self.assertEqual(files, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files})
        self.assertEqual(self.adapter.calls["capabilities"], 1)
        self.assertEqual(lease["schema_version"], "browser_session_lease/v1")
        self.assertEqual(lease["page"]["schema_version"], "browser_page_state/v1")

    def test_owner_tab_revision_and_handle_refused_before_callback(self):
        lease = self.acquire()
        for changes in ({"tab_id": "foreign"}, {"revision": 0}, {"handle": "foreign"}):
            self.assertEqual(self.call(self.action(lease, **changes))["status"], "blocked")
        self.assertEqual(self.call(self.action(lease), owner="foreign")["status"], "blocked")
        self.assertEqual(self.adapter.calls["act"], 0)
        self.assertEqual(self.call(self.action(lease))["status"], "observed")
        self.assertEqual(self.call(self.action(lease))["status"], "blocked")
        self.assertEqual(self.adapter.calls["act"], 1)

    def test_zero_ambiguous_and_bare_index_refused(self):
        lease = self.acquire()
        for extra in ({"handle": "", "role": "button", "name": "missing"},
                      {"handle": "", "index": 0}):
            self.assertEqual(self.call(self.action(lease, **extra))["status"], "blocked")
        self.adapter.elements.append({"role": "button", "name": "PRIVATE LABEL", "key": "two"})
        observed = self.call({"operation": "observe", "lease_id": lease["lease_id"], "tab_id": "tab-1"})
        args = self.action(lease, handle="", role="button", name="PRIVATE LABEL", revision=observed["page"]["revision"])
        self.assertEqual(self.call(args)["status"], "blocked")
        self.assertEqual(self.adapter.calls["act"], 0)

    def test_unknown_readback_and_unsolicited_page_change(self):
        lease = self.acquire()
        self.adapter.revision += 1
        self.assertEqual(self.call(self.action(lease))["status"], "blocked")
        refreshed = self.call({"operation": "observe", "lease_id": lease["lease_id"], "tab_id": "tab-1"})
        self.adapter.readback = False
        result = self.call(self.action({**lease, "page": refreshed["page"]}))
        self.assertEqual(result["status"], "unknown")

    def test_release_and_orphan_cleanup_reap_once(self):
        lease = self.acquire()
        args = {"operation": "release", "lease_id": lease["lease_id"]}
        first = self.call(args)
        self.assertEqual(first["status"], "released")
        self.assertEqual(self.call(args), first)
        self.assertFalse(self.adapter.live)
        self.assertEqual(self.adapter.calls["release"], 1)
        self.assertEqual(self.call(self.action(lease))["status"], "blocked")
        self.acquire(task="second")
        # Session-end is the real host cleanup surface, not an owner field in tool args.
        for hook in self.ctx.hooks["on_session_end"][-1:]:
            hook(session_id="owner")
        self.assertFalse(self.adapter.live)

    def test_concurrent_reservation_prevents_duplicate_start(self):
        self.adapter.entered, self.adapter.proceed = Event(), Event()
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(copy_context().run, self.call, request())
            self.assertTrue(self.adapter.entered.wait(5))
            try:
                duplicate = self.call(request())
                self.assertIn(duplicate["status"], {"pending", "unknown"})
                self.assertEqual(self.adapter.calls["start"], 1)
            finally:
                self.adapter.proceed.set()
            completed = future.result(timeout=5)
        self.assertEqual(self.call(request()), completed)

    def test_crash_is_unknown_and_never_restarted(self):
        self.adapter.crash = True
        with self.assertRaises(RuntimeError):
            self.call(request())
        other = HostContext(self.home, self.adapter)
        register(other)
        self.ctx = other
        self.enterContext(getattr(self.ctx, "browser_task")("task"))
        self.assertEqual(self.call(request())["status"], "unknown")
        self.assertEqual(self.adapter.calls["start"], 1)
        for hook in self.ctx.hooks["on_session_end"][-1:]:
            hook(session_id="owner")
        self.assertFalse(self.adapter.live)

    def test_caps_expiry_and_retention(self):
        self.adapter.cap["limits"]["actions"] = 1
        lease = self.acquire()
        result = self.call(self.action(lease))
        self.assertEqual(result["status"], "observed")
        self.assertEqual(self.call(self.action(lease))["reason"], "action_capped")
        self.assertFalse(self.adapter.live)
        self.adapter.cap["limits"]["leases"] = 1
        self.acquire(task="second")
        self.enterContext(getattr(self.ctx, "browser_task")("third"))
        self.assertEqual(self.call(request(task="third"))["reason"], "concurrency_capped")

    def test_metadata_is_compact_and_secret_free(self):
        lease = self.acquire()
        blob = json.dumps(lease) + "".join(p.read_text() for p in self.home.rglob("*.json"))
        for forbidden in ("PRIVATE", "?secret", "opaque-auth", "cookies", '"dom"', '"name"'):
            self.assertNotIn(forbidden, blob)
        self.assertLess(len(json.dumps(lease["page"]).encode()), 4096)
        self.assertEqual(lease["page"]["readback"], "observed")

    def test_disabled_and_non_browser_have_no_browser_import_io_or_context(self):
        other_home = Path(self.tmp.name) / "disabled"
        ctx = HostContext(other_home, self.adapter, enabled=False)
        with patch("builtins.__import__", wraps=__import__) as imports:
            register(ctx)
        self.assertNotIn("omh_browser", ctx.tools)
        self.assertFalse(other_home.exists())
        self.assertFalse(any("browser_bridge" in c.args[0] or "browser_lease_store" in c.args[0] for c in imports.call_args_list))
        hook = self.ctx.hooks["pre_tool_call"][-1]
        self.assertIsNone(hook(tool_name="read_file", args={}))
        self.assertFalse(self.home.exists())
        self.assertFalse(self.adapter.calls)

    def test_opaque_and_sensitive_actions_never_execute(self):
        lease = self.acquire()
        for operation in ("click", "navigate", "type", "evaluate", "GET"):
            result = self.call(self.action(lease, action=operation))
            self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.adapter.calls["act"], 0)


    def test_malformed_json_argument_shapes_refuse_without_host_calls(self):
        for args in (request(operation=[]), request(mode=[]), request(origins=[None]),
                     request(actions=[{}]), request(task=None), request(auth_boundary=[])):
            with self.subTest(args=args):
                try:
                    status = self.call(args)["status"]
                except (TypeError, KeyError, AttributeError) as exc:
                    status = type(exc).__name__
                self.assertEqual(status, "blocked")
        self.assertFalse(self.adapter.calls)


if __name__ == "__main__":
    unittest.main()
