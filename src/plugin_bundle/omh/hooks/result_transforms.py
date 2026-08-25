"""Composed ``transform_tool_result`` seam.

This is the single registered entry for the hook; it chains the two OMH
result transforms in a fixed order:

1. Code-mode discipline annotation — the first ``execute_code`` result of a
   session gains a bounded guidance block under its own JSON key
   (``code_mode_guidance.py``).
2. Full-width diff band padding — tool-result diffs get their painted lines
   padded to a uniform band (``diff_presentation.py``).

Both transforms are fail-open: anything either one declines passes through
to the next, and ``None`` from both leaves the host result untouched. The
order matters only when one result matches both (an execute_code result
whose output embeds a diff): annotating first lets the diff pass still see
and pad the final string.
"""

from __future__ import annotations

from typing import Any

from ..code_mode_guidance import annotate_execute_code_result
from .diff_presentation import transform_tool_result as _pad_diff_result


def transform_tool_result(**kwargs: Any) -> str | None:
    """Return the transformed result string, or ``None`` to leave it alone."""
    annotated = annotate_execute_code_result(
        tool_name=kwargs.get("tool_name"),
        result=kwargs.get("result"),
        session_id=str(kwargs.get("session_id", "") or ""),
    )
    if annotated is not None:
        kwargs = {**kwargs, "result": annotated}
    padded = _pad_diff_result(**kwargs)
    if padded is not None:
        return padded
    return annotated
