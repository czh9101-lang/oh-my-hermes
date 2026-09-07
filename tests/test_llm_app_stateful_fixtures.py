"""Executable synthetic-host coverage for llm-app-dev stateful fixtures."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import Barrier, Event
from typing import assert_never
import unittest

from llm_app_fixture_support import LimitState, MemoryKey, MemoryWrite, SyntheticFixtureHost


FIXTURES = Path(__file__).parent / "fixtures" / "llm_app_dev"
TIMEOUT_SECONDS = 2


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _limit_state(payload) -> LimitState:
    return LimitState(value=payload["value"], limit=payload["limit"], version=payload["version"])


class FixtureIdentityTests(unittest.TestCase):
    def test_every_record_has_exact_declared_identities_and_null_unobserved_telemetry(self) -> None:
        for path in sorted(FIXTURES.glob("*.json")):
            with self.subTest(path=path.name):
                payload = _fixture(path.name)
                self.assertEqual(payload["schema_version"], "llm_app_stateful_fixture/v1")
                self.assertEqual(payload["fixture_version"], "llm-app-stateful-contract/v1")
                self.assertEqual(payload["model_id"], "synthetic-fixture-model/v1")
                self.assertEqual(payload["prompt_id"], "stateful-contract-prompt/v1")
                self.assertEqual(payload["tool_bundle_id"], "synthetic-fixture-host/v1")
                self.assertEqual(payload["observations"], {"provider": None, "cost_usd": None, "user_visible_latency_ms": None})
                self.assertEqual(payload["messages"], [])

    def test_required_and_forbidden_pairs_are_committed_as_machine_values(self) -> None:
        groups = (
            _fixture("record-authority.json")["cases"],
            _fixture("presentation.json")["cases"],
            _fixture("memory.json")["eligibility"],
            _fixture("cross-capability.json")["cases"],
            _fixture("cross-capability.json")["predictive_loading"],
        )
        for cases in groups:
            self.assertEqual({case["pair_role"] for case in cases}, {"required", "forbidden"})


class RecordAndPresentationFixtureTests(unittest.TestCase):
    def test_record_authority_comes_from_current_host_and_parent_scope(self) -> None:
        for case in _fixture("record-authority.json")["cases"]:
            with self.subTest(case=case["id"]):
                self.assertEqual(SyntheticFixtureHost().authorize_record(case), case["expected"])

    def test_positions_use_only_acknowledged_current_final_order(self) -> None:
        for case in _fixture("presentation.json")["cases"]:
            with self.subTest(case=case["id"]):
                self.assertEqual(SyntheticFixtureHost.resolve_position(case), case["expected"])


class SharedLimitFixtureTests(unittest.TestCase):
    def test_repeated_calls_check_the_shared_resulting_state(self) -> None:
        fixture = _fixture("shared-limits.json")["sequential"]
        host = SyntheticFixtureHost(_limit_state(fixture["initial"]))
        outcomes = [host.apply_limit(request).status for request in fixture["requests"]]
        self.assertEqual(outcomes, fixture["expected_outcomes"])
        self.assertEqual({"value": host.value, "version": host.version}, fixture["expected_final"])

    def test_concurrent_calls_are_atomic_at_the_shared_resource(self) -> None:
        fixture = _fixture("shared-limits.json")["concurrent"]
        host = SyntheticFixtureHost(_limit_state(fixture["initial"]))
        ready = Barrier(3)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(host.apply_limit, request, ready) for request in fixture["requests"]]
            _ = ready.wait(timeout=TIMEOUT_SECONDS)
            outcomes = sorted(future.result(timeout=TIMEOUT_SECONDS).status for future in futures)
        self.assertEqual(outcomes, sorted(fixture["expected_outcomes"]))
        self.assertEqual({"value": host.value, "version": host.version}, fixture["expected_final"])

    def test_apply_time_gates_fail_without_partial_writes(self) -> None:
        for case in _fixture("shared-limits.json")["gate_cases"]:
            with self.subTest(case=case["id"]):
                host = SyntheticFixtureHost(LimitState(value=6, limit=10, version=1))
                outcome = host.apply_limit(case["request"])
                self.assertEqual(outcome.status, case["expected"])
                self.assertEqual((host.value, host.version), (6, 1))

    def test_idempotent_replay_does_not_apply_twice(self) -> None:
        fixture = _fixture("shared-limits.json")["idempotent_replay"]
        host = SyntheticFixtureHost(_limit_state(fixture["initial"]))
        first = host.apply_limit(fixture["request"])
        replay = host.apply_limit(fixture["request"])
        self.assertEqual([first.status, replay.status], fixture["expected_outcomes"])
        self.assertEqual({"value": host.value, "version": host.version}, fixture["expected_final"])


class MemoryFixtureTests(unittest.TestCase):
    def test_only_user_assertions_or_confirmations_are_eligible(self) -> None:
        key = MemoryKey("person-a", "tenant-a", "fact")
        for case in _fixture("memory.json")["eligibility"]:
            with self.subTest(case=case["id"]):
                host = SyntheticFixtureHost()
                self.assertEqual(host.store_memory(MemoryWrite(key, "value", case["source"])), case["expected"])

    def test_person_and_tenant_scopes_are_isolated(self) -> None:
        case = _fixture("memory.json")["scope"]
        key = MemoryKey(case["person_id"], case["tenant_id"], case["fact_name"])
        host = SyntheticFixtureHost()
        host.store_memory(MemoryWrite(key, case["value"], "user_assertion"))
        self.assertEqual(host.read_memory(key), case["value"])
        self.assertIsNone(host.read_memory(MemoryKey(case["other_person_id"], case["tenant_id"], case["fact_name"])))
        self.assertIsNone(host.read_memory(MemoryKey(case["person_id"], case["other_tenant_id"], case["fact_name"])))

    def test_user_correction_supersedes_the_prior_fact(self) -> None:
        case = _fixture("memory.json")["lifecycle"]
        key = MemoryKey("person-a", "tenant-a", case["fact_name"])
        host = SyntheticFixtureHost()
        host.store_memory(MemoryWrite(key, case["initial_value"], "user_assertion", case["retain_until"]))
        host.correct_memory(key, case["corrected_value"])
        self.assertEqual(host.read_memory(key), case["corrected_value"])

    def test_expired_retention_hides_the_fact(self) -> None:
        case = _fixture("memory.json")["lifecycle"]
        key = MemoryKey("person-a", "tenant-a", case["fact_name"])
        host = SyntheticFixtureHost()
        host.store_memory(MemoryWrite(key, case["initial_value"], "user_assertion", case["retain_until"]))
        self.assertIsNone(host.read_memory(key, case["read_after"]))

    def test_deletion_removes_the_fact(self) -> None:
        key = MemoryKey("person-a", "tenant-a", "fact")
        host = SyntheticFixtureHost()
        host.store_memory(MemoryWrite(key, "value", "user_assertion"))
        host.delete_memory(key)
        self.assertIsNone(host.read_memory(key))

    def test_disabled_memory_rejects_persistence(self) -> None:
        key = MemoryKey("person-a", "tenant-a", "fact")
        host = SyntheticFixtureHost(memory_enabled=False)
        self.assertEqual(host.store_memory(MemoryWrite(key, "value", "user_assertion")), "disabled")
        self.assertIsNone(host.read_memory(key))

    def test_delayed_extractors_cannot_resurrect_deleted_or_corrected_facts(self) -> None:
        key = MemoryKey("person-a", "tenant-a", "fact")
        for case in _fixture("memory.json")["races"]:
            with self.subTest(case=case["id"]):
                host = SyntheticFixtureHost()
                if case["initial_value"] is not None:
                    host.store_memory(MemoryWrite(key, case["initial_value"], "user_assertion"))
                token = host.stage_extractor(key)
                started = Event()
                release = Event()

                def delayed_apply() -> str:
                    started.set()
                    self.assertTrue(release.wait(timeout=TIMEOUT_SECONDS))
                    return host.apply_extractor(token, case["extractor_value"])

                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(delayed_apply)
                    self.assertTrue(started.wait(timeout=TIMEOUT_SECONDS))
                    match case["intervening"]:
                        case "delete":
                            host.delete_memory(key)
                            expected_value = None
                        case "correct":
                            host.correct_memory(key, case["corrected_value"])
                            expected_value = case["corrected_value"]
                        case unreachable:
                            assert_never(unreachable)
                    release.set()
                    self.assertEqual(future.result(timeout=TIMEOUT_SECONDS), case["expected"])
                self.assertEqual(host.read_memory(key), expected_value)


class CrossCapabilityFixtureTests(unittest.TestCase):
    def test_position_authority_and_limit_gates_compose(self) -> None:
        for case in _fixture("cross-capability.json")["cases"]:
            with self.subTest(case=case["id"]):
                host = SyntheticFixtureHost(_limit_state(case["limit"]))
                selected = host.resolve_position({**case, "source_order": ["child-1"], "position": 1})
                if selected == "refresh_or_ask":
                    outcome = selected
                else:
                    authority = host.authorize_record({**case, "now": 5, "lookup_id": selected, "target_id": "parent-1", "action": "update"})
                    request = {"amount": 2, "expected_version": 1, "idempotency_key": case["id"], "authorized": True, "approved": True, "policy_allows": True}
                    outcome = authority if authority != "authorized" else host.apply_limit(request).status
                self.assertEqual(outcome, case["expected"])
                self.assertEqual(host.value, case["expected_final_value"])

    def test_predictive_loading_is_bounded_fallback_and_never_authority(self) -> None:
        for case in _fixture("cross-capability.json")["predictive_loading"]:
            with self.subTest(case=case["id"]):
                path, authority = SyntheticFixtureHost.loading_path(case)
                self.assertEqual(path, case["expected_path"])
                self.assertEqual(authority, case["expected_authority"])


if __name__ == "__main__":
    _ = unittest.main()
