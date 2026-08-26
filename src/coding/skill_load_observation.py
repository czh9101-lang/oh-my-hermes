"""Fail-closed, opt-in Hermes loaded-skill inventory observation.

A successful child process, model response, static skill listing, or scheduler
source is not an inventory.  ``observed`` is emitted only for the closed,
nonce-bound machine protocol parsed here.  Installed Hermes versions that do
not implement that protocol are reported as ``unsupported``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
from threading import Lock
from typing import Final, cast

from ._hermes_child_process import start_pipe_drainers, terminate_process_group
from .hermes_child_dispatch import require_hermes_child_dispatch_boundary

SKILL_LOAD_OBSERVATION_SCHEMA_VERSION: Final = "skill_load_observation/v1"
HERMES_SKILL_INVENTORY_SCHEMA_VERSION: Final = "hermes_skill_inventory/v1"
PROBE_STATUSES: Final = ("observed", "unsupported", "probe_error")
LOAD_STATES: Final = ("all_loaded", "partially_loaded", "none_loaded", "not_applicable")
REASON_CODES: Final = (
    "inventory_validated",
    "inventory_protocol_unavailable",
    "inventory_process_error",
    "inventory_executable_unavailable",
    "inventory_timeout",
    "inventory_response_malformed",
    "inventory_nonce_mismatch",
    "inventory_expected_digest_mismatch",
    "inventory_digest_mismatch",
    "inventory_response_expired",
    "inventory_response_stale",
    "inventory_nonce_replayed",
)
_MAX_PROTOCOL_SECONDS: Final = 300
_SKILL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_OBSERVATION_FIELDS: Final = frozenset(
    {
        "schema_version", "probe_status", "reason_code", "nonce",
        "expected_digest", "inventory_digest", "tool_fingerprint",
        "runtime_fingerprint", "observed_at", "expires_at", "load_state",
        "expected_skills", "observed_skills", "missing_skills", "unexpected_skills",
    }
)
_INVENTORY_FIELDS: Final = frozenset(
    {
        "schema_version", "nonce", "expected_digest", "inventory_digest",
        "tool_fingerprint", "runtime_fingerprint", "observed_at", "expires_at",
        "observed_skills",
    }
)
_INVENTORY_CLAIM_FIELDS: Final = frozenset(
    {
        "inventory_digest", "load_state", "expected_skills", "observed_skills",
        "missing_skills", "unexpected_skills",
    }
)
_SAFE_ENV_NAMES: Final = frozenset(
    {
        "PATH", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TERM",
        "TMPDIR", "TMP", "TEMP", "SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC",
    }
)


@dataclass(frozen=True, slots=True)
class SkillLoadProbeRequest:
    expected_skills: Sequence[str]
    hermes: str = "hermes"
    timeout_seconds: float = 10.0
    termination_grace_seconds: float = 0.25
    env: Mapping[str, str] | None = None
    nonce: str | None = None


class InventoryNonceLedger:
    """Bounded in-process replay ledger for validated protocol nonces."""

    def __init__(self, *, capacity: int = 1024) -> None:
        if capacity <= 0:
            raise ValueError("nonce ledger capacity must be positive")
        self._capacity = capacity
        self._ordered: list[str] = []
        self._seen: set[str] = set()
        self._lock = Lock()

    def contains(self, nonce: str) -> bool:
        with self._lock:
            return nonce in self._seen

    def consume(self, nonce: str) -> bool:
        with self._lock:
            if nonce in self._seen:
                return False
            self._seen.add(nonce)
            self._ordered.append(nonce)
            if len(self._ordered) > self._capacity:
                self._seen.discard(self._ordered.pop(0))
            return True


def expected_skills_digest(skills: Sequence[str]) -> str:
    normalized = _normalized_skill_set(skills, field="expected_skills")
    return _set_digest(normalized)


def probe_skill_load(
    request: SkillLoadProbeRequest,
    *,
    confirmed: bool,
    nonce_ledger: InventoryNonceLedger | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Run the explicit machine inventory protocol once.

    Constructing a request does nothing.  Confirmation is checked before path
    inspection or process creation, matching the Hermes-child dispatch boundary.
    """
    require_hermes_child_dispatch_boundary(
        dispatch_policy="ask_before_dispatch", confirmed=confirmed
    )
    if request.timeout_seconds <= 0:
        raise ValueError("skill-load probe timeout must be positive")
    expected = _normalized_skill_set(request.expected_skills, field="expected_skills")
    nonce = request.nonce or os.urandom(32).hex()
    if not _HEX_64.fullmatch(nonce):
        raise ValueError("skill-load probe nonce must be 64 lowercase hexadecimal characters")
    expected_digest = _set_digest(expected)
    runtime_unavailable = hashlib.sha256(b"runtime-unavailable").hexdigest()
    unavailable_tool = hashlib.sha256(b"unresolved-executable").hexdigest()
    observed_at = _utc(now)
    env = _probe_environment(request.env)
    try:
        tool = _resolve_tool(request.hermes, env)
    except OSError:
        return _closed_observation(
            "probe_error", "inventory_executable_unavailable", nonce,
            expected_digest, unavailable_tool, runtime_unavailable, observed_at,
        )
    tool_fingerprint = tool.fingerprint
    if nonce_ledger is not None and nonce_ledger.contains(nonce):
        return _closed_observation(
            "probe_error", "inventory_nonce_replayed", nonce, expected_digest,
            tool_fingerprint, runtime_unavailable, observed_at,
        )

    outcome = _run_inventory_process(
        request, tool.executable, env, nonce, expected_digest, tool_fingerprint
    )
    if outcome.kind == "timeout":
        return _closed_observation(
            "probe_error", "inventory_timeout", nonce, expected_digest,
            tool_fingerprint, runtime_unavailable, observed_at,
        )
    if outcome.kind == "spawn_error":
        return _closed_observation(
            "probe_error", "inventory_process_error", nonce, expected_digest,
            tool_fingerprint, runtime_unavailable, observed_at,
        )
    if outcome.exit_code == 2:
        return _closed_observation(
            "unsupported", "inventory_protocol_unavailable", nonce, expected_digest,
            tool_fingerprint, runtime_unavailable, observed_at,
        )
    if outcome.exit_code != 0:
        return _closed_observation(
            "probe_error", "inventory_process_error", nonce, expected_digest,
            tool_fingerprint, runtime_unavailable, observed_at,
        )
    response, reason = _validated_inventory_response(
        outcome.stdout, nonce=nonce, expected_digest=expected_digest,
        tool_fingerprint=tool_fingerprint, now=observed_at,
    )
    if response is None:
        return _closed_observation(
            "probe_error", reason, nonce, expected_digest, tool_fingerprint,
            runtime_unavailable, observed_at,
        )
    if nonce_ledger is not None and not nonce_ledger.consume(nonce):
        return _closed_observation(
            "probe_error", "inventory_nonce_replayed", nonce, expected_digest,
            tool_fingerprint, runtime_unavailable, observed_at,
        )

    observed_value = response["observed_skills"]
    if not isinstance(observed_value, list):  # Kept local for static narrowing.
        raise ValueError("validated inventory lost its observed skill list")
    observed = tuple(cast(list[str], observed_value))
    missing = tuple(sorted(set(expected) - set(observed)))
    unexpected = tuple(sorted(set(observed) - set(expected)))
    if not expected:
        state = "not_applicable"
    elif set(expected).isdisjoint(observed):
        state = "none_loaded"
    elif missing:
        state = "partially_loaded"
    else:
        state = "all_loaded"
    payload: dict[str, object] = {
        "schema_version": SKILL_LOAD_OBSERVATION_SCHEMA_VERSION,
        "probe_status": "observed",
        "reason_code": "inventory_validated",
        "nonce": nonce,
        "expected_digest": expected_digest,
        "inventory_digest": response["inventory_digest"],
        "tool_fingerprint": tool_fingerprint,
        "runtime_fingerprint": response["runtime_fingerprint"],
        "observed_at": response["observed_at"],
        "expires_at": response["expires_at"],
        "load_state": state,
        "expected_skills": list(expected),
        "observed_skills": list(observed),
        "missing_skills": list(missing),
        "unexpected_skills": list(unexpected),
    }
    errors = validate_skill_load_observation(payload)
    if errors:
        return _closed_observation(
            "probe_error", "inventory_response_malformed", nonce, expected_digest,
            tool_fingerprint, runtime_unavailable, observed_at,
        )
    return payload


