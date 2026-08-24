from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Final


LOOP_PHASE_TRANSITION_SCHEMA: Final[str] = "loop_phase_transition/v1"
LOOP_GOAL_DRIVER_OBSERVATION_SCHEMA: Final[str] = "loop_goal_driver_observation/v1"
LOOP_PHASES: Final[frozenset[str]] = frozenset(
    {"interview", "plan", "research", "handoff", "execution", "feedback", "waiting", "blocked", "complete"}
)

_PHASE_TARGETS: Final[dict[str, tuple[str, str, str]]] = {
    "interview": ("planning", "plan", "goal_contract_observed"),
    "plan": ("research", "research", "plan_observed"),
    "research": ("executor_handoff", "handoff", "research_evidence_observed"),
    "handoff": ("executor_dispatch", "execution", "handoff_observed"),
    "execution": ("review_fix_loop", "feedback", "verification_observed"),
    "feedback": ("planning", "plan", "feedback_observed"),
}
_STORAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_UTC_RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)")
_TRANSITION_KEYS: Final = frozenset(
    "schema_version transition_id sequence loop_id from_phase to_phase phase_gate transition_kind "
    "cause source_ref evidence_refs observed_at native_goal".split()
)
_OBSERVATION_KEYS: Final = frozenset(
    "schema_version observation_id loop_id session_ref goal_command_sha256 observation_source "
    "observed_at activation turns summary privacy".split()
)
_TURN_KEYS: Final = frozenset(
    "turn_index session_ref from_phase to_phase phase_gate turn_ended_evidence_refs "
    "phase_gate_evidence_refs".split()
)
_FORBIDDEN_FIELD_PARTS: Final = frozenset({"quota", "score", "scoring", "prompt", "log", "logs"})


class _TransitionPolicyError(ValueError):
    """Raised when an in-memory transition cannot satisfy the policy contract."""


def phase_target(phase: str) -> tuple[str, str, str]:
    """Return the planned action, next phase, and evidence gate for a native phase."""
    try:
        return _PHASE_TARGETS[phase]
    except KeyError as exc:
        raise _TransitionPolicyError(f"loop phase cannot advance natively: {phase!r}") from exc


def validate_loop_phase_transition(value: object) -> list[str]:
    """Return every contract error in one phase-transition record."""
    if not isinstance(value, Mapping):
        return ["loop phase transition must be an object"]
    errors = _key_errors(value, _TRANSITION_KEYS, "loop phase transition")
    if value.get("schema_version") != LOOP_PHASE_TRANSITION_SCHEMA:
        errors.append(f"schema_version must be {LOOP_PHASE_TRANSITION_SCHEMA}")
    for field in ("transition_id", "loop_id"):
        if not _is_storage_id(value.get(field)):
            errors.append(f"{field} must be a storage-safe non-empty id")
    sequence = value.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        errors.append("sequence must be a positive integer")
    elif value.get("transition_id") != f"phase-transition-{sequence}":
        errors.append(f"transition_id must be phase-transition-{sequence}")
    from_phase = value.get("from_phase")
    to_phase = value.get("to_phase")
    gate = value.get("phase_gate")
    target = _PHASE_TARGETS.get(from_phase) if isinstance(from_phase, str) else None
    if target is None or target[1] != to_phase:
        errors.append(f"illegal loop phase transition: {from_phase!r} -> {to_phase!r}")
    elif gate != target[2]:
        errors.append(f"phase_gate must be {target[2]} for {from_phase} -> {to_phase}")
    for field in ("transition_kind", "cause", "source_ref"):
        if not _is_nonempty_text(value.get(field)):
            errors.append(f"{field} must be a non-empty metadata value")
    errors.extend(_reference_list_errors(value.get("evidence_refs"), "evidence_refs"))
    if not _is_utc_rfc3339(value.get("observed_at")):
        errors.append("observed_at must be a UTC RFC3339 timestamp")
    return errors


def validate_loop_goal_driver_observation(
    value: object,
    expected_loop_id: str = "",
    expected_goal_command_sha256: str = "",
) -> list[str]:
    """Return every error in metadata-only native-goal observation evidence."""
    if not isinstance(value, Mapping):
        return ["loop goal driver observation must be an object"]
    allowed_keys = _OBSERVATION_KEYS | {"native_goal_status"}
    errors = _key_errors(value, allowed_keys, "loop goal driver observation")
    forbidden = sorted(_forbidden_fields(value))
    if forbidden:
        errors.append(f"loop goal driver observation has forbidden fields: {forbidden}")
    if value.get("schema_version") != LOOP_GOAL_DRIVER_OBSERVATION_SCHEMA:
        errors.append(f"schema_version must be {LOOP_GOAL_DRIVER_OBSERVATION_SCHEMA}")
    for field in ("observation_id", "loop_id", "session_ref"):
        if not _is_storage_id(value.get(field)):
            errors.append(f"{field} must be a storage-safe non-empty id")
    if expected_loop_id and value.get("loop_id") != expected_loop_id:
        errors.append(f"loop_id must match expected loop_id {expected_loop_id}")
    digest = value.get("goal_command_sha256")
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        errors.append("goal_command_sha256 must be a lowercase 64-hex digest")
    if expected_goal_command_sha256 and digest != expected_goal_command_sha256:
        errors.append("goal_command_sha256 must match the prepared goal command")
    if value.get("observation_source") not in {"hermes_host", "wrapper", "operator"}:
        errors.append("observation_source must be hermes_host, wrapper, or operator")
    if not _is_utc_rfc3339(value.get("observed_at")):
        errors.append("observed_at must be a UTC RFC3339 timestamp")
    if value.get("privacy") != "metadata_only":
        errors.append("privacy must be metadata_only")
    errors.extend(_activation_errors(value.get("activation")))
    errors.extend(_turn_errors(value.get("turns"), value.get("session_ref")))
    return errors


