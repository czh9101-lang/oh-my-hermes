"""Parallel tool-call burst observation for the HUD.

Hermes executes a model turn's batched tool calls concurrently (its
``_execute_tool_calls_concurrent`` dispatch), but the transcript renders
only a collapsed "Tool calls (N)" group — nothing tells the user whether
that batch actually ran as a parallel shot. The ``pre_tool_call`` hook
fires once per call, so calls whose start ticks land inside one short
window are the concurrent batch. This module records those ticks (tool
name and timestamp only, never arguments or results) and projects the
latest burst for the ``[OMH]`` status line.
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
TOOL_BURSTS_FILE = "tool-bursts.json"
MAX_TOOL_BURST_ENTRIES = 40
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
TOOL_BURST_CLAIM_BOUNDARY = (
    "Burst grouping observes pre_tool_call start ticks only; it is evidence "
    "the host dispatched the calls as one batch, not proof every call "
    "overlapped for its full duration."
)
_MAX_TOOL_NAME_CHARS = 48


def _runtime_dir(omh_home: str = "") -> Path:
    root = Path(omh_home).expanduser() if omh_home else Path.home() / ".omh"
    return root / "runtime"


def tool_bursts_path(omh_home: str = "") -> Path:
    return _runtime_dir(omh_home) / TOOL_BURSTS_FILE


def record_tool_call(tool_name: object, *, omh_home: str = "", now: float | None = None) -> None:
    """Append one pre_tool_call tick. Best-effort: losing a tick is acceptable,
    breaking the hook that feeds the model is not."""
    name = " ".join(str(tool_name or "").split())[:_MAX_TOOL_NAME_CHARS]
    if not name:
        return
    tick = float(now if now is not None else time.time())
    path = tool_bursts_path(omh_home)
    try:
        with _awareness_delivery_lock(path):
            entries = _read_entries(path)
            entries.append({"tool": name, "ts": tick})
            entries = entries[-MAX_TOOL_BURST_ENTRIES:]
            _write_delivery_record(
                path,
                {"schema_version": TOOL_BURSTS_SCHEMA_VERSION, "entries": entries},
            )
    except (OSError, ValueError, TypeError):
        return


def _read_entries(path: Path) -> list[dict[str, Any]]:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raw = record.get("entries") if isinstance(record, dict) else None
    entries: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        tick = item.get("ts")
        tool = str(item.get("tool", "") or "")
        if isinstance(tick, (int, float)) and not isinstance(tick, bool) and tool:
            entries.append({"tool": tool[:_MAX_TOOL_NAME_CHARS], "ts": float(tick)})
    entries.sort(key=lambda entry: entry["ts"])
    return entries


def latest_parallel_shot(omh_home: str = "", *, now: float | None = None) -> dict[str, Any]:
    """Project the most recent concurrent batch, or an idle marker."""
    current = float(now if now is not None else time.time())
    entries = _read_entries(tool_bursts_path(omh_home))
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
    if current - last_tick > BURST_FRESH_SECONDS:
        return idle
    observed_at = (
        datetime.fromtimestamp(last_tick, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    return {
        "status": "observed",
        "size": len(latest),
        "distinct_tools": len({entry["tool"] for entry in latest}),
        "observed_at": observed_at,
        "claim_boundary": TOOL_BURST_CLAIM_BOUNDARY,
    }
