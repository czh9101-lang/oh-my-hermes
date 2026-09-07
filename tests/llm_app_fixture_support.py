"""Synthetic test host for executable llm-app-dev contract fixtures.

This adapter is test evidence for an illustrative host contract. It is not an
OMH backend, validator, permission service, or product enforcement surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Barrier, Lock
from typing import TypedDict, final


@dataclass(frozen=True, slots=True)
class ApplyOutcome:
    status: str
    value: int
    version: int


@dataclass(frozen=True, slots=True)
class ExtractorToken:
    key: "MemoryKey"
    fact_version: int
    deletion_generation: int


@dataclass(frozen=True, slots=True)
class LimitState:
    value: int
    limit: int
    version: int


@dataclass(frozen=True, slots=True)
class MemoryFact:
    value: str
    version: int
    retain_until: int


@dataclass(frozen=True, slots=True)
class MemoryKey:
    person_id: str
    tenant_id: str
    fact_name: str


@dataclass(frozen=True, slots=True)
class MemoryWrite:
    key: MemoryKey
    value: str
    source: str
    retain_until: int = 100


_Record = TypedDict("_Record", {"id": str, "host_owned": bool, "expires_at": int, "revoked": bool, "parent_id": str | None, "read_scopes": list[str], "actions": list[str]})
_RecordCase = TypedDict("_RecordCase", {"records": list[_Record], "lookup_id": str, "target_id": str, "action": str, "now": int})
_Receipt = TypedDict("_Receipt", {"acknowledged": bool, "state_version": int, "final_order": list[str]})
_PositionCase = TypedDict("_PositionCase", {"receipt": _Receipt | None, "current_version": int, "position": int})
_LimitRequest = TypedDict("_LimitRequest", {"amount": int, "expected_version": int, "idempotency_key": str, "authorized": bool, "approved": bool, "policy_allows": bool})
_LoadingCase = TypedDict("_LoadingCase", {"bounded": bool, "confidence": str})
_ELIGIBLE_MEMORY_SOURCES = frozenset(("user_assertion", "explicit_confirmation"))
_REFRESH_OR_ASK = "refresh_or_ask"


@final
class SyntheticFixtureHost:
    """Own mutable state only long enough to run one synthetic fixture."""

    value: int
    limit: int
    version: int
    memory_enabled: bool
    _lock: Lock
    _applied: dict[str, int]

    def __init__(self, limit_state: LimitState | None = None, *, memory_enabled: bool = True) -> None:
        state = limit_state or LimitState(value=0, limit=0, version=1)
        self.value = state.value
        self.limit = state.limit
        self.version = state.version
        self.memory_enabled = memory_enabled
        self._lock = Lock()
        self._applied = {}
        self._memory: dict[MemoryKey, MemoryFact] = {}
        self._deletion_generations: dict[MemoryKey, int] = {}

    def authorize_record(self, case: _RecordCase) -> str:
        records = {record["id"]: record for record in case["records"]}
        candidate = records.get(case["lookup_id"])
        if candidate is None:
            return "unknown_record"
        failure = self._record_failure(candidate, case["now"])
        if failure is not None:
            return failure
        target = records.get(case["target_id"])
        if target is None:
            return "parent_scope_denied"
        failure = self._record_failure(target, case["now"])
        if failure is not None:
            return failure
        if candidate["id"] != target["id"] and candidate["parent_id"] != target["id"]:
            return "parent_scope_denied"
        if f"record:{target['id']}" not in target["read_scopes"]:
            return "parent_scope_denied"
        if case["action"] not in target["actions"]:
            return "authorization_denied"
        return "authorized"

    @staticmethod
    def _record_failure(record: _Record, now: int) -> str | None:
        if not record["host_owned"]:
            return "untrusted_provenance"
        if record["expires_at"] <= now:
            return "expired_record"
        if record["revoked"]:
            return "revoked_record"
        return None

    @staticmethod
    def resolve_position(case: _PositionCase) -> str:
        receipt = case["receipt"]
        if receipt is None or not receipt["acknowledged"]:
            return _REFRESH_OR_ASK
        if receipt["state_version"] != case["current_version"]:
            return _REFRESH_OR_ASK
        index = case["position"] - 1
        if index < 0 or index >= len(receipt["final_order"]):
            return _REFRESH_OR_ASK
        return receipt["final_order"][index]

    def apply_limit(self, request: _LimitRequest, ready: Barrier | None = None) -> ApplyOutcome:
        if ready is not None:
            _ = ready.wait(timeout=2)
        with self._lock:
            status = self._apply_failure(request)
            if status is not None:
                return ApplyOutcome(status, self.value, self.version)
            self.value += request["amount"]
            self.version += 1
            outcome = ApplyOutcome("applied", self.value, self.version)
            self._applied[request["idempotency_key"]] = request["amount"]
            return outcome

    def _apply_failure(self, request: _LimitRequest) -> str | None:
        if not request["authorized"]:
            return "authorization_denied"
        if not request["approved"]:
            return "approval_missing"
        if not request["policy_allows"]:
            return "policy_denied"
        prior = self._applied.get(request["idempotency_key"])
        if prior is not None:
            return "idempotent_replay" if prior == request["amount"] else "idempotency_conflict"
        if request["expected_version"] != self.version:
            return "stale_version"
        if self.value + request["amount"] > self.limit:
            return "resulting_limit"
        return None

    def store_memory(self, write: MemoryWrite) -> str:
        if not self.memory_enabled:
            return "disabled"
        if write.source not in _ELIGIBLE_MEMORY_SOURCES:
            return "ineligible_source"
        with self._lock:
            current = self._memory.get(write.key)
            version = 1 if current is None else current.version + 1
            self._memory[write.key] = MemoryFact(write.value, version, write.retain_until)
        return "stored"

    def read_memory(self, key: MemoryKey, now: int = 0) -> str | None:
        with self._lock:
            fact = self._memory.get(key)
            if fact is None or now >= fact.retain_until:
                return None
            return fact.value

    def correct_memory(self, key: MemoryKey, value: str) -> None:
        with self._lock:
            current = self._memory[key]
            self._memory[key] = MemoryFact(value, current.version + 1, current.retain_until)

    def delete_memory(self, key: MemoryKey) -> None:
        with self._lock:
            self._deletion_generations[key] = self._deletion_generations.get(key, 0) + 1
            _ = self._memory.pop(key, None)

    def stage_extractor(self, key: MemoryKey) -> ExtractorToken:
        with self._lock:
            fact = self._memory.get(key)
            version = 0 if fact is None else fact.version
            return ExtractorToken(key, version, self._deletion_generations.get(key, 0))

    def apply_extractor(self, token: ExtractorToken, value: str) -> str:
        with self._lock:
            if self._deletion_generations.get(token.key, 0) != token.deletion_generation:
                return "stale_deletion_generation"
            fact = self._memory.get(token.key)
            version = 0 if fact is None else fact.version
            if version != token.fact_version:
                return "stale_fact_version"
            retain_until = 100 if fact is None else fact.retain_until
            self._memory[token.key] = MemoryFact(value, version + 1, retain_until)
            return "stored"

    @staticmethod
    def loading_path(case: _LoadingCase) -> tuple[str, bool]:
        path = "predictive" if case["bounded"] and case["confidence"] == "confident" else "on_demand"
        return path, False