def transition_loop_phase(
    cycle: dict[str, object],
    *,
    to_phase: str,
    phase_gate: str,
    transition_kind: str,
    cause: str,
    source_ref: str,
    evidence_refs: Iterable[str],
    observed_at: str,
    native_goal: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Append one evidence-backed legal transition and update only in-memory phase state."""
    existing_value = cycle.get("phase_transitions", [])
    if not isinstance(existing_value, list):
        raise _TransitionPolicyError("phase_transitions must be a list")
    existing = list(existing_value)
    for expected_sequence, item in enumerate(existing, start=1):
        errors = validate_loop_phase_transition(item)
        if errors:
            raise _TransitionPolicyError(errors[0])
        if item.get("sequence") != expected_sequence:
            raise _TransitionPolicyError(f"phase transition sequence must be contiguous at {expected_sequence}")
    sequence = len(existing) + 1
    transition: dict[str, object] = {
        "schema_version": LOOP_PHASE_TRANSITION_SCHEMA,
        "transition_id": f"phase-transition-{sequence}",
        "sequence": sequence,
        "loop_id": cycle.get("loop_id", ""),
        "from_phase": cycle.get("phase", ""),
        "to_phase": to_phase,
        "phase_gate": phase_gate,
        "transition_kind": transition_kind,
        "cause": cause,
        "source_ref": source_ref,
        "evidence_refs": list(evidence_refs),
        "observed_at": observed_at,
    }
    if native_goal is not None:
        transition["native_goal"] = dict(native_goal)
    errors = validate_loop_phase_transition(transition)
    if errors:
        raise _TransitionPolicyError(errors[0])
    existing.append(transition)
    cycle["phase_transitions"] = existing
    cycle["phase"] = to_phase
    return cycle


def native_goal_status(cycle: Mapping[str, object]) -> dict[str, object]:
    """Derive activation and same-session continuation from accepted observations."""
    default: dict[str, object] = {
        "activation_status": "not_observed",
        "continuation_status": "not_observed",
        "session_ref": "",
        "last_turn_index": 0,
    }
    observations = cycle.get("goal_driver_observations", [])
    if not isinstance(observations, list):
        return default
    accepted = [item for item in observations if not validate_loop_goal_driver_observation(item)]
    if not accepted:
        return default
    latest = accepted[-1]
    turns = latest.get("turns")
    if not isinstance(turns, list):
        return default
    return {
        "activation_status": "observed",
        "continuation_status": "observed",
        "session_ref": latest.get("session_ref", ""),
        "last_turn_index": len(turns),
    }


def _activation_errors(value: object) -> list[str]:
    if not isinstance(value, Mapping):
        return ["activation must be an object"]
    errors = _key_errors(value, frozenset({"status", "evidence_refs"}), "activation")
    if value.get("status") != "observed":
        errors.append("activation.status must be observed")
    errors.extend(_reference_list_errors(value.get("evidence_refs"), "activation.evidence_refs"))
    return errors


def _turn_errors(value: object, session_ref: object) -> list[str]:
    if not isinstance(value, list):
        return ["turns must be a list with at least two observed turns"]
    errors: list[str] = []
    if len(value) < 2:
        errors.append("turns must contain at least two observed turns")
    for expected_index, turn in enumerate(value, start=1):
        label = f"turns[{expected_index - 1}]"
        if not isinstance(turn, Mapping):
            errors.append(f"{label} must be an object")
            continue
        errors.extend(_key_errors(turn, _TURN_KEYS, label))
        if turn.get("turn_index") != expected_index:
            errors.append(f"{label}.turn_index: expected turn_index {expected_index}, got {turn.get('turn_index')}")
        if turn.get("session_ref") != session_ref:
            errors.append(f"{label}.session_ref must match session_ref")
        target = _PHASE_TARGETS.get(turn.get("from_phase"))
        if target is None or target[1] != turn.get("to_phase"):
            errors.append(f"{label} has an illegal loop phase transition")
        elif turn.get("phase_gate") != target[2]:
            errors.append(f"{label}.phase_gate must be {target[2]}")
        errors.extend(_reference_list_errors(turn.get("turn_ended_evidence_refs"), f"{label}.turn_ended_evidence_refs"))
        errors.extend(_reference_list_errors(turn.get("phase_gate_evidence_refs"), f"{label}.phase_gate_evidence_refs"))
    return errors


def _key_errors(value: Mapping[object, object], allowed: frozenset[str], label: str) -> list[str]:
    present = {key for key in value if isinstance(key, str)}
    missing = sorted(allowed - {"native_goal", "native_goal_status"} - present)
    unexpected = sorted(present - allowed)
    errors = [f"{label} is missing keys: {missing}"] if missing else []
    if unexpected:
        errors.append(f"{label} has unsupported keys: {unexpected}")
    return errors


def _reference_list_errors(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(_is_nonempty_text(item) for item in value):
        return [f"{label} must contain observed-progress evidence"]
    return []


def _is_storage_id(value: object) -> bool:
    return isinstance(value, str) and _STORAGE_ID.fullmatch(value) is not None and ".." not in value


def _is_nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "\n" not in value and "\r" not in value


def _is_utc_rfc3339(value: object) -> bool:
    if not isinstance(value, str) or _UTC_RFC3339.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _forbidden_fields(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str) and _FORBIDDEN_FIELD_PARTS.intersection(re.split(r"[^a-z]+", key.lower())):
                found.add(key)
            found.update(_forbidden_fields(child))
        return found
    if isinstance(value, list):
        for child in value:
            found.update(_forbidden_fields(child))
    return found
