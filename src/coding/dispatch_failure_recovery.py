"""Classify a failed dispatch, say how to repair it, and offer the operator a choice.

A spawned agent CLI that dies because the provider quota is spent or the stored
credential is invalid used to reach the operator as `status: failed` plus a
bounded output tail. Both are recoverable and neither is recoverable the same
way: a limit clears on its own and a credential does not, so the two need
different words, different repair steps, and — because switching a coding owner
behind the operator's back is exactly what `AGENTS.md` forbids — an explicit
choice rather than an automatic fallback.

Three pieces live here.

1. **`failure_kind`**, a closed enum every failed unit envelope carries. The two
   synthetic exit codes the dispatcher assigns itself (127 for a binary missing
   from PATH, 124 for a unit that burned its timeout) classify first, because
   they are the dispatcher's own observation of the process rather than text the
   provider wrote. Then `auth_shaped`, then `limit_shaped`, then `crash` as the
   fallback.

   Auth is checked before limit deliberately, and the two pattern sets are not
   disjoint in practice: a 401 body routinely also mentions a rate limit for
   unauthenticated callers. An invalid credential is a state the operator must
   repair before ANY attempt can succeed; a limit is a state that clears on its
   own. Classifying an overlapping failure as `limit_shaped` would file it into
   the wait-it-out lane, where waiting can never clear it. Classifying it as
   `auth_shaped` costs the operator one re-auth check that the limit lane would
   have skipped, which is the cheaper of the two mistakes.

2. **Observed auth-failure signals**, persisted per owner beside the limit
   signals and cleared by the same rule (the next exit-0 for that owner). These
   are deliberately NOT the presence markers in `executor_auth_signals`: a marker
   is absent for every legitimate API-key install and never vetoes anything. A
   signal here is an observed runtime failure of a real spawn, which is why it
   is allowed to become a spawn cooldown while a marker never is.

3. **The recovery choice.** Three named actions — retarget to another owner,
   re-run through the Hermes subagent lane, or wait and retry later — and the
   `--on-failure` degradation for every non-interactive caller. Nothing here
   picks one on the operator's behalf; the default mode prints the card and the
   options and changes nothing.

Nothing in this module spawns anything. It classifies, it persists metadata, and
it renders choices. Acting on a choice is the dispatcher's job.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

from ..executors import executor_label
from ..system.local_store import locked_json_update, read_json_object_result, utc_now
from ..system.paths import OmhPaths
from .executor_auth_signals import LIMIT_SIGNAL_STALE_AFTER_SECONDS, signal_age_seconds

# The closed enum. Every failed unit envelope carries exactly one of these.
FAILURE_KIND_AUTH_SHAPED = "auth_shaped"
FAILURE_KIND_LIMIT_SHAPED = "limit_shaped"
FAILURE_KIND_TIMEOUT = "timeout"
FAILURE_KIND_BINARY_MISSING = "binary_missing"
FAILURE_KIND_CRASH = "crash"

FAILURE_KINDS: tuple[str, ...] = (
    FAILURE_KIND_AUTH_SHAPED,
    FAILURE_KIND_LIMIT_SHAPED,
    FAILURE_KIND_TIMEOUT,
    FAILURE_KIND_BINARY_MISSING,
    FAILURE_KIND_CRASH,
)

# The two kinds a recovery choice is offered for: both describe the provider
# refusing to serve this owner right now, which another owner or a later attempt
# can answer. A crash, a timeout, or a missing binary is not that.
RECOVERABLE_FAILURE_KINDS: frozenset[str] = frozenset(
    {FAILURE_KIND_AUTH_SHAPED, FAILURE_KIND_LIMIT_SHAPED}
)

FAILURE_KIND_PRECEDENCE = (
    "binary_missing (synthetic exit 127) and timeout (synthetic exit 124) are the dispatcher's own "
    "observations of the process and classify before any text match. Text then classifies as "
    "auth_shaped before limit_shaped: an invalid credential must be repaired before any attempt can "
    "succeed, while a limit clears on its own, so an overlapping message belongs in the lane that "
    "waiting cannot clear. Everything else is crash."
)

# Deterministic auth-shape patterns, matched case-insensitively over the
# in-memory stdout/stderr tails of a FAILED spawn only. Multi-word and anchored
# to credential context for the same reason `_LIMIT_SHAPED_PATTERNS` is: a bare
# "401" matches a version string or a count, and a bare "token" or "auth"
# matches half of every agent CLI's ordinary narration. Only the boolean and the
# matched label are ever persisted — never the matched text.
_AUTH_SHAPED_PATTERNS: tuple[tuple[str, str], ...] = (
    ("invalid_api_key", "invalid api key"),
    ("invalid_api_key", "api key not found"),
    ("invalid_api_key", "incorrect api key"),
    ("token_expired", "token expired"),
    ("token_expired", "token has expired"),
    ("not_logged_in", "not logged in"),
    ("not_logged_in", "please run /login"),
    ("not_logged_in", "please log in"),
    ("authentication_failed", "authentication failed"),
    ("authentication_failed", "credentials are invalid"),
    ("authentication_failed", "invalid credentials"),
    ("http_401", "status 401"),
    ("http_401", "error 401"),
    ("http_401", "http 401"),
    ("http_401", "401 unauthorized"),
    ("oauth_revoked", "oauth token revoked"),
    ("oauth_revoked", "refresh token is invalid"),
)


def auth_shaped_label(output_tail: str, stderr_tail: str) -> str:
    """The auth shape a failed spawn's tails match, or an empty string."""
    haystack = f"{output_tail}\n{stderr_tail}".casefold()
    for label, pattern in _AUTH_SHAPED_PATTERNS:
        if pattern in haystack:
            return label
    return ""


