"""`cross_harness_live_receipt/v1`: controller provenance for the live lane.

The receipt is a separate artifact from the `cross_harness_benchmark_cli_input/v1`
envelope the controller emits. `cross_harness_benchmark/v1` fixtures, schemas,
scoring semantics, and trust anchors stay untouched: the receipt adds no field to
the v1 submission and changes no v1 outcome. It records two things v1 cannot
express, alongside the digest of the exact envelope it describes:

* which submitted fixture results this controller observed itself, and which were
  carried from an outside submission, as an ordered authenticity tier;
* the efficiency facts of the run (duration, tokens, cost), kept strictly outside
  the quality score.

Efficiency never earns points, never changes a level, and never turns a
`partial` into a `pass`. A fast run and a cheap run are separate facts from a
correct run.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from omh.quality.cross_harness_benchmark_values import (
    BenchmarkValidationError,
    JsonValue,
    corpus_digest,
    safe,
)


RECEIPT_SCHEMA: Final = "cross_harness_live_receipt/v1"
RUN_SCHEMA: Final = "cross_harness_live_run/v1"
DOCTOR_SCHEMA: Final = "cross_harness_live_doctor/v1"

MODES: Final = ("fake", "probe", "dispatch")
#: Ordered weakest to strongest. `unverified_submission` is the same claim the
#: v1 scorer always returns; the controller may only ever report a stronger tier
#: for results it executed itself.
AUTHENTICITY_TIERS: Final = (
    "fake_adapter",
    "unverified_submission",
    "mixed_controller_and_submitted",
    "controller_observed",
)
PROVENANCE: Final = ("fake_adapter", "carried_from_base", "controller_observed")
OBSERVATION_KINDS: Final = ("command_binding", "hermes_child_dispatch")
OBSERVATION_STATUSES: Final = ("completed", "failed", "timed_out", "not_executed")
CWD_CLASSES: Final = ("repository_root", "isolated_temporary")
FAILURE_CODES: Final = (
    "command_launch_failed",
    "timeout",
    "nonzero_exit",
    "observation_unavailable",
    "observation_invalid",
)
CLAIM_BOUNDARY: Final = (
    "Controller observation covers only the listed controller_observed fixtures "
    "and is not general live executor quality proof. Carried results stay "
    "unverified submissions. Efficiency facts are reported outside the quality "
    "score and never earn points."
)

_RECEIPT_FIELDS: Final = frozenset(
    {
        "schema_version",
        "mode",
        "harness_id",
        "corpus_digest",
        "envelope_digest",
        "evidence_authenticity",
        "controller_observed_fixture_ids",
        "carried_fixture_ids",
        "simulated_fixture_ids",
        "unsupported_fixture_ids",
        "fixture_bindings",
        "observations",
        "efficiency",
        "claim_boundary",
    }
)
_BINDING_FIELDS: Final = frozenset({"fixture_id", "provenance", "observation_ids"})
_OBSERVATION_FIELDS: Final = frozenset(
    {
        "observation_id",
        "kind",
        "cwd_class",
        "argv_digest",
        "status",
        "observed_exit",
        "observed_semantic_result",
        "failure_code",
        "duration_ms",
        "tokens",
        "cost_usd",
        "tools",
    }
)
_EFFICIENCY_FIELDS: Final = frozenset(
    {
        "duration_ms",
        "tokens",
        "cost_usd",
        "observations_total",
        "observations_reporting_tokens",
        "observations_reporting_cost_usd",
        "complete",
    }
)
_HEX_64_DIGITS: Final = frozenset("0123456789abcdef")


def envelope_digest(envelope: Mapping[str, JsonValue]) -> str:
    """Return the canonical digest binding a receipt to one emitted envelope."""
    return corpus_digest(dict(envelope))


def authenticity_tier(
    *, mode: str, observed_count: int, carried_count: int
) -> str:
    """Derive the honest tier from what the controller actually executed."""
    if mode not in MODES:
        raise ValueError("mode must be one of " + ", ".join(MODES))
    if mode == "fake":
        return "fake_adapter"
    if not observed_count:
        return "unverified_submission"
    if carried_count:
        return "mixed_controller_and_submitted"
    return "controller_observed"


def aggregate_efficiency(observations: list[Mapping[str, JsonValue]]) -> dict[str, JsonValue]:
    """Sum reported telemetry only; missing telemetry stays null, never estimated."""
    durations = [item["duration_ms"] for item in observations if isinstance(item["duration_ms"], int)]
    tokens = [item["tokens"] for item in observations if isinstance(item["tokens"], int)]
    costs = [
        item["cost_usd"]
        for item in observations
        if isinstance(item["cost_usd"], (int, float)) and not isinstance(item["cost_usd"], bool)
    ]
    return {
        "duration_ms": sum(durations) if durations else None,
        "tokens": sum(tokens) if tokens else None,
        "cost_usd": round(sum(costs), 6) if costs else None,
        "observations_total": len(observations),
        "observations_reporting_tokens": len(tokens),
        "observations_reporting_cost_usd": len(costs),
        "complete": bool(observations)
        and len(tokens) == len(observations)
        and len(costs) == len(observations),
    }


def validate_receipt(raw: JsonValue) -> tuple[str, ...]:
    """Return ordered reason codes; an empty tuple means the receipt is valid."""
    if not isinstance(raw, dict):
        return ("receipt_not_object",)
    reasons: list[str] = []
    _check_shape(raw, _RECEIPT_FIELDS, "receipt", reasons)
    if reasons:
        return tuple(reasons)
    _member(raw["schema_version"], (RECEIPT_SCHEMA,), "unknown_receipt_schema", reasons)
    _member(raw["mode"], MODES, "invalid_mode", reasons)
    _member(raw["evidence_authenticity"], AUTHENTICITY_TIERS, "invalid_authenticity_tier", reasons)
    _text(raw["harness_id"], "invalid_harness_id", reasons)
    _digest(raw["corpus_digest"], "invalid_corpus_digest", reasons)
    _digest(raw["envelope_digest"], "invalid_envelope_digest", reasons)
    if raw["claim_boundary"] != CLAIM_BOUNDARY:
        reasons.append("invalid_claim_boundary")
    observation_ids = _check_observations(raw["observations"], reasons)
    _check_bindings(raw["fixture_bindings"], observation_ids, reasons)
    _check_efficiency(raw["efficiency"], reasons)
    _check_coverage(raw, reasons)
    try:
        safe(dict(raw))
    except BenchmarkValidationError:
        reasons.append("unsafe_metadata")
    return tuple(reasons)


def _check_observations(raw: JsonValue, reasons: list[str]) -> set[str]:
    ids: set[str] = set()
    if not isinstance(raw, list):
        reasons.append("invalid_observations")
        return ids
    for item in raw:
        if not isinstance(item, dict):
            reasons.append("invalid_observation")
            continue
        before = len(reasons)
        _check_shape(item, _OBSERVATION_FIELDS, "observation", reasons)
        if len(reasons) != before:
            continue
        identifier = item["observation_id"]
        _text(identifier, "invalid_observation_id", reasons)
        if isinstance(identifier, str):
            if identifier in ids:
                reasons.append("duplicate_observation_id")
            ids.add(identifier)
        _member(item["kind"], OBSERVATION_KINDS, "invalid_observation_kind", reasons)
        _member(item["cwd_class"], CWD_CLASSES, "invalid_cwd_class", reasons)
        _member(item["status"], OBSERVATION_STATUSES, "invalid_observation_status", reasons)
        _digest(item["argv_digest"], "invalid_argv_digest", reasons)
        _optional_int(item["observed_exit"], "invalid_observed_exit", reasons)
        _optional_text(item["observed_semantic_result"], "invalid_semantic_result", reasons)
        _optional_member(item["failure_code"], FAILURE_CODES, "invalid_failure_code", reasons)
        for field, code in (
            ("duration_ms", "invalid_duration_ms"),
            ("tokens", "invalid_tokens"),
            ("tools", "invalid_tools"),
        ):
            _optional_int(item[field], code, reasons, non_negative=True)
        _optional_number(item["cost_usd"], "invalid_cost_usd", reasons)
    return ids


def _check_bindings(raw: JsonValue, observation_ids: set[str], reasons: list[str]) -> None:
    if not isinstance(raw, list):
        reasons.append("invalid_fixture_bindings")
        return
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            reasons.append("invalid_fixture_binding")
            continue
        before = len(reasons)
        _check_shape(item, _BINDING_FIELDS, "fixture_binding", reasons)
        if len(reasons) != before:
            continue
        fixture_id = item["fixture_id"]
        _text(fixture_id, "invalid_fixture_id", reasons)
        if isinstance(fixture_id, str):
            if fixture_id in seen:
                reasons.append("duplicate_fixture_binding")
            seen.add(fixture_id)
        _member(item["provenance"], PROVENANCE, "invalid_provenance", reasons)
        bound = item["observation_ids"]
        if not isinstance(bound, list) or any(not isinstance(value, str) for value in bound):
            reasons.append("invalid_binding_observation_ids")
            continue
        if any(value not in observation_ids for value in bound):
            reasons.append("unknown_binding_observation_id")
        if item["provenance"] == "controller_observed" and not bound:
            reasons.append("unbound_controller_observation")
        if item["provenance"] == "carried_from_base" and bound:
            reasons.append("carried_result_claims_observation")


def _check_efficiency(raw: JsonValue, reasons: list[str]) -> None:
    if not isinstance(raw, dict):
        reasons.append("invalid_efficiency")
        return
    before = len(reasons)
    _check_shape(raw, _EFFICIENCY_FIELDS, "efficiency", reasons)
    if len(reasons) != before:
        return
    _optional_int(raw["duration_ms"], "invalid_duration_ms", reasons, non_negative=True)
    _optional_int(raw["tokens"], "invalid_tokens", reasons, non_negative=True)
    _optional_number(raw["cost_usd"], "invalid_cost_usd", reasons)
    for field in ("observations_total", "observations_reporting_tokens", "observations_reporting_cost_usd"):
        if type(raw[field]) is not int or raw[field] < 0:
            reasons.append("invalid_efficiency_count")
    if not isinstance(raw["complete"], bool):
        reasons.append("invalid_efficiency_complete")


def _check_coverage(raw: Mapping[str, JsonValue], reasons: list[str]) -> None:
    """The four coverage lists partition the corpus; the tier may not outrun them."""
    lists: dict[str, list[str]] = {}
    for field in (
        "controller_observed_fixture_ids",
        "carried_fixture_ids",
        "simulated_fixture_ids",
        "unsupported_fixture_ids",
    ):
        value = raw[field]
        if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
            reasons.append("invalid_fixture_id_list")
            return
        lists[field] = [item for item in value if isinstance(item, str)]
    flattened = [item for value in lists.values() for item in value]
    if len(flattened) != len(set(flattened)):
        reasons.append("overlapping_fixture_coverage")
    observed = lists["controller_observed_fixture_ids"]
    if raw["evidence_authenticity"] == "controller_observed" and lists["carried_fixture_ids"]:
        reasons.append("authenticity_tier_overclaims")
    if raw["evidence_authenticity"] in {"controller_observed", "mixed_controller_and_submitted"} and not observed:
        reasons.append("authenticity_tier_overclaims")
    if raw["mode"] == "fake" and observed:
        reasons.append("fake_mode_claims_observation")
    if raw["mode"] != "fake" and lists["simulated_fixture_ids"]:
        reasons.append("live_mode_reports_simulated_result")


def _check_shape(raw: Mapping[str, JsonValue], fields: frozenset[str], label: str, reasons: list[str]) -> None:
    if set(raw) != fields:
        reasons.append(f"invalid_{label}_shape")


def _member(value: JsonValue, allowed: tuple[str, ...], code: str, reasons: list[str]) -> None:
    if value not in allowed:
        reasons.append(code)


def _optional_member(value: JsonValue, allowed: tuple[str, ...], code: str, reasons: list[str]) -> None:
    if value is not None and value not in allowed:
        reasons.append(code)


def _text(value: JsonValue, code: str, reasons: list[str]) -> None:
    if not isinstance(value, str) or not value:
        reasons.append(code)


def _optional_text(value: JsonValue, code: str, reasons: list[str]) -> None:
    if value is not None and (not isinstance(value, str) or not value):
        reasons.append(code)


def _optional_int(value: JsonValue, code: str, reasons: list[str], *, non_negative: bool = False) -> None:
    if value is None:
        return
    if type(value) is not int or (non_negative and value < 0):
        reasons.append(code)


def _optional_number(value: JsonValue, code: str, reasons: list[str]) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        reasons.append(code)


def _digest(value: JsonValue, code: str, reasons: list[str]) -> None:
    if not isinstance(value, str) or len(value) != 64 or set(value) - _HEX_64_DIGITS:
        reasons.append(code)
