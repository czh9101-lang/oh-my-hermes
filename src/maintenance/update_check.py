"""Startup update-availability check: compare this install against `origin/main`.

`omh` / `hermes` launch through `_launch_hermes_tui()` in `commands/main.py`,
which execs the user's own `hermes` binary. This module is the opt-in,
network-making half of that launch path (AGENTS.md Implementation
Boundaries): core OMH makes no network calls by default, so every function
here is inert -- zero network attempts -- until a user explicitly sets
`update_check.mode` away from its shipped default of ``off`` via
`omh update-check set`. That keeps this in the same explicit family as
`omh update` itself (which already makes network calls on explicit
invocation) rather than adding a second, always-on network surface.

The probe is a single bounded `curl` request against the GitHub commits API
for `main`, spawned as a subprocess -- never a Python-level network client
(`tests/test_handoff_safety_contract_enforcement.py` INVARIANT 2 statically
forbids importing `urllib.request`'s client surface, `socket`, or any other
network-client module anywhere in `src/`; this module is instead listed in
that file's INVARIANT 1 `PROCESS_SPAWN_ALLOWLIST`, the same door
`commands/setup.py` already uses for pip/npm/brew self-update). `curl`
absent, unreachable, or slow beyond the bounded timeout is a silent skip,
exactly like a network failure.

Two persisted documents, following the two existing `~/.omh` config
conventions:

- Policy (mode, interval_hours) lives under the ``update_check`` key in
  ``setup-profile.json``, the same read-modify-write document
  `capabilities/toggles.py` uses for capability policy.
- The probe result cache lives at ``<omh-home>/runtime/update-check.json``
  (``last_checked_at``, the remote identity, and the outcome), so the
  configured interval is respected across separate `omh`/`hermes` launches
  without a fresh network call every time.

A probe failure or timeout is a silent skip, never a blocked or delayed
launch, and never a false "up to date" or "behind" claim -- an install that
predates a comparable recorded identity reports as ``inconclusive`` instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
from typing import Any

from ..local_store import atomic_write_json, ensure_dir, file_lock, read_json_object_result
from ..paths import OmhPaths

UPDATE_CHECK_POLICY_SCHEMA_VERSION = "omh_update_check_policy/v1"
UPDATE_CHECK_CACHE_SCHEMA_VERSION = "omh_update_check_cache/v1"
UPDATE_CHECK_RESULT_SCHEMA_VERSION = "omh_update_check_result/v1"

UPDATE_CHECK_MODES = ("off", "notify", "auto")
DEFAULT_UPDATE_CHECK_MODE = "off"
DEFAULT_UPDATE_CHECK_INTERVAL_HOURS = 24.0

# `_interval_elapsed` feeds this straight into `timedelta(hours=...)`, which
# raises `OverflowError` on `inf`/a very large float -- on the launch path,
# that turns a bad `--interval-hours` value into a crash on every launch
# instead of a rejected `set`. Bounded to something a human could plausibly
# mean: at least hourly, at most roughly a year.
MIN_UPDATE_CHECK_INTERVAL_HOURS = 1.0
MAX_UPDATE_CHECK_INTERVAL_HOURS = 8760.0

# Bounded probe budget (owner design requirement): the launcher execs `hermes`
# right after this returns and hands it the terminal, so there is no
# "after the TUI paints" moment left in this process to report a late result
# in -- the whole check has to fit before that handoff, and stay small enough
# it is never a noticeable delay. Passed to `curl --max-time` and doubled as
# the hard `subprocess.run` timeout backstop below.
UPDATE_CHECK_NETWORK_TIMEOUT_SECONDS = 1.5

GITHUB_COMMITS_API_URL = "https://api.github.com/repos/rlaope/oh-my-hermes/commits/main"


def update_check_cache_path(paths: OmhPaths) -> Path:
    return paths.runtime_dir / "update-check.json"


def read_update_check_policy(paths: OmhPaths) -> dict[str, Any]:
    """Read `update_check.{mode,interval_hours}` from setup-profile.json.

    Absent or malformed data means the shipped default: off, 24h interval.
    A corrupt setup-profile.json (bad JSON, or JSON that is not an object)
    reads the same way as an absent one -- never raises -- because this
    function sits on the `omh`/`hermes` launch path and a decode error here
    must never crash a launch that has not even opted into this check.
    """
    profile, _error = read_json_object_result(paths.setup_profile_path)
    profile = profile or {}
    raw = profile.get("update_check")
    mode = DEFAULT_UPDATE_CHECK_MODE
    interval_hours = DEFAULT_UPDATE_CHECK_INTERVAL_HOURS
    if isinstance(raw, dict):
        candidate_mode = str(raw.get("mode", "")).strip()
        if candidate_mode in UPDATE_CHECK_MODES:
            mode = candidate_mode
        candidate_interval = raw.get("interval_hours")
        if (
            isinstance(candidate_interval, (int, float))
            and not isinstance(candidate_interval, bool)
            and MIN_UPDATE_CHECK_INTERVAL_HOURS <= candidate_interval <= MAX_UPDATE_CHECK_INTERVAL_HOURS
        ):
            interval_hours = float(candidate_interval)
    return {
        "schema_version": UPDATE_CHECK_POLICY_SCHEMA_VERSION,
        "mode": mode,
        "interval_hours": interval_hours,
    }


def write_update_check_policy(
    paths: OmhPaths,
    *,
    mode: str | None = None,
    interval_hours: float | None = None,
) -> dict[str, Any]:
    """Read-modify-write the policy, preserving every other profile field."""
    current = read_update_check_policy(paths)
    resolved_mode = current["mode"]
    if mode is not None:
        if mode not in UPDATE_CHECK_MODES:
            raise ValueError(f"unsupported update-check mode: {mode!r}; expected one of {', '.join(UPDATE_CHECK_MODES)}")
        resolved_mode = mode
    resolved_interval = current["interval_hours"]
    if interval_hours is not None:
        if isinstance(interval_hours, bool) or not (
            isinstance(interval_hours, (int, float))
            and MIN_UPDATE_CHECK_INTERVAL_HOURS <= interval_hours <= MAX_UPDATE_CHECK_INTERVAL_HOURS
        ):
            raise ValueError(
                "update-check interval-hours must be a number between "
                f"{MIN_UPDATE_CHECK_INTERVAL_HOURS} and {MAX_UPDATE_CHECK_INTERVAL_HOURS}"
            )
        resolved_interval = float(interval_hours)
    policy = {
        "schema_version": UPDATE_CHECK_POLICY_SCHEMA_VERSION,
        "mode": resolved_mode,
        "interval_hours": resolved_interval,
    }
    profile, _error = read_json_object_result(paths.setup_profile_path)
    profile = profile or {}
    profile["update_check"] = policy
    atomic_write_json(paths.setup_profile_path, profile, private=True)
    return policy


def read_update_check_cache(paths: OmhPaths) -> dict[str, Any]:
    """Read the probe result cache. A corrupt cache file reads as empty, never raises."""
    cache, _error = read_json_object_result(update_check_cache_path(paths))
    return cache or {}


def _write_cache(paths: OmhPaths, patch: dict[str, Any]) -> dict[str, Any]:
    current = read_update_check_cache(paths)
    merged = {**current, **patch, "schema_version": UPDATE_CHECK_CACHE_SCHEMA_VERSION}
    path = update_check_cache_path(paths)
    ensure_dir(path.parent, private=True)
    atomic_write_json(path, merged, private=True)
    return merged


def acquire_auto_update_lock(paths: OmhPaths):
    """Non-blocking exclusive lock so two launches never auto-update at once.

    Raises `FileLockTimeout` (from `local_store`) immediately, without
    polling, when another process already holds it -- the caller's job is to
    catch that and skip, not to wait.
    """
    return file_lock(update_check_cache_path(paths), timeout_seconds=0.0, private=True)


@dataclass(frozen=True)
class RemoteProbeResult:
    ok: bool
    sha: str | None
    etag: str | None
    not_modified: bool
    error: str | None


def _run_curl(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _parse_http_response(raw: str) -> tuple[int, dict[str, str], str]:
    """Split `curl -i` output into (status_code, lowercased headers, body)."""
    normalized = raw.replace("\r\n", "\n")
    head, _, body = normalized.partition("\n\n")
    lines = head.split("\n")
    status = 0
    if lines:
        parts = lines[0].split()
        if len(parts) >= 2 and parts[1].isdigit():
            status = int(parts[1])
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    return status, headers, body


def fetch_remote_main_identity(
    *,
    etag: str = "",
    timeout: float = UPDATE_CHECK_NETWORK_TIMEOUT_SECONDS,
    runner=None,
) -> RemoteProbeResult:
    """One conditional GET of `main`'s current commit sha, via `curl`. Never raises.

    `runner` defaults to a small `subprocess.run` wrapper; tests inject a
    fake `subprocess.CompletedProcess`-shaped stand-in so no unit test ever
    spawns a real process or reaches a real socket.
    """
    runner = runner or _run_curl
    argv = [
        "curl",
        "-sS",
        "-i",
        "--max-time",
        str(timeout),
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "User-Agent: oh-my-hermes-update-check",
    ]
    if etag:
        argv += ["-H", f"If-None-Match: {etag}"]
    argv.append(GITHUB_COMMITS_API_URL)
    try:
        completed = runner(argv, timeout=timeout + 0.5)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return RemoteProbeResult(ok=False, sha=None, etag=None, not_modified=False, error=str(exc))
    if completed.returncode != 0:
        error = (completed.stderr or "").strip() or f"curl exit {completed.returncode}"
        return RemoteProbeResult(ok=False, sha=None, etag=None, not_modified=False, error=error)
    status, headers, body = _parse_http_response(completed.stdout or "")
    if status == 304:
        return RemoteProbeResult(ok=True, sha=None, etag=etag, not_modified=True, error=None)
    if status != 200:
        return RemoteProbeResult(ok=False, sha=None, etag=None, not_modified=False, error=f"http {status}")
    try:
        data = json.loads(body)
    except ValueError as exc:
        return RemoteProbeResult(ok=False, sha=None, etag=None, not_modified=False, error=str(exc))
    sha = str(data.get("sha", "")) if isinstance(data, dict) else ""
    if not sha:
        return RemoteProbeResult(ok=False, sha=None, etag=None, not_modified=False, error="missing sha in response")
    response_etag = headers.get("etag", "")
    return RemoteProbeResult(ok=True, sha=sha, etag=response_etag or None, not_modified=False, error=None)


def _read_runtime_state(paths: OmhPaths) -> dict[str, Any]:
    """Read `runtime/state.json`. A corrupt file reads as empty, never raises."""
    state, _error = read_json_object_result(paths.runtime_state_path)
    return state or {}


def local_installed_commit(paths: OmhPaths) -> str:
    """The comparable remote identity recorded at the last `omh install`/`update`.

    Empty when the install predates recording one (or the recording probe
    failed / was skipped because update-check was off at that time) -- both
    read as "no comparable identity" by `evaluate_update_check`.
    """
    state = _read_runtime_state(paths)
    return str(state.get("release_source_commit", "") or "")


def local_installed_channel(paths: OmhPaths) -> str:
    """The release channel ("stable", "preview", "local", ...) recorded at the
    last `omh install`/`update`, or "" when nothing has recorded one yet.

    `release_source_commit` is only ever recorded for the `preview` channel's
    `main` source ref (`commands/setup.py:_release_source_commit_for_state`),
    so a `stable` or `local` install reports `inconclusive` forever -- there
    is no future `omh update` that resolves it, unlike a `preview` install
    that simply has not recorded an identity yet. `format_notice_line` uses
    this to stop telling a stable/local install to "run `omh update`" for a
    comparison that channel will never resolve.
    """
    state = _read_runtime_state(paths)
    return str(state.get("release_channel", "") or "")


def record_remote_commit_for_install(
    paths: OmhPaths,
    *,
    runner=None,
    timeout: float = UPDATE_CHECK_NETWORK_TIMEOUT_SECONDS,
) -> str:
    """Best-effort remote main sha for `omh install`/`omh update` to persist.

    Called only from the already-explicit install/update invocation (never
    from the startup launch path), and only when update-check is not off, so
    the piggybacked call stays inside that same explicit-network family.
    Returns "" on any failure; callers must never let this fail the
    install/update command it rides along with.
    """
    result = fetch_remote_main_identity(timeout=timeout, runner=runner)
    return result.sha or ""


def _interval_elapsed(cache: dict[str, Any], interval_hours: float, now: datetime) -> bool:
    last_checked_raw = str(cache.get("last_checked_at", ""))
    if not last_checked_raw:
        return True
    try:
        last_checked = datetime.fromisoformat(last_checked_raw)
    except ValueError:
        return True
    if last_checked.tzinfo is None:
        last_checked = last_checked.replace(tzinfo=timezone.utc)
    return (now - last_checked) >= timedelta(hours=interval_hours)


def evaluate_update_check(
    paths: OmhPaths,
    *,
    now: datetime | None = None,
    force: bool = False,
    runner=None,
    timeout: float = UPDATE_CHECK_NETWORK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run (or reuse the cached result of) the startup update-availability check.

    Zero network attempts when `update_check.mode` is `off` (the shipped
    default). Otherwise respects `interval_hours` across calls via the cache
    at `update_check_cache_path()`, and any probe failure is a silent skip
    that keeps whatever the cache already knew rather than raising or
    claiming a fresh (possibly wrong) outcome.

    `should_auto_update` is only ever true off a fresh probe in this call --
    never off a cache reused because the interval has not elapsed yet. A
    cached "behind" outcome is stale by definition (it is exactly what the
    previous auto-update either already acted on, or failed to act on);
    re-triggering `omh update` from it on every launch inside the same
    interval is what previously made auto mode re-update on every launch.
    `main.py`'s `_run_auto_update` calls `refresh_cache_after_auto_update()`
    after a successful auto-update so the cached outcome itself stops
    claiming "behind" too, rather than relying on this alone.
    """
    policy = read_update_check_policy(paths)
    mode = policy["mode"]
    if mode == "off":
        return {
            "schema_version": UPDATE_CHECK_RESULT_SCHEMA_VERSION,
            "mode": "off",
            "outcome": "skipped_off",
            "checked": False,
            "should_auto_update": False,
            "local_commit": "",
            "remote_commit": "",
            "channel": "",
        }

    now = now or datetime.now(timezone.utc)
    cache = read_update_check_cache(paths)
    local_commit = local_installed_commit(paths)
    channel = local_installed_channel(paths)

    if not force and not _interval_elapsed(cache, float(policy["interval_hours"]), now):
        outcome = str(cache.get("outcome") or "inconclusive")
        return {
            "schema_version": UPDATE_CHECK_RESULT_SCHEMA_VERSION,
            "mode": mode,
            "outcome": outcome,
            "checked": False,
            "should_auto_update": False,
            "local_commit": local_commit,
            "remote_commit": str(cache.get("remote_commit", "")),
            "channel": channel,
        }

    probe = fetch_remote_main_identity(etag=str(cache.get("remote_etag", "")), timeout=timeout, runner=runner)
    if not probe.ok:
        # Silent skip: nothing new is claimed. Advancing last_checked_at still
        # spaces the next attempt by a full interval instead of retrying on
        # every subsequent launch.
        _write_cache(paths, {"last_checked_at": now.isoformat(timespec="seconds")})
        outcome = str(cache.get("outcome") or "inconclusive")
        return {
            "schema_version": UPDATE_CHECK_RESULT_SCHEMA_VERSION,
            "mode": mode,
            "outcome": outcome,
            "checked": True,
            "should_auto_update": False,
            "local_commit": local_commit,
            "remote_commit": str(cache.get("remote_commit", "")),
            "channel": channel,
        }

    remote_commit = probe.sha if not probe.not_modified else str(cache.get("remote_commit", ""))
    remote_etag = probe.etag if probe.etag else str(cache.get("remote_etag", ""))

    if not local_commit or not remote_commit:
        outcome = "inconclusive"
    elif local_commit == remote_commit:
        outcome = "up_to_date"
    else:
        outcome = "behind"

    _write_cache(
        paths,
        {
            "last_checked_at": now.isoformat(timespec="seconds"),
            "remote_commit": remote_commit,
            "remote_etag": remote_etag,
            "outcome": outcome,
        },
    )
    return {
        "schema_version": UPDATE_CHECK_RESULT_SCHEMA_VERSION,
        "mode": mode,
        "outcome": outcome,
        "checked": True,
        "should_auto_update": mode == "auto" and outcome == "behind",
        "local_commit": local_commit,
        "remote_commit": remote_commit,
        "channel": channel,
    }


