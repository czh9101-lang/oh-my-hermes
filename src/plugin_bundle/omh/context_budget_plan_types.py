"""JSON contracts shared by route-bound plan writers and standalone readers."""
from __future__ import annotations

from typing import Final, Literal, TypedDict

EvidenceClass = Literal["observed", "assumed", "unknown"]
Action = Literal["continue", "checkpoint_required", "overflow_recovery_required", "capacity_unknown_hold", "rebind_loop_hold"]
CAPACITY_FIELDS: Final = (
    "context_window_tokens", "max_output_tokens", "compaction_reserve_tokens", "retained_history_tokens",
)
Evidence = TypedDict("Evidence", {"value": int | None, "class": EvidenceClass, "source": str, "observed_at": str})
Budget = TypedDict("Budget", {"value": int | None, "class": EvidenceClass})


class RouteIdentity(TypedDict):
    executor_profile: str
    provider: str | None
    wire_model: str
    contract_model_id: str
    model_family: str
    catalog_kind: str
    catalog_fingerprint: None


class Derivation(TypedDict):
    operation: str
    operands: dict[str, Evidence]


class RouteCapacity(TypedDict):
    schema_version: str
    route_identity: RouteIdentity
    route_identity_digest: str
    capacity: dict[str, Evidence]
    effective_capacity_digest: str
    usable_budget_tokens: Budget
    derivation: Derivation


class MustKeep(TypedDict):
    digest: str
    estimated_tokens_total: int


class Invalidation(TypedDict):
    reason: str
    action: Action


class ObservationClocks(TypedDict):
    local_estimate: None
    provider_usage: None
    compaction: None
    billing: None


class Observations(TypedDict):
    provider_usage_observed: Literal["not_observed"]
    compaction_observed: Literal["not_observed"]
    billing_observed: Literal["not_observed"]
    local_token_estimate: Budget
    observation_clocks: ObservationClocks


class Plan(Observations):
    schema_version: str
    session_ref: str
    plan_id: str
    superseded_plan_id: str | None
    route_capacity: RouteCapacity
    must_keep_pack: MustKeep
    stale: bool
    invalidation: Invalidation
    rebind_count: int
    route_history: list[str]
    active_budget_source: RouteCapacity


class Recovery(TypedDict):
    max_checkpoint_attempts: int
    completion: Literal["not_observed"]
    resolution: str


class HostRoute(TypedDict):
    wire_model_matches: bool
    provider: None
    provider_observation: Literal["unknown"]


class BudgetContext(Observations):
    schema_version: str
    plan_id: str | None
    must_keep_pack: MustKeep | None
    stale: bool
    invalidation: Invalidation
    usable_budget_tokens: Budget
    active_budget_source: RouteCapacity | None
    host_route: HostRoute
    recovery: Recovery
    claim_boundary: str


class BudgetPlanError(ValueError):
    """A bounded reason code for invalid local plan metadata."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)
