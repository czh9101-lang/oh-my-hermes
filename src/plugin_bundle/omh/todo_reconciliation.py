"""Per-turn reconciliation reminder for an open plan todo.

A model that declares a plan (todo init) and then answers "all done" in
chat while the checklist still shows open items leaves the HUD lying to
the user ('작업 다됐다는데 투두는 이렇게 남아있네'). No keyword trigger
can catch every phrasing of a completion claim, but an OPEN plan is a
state, not a phrasing — so while one exists, every turn's context
carries one compact line that binds completion claims to the checklist.
The reminder is awareness (instruction), never state and never
evidence; it stops the moment the plan is all done or cleared.

While the reader's stall finding stands, the same line also carries how
long the checklist has been unchanged. That covers the other half of the
failure: a plan does not only part ways with the session by being
contradicted, it parts ways by being left behind while the session talks
about something else.
"""
from __future__ import annotations

from .runtime_reader import read_omh_todo, todo_unchanged_text

_MAX_ACTIVE_TEXT_CHARS = 80

# The TUI has shown a stopped checklist to the PERSON since the in-flight
# liveness signal landed; the session driving that checklist never saw the
# finding anywhere. A plan that sits with one item active for hours while the
# session answers about other things and ends its turns is the observed
# failure, and it is not a completion claim, so the rule below cannot catch
# it. This sentence states what the reader observed and asks for one sentence
# of accounting. It rides a turn that is already happening and starts
# nothing, which is exactly why it is a context line and not a driver.
TODO_UNCHANGED_RULE = (
    "The checklist standing still is an observation, not evidence that the work "
    "failed: say where the plan stands, and if nothing is blocking it, move it."
)

# The half that was missing. The reconciliation rule below guards a COMPLETION
# CLAIM -- it fires when the model says it is done while items are open. It has
# nothing to say about the far more common way a plan dies: the model answers
# whatever arrived, reports, and ends the turn with the list untouched. A real
# run spent 36 minutes that way, and an earlier one ended with 3 of 5 phases
# pending while every notification got a courteous status reply.
#
# `omh_todo` exists to carry a goal ACROSS turns. A checklist that only ever
# catches a contradiction is a detector, not a plan. So this line states the
# obvious thing nobody was saying: open items mean the work is not finished.
#
# The termination criterion is explicit and it is what keeps this bounded --
# this is a long loop with a stop condition, not an unbounded one. It ends when
# every item is done, or when an item is recorded blocked with its reason. It
# does not end because a turn happened to produce a paragraph.
TODO_CONTINUATION_RULE = (
    "Open items mean this plan is not finished. Unless something is blocking "
    "it, advance the next item in this turn rather than ending on a status "
    "report. This stops when every item is done or an item is recorded blocked "
    "with its reason -- not when a turn has produced an answer."
)

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
    unchanged = todo_unchanged_text(todo)
    if not unchanged:
        return f"{head}. {TODO_CONTINUATION_RULE} {TODO_RECONCILIATION_RULE}"
    stall = todo.get("stall") if isinstance(todo.get("stall"), dict) else {}
    # "no tool call in flight" is only true of the quiet finding. The busy one
    # is the more interesting reading and saying the wrong one would make the
    # line refutable on its face.
    observed = (
        "unchanged {age} while calls kept running".format(age=unchanged)
        if stall.get("status") == "unchanged_while_busy"
        else "unchanged {age}, no tool call in flight".format(age=unchanged)
    )
    return (
        f"{head} · {observed}. "
        f"{TODO_CONTINUATION_RULE} {TODO_RECONCILIATION_RULE} {TODO_UNCHANGED_RULE}"
    )
