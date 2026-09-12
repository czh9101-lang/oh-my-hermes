"""Pure, fence-aware extraction and one-time stamping of authored release notes."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
from typing import Final

MAX_CHANGELOG_BYTES: Final = 2 * 1024 * 1024
MAX_NOTES_BYTES: Final = 256 * 1024
_VERSION: Final = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
_HEADING: Final = re.compile(r"## ([^\r\n]+)(?:\r?\n)?$")
_FENCE: Final = re.compile(r" {0,3}(`{3,}|~{3,})([^\r\n]*)")


class ChangelogError(ValueError):
    """A bounded machine category, never authored text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class Section:
    name: str
    start: int
    body_start: int
    end: int


def release_version(value: str) -> str:
    version = value.removeprefix('v')
    if len(version) > 64 or _VERSION.fullmatch(version) is None:
        raise ChangelogError('invalid_version')
    return version


def parse_changelog(raw: bytes) -> tuple[str, tuple[Section, ...]]:
    """Keep original newlines and offsets; headings inside fences are content."""
    if len(raw) > MAX_CHANGELOG_BYTES:
        raise ChangelogError('changelog_too_large')
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ChangelogError('invalid_utf8') from exc
    headings: list[tuple[str, int, int]] = []
    fence_char = ''
    fence_length = 0
    offset = 0
    lines = text.split('\n')
    for index, content in enumerate(lines):
        line = content + ('\n' if index < len(lines) - 1 else '')
        fence = _FENCE.fullmatch(line.rstrip('\r\n'))
        if fence_char:
            if fence and fence[1][0] == fence_char and len(fence[1]) >= fence_length and not fence[2].strip():
                fence_char = ''
        elif fence and not (fence[1][0] == '`' and '`' in fence[2]):
            fence_char, fence_length = fence[1][0], len(fence[1])
        else:
            heading = _HEADING.fullmatch(line)
            if heading:
                name = heading[1]
                if name[:1].isdigit():
                    pieces = name.split(' - ')
                    if len(pieces) != 2 or _VERSION.fullmatch(pieces[0]) is None:
                        raise ChangelogError('invalid_version_heading')
                    try:
                        stamp_date = date.fromisoformat(pieces[1])
                    except ValueError as exc:
                        raise ChangelogError('invalid_date') from exc
                    if stamp_date.isoformat() != pieces[1]:
                        raise ChangelogError('invalid_date')
                headings.append((name, offset, offset + len(line)))
        offset += len(line)
    sections = tuple(
        Section(name, start, body_start, headings[i + 1][1] if i + 1 < len(headings) else len(text))
        for i, (name, start, body_start) in enumerate(headings)
    )
    return text, sections


def _body(text: str, section: Section) -> bytes:
    lines = text[section.body_start:section.end].replace('\r\n', '\n').split('\n')
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    body = '\n'.join(lines) + '\n' if lines else ''
    encoded = body.encode('utf-8')
    if len(encoded) > MAX_NOTES_BYTES:
        raise ChangelogError('notes_too_large')
    return encoded


def _one(sections: tuple[Section, ...], name: str) -> Section:
    matches = tuple(section for section in sections if section.name == name or
                    (name != 'Unreleased' and section.name.startswith(name + ' - ')))
    if len(matches) != 1:
        raise ChangelogError('section_missing' if not matches else 'section_duplicate')
    return matches[0]


def extract_notes(raw: bytes, version: str) -> bytes:
    """Return the exact authored interior with canonical LF section boundaries."""
    target = release_version(version)
    text, sections = parse_changelog(raw)
    body = _body(text, _one(sections, target))
    if not body:
        raise ChangelogError('notes_empty')
    return body


def stamp_changelog(raw: bytes, version: str, today: date) -> tuple[bytes, bytes]:
    """Return a minimal replacement, or unchanged bytes for a recoverable rerun."""
    target = release_version(version)
    text, sections = parse_changelog(raw)
    unreleased = _one(sections, 'Unreleased')
    pending = _body(text, unreleased)
    targets = tuple(section for section in sections if section.name.startswith(target + ' - '))
    if targets:
        body = extract_notes(raw, target)
        if pending:
            raise ChangelogError('ambiguous_preparation')
        return raw, body
    if not pending:
        raise ChangelogError('notes_empty')
    # Only replace the selected section; unrelated historical bytes stay intact.
    newline = '\r\n' if text[unreleased.start:unreleased.body_start].endswith('\r\n') else '\n'
    replacement = (
        f'## Unreleased{newline}{newline}## {target} - {today.isoformat()}{newline}'
        + text[unreleased.body_start:unreleased.end]
    )
    stamped = (text[:unreleased.start] + replacement + text[unreleased.end:]).encode('utf-8')
    if len(stamped) > MAX_CHANGELOG_BYTES:
        raise ChangelogError('changelog_too_large')
    return stamped, pending
