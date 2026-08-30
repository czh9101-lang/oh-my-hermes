"""`cross_harness_live_receipt/v2`: controller provenance for the live lane.

The receipt is a separate artifact from the `cross_harness_benchmark_cli_input/v1`
envelope the controller emits. `cross_harness_benchmark/v1` fixtures, schemas,
scoring semantics, and trust anchors stay untouched: the receipt adds no field to
the v1 submission and changes no v1 outcome. It records four things v1 cannot
express, alongside the digest of the exact envelope it describes:

* which submitted fixture results this controller observed itself, and which were
  carried from an outside submission, as an ordered authenticity tier;
* the efficiency facts of the run (duration, tokens, cost), kept strictly outside
  the quality score;
* a ternary per-unit verdict (`PASS` / `FAIL` / `INCONCLUSIVE`) plus the
  aggregate that excludes `INCONCLUSIVE` units from the pass-rate denominator;
* an optional `cross_harness_live_baseline_comparison/v1` block naming the
  verdict transitions against a prior receipt of the same task set.

Efficiency never earns points, never changes a level, and never turns a
`partial` into a `pass`. A fast run and a cheap run are separate facts from a
correct run.

`INCONCLUSIVE` is a statement about the controller, never about the harness: it
means this controller could not grade the unit at all (nothing was executed for
it, the launch failed, the timeout elapsed before any output, the observation
artifact was unreadable, or the graded telemetry channel reported nothing). A
unit that ran and produced a wrong observed result is `FAIL`, never
`INCONCLUSIVE`. Because an ungraded unit is not a passing unit and not a failing
one either, it is counted separately and left out of the pass-rate denominator,
which the summary states in the artifact rather than leaving to a reader.

Schema history: `v1` (PR #1173) carried the coverage lists, observations, and
efficiency block. `v2` adds `verdicts`, `verdict_summary`, and
`baseline_comparison`. Only `v2` is emitted and only `v2` validates, so a `v1`
receipt handed in as a baseline is refused rather than silently half-compared.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from omh.quality.cross_harness_benchmark_values import (
    BenchmarkValidationError,
    JsonValue,
    corpus_digest,
    safe,
)


RECEIPT_SCHEMA: Final = "cross_harness_live_receipt/v2"
COMPARISON_SCHEMA: Final = "cross_harness_live_baseline_comparison/v1"
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
#: Ternary per-unit outcome. `INCONCLUSIVE` marks controller-side inability to
#: grade, never a graded-but-failing unit.
VERDICTS: Final = ("PASS", "FAIL", "INCONCLUSIVE")
#: Why the controller could not grade a unit. One of these is required on every
#: `INCONCLUSIVE` verdict and forbidden on every graded one.
INCONCLUSIVE_REASONS: Final = (
    "no_controller_observation",
    "execution_launch_failed",
    "timed_out_before_output",
    "artifact_unreadable",
    "telemetry_channel_absent",
)
#: Baseline transition labels. `not_comparable` is the honest label whenever one
#: side of a comparison is not a graded value; it is never folded into `STABLE`.
COMPARISON_LABELS: Final = ("IMPROVED", "REGRESSED", "STABLE", "not_comparable")
DELTA_LABELS: Final = ("comparable", "not_comparable")
#: Named in the artifact so a reader never has to infer which units were counted.
PASS_RATE_DENOMINATOR: Final = "graded_units_only"
CLAIM_BOUNDARY: Final = (
    "Controller observation covers only the listed controller_observed fixtures "
    "and is not general live executor quality proof. Carried results stay "
    "unverified submissions. Efficiency facts are reported outside the quality "
    "score and never earn points. INCONCLUSIVE units are ungraded, not failing, "
    "and are excluded from the pass-rate denominator. A baseline comparison "
    "reports verdict transitions between two receipts and re-grades nothing."
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
        "verdicts",
        "verdict_summary",
        "baseline_comparison",
        "claim_boundary",
    }
)
_BINDING_FIELDS: Final = frozenset({"fixture_id", "provenance", "observation_ids"})
_VERDICT_FIELDS: Final = frozenset({"fixture_id", "verdict", "reason_code"})
_VERDICT_SUMMARY_FIELDS: Final = frozenset(
    {
        "units_total",
        "pass_count",
        "fail_count",
        "inconclusive_count",
        "graded_total",
        "pass_rate",
        "pass_rate_denominator",
        "inconclusive_excluded_from_pass_rate",
    }
)
_COMPARISON_FIELDS: Final = frozenset(
    {
        "schema_version",
        "baseline_mode",
        "baseline_corpus_digest",
        "baseline_envelope_digest",
        "units",
        "summary",
        "efficiency_delta",
    }
)
_COMPARISON_UNIT_FIELDS: Final = frozenset(
    {"fixture_id", "baseline_verdict", "current_verdict", "label"}
)
_COMPARISON_SUMMARY_FIELDS: Final = frozenset(
    {
        "label",
        "improved_count",
        "regressed_count",
        "stable_count",
        "not_comparable_count",
        "baseline_pass_rate",
        "current_pass_rate",
    }
)
_DELTA_FIELDS: Final = frozenset({"baseline", "current", "delta", "label"})
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


class BaselineComparisonError(ValueError):
    """A refusal to compare two receipts that do not describe the same run shape."""


def observation_inconclusive_reason(observation: Mapping[str, JsonValue]) -> str | None:
    """Return why this observation cannot be graded, or None when it can be.

    A process that ran and exited wrong is gradeable and grades to `FAIL`; only
    the controller's own inability to see a result lands here.
    """
    status = observation.get("status")
    failure_code = observation.get("failure_code")
    if status == "not_executed":
        return "no_controller_observation"
    if status == "timed_out":
        return "timed_out_before_output"
    if failure_code == "command_launch_failed":
        return "execution_launch_failed"
    if failure_code in {"observation_invalid", "observation_unavailable"}:
        return "artifact_unreadable"
    if status == "completed":
        semantic = observation.get("observed_semantic_result")
        if semantic is None:
            return "telemetry_channel_absent"
        if semantic == "unvalidated":
            return "artifact_unreadable"
    return None


def unit_verdicts(
    fixture_ids: Sequence[str],
    bindings: Sequence[Mapping[str, JsonValue]],
    observations: Sequence[Mapping[str, JsonValue]],
) -> list[dict[str, JsonValue]]:
    """Grade every unit of the task set from the observations bound to it.

    The task set is the whole corpus, not the submitted subset, so two runs of
    different coverage still compare unit for unit. Observed failure outranks an
    ungradeable sibling observation: positive evidence of a wrong result is a
    `FAIL` even when another channel of the same unit went dark.
    """
    by_id = {str(item["observation_id"]): item for item in observations}
    bound = {
        str(item["fixture_id"]): [str(value) for value in item["observation_ids"]]
        for item in bindings
        if isinstance(item.get("observation_ids"), list)
    }
    verdicts: list[dict[str, JsonValue]] = []
    for fixture_id in fixture_ids:
        blocked: list[str] = []
        failed = False
        identifiers = bound.get(fixture_id) or []
        for observation_id in identifiers:
            observation = by_id.get(observation_id)
            if observation is None:
                blocked.append("no_controller_observation")
                continue
            reason = observation_inconclusive_reason(observation)
            if reason is not None:
                blocked.append(reason)
            elif observation.get("status") != "completed":
                failed = True
        if not identifiers:
            verdict, reason_code = "INCONCLUSIVE", "no_controller_observation"
        elif failed:
            verdict, reason_code = "FAIL", None
        elif blocked:
            verdict, reason_code = "INCONCLUSIVE", blocked[0]
        else:
            verdict, reason_code = "PASS", None
        verdicts.append(
            {"fixture_id": fixture_id, "verdict": verdict, "reason_code": reason_code}
        )
    return verdicts


def verdict_summary(verdicts: Sequence[Mapping[str, JsonValue]]) -> dict[str, JsonValue]:
    """Count all three verdicts and rate only the graded ones."""
    counts = {value: 0 for value in VERDICTS}
    for item in verdicts:
        verdict = item.get("verdict")
        if isinstance(verdict, str) and verdict in counts:
            counts[verdict] += 1
    graded = counts["PASS"] + counts["FAIL"]
    return {
        "units_total": len(verdicts),
        "pass_count": counts["PASS"],
        "fail_count": counts["FAIL"],
        "inconclusive_count": counts["INCONCLUSIVE"],
        "graded_total": graded,
        "pass_rate": round(counts["PASS"] / graded, 6) if graded else None,
        "pass_rate_denominator": PASS_RATE_DENOMINATOR,
        "inconclusive_excluded_from_pass_rate": True,
    }


def transition_label(baseline_verdict: str, current_verdict: str) -> str:
    """Label one verdict transition; an ungraded side is never a direction."""
    if baseline_verdict not in VERDICTS or current_verdict not in VERDICTS:
        raise BaselineComparisonError("baseline_verdict_unknown")
    if baseline_verdict == current_verdict:
        return "STABLE"
    if "INCONCLUSIVE" in (baseline_verdict, current_verdict):
        return "not_comparable"
    return "IMPROVED" if current_verdict == "PASS" else "REGRESSED"


def compare_to_baseline(
    current: Mapping[str, JsonValue], baseline: Mapping[str, JsonValue]
) -> dict[str, JsonValue]:
    """Compare two receipts of the same task set; refuse anything else.

    Refusal is deliberate: silently intersecting two different task sets would
    report a pass rate over units the reader never chose.
    """
    if baseline.get("schema_version") != RECEIPT_SCHEMA:
        raise BaselineComparisonError("baseline_schema_unsupported")
    if baseline.get("corpus_digest") != current.get("corpus_digest"):
        raise BaselineComparisonError("baseline_corpus_mismatch")
    current_verdicts = _verdict_map(current.get("verdicts"), "current")
    baseline_verdicts = _verdict_map(baseline.get("verdicts"), "baseline")
    only_current = sorted(set(current_verdicts) - set(baseline_verdicts))
    only_baseline = sorted(set(baseline_verdicts) - set(current_verdicts))
    if only_current or only_baseline:
        raise BaselineComparisonError(
            "baseline_task_set_mismatch:only_in_current="
            + ",".join(only_current)
            + ";only_in_baseline="
            + ",".join(only_baseline)
        )
    units: list[JsonValue] = []
    counts = {label: 0 for label in COMPARISON_LABELS}
    for fixture_id in current_verdicts:
        before, after = baseline_verdicts[fixture_id], current_verdicts[fixture_id]
        label = transition_label(before, after)
        counts[label] += 1
        units.append(
            {
                "fixture_id": fixture_id,
                "baseline_verdict": before,
                "current_verdict": after,
                "label": label,
            }
        )
    return {
        "schema_version": COMPARISON_SCHEMA,
        "baseline_mode": baseline.get("mode"),
        "baseline_corpus_digest": baseline.get("corpus_digest"),
        "baseline_envelope_digest": baseline.get("envelope_digest"),
        "units": units,
        "summary": {
            "label": _aggregate_label(counts),
            "improved_count": counts["IMPROVED"],
            "regressed_count": counts["REGRESSED"],
            "stable_count": counts["STABLE"],
            "not_comparable_count": counts["not_comparable"],
            "baseline_pass_rate": _pass_rate(baseline.get("verdict_summary")),
            "current_pass_rate": _pass_rate(current.get("verdict_summary")),
        },
        "efficiency_delta": _efficiency_delta(
            current.get("efficiency"), baseline.get("efficiency")
        ),
    }


def _aggregate_label(counts: Mapping[str, int]) -> str:
    """Worst direction wins: one regression is the headline, not an average."""
    if counts["REGRESSED"]:
        return "REGRESSED"
    if counts["IMPROVED"]:
        return "IMPROVED"
    if counts["STABLE"]:
        return "STABLE"
    return "not_comparable"


def _efficiency_delta(
    current: JsonValue, baseline: JsonValue
) -> dict[str, JsonValue]:
    """Subtract telemetry only where both sides observed it; never estimate."""
    current_map = current if isinstance(current, dict) else {}
    baseline_map = baseline if isinstance(baseline, dict) else {}
    block: dict[str, JsonValue] = {}
    for field in ("tokens", "cost_usd"):
        before = _number(baseline_map.get(field))
        after = _number(current_map.get(field))
        comparable = before is not None and after is not None
        delta: JsonValue = None
        if comparable:
            delta = round(after - before, 6) if field == "cost_usd" else after - before
        block[field] = {
            "baseline": before,
            "current": after,
            "delta": delta,
            "label": "comparable" if comparable else "not_comparable",
        }
    return block


def _pass_rate(summary: JsonValue) -> JsonValue:
    if not isinstance(summary, dict):
        raise BaselineComparisonError("baseline_verdict_summary_missing")
    value = summary.get("pass_rate")
    if value is not None and _number(value) is None:
        raise BaselineComparisonError("baseline_pass_rate_invalid")
    return value


def _number(value: JsonValue) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _verdict_map(raw: JsonValue, side: str) -> dict[str, str]:
    if not isinstance(raw, list) or not raw:
        raise BaselineComparisonError(f"{side}_verdicts_missing")
    mapped: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise BaselineComparisonError(f"{side}_verdict_invalid")
        fixture_id, verdict = item.get("fixture_id"), item.get("verdict")
        if not isinstance(fixture_id, str) or not fixture_id or verdict not in VERDICTS:
            raise BaselineComparisonError(f"{side}_verdict_invalid")
        if fixture_id in mapped:
            raise BaselineComparisonError(f"{side}_verdict_duplicate")
        mapped[fixture_id] = str(verdict)
    return mapped


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
    verdicts = _check_verdicts(raw["verdicts"], reasons)
    _check_verdict_summary(raw["verdict_summary"], verdicts, reasons)
    _check_comparison(raw["baseline_comparison"], set(verdicts), reasons)
    _check_coverage(raw, set(verdicts), reasons)
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


def _check_verdicts(raw: JsonValue, reasons: list[str]) -> dict[str, str]:
    """A reason code is required on every INCONCLUSIVE unit and only on those."""
    verdicts: dict[str, str] = {}
    if not isinstance(raw, list):
        reasons.append("invalid_verdicts")
        return verdicts
    for item in raw:
        if not isinstance(item, dict):
            reasons.append("invalid_verdict")
            continue
        before = len(reasons)
        _check_shape(item, _VERDICT_FIELDS, "verdict", reasons)
        if len(reasons) != before:
            continue
        fixture_id = item["fixture_id"]
        _text(fixture_id, "invalid_verdict_fixture_id", reasons)
        _member(item["verdict"], VERDICTS, "invalid_verdict_value", reasons)
        reason_code = item["reason_code"]
        if item["verdict"] == "INCONCLUSIVE":
            _member(reason_code, INCONCLUSIVE_REASONS, "invalid_inconclusive_reason", reasons)
        elif reason_code is not None:
            reasons.append("graded_verdict_claims_inconclusive_reason")
        if isinstance(fixture_id, str) and fixture_id:
            if fixture_id in verdicts:
                reasons.append("duplicate_verdict_fixture_id")
            verdicts[fixture_id] = str(item["verdict"])
    return verdicts


def _check_verdict_summary(
    raw: JsonValue, verdicts: Mapping[str, str], reasons: list[str]
) -> None:
    """The aggregate must restate the verdict list and exclude INCONCLUSIVE units."""
    if not isinstance(raw, dict):
        reasons.append("invalid_verdict_summary")
        return
    before = len(reasons)
    _check_shape(raw, _VERDICT_SUMMARY_FIELDS, "verdict_summary", reasons)
    if len(reasons) != before:
        return
    for field in ("units_total", "pass_count", "fail_count", "inconclusive_count", "graded_total"):
        if type(raw[field]) is not int or raw[field] < 0:
            reasons.append("invalid_verdict_count")
            return
    if raw["pass_rate_denominator"] != PASS_RATE_DENOMINATOR:
        reasons.append("invalid_pass_rate_denominator")
    if raw["inconclusive_excluded_from_pass_rate"] is not True:
        reasons.append("inconclusive_not_excluded_from_pass_rate")
    if raw["graded_total"] != raw["pass_count"] + raw["fail_count"]:
        reasons.append("invalid_graded_total")
    if raw["units_total"] != raw["graded_total"] + raw["inconclusive_count"]:
        reasons.append("invalid_units_total")
    expected = verdict_summary([{"fixture_id": key, "verdict": value} for key, value in verdicts.items()])
    if any(raw[field] != expected[field] for field in ("units_total", "pass_count", "fail_count", "inconclusive_count")):
        reasons.append("verdict_summary_mismatch")
    rate = raw["pass_rate"]
    if raw["graded_total"] == 0:
        if rate is not None:
            reasons.append("pass_rate_without_graded_units")
    elif _number(rate) is None or not 0 <= float(rate) <= 1:  # type: ignore[arg-type]
        reasons.append("invalid_pass_rate")


def _check_comparison(raw: JsonValue, fixture_ids: set[str], reasons: list[str]) -> None:
    """Validate the optional baseline block against this receipt's own task set."""
    if raw is None:
        return
    if not isinstance(raw, dict):
        reasons.append("invalid_baseline_comparison")
        return
    before = len(reasons)
    _check_shape(raw, _COMPARISON_FIELDS, "baseline_comparison", reasons)
    if len(reasons) != before:
        return
    _member(raw["schema_version"], (COMPARISON_SCHEMA,), "unknown_comparison_schema", reasons)
    _member(raw["baseline_mode"], MODES, "invalid_baseline_mode", reasons)
    _digest(raw["baseline_corpus_digest"], "invalid_baseline_corpus_digest", reasons)
    _digest(raw["baseline_envelope_digest"], "invalid_baseline_envelope_digest", reasons)
    compared = _check_comparison_units(raw["units"], reasons)
    if compared is not None and compared != fixture_ids:
        reasons.append("comparison_task_set_mismatch")
    _check_comparison_summary(raw["summary"], reasons)
    _check_efficiency_delta(raw["efficiency_delta"], reasons)