def classify_failure_kind(*, exit_code: int, limit_label: str = "", auth_label: str = "") -> str:
    """The closed-enum kind of one observed unit failure. See FAILURE_KIND_PRECEDENCE."""
    if exit_code == 0:
        return ""
    if exit_code == 127:
        return FAILURE_KIND_BINARY_MISSING
    if exit_code == 124:
        return FAILURE_KIND_TIMEOUT
    if auth_label:
        return FAILURE_KIND_AUTH_SHAPED
    if limit_label:
        return FAILURE_KIND_LIMIT_SHAPED
    return FAILURE_KIND_CRASH


EXECUTOR_AUTH_FAILURE_SIGNALS_SCHEMA_VERSION = "executor_auth_failure_signals/v1"
EXECUTOR_AUTH_FAILURE_SIGNALS_CLAIM_BOUNDARY = (
    "An auth-failure signal records that one observed local dispatch failure matched a "
    "credential-rejection shape. It is not provider login truth, not an entitlement statement, and "
    "no secret value is ever read into omh state."
)

# Deliberately the same window the limit signals use, imported rather than
# restated so the two cadences cannot drift. It is NOT a claim that a rejected
# credential heals in six hours — it is how long the veto below holds before the
# next dispatch is allowed to re-observe the answer for itself. A signal that
# never went stale would be a permanent block, because the exit-0 that clears it
# can only come from a spawn the veto is refusing.
AUTH_FAILURE_SIGNAL_STALE_AFTER_SECONDS = LIMIT_SIGNAL_STALE_AFTER_SECONDS


def record_auth_failure_signal(
    paths: OmhPaths,
    owner: str,
    *,
    run_ref: str,
    unit_id: str,
    pattern_label: str,
) -> None:
    """Persist the last observed auth-shaped dispatch failure for one owner."""

    def _update(state: dict[str, Any]) -> dict[str, Any]:
        state["schema_version"] = EXECUTOR_AUTH_FAILURE_SIGNALS_SCHEMA_VERSION
        profiles = state.setdefault("profiles", {})
        profiles[owner] = {
            "last_auth_shaped_at": utc_now(),
            "run_ref": run_ref,
            "unit_id": unit_id,
            "pattern_label": pattern_label,
        }
        state["claim_boundary"] = EXECUTOR_AUTH_FAILURE_SIGNALS_CLAIM_BOUNDARY
        return state

    try:
        locked_json_update(paths.executor_auth_failure_signals_path, _update, private=True)
    except (OSError, TimeoutError):
        # Same rule the limit signal holds: an advisory that cannot be written
        # must never abort the dispatch it describes.
        pass


