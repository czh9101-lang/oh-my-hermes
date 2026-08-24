from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import stat
import sys
from typing import Final, Never


MAX_LOOP_OBSERVATION_BYTES: Final = 65_536
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


def read_loop_observation_json(source: str) -> dict[str, object]:
    """Read one bounded strict JSON object from stdin or a stable regular file."""
    encoded = (
        _read_stdin_bytes()
        if source == "-"
        else _read_stable_file(Path(source).expanduser())
    )
    if len(encoded) > MAX_LOOP_OBSERVATION_BYTES:
        raise ValueError(
            f"observation JSON exceeds {MAX_LOOP_OBSERVATION_BYTES} bytes"
        )
    try:
        text = encoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("observation JSON must be valid UTF-8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_number,
        )
    except RecursionError as exc:
        raise ValueError("observation JSON nesting is too deep") from exc
    if not isinstance(value, dict):
        raise ValueError("observation JSON must be an object")
    return value


def _read_stdin_bytes() -> bytes:
    try:
        return sys.stdin.buffer.read(MAX_LOOP_OBSERVATION_BYTES + 1)
    except AttributeError:
        text = sys.stdin.read(MAX_LOOP_OBSERVATION_BYTES + 1)
        try:
            return text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("observation JSON must be valid UTF-8") from exc


def _read_stable_file(path: Path) -> bytes:
    if not _NOFOLLOW:
        raise ValueError(
            "observation JSON path must be a stable regular file"
        )
    flags = os.O_RDONLY | _CLOEXEC | _NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            raise ValueError(
                "observation JSON path must be a stable regular file"
            ) from exc
        raise
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(
                "observation JSON path must be a stable regular file"
            )
        if before.st_size > MAX_LOOP_OBSERVATION_BYTES:
            raise ValueError(
                f"observation JSON exceeds {MAX_LOOP_OBSERVATION_BYTES} bytes"
            )
        chunks: list[bytes] = []
        remaining = MAX_LOOP_OBSERVATION_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if _file_identity(before) != _file_identity(after):
        raise ValueError(
            "observation JSON path must be a stable regular file"
        )
    return b"".join(chunks)


def _file_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite_number(value: str) -> Never:
    raise ValueError(f"non-finite JSON number is unsupported: {value}")
