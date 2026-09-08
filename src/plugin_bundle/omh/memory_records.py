"""Reviewed project-memory records as a prefetch section.

The reviewed store (`memory/records/`, `project_memory_record/v2`) and the
provider used to be two products that never met. `omh memory recall` ranked the
records into a pack for a coding handoff, and the Hermes provider served only
memory blocks, so a record the reviewer admitted never reached a Hermes turn --
measured on a machine three weeks into daily use: zero records rendered, ever.

This module is the bridge. It reads what `read_approved_records` already
returns inside the Hermes process -- replay-eligible v2 records with a matching
immutable review -- ranks them for the turn, cuts them to a budget, and renders
one section the provider appends to its pack. No model call, no network, no
import of the `omh` package (the Hermes process cannot import it).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .hermes_memory import read_approved_records

DEFAULT_RECORD_RENDER_BUDGET_CHARS = 2400
DEFAULT_RECORD_LIMIT = 6
RECORD_SUMMARY_LIMIT_CHARS = 500

_CJK_RUN = re.compile(r"[぀-ヿ㐀-鿿가-힯]+")
_ASCII_WORD = re.compile(r"[a-z0-9][a-z0-9_\-]+")


def read_project_memory_records(homes: list[Path] | tuple[Path, ...]) -> list[dict[str, Any]]:
    """Eligible records from every home, first home wins on a shared record id."""
    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    for home in homes:
        for record in read_approved_records(home):
            record_id = str(record.get("record_id", "") or "")
            if not record_id or record_id in seen:
                continue
            seen.add(record_id)
            records.append(record)
    return records


def rank_project_memory_records(records: list[dict[str, Any]], query: str = "") -> list[dict[str, Any]]:
    """Query overlap first, then newest approval; ties keep the record id order.

    The same shape `omh memory recall` uses -- token overlap with the summary,
    a bonus for tag hits -- reduced to what a prefetch needs. A query with no
    indexable tokens ranks by recency alone rather than excluding everything.
    """
    query_tokens = _tokens(query)
    # Three stable sorts, least significant first: id order breaks ties,
    # newest approval wins among equal scores, score decides.
    by_id = sorted(records, key=lambda record: str(record.get("record_id", "")))
    by_recency = sorted(by_id, key=lambda record: str(record.get("approved_at", "") or ""), reverse=True)
    return sorted(by_recency, key=lambda record: _score(record, query_tokens), reverse=True)


def _score(record: dict[str, Any], query_tokens: set[str]) -> int:
    if not query_tokens:
        return 0
    tags = {str(tag).strip().lower() for tag in record.get("tags", []) or [] if str(tag).strip()}
    record_tokens = _tokens(" ".join([str(record.get("summary", "")), str(record.get("record_type", "")), " ".join(tags)]))
    return len(query_tokens & record_tokens) * 10 + len(query_tokens & tags) * 5


def render_memory_records(
    records: list[dict[str, Any]],
    *,
    budget_chars: int = DEFAULT_RECORD_RENDER_BUDGET_CHARS,
    limit: int = DEFAULT_RECORD_LIMIT,
) -> tuple[str, int]:
    """Bound the entire section, including escaped text and aggregate omissions.

    Return no section when even its omission report cannot fit. The count
    includes only complete record elements, never omission metadata.
    """
    if not records:
        return "", 0
    lines, rendered = ["<memory_records>"], 0
    omissions = {"render_budget_exhausted": 0, "record_limit_reached": 0}
    closing = "</memory_records>"
    used = len(lines[0]) + 1 + len(closing)
    # Reserve both possible reports once, not an unbounded line per record.
    reserve = sum(len(f'\n  <omitted count="{len(records)}" reason="{reason}" />') for reason in omissions)
    for record in records:
        if rendered >= max(limit, 0):
            omissions["record_limit_reached"] += 1
            continue
        element = _render_record(record)
        if used + 1 + len(element) + (reserve if len(records) > 1 else 0) > max(budget_chars, 0):
            omissions["render_budget_exhausted"] += 1
            continue
        used += 1 + len(element)
        rendered += 1
        lines.append(element)
    lines.extend(f'  <omitted count="{count}" reason="{reason}" />' for reason, count in omissions.items() if count)
    lines.append(closing)
    text = "\n".join(lines)
    return (text, rendered) if len(text) <= max(budget_chars, 0) else ("", 0)


def _render_record(record: dict[str, Any]) -> str:
    summary = str(record.get("summary", "") or "")[:RECORD_SUMMARY_LIMIT_CHARS]
    approved = str(record.get("approved_at", "") or "")[:10]
    return (
        f'  <record id="{_attribute(record.get("record_id", ""))}" type="{_attribute(record.get("record_type", ""))}"'
        f' approved="{_attribute(approved)}">{_text(summary)}</record>'
    )


def _tokens(value: str) -> set[str]:
    lowered = str(value or "").lower()
    tokens = set(_ASCII_WORD.findall(lowered))
    for run in _CJK_RUN.findall(lowered):
        tokens.add(run)
        tokens.update(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


def _attribute(value: object) -> str:
    return _text(str(value or "")).replace('"', "&quot;")


def _text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


__all__ = [
    "DEFAULT_RECORD_LIMIT",
    "DEFAULT_RECORD_RENDER_BUDGET_CHARS",
    "rank_project_memory_records",
    "read_project_memory_records",
    "render_memory_records",
]
