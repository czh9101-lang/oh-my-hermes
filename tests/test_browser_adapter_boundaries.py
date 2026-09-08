"""Adversarial boundary tests, no clocks or concurrency decided by sleeps."""
from copy import deepcopy
from contextlib import ExitStack, contextmanager
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import unittest
from unittest.mock import patch

from _browser_adapter_support import Adapter, request
from omh.workflows.browser_adapter import BrowserContractError, capability_snapshot, acquisition_request, pending_effect, page_state, resolve_handle
from omh.workflows.browser_lease_store import BrowserLeaseStore, BrowserSessionManager, MAX_RECORDS
from omh.system.local_store import file_lock, FileLockTimeout


class BrowserBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.adapter = Adapter()
        self.now = 1000
        self.store = BrowserLeaseStore(Path(self.tmp.name) / "omh")
        self.manager = BrowserSessionManager(self.store, self.adapter, clock=lambda: self.now)

    def acquire(self, **changes):
        return self.manager.acquire("owner", request(**changes))

    def act(self, lease):
        return {"operation": "act", "action": "read", "lease_id": lease["lease_id"],
                "tab_id": "tab-1", "revision": lease["page"]["revision"],
                "handle": lease["page"]["elements"][0]["handle"]}

    def test_live_dom_change_blocks_before_act_callback(self):
        self._live_dom_change()

    def test_host_start_receives_declared_scope_and_auth_reference(self):
        received = []
        original = self.adapter.start

        def start(lease_id, scope, deadline):
            received.append(scope)
            return original(lease_id, scope, deadline)

        self.adapter.start = start
        lease = self.acquire()
        self.assertIsInstance(received[0], dict)
        self.assertEqual(received[0]["auth_boundary_ref"], lease["auth_boundary_ref"])
        self.assertEqual(received[0]["mode"], "headless")
        self.assertEqual(received[0]["actions"], ["read"])

    def _live_dom_change(self):
        lease = self.acquire()
        self.adapter.revision += 1
        self.assertEqual(self.manager.operate("owner", self.act(lease))["status"], "blocked")
        self.assertEqual(self.adapter.calls["act"], 0)

    def test_exact_capability_expiry_reaps_without_action(self):
        lease = self.acquire()
        self.now += 30
        self.assertEqual(self.manager.operate("owner", self.act(lease))["reason"], "capability_expired")
        self.assertFalse(self.adapter.live)
        self.assertEqual(self.adapter.calls["act"], 0)

    def test_hard_lease_expiry_reaps_on_reuse(self):
        self.adapter.cap["limits"]["ttl_seconds"] = 10
        self.acquire()
        self.now += 10
        self.assertEqual(self.acquire()["reason"], "expired")
        self.assertFalse(self.adapter.live)
        self.assertEqual(self.adapter.calls["start"], 1)

    def test_deadline_expiring_during_capabilities_prevents_start(self):
        original = self.adapter.capabilities

        def capabilities():
            self.now += 301
            return original()

        self.adapter.capabilities = capabilities
        self.assertEqual(self.acquire()["status"], "blocked")
        self.assertEqual(self.adapter.calls["start"], 0)

    def test_cleanup_failure_can_be_recovered_after_restart(self):
        lease = self.acquire()
        original = self.adapter.release
        with patch.object(self.adapter, "release", side_effect=TimeoutError("host failed")):
            with self.assertRaises(TimeoutError):
                self.manager.release("owner", lease["lease_id"])
        manager = BrowserSessionManager(self.store, self.adapter, clock=lambda: self.now)
        self.assertEqual(manager.cleanup("owner")[0]["status"], "orphaned")
        self.assertFalse(self.adapter.live)
        self.assertEqual(self.adapter.release, original)

    def test_cleanup_skips_foreign_first_adapter_and_version(self):
        for field in ("adapter_id", "adapter_version"):
            with self.subTest(field=field), TemporaryDirectory() as tmp:
                store = BrowserLeaseStore(Path(tmp) / "omh")
                pairs = []
                for value in ("first", "second"):
                    adapter = Adapter()
                    setattr(adapter, field, value)
                    manager = BrowserSessionManager(store, adapter, clock=lambda: self.now)
                    lease = manager.acquire("owner", request())
                    pairs.append((lease["lease_id"], manager, adapter, lease))
                # The serialized store sorts keys: the current adapter is LAST.
                pairs.sort(key=lambda pair: pair[0])
                foreign, current = pairs
                try:
                    refused = current[1].operate("owner", self.act(foreign[3]))
                    self.assertEqual(refused["reason"], "foreign_adapter")
                    error = None
                    try:
                        current[1].cleanup("owner")
                    except BrowserContractError as exc:
                        error = str(exc)
                    self.assertFalse(current[2].live, {"cleanup_error": error,
                                                     "own_resources": current[2].live})
                    self.assertEqual(current[2].calls["release"], 1)
                    self.assertEqual(foreign[2].live, {foreign[0]})
                    self.assertEqual(foreign[2].calls["release"], 0)
                    self.assertEqual(current[1].cleanup("owner"), [])
                finally:
                    for lease_id, manager, adapter, _ in pairs:
                        manager.release("owner", lease_id)

    def test_concurrent_action_reservation_allows_one_callback(self):
        self._concurrent_action()

    def test_cross_manager_release_reservation_prevents_duplicate_callback(self):
        lease = self.acquire()
        entered, proceed, duplicate_entered = Event(), Event(), Event()
        original = self.adapter.release
        count = []

        @contextmanager
        def observed_lock(path, **kwargs):
            # Subscribe to actual OS-lock contention, not elapsed time. The
            # real lock and real store remain in the integration under test.
            with ExitStack() as stack:
                try:
                    held = stack.enter_context(file_lock(path, **{**kwargs, "timeout_seconds": 0}))
                except FileLockTimeout:
                    proceed.set()
                    held = stack.enter_context(file_lock(path, **kwargs))
                yield held

        def release(*args):
            count.append(1)
            if len(count) == 1:
                entered.set()
                if not proceed.wait(5):
                    raise TimeoutError("fixture signal")
            else:
                duplicate_entered.set()
                proceed.set()
            return original(*args)

        self.adapter.release = release
        other = BrowserSessionManager(self.store, self.adapter, clock=lambda: self.now)
        with patch("omh.workflows.browser_lease_store.file_lock", observed_lock), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.manager.release, "owner", lease["lease_id"])
            self.assertTrue(entered.wait(5))
            second = pool.submit(other.release, "owner", lease["lease_id"])
            first.result(timeout=5)
            second.result(timeout=5)
        self.assertFalse(duplicate_entered.is_set())
        self.assertEqual(self.adapter.calls["release"], 1)

    def _concurrent_action(self):
        lease = self.acquire()
        entered, proceed = Event(), Event()
        original = self.adapter.act

        def act(*args):
            entered.set()
            if not proceed.wait(5):
                raise TimeoutError("fixture signal")
            return original(*args)

        self.adapter.act = act
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(self.manager.operate, "owner", self.act(lease))
            self.assertTrue(entered.wait(5))
            try:
                self.assertEqual(self.manager.operate("owner", self.act(lease))["status"], "blocked")
            finally:
                proceed.set()
            self.assertEqual(future.result(timeout=5)["status"], "observed")
        self.assertEqual(self.adapter.calls["act"], 1)

    def test_corrupt_json_and_nested_state_fail_closed_without_callbacks(self):
        self.acquire()
        saved = self.store.path.read_bytes()
        mutations = []
        for field, value in (("actions", ["read", "raw-secret"]), ("origins", ["http://localhost/?credential=SECRET"]),
                             ("reason", "raw-secret"), ("acquisition", {"status": "active", "secret": "raw-secret"})):
            state = json.loads(saved)
            next(iter(state["leases"].values()))[field] = value
            mutations.append(json.dumps(state))
        state = json.loads(saved)
        next(iter(state["leases"].values()))["pages"]["tab-1"]["cookies"] = "raw-secret"
        mutations.extend([json.dumps(state), "{", "[]", '{"schema_version":"browser_lease_store/v1","leases":null}'])
        calls = self.adapter.calls.copy()
        for content in mutations:
            with self.subTest(content=content[:40]):
                self.store.path.write_text(content)
                with self.assertRaisesRegex(BrowserContractError, "store_corrupt"):
                    self.acquire()
        self.assertEqual(self.adapter.calls, calls)

    def test_tab_and_semantic_caps_reap_partial_start(self):
        self.adapter.start = lambda *args: {"tabs": ["a", "b"]}
        self.assertEqual(self.acquire()["reason"], "tab_capped")
        self.assertEqual(self.adapter.calls["release"], 1)
        self.adapter.elements = self.adapter.elements * 9
        self.adapter.start = lambda *args: {"tabs": ["a"]}
        self.assertEqual(self.acquire(task="other")["reason"], "state_capped")
        self.assertEqual(self.adapter.calls["release"], 2)

    def test_metadata_cap_retains_tombstones(self):
        for index in range(MAX_RECORDS):
            lease = self.acquire(task=f"task-{index}")
            self.manager.release("owner", lease["lease_id"])
        before = self.store.path.read_bytes()
        self.assertEqual(self.acquire(task="over-cap")["reason"], "metadata_capped")
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.adapter.calls["start"], MAX_RECORDS)

    def test_owner_auth_task_adapter_version_change_never_reuses(self):
        leases = [self.acquire()]
        self.manager.release("owner", leases[0]["lease_id"])
        for changes in ({"auth_boundary": "another"}, {"task": "another"}, {"origins": ["http://localhost:80"]}):
            lease = self.acquire(**changes)
            if changes.get("origins"):
                self.assertEqual(lease["status"], "released")  # equivalent canonical origin
            else:
                self.assertNotEqual(lease["lease_id"], leases[0]["lease_id"])
                self.manager.release("owner", lease["lease_id"])
        self.adapter.adapter_version = "2"
        self.assertNotEqual(self.acquire()["lease_id"], leases[0]["lease_id"])

    def test_mutation_capability_cannot_be_faked_with_tool_dispatch(self):
        identity = acquisition_request("owner", self.adapter, request())
        self.adapter.cap["mutation_interception"] = "tool_dispatch"
        with self.assertRaises(BrowserContractError):
            capability_snapshot(self.adapter.cap, identity, 1000)

    def test_preview_metadata_is_not_effect_authorization(self):
        raw = {"schema_version": "browser_pending_effect/v1", "held": True, "method": "GET",
               "origin": "http://localhost", "payload_bytes": 0, "payload_digest": "a" * 64,
               "payload_shape": "empty", "target_role": "button", "target_name_digest": "b" * 64,
               "opens_new_tab": False, "redirected": False, "preview_ref": "c" * 64}
        self.assertEqual(pending_effect(raw), raw)
        for key, value in (("held", False), ("redirected", True), ("opens_new_tab", True), ("headers", "secret")):
            bad = deepcopy(raw)
            bad[key] = value
            with self.assertRaises(BrowserContractError):
                pending_effect(bad)

    def test_pure_parsing_has_zero_io_process_or_network_calls(self):
        raw = {"url": "http://localhost/", "revision": 1, "readback": True,
               "elements": [{"role": "button", "name": "transient", "key": "one"}]}
        with patch("builtins.open", side_effect=AssertionError("filesystem IO")), \
             patch("os.scandir", side_effect=AssertionError("repository scan")), \
             patch("subprocess.Popen", side_effect=AssertionError("process")), \
             patch("socket.socket", side_effect=AssertionError("network")):
            identity = acquisition_request("owner", self.adapter, request())
            cap = capability_snapshot(self.adapter.cap, identity, 1000)
            page = page_state(raw, {**identity, "capabilities": cap}, "tab-1")
            selected = resolve_handle(page, {"revision": 1, "role": "button", "name": "transient"})
            self.assertEqual(selected["role"], "button")
        self.assertFalse(self.adapter.calls)
        self.assertFalse(self.store.path.exists())

    def test_private_store_and_symlink_refusal(self):
        self.acquire()
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.path.parent.stat().st_mode & 0o777, 0o700)
        target = Path(self.tmp.name) / "other"
        target.write_text("untouched")
        self.store.path.unlink()
        self.store.path.symlink_to(target)
        with self.assertRaisesRegex(BrowserContractError, "unsafe_store"):
            self.acquire()
        self.assertEqual(target.read_text(), "untouched")


if __name__ == "__main__":
    unittest.main()
