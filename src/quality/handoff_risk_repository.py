"""Read-only Git index/tree/stat observations; never hash working file contents.

Do not replace this with status/diff-files: index refresh can read secret files
and invoke repository clean filters. ls-tree reads tree objects, never blobs.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import subprocess
from threading import Thread
from typing import IO, Final

from .handoff_risk_model import (
    MAX_ENTRIES, MAX_METADATA_BYTES, Finding, FindingId, ScanError, Signal, finding,
)

_GIT_TIMEOUT: Final = 5
_SECRET: Final = re.compile(r"(?:^\.env(?:\.|$)|^\.?(?:credentials|secrets?)(?:\.|$)|^id_(?:rsa|dsa|ecdsa|ed25519)(?:\.|$)|\.(?:pem|key|p12|pfx)$)", re.I)
_STAT: Final = re.compile(rb"  ctime: (\d+):(\d+)\n  mtime: (\d+):(\d+)\n  dev: \d+\s+ino: \d+\n  uid: \d+\s+gid: \d+\n  size: (\d+)\s+flags: ([0-9a-f]+)\n")


@dataclass(frozen=True, slots=True)
class RepositoryObservation:
    findings: tuple[Finding, ...]
    branch: str | None
    tracked_count: int
    dirty_count: int
    untracked_count: int


def _git(repo: Path, args: tuple[str, ...]) -> tuple[int, bytes]:
    """Bound pipe retention and execution; kill and reap on overflow or timeout."""
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR") if key in os.environ}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_OPTIONAL_LOCKS="0", GIT_ATTR_NOSYSTEM="1", LC_ALL="C")
    argv = ["git", "--no-optional-locks", "--no-lazy-fetch", "-c", "core.fsmonitor=false",
            "-c", f"core.hooksPath={os.devnull}", "-C", str(repo), *args]
    chunks: list[bytes] = []
    try:
        with subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=False) as process:
            # PIPE guarantees stdout; retaining the reference proves ownership to typing.
            stream: IO[bytes] | None = process.stdout
            assert stream is not None
            read_bytes: Callable[[int], bytes] = stream.read

            def collect() -> None:
                data = read_bytes(MAX_METADATA_BYTES + 1)
                chunks.append(data)
                if len(data) > MAX_METADATA_BYTES:
                    process.kill()

            reader = Thread(target=collect, name="omh-risk-git-reader")
            reader.start()
            try:
                code = process.wait(timeout=_GIT_TIMEOUT)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                _ = process.wait()
                raise ScanError("repository_timeout") from exc
            finally:
                reader.join()
    except OSError as exc:
        raise ScanError("repository_unreadable") from exc
    data = b"".join(chunks)
    if len(data) > MAX_METADATA_BYTES:
        raise ScanError("metadata_oversized")
    return code, data


def _required(repo: Path, args: tuple[str, ...]) -> bytes:
    code, data = _git(repo, args)
    if code:
        raise ScanError("repository_invalid")
    return data


def _paths(data: bytes) -> list[bytes]:
    if data and not data.endswith(b"\0"):
        raise ScanError("repository_invalid")
    paths = data.split(b"\0")[:-1]
    if len(paths) > MAX_ENTRIES:
        raise ScanError("metadata_oversized")
    return paths


def _secret(path: bytes) -> bool:
    name = os.fsdecode(path).rsplit("/", 1)[-1].lower()
    if name.endswith((".example", ".sample", ".template", ".dist")):
        return False
    return _SECRET.search(name) is not None


def observe_repository(repo: Path) -> RepositoryObservation:
    """Compare staged tree entries and conservative stat changes, not blob values."""
    if _required(repo, ("rev-parse", "--is-inside-work-tree")).strip() != b"true":
        raise ScanError("repository_invalid")
    # Resolve root through Git so subdirectory requests still cover the repository.
    root = Path(os.fsdecode(_required(repo, ("rev-parse", "--show-toplevel")).rstrip(b"\n")))
    branch_code, branch_data = _git(root, ("symbolic-ref", "--quiet", "--short", "HEAD"))
    if branch_code not in (0, 1):
        raise ScanError("repository_invalid")
    branch = os.fsdecode(branch_data.rstrip(b"\n")) if branch_code == 0 else None
    head_code, head = _git(root, ("rev-parse", "--verify", "HEAD"))
    tree: dict[bytes, bytes] = {}
    metadata_bytes = len(head) + len(branch_data)
    if head_code == 0:
        tree_data = _required(root, ("ls-tree", "-r", "-z", "HEAD"))
        metadata_bytes += len(tree_data)
        for entry in _paths(tree_data):
            header, path = entry.split(b"\t", 1)
            mode, _, oid = header.split(b" ")
            tree[path] = mode + b" " + oid
    elif branch is None or head_code != 128:
        raise ScanError("repository_invalid")
    index = _required(root, ("ls-files", "--stage", "--debug", "-z"))
    others = _required(root, ("ls-files", "--others", "--exclude-standard", "-z"))
    metadata_bytes += len(index) + len(others)
    if metadata_bytes > MAX_METADATA_BYTES:
        raise ScanError("metadata_oversized")
    untracked = _paths(others)
    tracked: set[bytes] = set()
    dirty: set[bytes] = set()
    secret: list[bytes] = []
    offset = 0
    while offset < len(index):
        end = index.find(b"\0", offset)
        if end < 0:
            raise ScanError("repository_invalid")
        header, path = index[offset:end].split(b"\t", 1)
        mode, oid, stage = header.split(b" ")
        record = _STAT.match(index, end + 1)
        if record is None:
            raise ScanError("repository_invalid")
        offset = record.end()
        tracked.add(path)
        if len(tracked) > MAX_ENTRIES:
            raise ScanError("metadata_oversized")
        if _secret(path):
            secret.append(path)
        if stage != b"0" or tree.get(path) != mode + b" " + oid:
            dirty.add(path)
        # lstat never follows a symlink to secret contents. Submodules are opaque.
        if mode != b"160000":
            try:
                info = (root / os.fsdecode(path)).lstat()
            except FileNotFoundError:
                dirty.add(path)
                continue
            csec, cnsec, msec, mnsec, size, _ = record.groups()
            if (info.st_size != int(size)
                    or info.st_mtime_ns != int(msec) * 1000000000 + int(mnsec)
                    or info.st_ctime_ns != int(csec) * 1000000000 + int(cnsec)
                    or stat.S_ISLNK(info.st_mode) != (mode == b"120000")):
                dirty.add(path)
    dirty.update(tree.keys() - tracked)
    if len(tracked | set(untracked) | dirty) > MAX_ENTRIES:
        raise ScanError("metadata_oversized")
    findings: list[Finding] = []
    groups: tuple[tuple[FindingId, Iterable[bytes]], ...] = (
        ("dirty_worktree", dirty), ("untracked_files", untracked), ("tracked_secret_path", secret),
    )
    for kind, paths in groups:
        digest_source = b"\0".join(sorted(paths))
        if digest_source:
            findings.append(finding(Signal(kind, digest_source)))
    return RepositoryObservation(tuple(findings), branch, len(tracked), len(dirty), len(untracked))
