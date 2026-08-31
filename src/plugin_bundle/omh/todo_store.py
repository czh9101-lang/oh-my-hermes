"""Shared store for the HUD todo artifact.

One todo list per OMH home, written to ``$OMH_HOME/runtime/todo.json``. The
CLI (`omh runtime todo`) and the `omh_todo` plugin tool both write through this
module so the schema has a single source of truth; `runtime_reader` projects
the file into the HUD payload read-only.

Because the artifact is global to the home and its writers are not, a record
carries the session that declared it (``session_ref``) whenever the writer
knows one. Nothing here enforces the scope -- the reader does, in
``runtime_reader._todo_summary`` -- but without the stamp a plan from one
session, or from a concurrent writer sharing the same home, is
indistinguishable from the reader's own.
"""
from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TODO_SCHEMA_VERSION = "omh_todo/v1"
TODO_FILENAME = "todo.json"
TODO_ITEM_STATES = ("pending", "active", "done")
MAX_TODO_ITEMS = 20
MAX_TODO_TEXT_CHARS = 200
MAX_TODO_TITLE_CHARS = 80
MAX_TODO_SOURCE_CHARS = 80
# Optional owning-session id, stamped when the writer knows which host session
# declared the plan. Bounded to the host-observation session limit because it
# is the same identifier. A record without it is a legacy or CLI write, and
# readers scope it by write time instead of by identity.
MAX_TODO_SESSION_REF_CHARS = 160
# Optional phase label per item ("Internal Context", "Delivery", ...). A
# phase-structured plan declared BEFORE engine work bounds the run: progress
# is a checklist walked phase by phase, not an open-ended reasoning loop.
MAX_TODO_PHASE_CHARS = 60
# Optional nesting depth per item: 0 is a top-level task, 1..3 are subtask
# levels rendered indented beneath it (e.g. "검증작업하기" with usability /
# UI / load-verification children). Three levels is the owner's declared
# ceiling; deeper nesting stops reading as a checklist.
MAX_TODO_DEPTH = 3
TODO_CLAIM_BOUNDARY = (
    "Todo items are plan declarations. They are not execution, verification, "
    "review, CI, merge-readiness, or merge evidence."
)


class TodoValidationError(ValueError):
    """The supplied todo payload does not satisfy the omh_todo/v1 contract."""


class TodoStoreError(RuntimeError):
    """The todo destination under the OMH home is unsafe to write."""


# C0/C1 control characters (ESC, BEL, CR, LF included) are stripped on write
# and again on read so neither the artifact at rest nor the HUD projection can
# carry terminal escapes or forge extra checklist lines.
_CONTROL_CHARACTERS = {code: None for code in (*range(0x00, 0x20), 0x7F, *range(0x80, 0xA0))}


def strip_control_characters(value: object) -> str:
    return str(value or "").translate(_CONTROL_CHARACTERS).strip()


def validate_todo_items(items: object) -> list[dict[str, Any]]:
    if not isinstance(items, list) or not items:
        raise TodoValidationError("todo items must be a non-empty list")
    if len(items) > MAX_TODO_ITEMS:
        raise TodoValidationError(f"todo items are capped at {MAX_TODO_ITEMS}")
    validated: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            raise TodoValidationError("each todo item must be an object")
        text = strip_control_characters(item.get("text", ""))
        if not text:
            raise TodoValidationError("each todo item needs non-empty text")
        if len(text) > MAX_TODO_TEXT_CHARS:
            raise TodoValidationError(f"todo item text is capped at {MAX_TODO_TEXT_CHARS} characters")
        state = str(item.get("state", "pending"))
        if state not in TODO_ITEM_STATES:
            raise TodoValidationError(f"todo item state must be one of {', '.join(TODO_ITEM_STATES)}")
        phase = strip_control_characters(item.get("phase", ""))
        if len(phase) > MAX_TODO_PHASE_CHARS:
            raise TodoValidationError(f"todo item phase is capped at {MAX_TODO_PHASE_CHARS} characters")
        depth = item.get("depth", 0)
        if isinstance(depth, bool) or not isinstance(depth, int) or not 0 <= depth <= MAX_TODO_DEPTH:
            raise TodoValidationError(f"todo item depth must be an integer from 0 to {MAX_TODO_DEPTH}")
        entry: dict[str, Any] = {"text": text, "state": state}
        if phase:
            entry["phase"] = phase
        if depth:
            entry["depth"] = depth
        validated.append(entry)
    return validated


def build_todo_record(
    title: object, items: object, *, source: str, session_ref: object = ""
) -> dict[str, Any]:
    """Build the on-disk todo record.

    ``session_ref`` names the host session that declared this plan, when the
    writer knows it. It is additive-optional inside ``omh_todo/v1``: the key is
    written only when non-empty, so a CLI write is byte-identical to what it
    was before the field existed, and a reader that predates it still reads
    every field it knew.
    """
    safe_title = strip_control_characters(title)
    if len(safe_title) > MAX_TODO_TITLE_CHARS:
        raise TodoValidationError(f"todo title is capped at {MAX_TODO_TITLE_CHARS} characters")
    safe_source = strip_control_characters(source)[:MAX_TODO_SOURCE_CHARS]
    safe_session_ref = strip_control_characters(session_ref)[:MAX_TODO_SESSION_REF_CHARS]
    record: dict[str, Any] = {
        "schema_version": TODO_SCHEMA_VERSION,
        "title": safe_title,
        "source": safe_source,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "items": validate_todo_items(items),
        "claim_boundary": TODO_CLAIM_BOUNDARY,
    }
    if safe_session_ref:
        record["session_ref"] = safe_session_ref
    return record


def todo_path(omh_home: Path) -> Path:
    return omh_home / "runtime" / TODO_FILENAME


def write_todo(omh_home: Path, record: dict[str, Any]) -> Path:
    destination = todo_path(omh_home)
    _reject_symlink_ancestry(destination, root=omh_home)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}-{secrets.token_hex(8)}.tmp")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Post-mkdir TOCTOU recheck; the ancestry walk above already rejected a
        # pre-existing symlink at this path.
        if destination.is_symlink():
            raise TodoStoreError(f"refusing symlinked todo destination: {destination}")
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        os.replace(temporary, destination)
    except OSError as error:
        raise TodoStoreError(f"todo destination is not writable: {error}") from error
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()
    return destination


def clear_todo(omh_home: Path) -> bool:
    destination = todo_path(omh_home)
    _reject_symlink_ancestry(destination, root=omh_home)
    if not destination.is_file() or destination.is_symlink():
        return False
    try:
        destination.unlink()
    except OSError as error:
        raise TodoStoreError(f"todo destination is not removable: {error}") from error
    return True


def _reject_symlink_ancestry(path: Path, *, root: Path) -> None:
    current = path
    while True:
        if current.is_symlink():
            raise TodoStoreError(f"refusing symlinked todo path: {current}")
        if current == root or current == current.parent:
            return
        current = current.parent