def clear_auth_failure_signal(paths: OmhPaths, owner: str) -> None:
    """Drop one owner's auth-failure signal; a later exit-0 is fresher evidence."""
    if not paths.executor_auth_failure_signals_path.exists():
        return

    def _update(state: dict[str, Any]) -> dict[str, Any]:
        profiles = state.get("profiles")
        if isinstance(profiles, dict):
            profiles.pop(owner, None)
        return state

    try:
        locked_json_update(paths.executor_auth_failure_signals_path, _update, private=True)
    except (OSError, TimeoutError):
        pass


def last_auth_failure_signal(paths: OmhPaths, owner: str) -> dict[str, Any]:
    """The last observed auth-shaped failure for one owner, with read-time age.

    `age_seconds` and `stale` are computed on read for the same reason the limit
    signal computes them there: a stored observation must never be mistaken for
    current provider state.
    """
    state, error = read_json_object_result(paths.executor_auth_failure_signals_path)
    if error or not isinstance(state, dict):
        return {}
    profiles = state.get("profiles")
    if not isinstance(profiles, dict):
        return {}
    entry = profiles.get(str(owner or "").strip().casefold())
    if not isinstance(entry, dict):
        return {}
    payload = {str(key): value for key, value in entry.items()}
    age = signal_age_seconds(str(payload.get("last_auth_shaped_at", "") or ""))
    if age is not None:
        payload["age_seconds"] = age
        payload["stale"] = age > AUTH_FAILURE_SIGNAL_STALE_AFTER_SECONDS
    return payload


COOLDOWN_STATUS_LIMIT = "executor_limit_cooldown"
COOLDOWN_STATUS_AUTH = "executor_auth_invalid"

COOLDOWN_CLAIM_BOUNDARY = (
    "A spawn cooldown refuses one spawn because THIS machine observed that owner's own CLI fail "
    "that way inside the staleness window. It is an observed-failure veto, never a marker-absence "
    "veto, and it is not provider truth: `--ignore-limit-signal` re-observes the answer directly."
)

# The one place an observed signal becomes a veto rather than a ranking hint.
# This does not contradict the advisory-marker principle in
# `executor_auth_signals`: that principle forbids pre-blocking on the ABSENCE of
# a local login file, which every API-key install legitimately shows. This veto
# fires only on a runtime failure omh itself observed from a real spawn of that
# owner, inside the staleness window, and the operator overrides it with
# `--ignore-limit-signal`.
_AUTH_REPAIR_COMMANDS: dict[str, str] = {
    "claude-code": "claude   # then run /login inside the session",
    "codex": "codex login",
}


def auth_repair_command(owner: str) -> str:
    """The command that re-authenticates one owner's CLI, or a neutral instruction.

    Executor-specific values inside an executor-neutral surface: an owner whose
    login command this repo has not verified gets the neutral sentence rather
    than a guessed command, so no CLI reads as the assumed default.
    """
    return _AUTH_REPAIR_COMMANDS.get(owner, f"re-authenticate the {owner} CLI, then re-run this dispatch")


DISPATCH_REPAIR_CARD_SCHEMA_VERSION = "dispatch_failure_repair_card/v1"
_MAX_CARD_TEXT = 300


