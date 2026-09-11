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

The same turn-shaped gap exists one level out: a dispatched unit ends,
the session reports it, and the turn ends without the result being
verified or the plan updated. So the reminder also carries any finished
dispatch the plan has not written down (`dispatch_outcomes`), each with
the verb it owes, plus `DISPATCH_COMPLETION_RULE`. Same boundary: the
lines are instruction and a pointer at a record, never evidence that a
unit did anything.
"""
from __future__ import annotations

from typing import Any

from .dispatch_outcomes import unacknowledged_outcomes
from .runtime_reader import TODO_UNCHANGED_STATUSES, read_omh_todo, todo_unchanged_text

try:  # Match a continuation claim on the router's own fold when OMH is installed.
    from omh.routing.visual_qa_cues import contains_cue_phrase as _contains_cue_phrase
except ImportError:  # pragma: no cover - standalone plugin hosts keep the local fold.
    _contains_cue_phrase = None

_MAX_ACTIVE_TEXT_CHARS = 80
# The reminder is one compact block, not a report: three outcome lines plus a
# count of the rest is enough to make the event impossible to miss without
# turning the per-turn context into a dispatch board.
_MAX_OUTCOME_LINES = 3

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

# The chain a finished dispatch owes. Written as an obligation for THIS turn
# because the failure it replaces was structurally polite: a status report,
# then the turn ended, and the unit's result sat unverified while the next
# item never started.
DISPATCH_COMPLETION_RULE = (
    "A finished dispatch is an event to act on in this turn: verify its "
    "result, record the outcome on the plan (done or blocked with reason), "
    "then run the recovery or the next item. Do not announce continuation "
    "you have not started."
)

# Closing phrasings that promise a next step. Matched only to ask whether the
# step was actually armed -- never to suppress the sentence.
CONTINUATION_CLAIM_PHRASES = (
    "계속 진행",
    "이어서 진행",
    "will continue",
    "continuing",
    "proceeding with",
)

CONTINUATION_CLAIM_FINDING = (
    "The previous turn announced a continuation but nothing resumed: the plan "
    "did not change, or a finished dispatch is still unacknowledged. Start the "
    "next step in this turn -- verify the finished result, record it on the "
    "plan, then dispatch or advance -- or say plainly that the work is stopped "
    "and why. A promise to continue is not a continuation."
)


def open_todo_reminder(
    *,
    omh_home: str = "",
    hermes_home: str = "",
    session_ref: str = "",
    outcomes: list[dict[str, Any]] | None = None,
) -> str:
    """The per-turn plan line, plus any dispatch outcome nobody wrote down.

    ``session_ref`` is the session whose turn is starting; its own plan is
    the one a completion claim must reconcile against, never another
    session's. ``outcomes`` lets a caller that already read them (the hook
    also needs the count for its honesty check) hand them in rather than
    making this scan the runtime a second time on the same turn.
    """
    lines: list[str] = []
    head = _open_plan_line(omh_home=omh_home, hermes_home=hermes_home, session_ref=session_ref)
    if head:
        lines.append(head)
    lines.extend(
        _dispatch_outcome_lines(
            unacknowledged_outcomes(omh_home, hermes_home, session_ref)
            if outcomes is None
            else outcomes
        )
    )
    return "\n".join(lines)


def _open_plan_line(*, omh_home: str, hermes_home: str, session_ref: str) -> str:
    todo = read_omh_todo(omh_home or None, hermes_home or None, session_ref=session_ref)
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


def _dispatch_outcome_lines(outcomes: list[dict[str, Any]]) -> list[str]:
    if not outcomes:
        return []
    lines = [
        "dispatch {run_ref}/{unit_id} ended {state}; next: {verb}".format(
            run_ref=outcome.get("run_ref", "unknown"),
            unit_id=outcome.get("unit_id", "unknown"),
            state=_outcome_state(outcome),
            verb=outcome.get("next", "verify_result"),
        )
        for outcome in outcomes[:_MAX_OUTCOME_LINES]
    ]
    remaining = len(outcomes) - len(lines)
    if remaining > 0:
        lines.append(f"(+{remaining} more)")
    lines.append(DISPATCH_COMPLETION_RULE)
    return lines


def _outcome_state(outcome: dict[str, Any]) -> str:
    for key in ("unit_state", "failure_kind", "status"):
        value = str(outcome.get(key, "") or "")
        if value:
            return value
    return "unknown"


def continuation_claim_without_resume(
    text: str, *, todo_stall_status: str, unacknowledged: int
) -> str | None:
    """A finding when a turn promises to continue and nothing was started.

    "계속 진행하겠습니다" with no auto-resume armed is the exact closing the
    incident produced. The claim itself is fine; what makes it a finding is
    the state it was made in -- the plan unchanged, or a finished dispatch
    still unacknowledged. Either one is enough, because either one means the
    promised step did not start. Pure: the caller supplies both states, so
    this never reads a file and never decides on its own that a run is stuck.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    # The reader owns which statuses mean "the checklist stopped moving"; a
    # copy of that pair here would let a third one be added there without this
    # guard ever noticing.
    stalled = str(todo_stall_status or "") in TODO_UNCHANGED_STATUSES
    outstanding = isinstance(unacknowledged, int) and unacknowledged > 0
    if not stalled and not outstanding:
        return None
    if not _claims_continuation(text):
        return None
    return CONTINUATION_CLAIM_FINDING


def _claims_continuation(text: str) -> bool:
    if _contains_cue_phrase is not None:
        return bool(_contains_cue_phrase(text, CONTINUATION_CLAIM_PHRASES))
    folded = text.casefold()
    return any(phrase.casefold() in folded for phrase in CONTINUATION_CLAIM_PHRASES)