@dataclass(frozen=True, slots=True)
class _ResolvedTool:
    executable: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class _ProcessOutcome:
    kind: str
    exit_code: int | None
    stdout: bytes


def _run_inventory_process(
    request: SkillLoadProbeRequest,
    executable: str,
    env: Mapping[str, str],
    nonce: str,
    expected_digest: str,
    tool_fingerprint: str,
) -> _ProcessOutcome:
    argv = (
        executable, "--safe-mode", "--ignore-user-config", "--ignore-rules",
        "skills", "inventory", "--protocol",
        HERMES_SKILL_INVENTORY_SCHEMA_VERSION, "--nonce", nonce,
        "--expected-digest", expected_digest, "--tool-fingerprint", tool_fingerprint,
    )
    process: subprocess.Popen[bytes] | None = None
    drainers = None
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=False, close_fds=True, start_new_session=os.name != "nt",
        )
        drainers = start_pipe_drainers(process)
        try:
            process.wait(timeout=request.timeout_seconds)
            kind = "complete"
        except subprocess.TimeoutExpired:
            kind = "timeout"
            terminate_process_group(process, request.termination_grace_seconds, signal.SIGTERM)
        finally:
            terminate_process_group(process, request.termination_grace_seconds, signal.SIGTERM)
            for drainer in drainers:
                drainer.done.wait(max(0.1, request.termination_grace_seconds))
                drainer.thread.join(timeout=max(0.1, request.termination_grace_seconds))
        stdout_capture = drainers[0].capture()
        if stdout_capture.truncated:
            return _ProcessOutcome("complete", process.returncode, b"")
        return _ProcessOutcome(kind, process.returncode, stdout_capture.data)
    except OSError:
        return _ProcessOutcome("spawn_error", None, b"")