def build_repair_card(
    *,
    owner: str,
    failure_kind: str,
    detail: str = "",
    remaining_seconds: int | None = None,
) -> dict[str, Any]:
    """The deterministic repair card for one recoverable dispatch failure.

    Two kinds, two repair steps, one shape: a reader branches on `failure_kind`
    and `reason_code`, never on the prose.
    """
    card: dict[str, Any] = {
        "schema_version": DISPATCH_REPAIR_CARD_SCHEMA_VERSION,
        "status": "prepared_not_observed",
        "owner": owner,
        "label": executor_label(owner) if owner else "",
        "failure_kind": failure_kind,
        "reason_code": failure_kind,
        "detail": str(detail)[:_MAX_CARD_TEXT],
        "claim_boundary": COOLDOWN_CLAIM_BOUNDARY,
    }
    if failure_kind == FAILURE_KIND_AUTH_SHAPED:
        card["repair_steps"] = [
            {
                "id": "reauthenticate_owner",
                "action": f"Re-authenticate the {owner} CLI on this machine; omh never reads or stores the credential.",
                "command": auth_repair_command(owner),
            },
            {
                "id": "redispatch_after_repair",
                "action": (
                    "Re-run the dispatch for this unit once the CLI answers as authenticated. "
                    "The observed signal is re-checked, not trusted, on the next attempt."
                ),
                "command": "omh coding fanout dispatch <fanout-id> --unit <unit-id> --goal-file <file>",
            },
        ]
    else:
        wait_note = (
            "Wait for the provider window to reset."
            if remaining_seconds is None
            else f"Wait for the provider window to reset (cooldown re-checks in about {remaining_seconds}s)."
        )
        card["repair_steps"] = [
            {
                "id": "wait_for_provider_window",
                "action": wait_note,
                "command": "omh coding executor-readiness --json",
            },
            {
                "id": "redispatch_or_retarget",
                "action": (
                    "Re-run the dispatch for this unit, or choose another coding owner explicitly. "
                    "omh never switches owners on its own."
                ),
                "command": "omh coding fanout dispatch <fanout-id> --unit <unit-id> --goal-file <file>",
            },
        ]
    if remaining_seconds is not None:
        card["cooldown_remaining_seconds"] = int(remaining_seconds)
    return card


def spawn_cooldown(paths: OmhPaths, owner: str) -> dict[str, Any] | None:
    """The veto for one owner with a fresh observed auth or limit signal, or None.

    Auth is checked first for the same reason `classify_failure_kind` checks it
    first: waiting out a credential rejection never clears it.

    `stale is False` rather than a falsy check: `stale` exists only when the
    record's observation time could be read, so a record whose age is unknown
    cannot be SHOWN to be inside the window and never vetoes a spawn. It still
    ranks the owner down in the choose-executor context, which is the weaker
    claim an unaged observation supports.
    """
    auth = last_auth_failure_signal(paths, owner)
    if auth.get("stale") is False:
        return _cooldown(
            owner=owner,
            status=COOLDOWN_STATUS_AUTH,
            failure_kind=FAILURE_KIND_AUTH_SHAPED,
            signal=auth,
            stale_after=AUTH_FAILURE_SIGNAL_STALE_AFTER_SECONDS,
            observed_key="last_auth_shaped_at",
        )
    from .executor_auth_signals import last_limit_signal_for_profile

    limit = last_limit_signal_for_profile(paths, owner)
    if limit.get("stale") is False:
        return _cooldown(
            owner=owner,
            status=COOLDOWN_STATUS_LIMIT,
            failure_kind=FAILURE_KIND_LIMIT_SHAPED,
            signal=limit,
            stale_after=LIMIT_SIGNAL_STALE_AFTER_SECONDS,
            observed_key="last_limit_shaped_at",
        )
    return None


def _cooldown(
    *,
    owner: str,
    status: str,
    failure_kind: str,
    signal: Mapping[str, Any],
    stale_after: int,
    observed_key: str,
) -> dict[str, Any]:
    age = signal.get("age_seconds")
    remaining = None
    if isinstance(age, int) and not isinstance(age, bool):
        remaining = max(0, int(stale_after) - age)
    detail = (
        f"this machine observed {owner} fail as {failure_kind} "
        f"({signal.get('pattern_label') or 'unlabelled'}) at {signal.get(observed_key) or 'an unrecorded time'}"
    )
    return {
        "status": status,
        "failure_kind": failure_kind,
        "pattern_label": str(signal.get("pattern_label", "")),
        "observed_at": str(signal.get(observed_key, "")),
        "cooldown_remaining_seconds": remaining,
        "reason": (
            f"{detail}; the spawn is refused inside the staleness window. "
            "Pass --ignore-limit-signal to spawn anyway and re-observe the answer directly."
        ),
        "repair_card": build_repair_card(
            owner=owner,
            failure_kind=failure_kind,
            detail=detail,
            remaining_seconds=remaining,
        ),
        "claim_boundary": COOLDOWN_CLAIM_BOUNDARY,
    }


