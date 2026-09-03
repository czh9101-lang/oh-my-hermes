"""Persistent state, pointer switching, and retention for staged self updates."""

from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
import shutil
from typing import Any, Callable

try:
    from ..core.errors import OmhError
    from ..system.local_store import atomic_write_json, read_json_object_result
    from .self_update_platform import SelfUpdatePlatform
except ImportError:  # pragma: no cover - direct-source installer smoke.
    from core.errors import OmhError
    from system.local_store import atomic_write_json, read_json_object_result
    from install.self_update_platform import SelfUpdatePlatform

STATE_SCHEMA_VERSION = "self_update_state/v1"


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def state_path(root: Path) -> Path:
    return root / "self-update.json"


def generation_entry(path: Path, kind: str = "generation", version: str = "") -> dict[str, str]:
    return {"id": path.name, "path": str(path), "kind": kind, "version": version}


def load_state(root: Path, legacy: Path) -> dict[str, Any]:
    path = state_path(root)
    state, error = read_json_object_result(path)
    if state is None:
        if path.exists():
            raise OmhError(f"cannot read {path}: {error}; run omh update --recover-known-good")
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "active": generation_entry(legacy, "legacy"),
            "previous_known_good": None,
            "pointer": {"path": str(root / "current"), "target": ""},
            "migration": {"status": "pending"},
            "activation_in_progress": None,
            "retained_generations": [],
        }
    schema = state.get("schema_version")
    if schema != STATE_SCHEMA_VERSION:
        reason = "newer" if isinstance(schema, str) and schema > STATE_SCHEMA_VERSION else "unsupported"
        raise OmhError(f"cannot use {path}: {reason} self-update state schema {schema!r}")
    return state


def save_state(root: Path, state: dict[str, Any]) -> None:
    state["schema_version"] = STATE_SCHEMA_VERSION
    state["updated_at"] = now()
    atomic_write_json(state_path(root), state, private=True)


def pointer_target(root: Path, *, platform: SelfUpdatePlatform | None = None) -> Path | None:
    return (platform or SelfUpdatePlatform.host()).link_target(root, root / "current")


def switch_current(
    root: Path,
    target: Path,
    *,
    is_windows: bool | None = None,
    platform: SelfUpdatePlatform | None = None,
) -> None:
    """Atomically replace the one consumer pointer with a link or junction."""
    selected = platform or SelfUpdatePlatform.host(is_windows=is_windows)
    selected.replace_current(root, target)


def migration_needed(state: dict[str, Any]) -> bool:
    return state.get("migration", {}).get("status") != "completed"


def mark_activation(state: dict[str, Any], candidate: Path, previous: Path) -> None:
    state["activation_in_progress"] = {
        "candidate": str(candidate),
        "previous": str(previous),
        "marker_written_at": now(),
        "phase": "pre_switch",
    }


def mark_switched(state: dict[str, Any]) -> None:
    marker = state.get("activation_in_progress")
    if isinstance(marker, dict):
        marker["phase"] = "post_switch"


def interrupted_activation(
    root: Path,
    state: dict[str, Any],
    *,
    platform: SelfUpdatePlatform | None = None,
) -> tuple[Path, Path] | None:
    marker = state.get("activation_in_progress")
    if not isinstance(marker, dict):
        return None
    try:
        candidate = Path(str(marker["candidate"]))
        previous = Path(str(marker["previous"]))
    except KeyError as exc:
        raise OmhError("cannot recover malformed activation marker") from exc
    pointed = pointer_target(root, platform=platform)
    if pointed is not None and pointed.resolve() == candidate.resolve():
        return candidate, previous
    shutil.rmtree(candidate, ignore_errors=True)
    state["activation_in_progress"] = None
    save_state(root, state)
    return None


def record_pointer(state: dict[str, Any], root: Path, target: Path) -> None:
    state["pointer"] = {"path": str(root / "current"), "target": os.path.relpath(target, root)}


def commit_activation(root: Path, state: dict[str, Any], candidate: Path) -> None:
    state["previous_known_good"] = state["active"]
    state["active"] = generation_entry(candidate)
    state["activation_in_progress"] = None
    state.pop("recovery_selected", None)
    record_pointer(state, root, candidate)


def restore_known_good(root: Path, state: dict[str, Any], target: Path) -> None:
    state["active"] = generation_entry(target, str(state.get("active", {}).get("kind", "generation")))
    state["activation_in_progress"] = None
    state["recovery_selected"] = target.name
    record_pointer(state, root, target)


def recovery_target(state: dict[str, Any]) -> Path:
    selected = str(state.get("recovery_selected", ""))
    active = state.get("active")
    if selected and isinstance(active, dict) and active.get("id") == selected:
        target = Path(str(active.get("path", "")))
    else:
        previous = state.get("previous_known_good")
        if not isinstance(previous, dict):
            raise OmhError("no retained previous known-good generation is available")
        target = Path(str(previous.get("path", "")))
    if not target.is_dir():
        raise OmhError("no retained previous known-good generation is available")
    return target


def collect_garbage(
    root: Path,
    state: dict[str, Any],
    result: dict[str, Any],
    *,
    running_generation: Path | None,
) -> None:
    active = str(state.get("active", {}).get("id", ""))
    previous = str((state.get("previous_known_good") or {}).get("id", ""))
    keep = {item for item in (active, previous, "bootstrap-legacy") if item}
    if running_generation is not None:
        keep.add(running_generation.name)
    generations = root / "generations"
    for item in generations.iterdir() if generations.exists() else ():
        if item.name not in keep:
            shutil.rmtree(item, ignore_errors=True)
            result["cleanup"]["collected"].append(str(item))
    state["retained_generations"] = sorted(keep & {item.name for item in generations.iterdir()}) if generations.exists() else []


def reconcile_interruption(
    root: Path,
    state: dict[str, Any],
    *,
    reenter: Callable[[Path], tuple[bool, str]],
    platform: SelfUpdatePlatform | None = None,
) -> tuple[str, Path | None]:
    interrupted = interrupted_activation(root, state, platform=platform)
    if interrupted is None:
        return "discarded_unswitched_candidate", None
    candidate, previous = interrupted
    passed, _reason = reenter(candidate)
    if passed:
        commit_activation(root, state, candidate)
        save_state(root, state)
        return "completed_interrupted_activation", candidate
    switch_current(root, previous, platform=platform)
    reenter(previous)
    state["activation_in_progress"] = None
    record_pointer(state, root, previous)
    save_state(root, state)
    return "rolled_back_interrupted_activation", previous