def _validated_inventory_response(
    raw: bytes,
    *,
    nonce: str,
    expected_digest: str,
    tool_fingerprint: str,
    now: datetime,
) -> tuple[dict[str, object] | None, str]:
    try:
        decoded = raw.decode("utf-8")
        decoded_candidate = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "inventory_response_malformed"
    if not isinstance(decoded_candidate, dict) or set(decoded_candidate) != _INVENTORY_FIELDS:
        return None, "inventory_response_malformed"
    candidate = cast(dict[str, object], decoded_candidate)
    if candidate.get("schema_version") != HERMES_SKILL_INVENTORY_SCHEMA_VERSION:
        return None, "inventory_response_malformed"
    if candidate.get("nonce") != nonce:
        return None, "inventory_nonce_mismatch"
    if candidate.get("expected_digest") != expected_digest:
        return None, "inventory_expected_digest_mismatch"
    if candidate.get("tool_fingerprint") != tool_fingerprint:
        return None, "inventory_response_malformed"
    runtime = candidate.get("runtime_fingerprint")
    if not isinstance(runtime, str) or not _HEX_64.fullmatch(runtime):
        return None, "inventory_response_malformed"
    observed_raw = candidate.get("observed_skills")
    if not isinstance(observed_raw, list):
        return None, "inventory_response_malformed"
    try:
        observed = _normalized_skill_set(observed_raw, field="observed_skills")
    except ValueError:
        return None, "inventory_response_malformed"
    if observed_raw != list(observed):
        return None, "inventory_response_malformed"
    inventory_digest = candidate.get("inventory_digest")
    if inventory_digest != _set_digest(observed):
        return None, "inventory_digest_mismatch"
    try:
        response_observed = _parse_time(candidate.get("observed_at"))
        expires = _parse_time(candidate.get("expires_at"))
    except ValueError:
        return None, "inventory_response_malformed"
    if response_observed > now + timedelta(seconds=5):
        return None, "inventory_response_malformed"
    if response_observed < now - timedelta(seconds=_MAX_PROTOCOL_SECONDS):
        return None, "inventory_response_stale"
    if expires <= now:
        return None, "inventory_response_expired"
    if expires <= response_observed or expires > response_observed + timedelta(seconds=_MAX_PROTOCOL_SECONDS):
        return None, "inventory_response_malformed"
    candidate["observed_skills"] = list(observed)
    return candidate, "inventory_validated"