ON_FAILURE_REPORT = "report"
ON_FAILURE_HERMES = "hermes"
ON_FAILURE_WAIT = "wait"
ON_FAILURE_RETARGET_PREFIX = "retarget:"

ON_FAILURE_MODES: tuple[str, ...] = (
    ON_FAILURE_REPORT,
    f"{ON_FAILURE_RETARGET_PREFIX}<owner>",
    ON_FAILURE_HERMES,
    ON_FAILURE_WAIT,
)

CHOICE_RETARGET = "retarget"
CHOICE_HERMES = "hermes"
CHOICE_WAIT = "wait"
CHOICE_REPORT = "report"

FAILURE_RECOVERY_SCHEMA_VERSION = "fanout_failure_recovery/v1"
FAILURE_RECOVERY_CLAIM_BOUNDARY = (
    "A failure-recovery record states which recovery action the operator explicitly chose for a "
    "failed unit and what that action observed. Choosing one is not a claim that it succeeded, and "
    "omh never switches a coding owner without an explicit choice recorded here."
)


class OnFailureModeError(ValueError):
    """`--on-failure` was given a value outside the closed mode set."""


def parse_on_failure(value: str, *, known_owners: Sequence[str] = ()) -> tuple[str, str]:
    """Split `--on-failure` into (mode, target_owner). Empty reads as `report`.

    `retarget:<owner>` is the only form carrying a value, and the owner is
    validated against `known_owners` when the caller supplies a roster, so a
    typo fails at parse time rather than as a mid-run refusal.
    """
    raw = str(value or "").strip()
    if not raw:
        return ON_FAILURE_REPORT, ""
    if raw in {ON_FAILURE_REPORT, ON_FAILURE_HERMES, ON_FAILURE_WAIT}:
        return raw, ""
    if raw.startswith(ON_FAILURE_RETARGET_PREFIX):
        owner = raw[len(ON_FAILURE_RETARGET_PREFIX) :].strip()
        if not owner:
            raise OnFailureModeError("--on-failure=retarget: requires an owner, for example retarget:codex")
        if known_owners and owner not in known_owners:
            raise OnFailureModeError(
                f"unknown retarget owner {owner!r}; choose one of: {', '.join(known_owners)}"
            )
        return CHOICE_RETARGET, owner
    raise OnFailureModeError(
        f"unsupported --on-failure value {raw!r}; choose one of: {', '.join(ON_FAILURE_MODES)}"
    )