def _check_comparison_units(raw: JsonValue, reasons: list[str]) -> set[str] | None:
    if not isinstance(raw, list):
        reasons.append("invalid_comparison_units")
        return None
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            reasons.append("invalid_comparison_unit")
            return None
        before = len(reasons)
        _check_shape(item, _COMPARISON_UNIT_FIELDS, "comparison_unit", reasons)
        if len(reasons) != before:
            return None
        _text(item["fixture_id"], "invalid_comparison_fixture_id", reasons)
        _member(item["baseline_verdict"], VERDICTS, "invalid_comparison_verdict", reasons)
        _member(item["current_verdict"], VERDICTS, "invalid_comparison_verdict", reasons)
        _member(item["label"], COMPARISON_LABELS, "invalid_comparison_label", reasons)
        if isinstance(item["fixture_id"], str):
            seen.add(item["fixture_id"])
    return seen


def _check_comparison_summary(raw: JsonValue, reasons: list[str]) -> None:
    if not isinstance(raw, dict):
        reasons.append("invalid_comparison_summary")
        return
    before = len(reasons)
    _check_shape(raw, _COMPARISON_SUMMARY_FIELDS, "comparison_summary", reasons)
    if len(reasons) != before:
        return
    _member(raw["label"], COMPARISON_LABELS, "invalid_comparison_label", reasons)
    for field in ("improved_count", "regressed_count", "stable_count", "not_comparable_count"):
        if type(raw[field]) is not int or raw[field] < 0:
            reasons.append("invalid_comparison_count")
    for field in ("baseline_pass_rate", "current_pass_rate"):
        value = raw[field]
        if value is not None and (_number(value) is None or not 0 <= float(value) <= 1):
            reasons.append("invalid_comparison_pass_rate")