def _closed_observation(
    status: str,
    reason: str,
    nonce: str,
    expected_digest: str,
    tool_fingerprint: str,
    runtime_fingerprint: str,
    observed_at: datetime,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": SKILL_LOAD_OBSERVATION_SCHEMA_VERSION,
        "probe_status": status,
        "reason_code": reason,
        "nonce": nonce,
        "expected_digest": expected_digest,
        "tool_fingerprint": tool_fingerprint,
        "runtime_fingerprint": runtime_fingerprint,
        "observed_at": _format_time(observed_at),
        "expires_at": _format_time(observed_at + timedelta(seconds=_MAX_PROTOCOL_SECONDS)),
    }
    errors = validate_skill_load_observation(payload)
    if errors:
        raise ValueError("invalid closed skill-load observation: " + "; ".join(errors))
    return payload


def validate_skill_load_observation(payload: Mapping[str, object] | object) -> list[str]:
    if not isinstance(payload, Mapping):
        return ["payload must be an object"]
    errors: list[str] = []
    extras = sorted(set(payload) - _OBSERVATION_FIELDS)
    if extras:
        errors.append(f"unsupported fields: {extras}")
    if payload.get("schema_version") != SKILL_LOAD_OBSERVATION_SCHEMA_VERSION:
        errors.append("schema_version is invalid")
    status = payload.get("probe_status")
    if status not in PROBE_STATUSES:
        errors.append("probe_status is invalid")
    if payload.get("reason_code") not in REASON_CODES:
        errors.append("reason_code is invalid")
    for field in ("nonce", "expected_digest", "tool_fingerprint", "runtime_fingerprint"):
        value = payload.get(field)
        if not isinstance(value, str) or not _HEX_64.fullmatch(value):
            errors.append(f"{field} is invalid")
    for field in ("observed_at", "expires_at"):
        try:
            _parse_time(payload.get(field))
        except ValueError:
            errors.append(f"{field} is invalid")
    if status == "observed":
        if payload.get("reason_code") != "inventory_validated":
            errors.append("observed probe requires inventory_validated")
        if payload.get("load_state") not in LOAD_STATES:
            errors.append("observed probe requires load_state")
        sets: dict[str, tuple[str, ...]] = {}
        for field in ("expected_skills", "observed_skills", "missing_skills", "unexpected_skills"):
            raw = payload.get(field)
            if not isinstance(raw, list):
                errors.append(f"{field} must be a sorted unique list")
                continue
            try:
                normalized = _normalized_skill_set(raw, field=field)
            except ValueError:
                errors.append(f"{field} must be a sorted unique list")
                continue
            if raw != list(normalized):
                errors.append(f"{field} must be a sorted unique list")
            sets[field] = normalized
        digest = payload.get("inventory_digest")
        if not isinstance(digest, str) or not _HEX_64.fullmatch(digest):
            errors.append("inventory_digest is invalid")
        if len(sets) == 4:
            expected = sets["expected_skills"]
            observed = sets["observed_skills"]
            missing = tuple(sorted(set(expected) - set(observed)))
            unexpected = tuple(sorted(set(observed) - set(expected)))
            if sets["missing_skills"] != missing or sets["unexpected_skills"] != unexpected:
                errors.append("inventory set differences are invalid")
            if payload.get("expected_digest") != _set_digest(expected):
                errors.append("expected_digest does not bind expected_skills")
            if digest != _set_digest(observed):
                errors.append("inventory_digest does not bind observed_skills")
            expected_state = (
                "not_applicable" if not expected else
                "none_loaded" if set(expected).isdisjoint(observed) else
                "partially_loaded" if missing else "all_loaded"
            )
            if payload.get("load_state") != expected_state:
                errors.append("load_state does not match inventory sets")
    else:
        forbidden = sorted(set(payload) & _INVENTORY_CLAIM_FIELDS)
        if forbidden:
            errors.append(f"non-observed probe carries inventory claims: {forbidden}")
        if status == "unsupported" and payload.get("reason_code") != "inventory_protocol_unavailable":
            errors.append("unsupported probe reason is invalid")
        if status == "probe_error" and payload.get("reason_code") in {
            "inventory_validated", "inventory_protocol_unavailable"
        }:
            errors.append("probe_error reason is invalid")
    return errors


