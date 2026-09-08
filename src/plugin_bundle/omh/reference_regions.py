"""Lexical reference regions shared by routing matchers, never output rewriting."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re
import unicodedata


_QUOTES = {"'": "'", '"': '"', "\u2018": "\u2019", "\u201c": "\u201d", "\u300c": "\u300d", "\u300e": "\u300f"}
_DELIMITERS = re.compile(r"\\.|`+|~+|['\"\u2018\u2019\u201c\u201d\u300c\u300d\u300e\u300f]", re.DOTALL)


@dataclass(frozen=True)
class ReferenceRegions:
    executable_text: str
    references: tuple[str, ...]


def executable_routing_text(message: str) -> str:
    """Project matching input without altering the original message or CJK clauses."""
    return reference_regions(message).executable_text


@lru_cache(maxsize=4096)
def reference_regions(message: str) -> ReferenceRegions:
    """Shield quotes, exact-length inline backticks, and backtick/tilde fences.

    Escapes consume the following character. Apostrophes between word characters
    are not delimiters. Fences start on an otherwise blank line and close on a
    line containing only the same delimiter, at least as long as the opener.
    Unclosed regions shield through EOF, leaving earlier executable text intact.
    Masking preserves character positions and line breaks, never joins words.
    """
    spans: list[tuple[int, int]] = []
    start = -1
    closer = ""
    fence = False
    for match in _DELIMITERS.finditer(message):
        token = match.group()
        if token.startswith("\\"):
            continue
        index, end = match.span()
        apostrophe = (
            token in {"'", "\u2019"}
            and index > 0
            and end < len(message)
            and message[index - 1].isalnum()
            and message[end].isalnum()
            # CJK clauses touch quotes without spaces; they are boundaries,
            # unlike apostrophes within Latin words or contractions.
            and unicodedata.east_asian_width(message[index - 1]) not in {"W", "F"}
            and unicodedata.east_asian_width(message[end]) not in {"W", "F"}
        )
        if apostrophe:
            continue
        if start >= 0:
            if fence:
                line_end = message.find("\n", end)
                if line_end == -1:
                    line_end = len(message)
                if (
                    token[0] == closer[0]
                    and len(token) >= len(closer)
                    and not message[message.rfind("\n", 0, index) + 1:index].strip()
                    and not message[end:line_end].strip()
                ):
                    spans.append((start, end))
                    start = -1
            elif token == closer:
                spans.append((start, end))
                start = -1
            continue
        if token in _QUOTES:
            start, closer, fence = index, _QUOTES[token], False
        elif token[0] in {"`", "~"}:
            fence = len(token) >= 3 and not message[message.rfind("\n", 0, index) + 1:index].strip()
            if token[0] == "`" or fence:
                start, closer = index, token
    if start >= 0:
        spans.append((start, len(message)))
    projected = list(message)
    references: list[str] = []
    for start, end in spans:
        references.append(message[start:end])
        projected[start:end] = [char if char in "\r\n" else " " for char in message[start:end]]
    return ReferenceRegions("".join(projected), tuple(references))
