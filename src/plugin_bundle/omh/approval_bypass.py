"""Effective approval-bypass (yolo) observation for the HUD.

The Shift+Tab yolo toggle lives only in the host process's
``tools.approval`` session state — nothing on disk records it — so the
HUD can only report what the plugin's hooks observe in-process. Both
``pre_llm_call`` (fires at each turn start, so a toggle shows up on the
user's next message) and ``pre_tool_call`` tick this ledger with the same
effective-bypass answer the host's own status surfaces report: global
``approvals.mode=off``, the process-scoped ``--yolo`` env, or the
per-session Shift+Tab flag, ORed together. Only the boolean and its
timestamp are recorded — never a session key, command, or prompt.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Reuse the awareness ledger's portable lock and atomic-write primitives so
# there is exactly one file-locking implementation in the plugin.
from .awareness_delivery import _awareness_delivery_lock, _write_delivery_record

APPROVAL_BYPASS_SCHEMA_VERSION = "omh_approval_bypass/v1"
APPROVAL_BYPASS_FILE = "approval-bypass.json"
# A hook-observed flag outlives the turn that observed it, but not forever:
# after a host restart the per-session flag resets to off in a fresh
# process, and nothing re-observes until the next turn. Six hours bounds
# how long a pre-restart observation can keep claiming a state no process
# holds any more.
APPROVAL_BYPASS_FRESH_SECONDS = 6 * 3600.0
APPROVAL_BYPASS_CLAIM_BOUNDARY = (
    "Hook-observed effective approval-bypass state; it can lag Shift+Tab "
    "toggles and host restarts until the next turn or tool call, and is "
    "never approval, execution, or safety evidence."
)


def _runtime_dir(omh_home: str = "") -> Path:
    root = Path(omh_home).expanduser() if omh_home else Path.home() / ".omh"
    return root / "runtime"


def approval_bypass_path(omh_home: str = "") -> Path:
    return _runtime_dir(omh_home) / APPROVAL_BYPASS_FILE


def record_approval_bypass(*, omh_home: str = "", now: float | None = None) -> None:
    """Record the current effective bypass state. Best-effort: losing an
    observation is acceptable, breaking the hook that feeds the model is not."""
    try:
        from tools.approval import is_approval_bypass_active

        enabled = bool(is_approval_bypass_active())
    except Exception:
        # No host approval surface in this process (unit tests, a stripped
        # embed, an older Hermes): there is nothing true to record, and the
        # ledger keeps whatever was last observed rather than a guess.
        return
    tick = float(now if now is not None else time.time())
    path = approval_bypass_path(omh_home)
    record = {
        "schema_version": APPROVAL_BYPASS_SCHEMA_VERSION,
        "enabled": enabled,
        "observed_ts": tick,
        "claim_boundary": APPROVAL_BYPASS_CLAIM_BOUNDARY,
    }
    try:
        with _awareness_delivery_lock(path):
            _write_delivery_record(path, record)
    except (OSError, ValueError, TypeError):
        return


def latest_approval_bypass(omh_home: str = "", *, now: float | None = None) -> dict[str, Any]:
    """Project the last observed bypass state, or an idle marker."""
    import json

    current = float(now if now is not None else time.time())
    idle: dict[str, Any] = {"status": "idle"}
    try:
        record = json.loads(approval_bypass_path(omh_home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return idle
    if not isinstance(record, dict):
        return idle
    tick = record.get("observed_ts")
    enabled = record.get("enabled")
    if not isinstance(tick, (int, float)) or isinstance(tick, bool) or not isinstance(enabled, bool):
        return idle
    if current - float(tick) > APPROVAL_BYPASS_FRESH_SECONDS:
        return idle
    observed_at = (
        datetime.fromtimestamp(float(tick), tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    return {
        "status": "observed",
        "enabled": enabled,
        "observed_at": observed_at,
        "claim_boundary": APPROVAL_BYPASS_CLAIM_BOUNDARY,
    }
