"""Contracts for effective approval-bypass (yolo) observation.

The Shift+Tab yolo toggle lives only in the host process's
``tools.approval`` session state, so the HUD reports what the plugin's
``pre_llm_call`` and ``pre_tool_call`` hooks last observed in-process.
The ledger stores one boolean and a timestamp — never a session key,
command, or prompt — and an unobserved or stale ledger projects idle so
the widget renders nothing rather than a guess.
"""

import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

from omh.plugin_bundle.omh.approval_bypass import (
    APPROVAL_BYPASS_FRESH_SECONDS,
    approval_bypass_path,
    latest_approval_bypass,
    record_approval_bypass,
)
from omh.plugin_bundle.omh.runtime_reader import read_omh_hud

NOW = 1_787_040_000.0


class ApprovalBypassLedgerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = str(Path(self._tmp.name) / "omh")

    def _install_host_approval(self, enabled: bool) -> None:
        previous = {name: sys.modules.get(name) for name in ("tools", "tools.approval")}

        def restore():
            for name, module in previous.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module

        self.addCleanup(restore)
        tools_module = types.ModuleType("tools")
        approval_module = types.ModuleType("tools.approval")
        approval_module.is_approval_bypass_active = lambda: enabled
        tools_module.approval = approval_module
        sys.modules["tools"] = tools_module
        sys.modules["tools.approval"] = approval_module

    def test_an_enabled_observation_projects_on(self):
        self._install_host_approval(True)
        record_approval_bypass(omh_home=self.home, now=NOW)
        state = latest_approval_bypass(self.home, now=NOW + 5)
        self.assertEqual(state["status"], "observed")
        self.assertTrue(state["enabled"])
        self.assertIn("never approval", state["claim_boundary"])

    def test_a_disabled_observation_projects_off(self):
        self._install_host_approval(False)
        record_approval_bypass(omh_home=self.home, now=NOW)
        state = latest_approval_bypass(self.home, now=NOW + 5)
        self.assertEqual(state["status"], "observed")
        self.assertFalse(state["enabled"])

    def test_without_the_host_surface_nothing_is_recorded(self):
        # No tools.approval in this process: the hook records nothing rather
        # than a guess, and the projection stays idle.
        record_approval_bypass(omh_home=self.home, now=NOW)
        self.assertFalse(approval_bypass_path(self.home).exists())
        self.assertEqual(latest_approval_bypass(self.home, now=NOW)["status"], "idle")

    def test_a_stale_observation_expires_to_idle(self):
        # A host restart resets the in-memory flag without any hook firing,
        # so an old observation must stop claiming a state no process holds.
        self._install_host_approval(True)
        record_approval_bypass(omh_home=self.home, now=NOW)
        fresh = latest_approval_bypass(self.home, now=NOW + APPROVAL_BYPASS_FRESH_SECONDS - 1)
        self.assertEqual(fresh["status"], "observed")
        stale = latest_approval_bypass(self.home, now=NOW + APPROVAL_BYPASS_FRESH_SECONDS + 1)
        self.assertEqual(stale["status"], "idle")

    def test_the_ledger_stores_metadata_only(self):
        import json

        self._install_host_approval(True)
        record_approval_bypass(omh_home=self.home, now=NOW)
        record = json.loads(approval_bypass_path(self.home).read_text(encoding="utf-8"))
        self.assertEqual(
            sorted(record), ["claim_boundary", "enabled", "observed_ts", "schema_version"]
        )

    def test_the_hud_payload_carries_the_state(self):
        self._install_host_approval(True)
        record_approval_bypass(omh_home=self.home, now=time.time())
        hermes = str(Path(self._tmp.name) / "hermes")
        payload = read_omh_hud(self.home, hermes)
        self.assertEqual(payload["yolo"]["status"], "observed")
        self.assertTrue(payload["yolo"]["enabled"])
        self.assertEqual(payload["privacy"], "metadata_only")

    def test_both_hooks_tick_the_ledger(self):
        from omh.plugin_bundle.omh.hooks.llm_hooks import pre_llm_call
        from omh.plugin_bundle.omh.hooks.tool_hooks import pre_tool_call

        self._install_host_approval(True)
        pre_tool_call(tool_name="terminal", omh_home=self.home)
        self.assertTrue(approval_bypass_path(self.home).exists())
        approval_bypass_path(self.home).unlink()
        pre_llm_call(user_message="hello", omh_home=self.home, include_omh_awareness=False)
        self.assertTrue(approval_bypass_path(self.home).exists())


if __name__ == "__main__":
    unittest.main()