def skill_load_observation_is_fresh(
    payload: Mapping[str, object], *, now: datetime | None = None
) -> bool:
    if validate_skill_load_observation(payload):
        return False
    try:
        observed = _parse_time(payload.get("observed_at"))
        expires = _parse_time(payload.get("expires_at"))
    except ValueError:
        return False
    current = _utc(now)
    return observed <= current < expires


def _normalized_skill_set(values: Sequence[object], *, field: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{field} must be a sequence")
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not _SKILL_NAME.fullmatch(value):
            raise ValueError(f"{field} contains an invalid skill name")
        normalized.append(value)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field} contains duplicate skill names")
    return tuple(sorted(normalized))


def _set_digest(values: Sequence[str]) -> str:
    encoded = json.dumps(list(values), separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _probe_environment(source: Mapping[str, str] | None) -> dict[str, str]:
    values = os.environ if source is None else source
    env = {name: values[name] for name in _SAFE_ENV_NAMES if values.get(name)}
    env.setdefault("PATH", os.defpath)
    env.update(
        {
            "HERMES_SAFE_MODE": "1",
            "HERMES_IGNORE_USER_CONFIG": "1",
            "HERMES_IGNORE_RULES": "1",
            "OMH_ISOLATED_HERMES_ROUTING": "disabled",
            "OMH_ISOLATED_HERMES_MAX_DEPTH": "1",
        }
    )
    return env


def _resolve_tool(executable: str, env: Mapping[str, str]) -> _ResolvedTool:
    has_separator = os.sep in executable or bool(os.altsep and os.altsep in executable)
    if has_separator or Path(executable).is_absolute():
        selected = Path(executable).expanduser()
    else:
        found = shutil.which(executable, path=env.get("PATH"))
        if found is None:
            raise FileNotFoundError(executable)
        selected = Path(found)
    resolved = selected.resolve(strict=True)
    before = resolved.stat()
    if not stat.S_ISREG(before.st_mode) or not os.access(resolved, os.R_OK | os.X_OK):
        raise PermissionError(executable)

    digest = hashlib.sha256()
    digest.update(b"omh-skill-load-tool/v1\0")
    digest.update(os.fsencode(resolved))
    digest.update(
        f"\0{before.st_dev}\0{before.st_ino}\0{before.st_mode}\0{before.st_size}".encode("ascii")
    )
    with resolved.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    identity_before = (before.st_dev, before.st_ino, before.st_mode, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns)
    if identity_after != identity_before:
        raise OSError("executable changed during fingerprinting")
    return _ResolvedTool(str(resolved), digest.hexdigest())


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("timestamp must be UTC")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.tzinfo is None:
        raise ValueError("timestamp must have timezone")
    return parsed.astimezone(timezone.utc)


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return current.astimezone(timezone.utc)


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
