"""Per-turn reconciliation reminder for an open plan todo.

A model that declares a plan (todo init) and then answers "all done" in
chat while the checklist still shows open items leaves the HUD lying to
the user ('작업 다됐다는데 투두는 이렇게 남아있네'). No keyword trigger
can catch every phrasing of a completion claim, but an OPEN plan is a
state, not a phrasing — so while one exists, every turn's context
carries one compact line that binds completion claims to the checklist.
The reminder is awareness (instruction), never state and never
evidence; it stops the moment the plan is all done or cleared.
"""
from __future__ import annotations

from .runtime_reader import read_omh_todo

_MAX_ACTIVE_TEXT_CHARS = 80

TODO_RECONCILIATION_RULE = (
    "Before claiming this work is finished, reconcile the checklist with "
    "omh_todo: mark completed items done, keep exactly one item active, and "
    "either finish the remaining items or say which stay open and why. A "
    "completion claim in chat while the HUD checklist shows open items is a "
    "visible contradiction. Todo updates are declarations, never execution "
    "evidence."
)


def open_todo_reminder(*, omh_home: str = "", session_ref: str = "") -> str:
    """One compact context line while an established plan has open items.

    ``session_ref`` is the session whose turn is starting; its own plan is
    the one a completion claim must reconcile against, never another
    session's.
    """
    todo = read_omh_todo(omh_home or None, session_ref=session_ref)
    if todo.get("status") != "established":
        return ""
    counts = todo.get("counts") if isinstance(todo.get("counts"), dict) else {}
    done = counts.get("done")
    total = counts.get("total")
    if not isinstance(done, int) or not isinstance(total, int) or total <= 0 or done >= total:
        return ""
    items = todo.get("items") if isinstance(todo.get("items"), list) else []
    active = next(
        (
            str(item.get("text", ""))[:_MAX_ACTIVE_TEXT_CHARS]
            for item in items
            if isinstance(item, dict) and item.get("state") == "active"
        ),
        "",
    )
    head = f"[OMH plan todo] {done}/{total} done"
    if active:
        head = f"{head} · active: {active}"
    return f"{head}. {TODO_RECONCILIATION_RULE}"
