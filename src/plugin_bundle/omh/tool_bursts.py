"""Parallel tool-call burst observation and in-flight liveness for the HUD.

Hermes executes a model turn's batched tool calls concurrently (its
``_execute_tool_calls_concurrent`` dispatch), but the transcript renders
only a collapsed "Tool calls (N)" group -- nothing tells the user whether
that batch actually ran as a parallel shot, or whether the batch is still
running at all. The ``pre_tool_call`` hook fires once per call, so calls
whose start ticks land inside one short window are the concurrent batch;
this module records those ticks (tool name and timestamp only, never
arguments or results) and projects the latest burst for the ``[OMH]``
status line.

``post_tool_call`` (a second supported Hermes observer hook, paired with
``pre_tool_call`` by ``tool_call_id``) closes what ``pre_tool_call`` opens.
Pairing the two gives an exact in-flight count for the main session -- the
gap the ring-buffer-only design could not see: a stopped turn with an
incomplete todo item read identically to 40 tool calls genuinely running,
because nothing distinguished "the ring saturated" from "work is
happening". A host that omits ``tool_call_id`` degrades silently to the
pre-pairing behavior: the tick still lands for burst grouping, but no
in-flight entry opens, and the HUD's liveness signal simply stays quiet
for that call.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Reuse the awareness ledger's portable lock and atomic-write primitives so
# there is exactly one file-locking implementation in the plugin.
from .awareness_delivery import _awareness_delivery_lock, _write_delivery_record

TOOL_BURSTS_SCHEMA_VERSION = "omh_tool_bursts/v1"
TOOL_ACTIVITY_SCHEMA_VERSION = "omh_tool_activity/v1"
TOOL_BURSTS_FILE = "tool-bursts.json"
# Raised from 40 (2026-08, HUD liveness fix): the ring ceiling used to be
# what the "parallel shot x40" badge actually measured -- a fanout wave of
# 40+ short calls saturated the ring long before it went idle, so the badge
# read the buffer filling up, not the truth about what was running. 200
# covers a large fanout dispatch batch with headroom while keeping the file
# small (each entry is two-three short fields).
MAX_TOOL_BURST_ENTRIES = 200
# Same headroom rationale as MAX_TOOL_BURST_ENTRIES, applied to the
# in-flight ledger: a host that never sends a matching post_tool_call (a
# crash, an unsupported host) must not let open_calls grow without bound.
# Losing the oldest open entry under pathological load is acceptable --
# the ledger is best-effort observation, not a correctness-critical store.
MAX_OPEN_TOOL_CALLS = 200
# Ticks closer together than this belong to one dispatch burst: Hermes
# starts a concurrent batch's workers within milliseconds of each other,
# while consecutive sequential turns are separated by at least one model
# round-trip.
BURST_WINDOW_SECONDS = 1.5
# The HUD shows the latest burst only while it is recent enough to still
# describe "what just happened". Seconds, not minutes, by owner direction
# ('한 몇초만 지나면 바로없어지게'): the badge flags the batch as it lands
# and vanishes right after, refreshed continuously through a busy wave by
# the next batch's ticks.
BURST_FRESH_SECONDS = 8.0
# An open call with no matching post_tool_call this long is not still
# running -- it is a process restart, a crashed host, or a lost tick. It is
# reported as expired rather than kept open forever, and never reported as
# completed (nothing observed it finishing).
TOOL_CALL_OPEN_TTL_SECONDS = 15 * 60
TOOL_BURST_CLAIM_BOUNDARY = (
    "Burst grouping observes pre_tool_call start ticks only; it is evidence "
    "the host dispatched the calls as one batch, not proof every call "
    "overlapped for its full duration."
)
TOOL_ACTIVITY_CLAIM_BOUNDARY = (
    "In-flight state is the local pairing of this host's pre_tool_call and "
    "post_tool_call ticks by tool_call_id. It is evidence of calls this OMH "
    "install observed opening and closing, not a complete accounting of "
    "every tool call Hermes ran."
)
_MAX_TOOL_NAME_CHARS = 48
_MAX_ID_CHARS = 128


def _runtime_dir(omh_home: str = "") -> Path:
    root = Path(omh_home).expanduser() if omh_home else Path.home() / ".omh"
    return root / "runtime"


def tool_bursts_path(omh_home: str = "") -> Path:
    return _runtime_dir(omh_home) / TOOL_BURSTS_FILE


def _normalized_name(tool_name: object) -> str:
    return " ".join(str(tool_name or "").split())[:_MAX_TOOL_NAME_CHARS]


def _normalized_id(value: object) -> str:
    return str(value or "").strip()[:_MAX_ID_CHARS]


def record_tool_call(
    tool_name: object,
    *,
    omh_home: str = "",
    now: float | None = None,
    tool_call_id: object = None,
    turn_id: object = None,
) -> None:
    """Append one pre_tool_call tick and, when the host supplies a
    tool_call_id, open an in-flight entry post_tool_call will close.
    Best-effort: losing a tick is acceptable, breaking the hook that feeds
    the model is not."""
    name = _normalized_name(tool_name)
    if not name:
        return
    tick = float(now if now is not None else time.time())
    call_id = _normalized_id(tool_call_id)
    path = tool_bursts_path(omh_home)
    try:
        with _awareness_delivery_lock(path):
            record = _read_record(path)
            entries = record["entries"]
            entries.append({"tool": name, "ts": tick, "id": call_id})
            entries = entries[-MAX_TOOL_BURST_ENTRIES:]
            open_calls = _prune_expired_opens(record["open_calls"], now=tick)
            if call_id:
                open_calls[call_id] = {
                    "tool": name,
                    "turn_id": _normalized_id(turn_id),
                    "started_at": tick,
                }
                open_calls = _cap_open_calls(open_calls)
            _write_delivery_record(
                path,
                {
                    "schema_version": TOOL_BURSTS_SCHEMA_VERSION,
                    "entries": entries,
                    "open_calls": open_calls,
                },
            )
    except (OSError, ValueError, TypeError):
        return


def record_tool_call_close(tool_call_id: object, *, omh_home: str = "", now: float | None = None) -> None:
    """post_tool_call: close the in-flight entry pre_tool_call opened.

    A tool_call_id with no open entry (already expired, or a host that never
    sent the matching pre_tool_call tick) is a silent no-op -- there is
    nothing to close. Best-effort, same as ``record_tool_call``."""
    call_id = _normalized_id(tool_call_id)
    if not call_id:
        return
    tick = float(now if now is not None else time.time())
    path = tool_bursts_path(omh_home)
    try:
        with _awareness_delivery_lock(path):
            record = _read_record(path)
            open_calls = _prune_expired_opens(record["open_calls"], now=tick)
            if call_id not in open_calls:
                return
            del open_calls[call_id]
            _write_delivery_record(
                path,
                {
                    "schema_version": TOOL_BURSTS_SCHEMA_VERSION,
                    "entries": record["entries"],
                    "open_calls": open_calls,
                },
            )
    except (OSError, ValueError, TypeError):
        return


def _read_record(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return {"entries": _sanitized_entries(raw), "open_calls": _sanitized_open_calls(raw)}


def _sanitized_entries(raw: dict[str, Any]) -> list[dict[str, Any]]:
    source = raw.get("entries")
    entries: list[dict[str, Any]] = []
    for item in source if isinstance(source, list) else []:
        if not isinstance(item, dict):
            continue
        tick = item.get("ts")
        tool = str(item.get("tool", "") or "")
        if isinstance(tick, (int, float)) and not isinstance(tick, bool) and tool:
            entries.append(
                {
                    "tool": tool[:_MAX_TOOL_NAME_CHARS],
                    "ts": float(tick),
                    "id": _normalized_id(item.get("id")),
                }
            )
    entries.sort(key=lambda entry: entry["ts"])
    return entries


def _sanitized_open_calls(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    source = raw.get("open_calls")
    open_calls: dict[str, dict[str, Any]] = {}
    for call_id, item in (source.items() if isinstance(source, dict) else ()):
        if not isinstance(item, dict):
            continue
        started_at = item.get("started_at")
        normalized_id = _normalized_id(call_id)
        if not normalized_id or not isinstance(started_at, (int, float)) or isinstance(started_at, bool):
            continue
        open_calls[normalized_id] = {
            "tool": str(item.get("tool", "") or "")[:_MAX_TOOL_NAME_CHARS],
            "turn_id": _normalized_id(item.get("turn_id")),
            "started_at": float(started_at),
        }
    return open_calls


def _prune_expired_opens(open_calls: dict[str, dict[str, Any]], *, now: float) -> dict[str, dict[str, Any]]:
    return {
        call_id: entry
        for call_id, entry in open_calls.items()
        if now - float(entry.get("started_at", now)) <= TOOL_CALL_OPEN_TTL_SECONDS
    }


def _cap_open_calls(open_calls: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if len(open_calls) <= MAX_OPEN_TOOL_CALLS:
        return open_calls
    newest_first = sorted(open_calls.items(), key=lambda item: item[1]["started_at"], reverse=True)
    return dict(newest_first[:MAX_OPEN_TOOL_CALLS])


def _iso(tick: float) -> str:
    return datetime.fromtimestamp(tick, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _latest_shot_from_entries(
    entries: list[dict[str, Any]],
    open_calls: dict[str, dict[str, Any]],
    *,
    now: float,
) -> dict[str, Any]:
    idle: dict[str, Any] = {"status": "idle"}
    if not entries:
        return idle
    groups: list[list[dict[str, Any]]] = [[entries[0]]]
    for entry in entries[1:]:
        if entry["ts"] - groups[-1][-1]["ts"] <= BURST_WINDOW_SECONDS:
            groups[-1].append(entry)
        else:
            groups.append([entry])
    latest = next((group for group in reversed(groups) if len(group) >= 2), None)
    if latest is None:
        return idle
    last_tick = latest[-1]["ts"]
    if now - last_tick > BURST_FRESH_SECONDS:
        return idle
    open_count = sum(1 for entry in latest if entry.get("id") and entry["id"] in open_calls)
    return {
        "status": "observed",
        "size": len(latest),
        "distinct_tools": len({entry["tool"] for entry in latest}),
        "observed_at": _iso(last_tick),
        # True in-flight split within this shot's own members, not the ring
        # ceiling: a batch of 40 that saturated the old ring could not tell
        # "still running" from "buffer full". A member with no tool_call_id
        # (host degraded to pre-pairing behavior) counts as completed here --
        # it was never opened, so it cannot be reported open.
        "open_count": open_count,
        "completed_count": len(latest) - open_count,
        "claim_boundary": TOOL_BURST_CLAIM_BOUNDARY,
    }


def latest_parallel_shot(omh_home: str = "", *, now: float | None = None) -> dict[str, Any]:
    """Project the most recent concurrent batch, or an idle marker."""
    current = float(now if now is not None else time.time())
    record = _read_record(tool_bursts_path(omh_home))
    open_calls = _prune_expired_opens(record["open_calls"], now=current)
    return _latest_shot_from_entries(record["entries"], open_calls, now=current)


def tool_call_activity(omh_home: str = "", *, now: float | None = None) -> dict[str, Any]:
    """The HUD's liveness signal: exact open tool-call count plus the shot.

    ``live`` is true precisely while at least one tool call this OMH install
    saw open has not yet closed and has not expired -- it answers "is
    something actually running right now", the question the ring-buffer-only
    badge and a lingering active todo item could not.
    """
    current = float(now if now is not None else time.time())
    record = _read_record(tool_bursts_path(omh_home))
    open_calls = _prune_expired_opens(record["open_calls"], now=current)
    count = len(open_calls)
    if count:
        oldest_id, oldest = min(open_calls.items(), key=lambda item: item[1]["started_at"])
        oldest_started_at = _iso(oldest["started_at"])
        oldest_elapsed_seconds: float | None = max(0.0, current - oldest["started_at"])
    else:
        oldest_started_at = ""
        oldest_elapsed_seconds = None
    return {
        "schema_version": TOOL_ACTIVITY_SCHEMA_VERSION,
        "open_call_count": count,
        "oldest_open_started_at": oldest_started_at,
        "oldest_open_elapsed_seconds": oldest_elapsed_seconds,
        "live": count > 0,
        "latest_shot": _latest_shot_from_entries(record["entries"], open_calls, now=current),
        "claim_boundary": TOOL_ACTIVITY_CLAIM_BOUNDARY,
    }