def _check_efficiency_delta(raw: JsonValue, reasons: list[str]) -> None:
    """A delta exists only where both sides observed the figure."""
    if not isinstance(raw, dict) or set(raw) != {"tokens", "cost_usd"}:
        reasons.append("invalid_efficiency_delta")
        return
    for field, block in raw.items():
        if not isinstance(block, dict):
            reasons.append("invalid_efficiency_delta")
            return
        before = len(reasons)
        _check_shape(block, _DELTA_FIELDS, "efficiency_delta", reasons)
        if len(reasons) != before:
            return
        _member(block["label"], DELTA_LABELS, "invalid_delta_label", reasons)
        both = _number(block["baseline"]) is not None and _number(block["current"]) is not None
        if both != (block["label"] == "comparable"):
            reasons.append("invalid_delta_label")
        if both and _number(block["delta"]) is None:
            reasons.append("missing_delta")
        if not both and block["delta"] is not None:
            reasons.append("estimated_delta")
        if field == "tokens" and block["delta"] is not None and type(block["delta"]) is not int:
            reasons.append("invalid_token_delta")


def _check_coverage(raw: Mapping[str, JsonValue], fixture_ids: set[str], reasons: list[str]) -> None:
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
    if set(flattened) != fixture_ids:
        reasons.append("verdict_coverage_mismatch")
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