def refresh_cache_after_auto_update(paths: OmhPaths) -> dict[str, Any]:
    """Re-anchor the cache to the just-updated local identity.

    Called only from `main.py`'s `_run_auto_update` after `omh update` exits
    0. Without this, the cache keeps whatever "behind" outcome triggered the
    auto-update, so the next launch inside the same interval reads a stale
    claim (fixed separately in `evaluate_update_check` to never re-trigger
    an auto-update from it, but still wrong for `omh update-check status` and
    `notify` mode to display). Never probes the network: `omh update` already
    recorded a fresh `release_source_commit` when it ran, so comparing that
    against the cache's own `remote_commit` (the identity the auto-update was
    acting on) is enough to tell "converged" from "still short of it,"
    without spending another interval-bounded network attempt on it.
    """
    cache = read_update_check_cache(paths)
    local_commit = local_installed_commit(paths)
    remote_commit = str(cache.get("remote_commit", ""))
    outcome = "up_to_date" if local_commit and local_commit == remote_commit else "inconclusive"
    return _write_cache(paths, {"outcome": outcome})


def _short_commit(sha: str) -> str:
    return sha[:7] if sha else "unknown"


def format_notice_line(result: dict[str, Any]) -> str:
    """The one-line notice `notify` mode prints, or `auto` prints when it
    cannot tell whether an update is needed. Empty string means print nothing.
    """
    outcome = result.get("outcome")
    if outcome == "behind":
        local = _short_commit(str(result.get("local_commit", "")))
        remote = _short_commit(str(result.get("remote_commit", "")))
        return f"OMH update available: {local} -> {remote}; run `omh update`."
    if outcome == "inconclusive":
        # `release_source_commit` -- the identity this check compares against
        # `origin/main` -- is only ever recorded for a `preview`-channel
        # install (`commands/setup.py:_release_source_commit_for_state`). A
        # `stable` or `local` install is not a temporary gap that the
        # suggested `omh update` would close; it stays inconclusive forever
        # because it deliberately is not tracking `main`. An empty/unknown
        # channel (e.g. an install that predates this field) keeps the
        # original, actionable wording since that gap really can close.
        channel = str(result.get("channel", ""))
        if channel and channel != "preview":
            return f"OMH update check: not applicable on the {channel} channel (only preview tracks main)."
        return "OMH update check inconclusive -- run `omh update`."
    return ""