def recovery_candidates(units: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The failed units a recovery choice is offered for, in summary order."""
    return [
        {
            "unit_id": str(entry.get("unit_id", "")),
            "owner": str(entry.get("owner", "")),
            "failure_kind": str(entry.get("failure_kind", "")),
            "status": str(entry.get("status", "")),
        }
        for entry in units
        if isinstance(entry, Mapping)
        and str(entry.get("failure_kind", "")) in RECOVERABLE_FAILURE_KINDS
        and not entry.get("process_succeeded")
    ]


def retarget_candidates(
    choice_context: Mapping[str, Any] | None,
    *,
    exclude_owner: str,
) -> list[dict[str, Any]]:
    """Ranked retarget owners from the choose-executor context, minus the failed one.

    The context ranks; it never vetoes, so every candidate it lists other than
    the owner that just failed stays offerable and carries its own readiness and
    signal state for the operator to read.
    """
    candidates = (choice_context or {}).get("candidates")
    if not isinstance(candidates, Sequence):
        return []
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        profile = str(candidate.get("profile", ""))
        if not profile or profile == exclude_owner:
            continue
        rows.append(
            {
                "profile": profile,
                "label": str(candidate.get("label", "")),
                "readiness_status": str(candidate.get("readiness_status", "")),
                "auth_marker": str(
                    (candidate.get("auth_signal") or {}).get("login_marker", "")
                    if isinstance(candidate.get("auth_signal"), Mapping)
                    else ""
                ),
            }
        )
    return rows


def recovery_options(
    *,
    candidate: Mapping[str, Any],
    retargets: Sequence[Mapping[str, Any]],
    hermes_available: bool,
) -> list[dict[str, Any]]:
    """The numbered choices offered for one failed unit, in a fixed order.

    An option that this environment cannot carry out is still listed, with
    `available: false` and the reason, rather than silently dropped: an operator
    who was told there were three ways out should be told which one is closed
    and why.
    """
    options: list[dict[str, Any]] = [
        {
            "key": "1",
            "choice": CHOICE_RETARGET,
            "title": "Retarget this unit to another coding owner and re-dispatch it now.",
            "available": bool(retargets),
            "unavailable_reason": "" if retargets else "no other locally-installed coding owner is offered",
            "candidates": [dict(row) for row in retargets],
        },
        {
            "key": "2",
            "choice": CHOICE_HERMES,
            "title": "Re-run this unit through the Hermes subagent lane (separate auth and quota).",
            "available": bool(hermes_available),
            "unavailable_reason": (
                ""
                if hermes_available
                else "supply --hermes-model, --hermes-provider, and --hermes-reasoning to offer this lane"
            ),
            "consent_note": (
                "Choosing this is the explicit dispatch consent the Hermes child boundary requires; "
                "it is recorded as such."
            ),
        },
        {
            "key": "3",
            "choice": CHOICE_WAIT,
            "title": "Wait: mark the unit for retry later and stop this run.",
            "available": True,
            "unavailable_reason": "",
        },
    ]
    return options


_PROMPT_ATTEMPTS = 3


def prompt_recovery_choice(
    *,
    candidate: Mapping[str, Any],
    options: Sequence[Mapping[str, Any]],
    read_line: Callable[[str], str],
    write_line: Callable[[str], None],
) -> dict[str, Any]:
    """Ask the operator which recovery action to take for one failed unit.

    The reading seam is injected on purpose: the interview is exercised without
    a tty anywhere in the test suite. An empty answer, an unavailable option, or
    a value outside the set re-asks; exhausting the attempts (or an input stream
    that ended) falls back to `report`, which changes nothing.
    """
    unit_id = str(candidate.get("unit_id", ""))
    owner = str(candidate.get("owner", ""))
    kind = str(candidate.get("failure_kind", ""))
    write_line(f"Unit {unit_id} failed on {owner} as {kind}. Choose a recovery action:")
    for option in options:
        suffix = "" if option.get("available") else f"  (unavailable: {option.get('unavailable_reason')})"
        write_line(f"  [{option.get('key')}] {option.get('title')}{suffix}")
        for row in option.get("candidates", []) or []:
            if isinstance(row, Mapping):
                write_line(
                    f"        - {row.get('profile')} (readiness {row.get('readiness_status')}, "
                    f"login marker {row.get('auth_marker')})"
                )
    by_key = {str(option.get("key")): option for option in options}
    for _attempt in range(_PROMPT_ATTEMPTS):
        try:
            answer = read_line(f"Recovery choice for {unit_id} [1/2/3]: ").strip()
        except (EOFError, OSError):
            break
        option = by_key.get(answer)
        if option is None:
            write_line("Answer 1, 2, or 3.")
            continue
        if not option.get("available"):
            write_line(f"Option {answer} is unavailable: {option.get('unavailable_reason')}")
            continue
        if option.get("choice") != CHOICE_RETARGET:
            return {"choice": str(option["choice"]), "target_owner": ""}
        target = _prompt_retarget_owner(
            unit_id=unit_id,
            rows=[row for row in option.get("candidates", []) or [] if isinstance(row, Mapping)],
            read_line=read_line,
            write_line=write_line,
        )
        if target:
            return {"choice": CHOICE_RETARGET, "target_owner": target}
    write_line(f"No recovery action chosen for {unit_id}; reporting it unchanged.")
    return {"choice": CHOICE_REPORT, "target_owner": ""}


def _prompt_retarget_owner(
    *,
    unit_id: str,
    rows: Sequence[Mapping[str, Any]],
    read_line: Callable[[str], str],
    write_line: Callable[[str], None],
) -> str:
    if len(rows) == 1:
        return str(rows[0].get("profile", ""))
    names = [str(row.get("profile", "")) for row in rows if row.get("profile")]
    for _attempt in range(_PROMPT_ATTEMPTS):
        write_line(f"Retarget owners for {unit_id}: {', '.join(names)}")
        try:
            answer = read_line(f"Owner for {unit_id}: ").strip()
        except (EOFError, OSError):
            return ""
        if answer in names:
            return answer
        write_line(f"Choose one of: {', '.join(names)}")
    return ""


HERMES_LANE_CONSENT = (
    "The operator selected the Hermes subagent lane for this unit in the dispatch recovery "
    "interview. That selection is the explicit dispatch confirmation the Hermes child boundary "
    "requires, equivalent to --confirm-dispatch, and it is recorded here as such."
)
HERMES_ROUTING_KEYS: tuple[str, ...] = ("model", "provider", "reasoning")


def hermes_routing_available(routing: Mapping[str, Any] | None) -> bool:
    """Whether the caller supplied the three routing aliases the child lane needs."""
    if not isinstance(routing, Mapping):
        return False
    return all(str(routing.get(key, "") or "").strip() for key in HERMES_ROUTING_KEYS)


def dispatch_unit_via_hermes_child(
    *,
    prompt: str,
    routing: Mapping[str, Any],
    parent_run_id: str,
    run_id: str,
    cwd: Any,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Run one recovery unit through the sanctioned Hermes child boundary.

    `confirmed=True` is not a bypass: the operator's interview selection (or an
    explicit `--on-failure=hermes`) is the same explicit act `--confirm-dispatch`
    carries on the command line, and `HERMES_LANE_CONSENT` is what the journal
    records it as. Every other part of the boundary — the depth-one limit, the
    stdin-only prompt, the isolated child home, the sealed observation — is the
    child module's own and is untouched here.
    """
    from .hermes_child_dispatch import (
        HermesChildDispatchError,
        HermesChildRequest,
        dispatch_hermes_child,
    )

    try:
        result = dispatch_hermes_child(
            HermesChildRequest(
                prompt=prompt,
                model=str(routing.get("model", "")),
                provider=str(routing.get("provider", "")),
                reasoning=str(routing.get("reasoning", "")),
                parent_run_id=parent_run_id,
                run_id=run_id,
                timeout_seconds=float(timeout_seconds),
                cwd=cwd,
            ),
            dispatch_policy="ask_before_dispatch",
            confirmed=True,
        )
    except (HermesChildDispatchError, ValueError, OSError) as exc:
        return {"status": "hermes_child_refused", "reason": str(exc)[:_MAX_CARD_TEXT]}
    return {
        "status": str(result.status),
        "exit_code": result.exit_code,
        "run_id": str(result.run_id),
        "model": str(result.model),
        "usage": {
            key: value
            for key, value in (result.usage or {}).items()
            if value is None or isinstance(value, (str, int, float, bool))
        },
    }


def recovery_decision(
    *,
    candidate: Mapping[str, Any],
    choice: str,
    target_owner: str = "",
    consent: str = "",
    reason: str = "",
) -> dict[str, Any]:
    """One recorded recovery decision, before any action is carried out."""
    decision: dict[str, Any] = {
        "unit_id": str(candidate.get("unit_id", "")),
        "owner": str(candidate.get("owner", "")),
        "failure_kind": str(candidate.get("failure_kind", "")),
        "choice": choice,
        "decided_at": utc_now(),
    }
    if target_owner:
        decision["target_owner"] = target_owner
    if consent:
        decision["consent"] = consent
    if reason:
        decision["reason"] = reason
    return decision
