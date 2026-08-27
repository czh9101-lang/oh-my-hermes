from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import textwrap
import unittest

from _local_package import load_local_package

load_local_package()

from omh.coding.skill_load_observation import SkillLoadProbeRequest  # noqa: E402


_FAKE = r'''#!/usr/bin/env python3
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone

args = sys.argv[1:]
if args[:6] != ["--safe-mode", "--ignore-user-config", "--ignore-rules", "skills", "inventory", "--protocol"]:
    raise SystemExit(91)
protocol = args[6]
nonce = args[args.index("--nonce") + 1]
expected_digest = args[args.index("--expected-digest") + 1]
tool_fingerprint = args[args.index("--tool-fingerprint") + 1]
mode = __import__("pathlib").Path(sys.argv[0]).stem
expected = [] if mode == "not-applicable" else ["alpha", "beta"]
observed = {
    "all": expected,
    "partial": expected[:1],
    "none": [],
    "unexpected": [*expected, "gamma"],
    "unexpected-only": ["gamma"],
    "not-applicable": [],
}.get(mode, expected)
now = datetime.now(timezone.utc).replace(microsecond=0)
payload = {
    "schema_version": protocol,
    "nonce": nonce,
    "expected_digest": expected_digest,
    "inventory_digest": hashlib.sha256(json.dumps(sorted(observed), separators=(",", ":")).encode()).hexdigest(),
    "observed_skills": observed,
    "tool_fingerprint": tool_fingerprint,
    "runtime_fingerprint": "b" * 64,
    "observed_at": now.isoformat().replace("+00:00", "Z"),
    "expires_at": (now + timedelta(seconds=60)).isoformat().replace("+00:00", "Z"),
}
if mode == "unsupported":
    print("unsupported inventory command", file=sys.stderr)
    raise SystemExit(2)
if mode == "error":
    print("SECRET_RAW_LOG must not escape", file=sys.stderr)
    raise SystemExit(9)
if mode == "malformed":
    print('{"prompt":"SECRET_PROMPT"}')
    raise SystemExit(0)
if mode == "duplicate":
    payload["observed_skills"] = ["alpha", "alpha"]
if mode == "nonce-mismatch":
    payload["nonce"] = "f" * 64
if mode == "digest-mismatch":
    payload["expected_digest"] = "e" * 64
if mode == "inventory-digest-mismatch":
    payload["inventory_digest"] = "d" * 64
if mode == "tool-mismatch":
    payload["tool_fingerprint"] = "c" * 64
if mode == "expired":
    payload["observed_at"] = (now - timedelta(seconds=120)).isoformat().replace("+00:00", "Z")
    payload["expires_at"] = (now - timedelta(seconds=60)).isoformat().replace("+00:00", "Z")
if mode == "stale":
    payload["observed_at"] = (now - timedelta(seconds=600)).isoformat().replace("+00:00", "Z")
    payload["expires_at"] = (now + timedelta(seconds=60)).isoformat().replace("+00:00", "Z")
if mode == "forbidden":
    payload["raw_log"] = "SECRET_CREDENTIAL"
print(json.dumps(payload))
'''


class SkillLoadObservationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory(prefix="omh-skill-load-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hermes = self.root / "hermes.py"
        self.hermes.write_text(textwrap.dedent(_FAKE), encoding="utf-8")
        self.hermes.chmod(0o755)
    def executable(self, path: Path, runtime_digit: str) -> Path:
        path.write_text(
            textwrap.dedent(_FAKE).replace(
                '"runtime_fingerprint": "b" * 64',
                f'"runtime_fingerprint": "{runtime_digit}" * 64',
            ),
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path
    def request(self, mode: str, expected: tuple[str, ...] = ("beta", "alpha"), **changes: object) -> SkillLoadProbeRequest:
        executable = self.root / f"{mode}.py"
        executable.write_bytes(self.hermes.read_bytes())
        executable.chmod(0o755)
        values: dict[str, object] = {
            "expected_skills": expected,
            "hermes": str(executable),
            "timeout_seconds": 5.0,
            "env": {"PATH": "/usr/bin:/bin"},
        }
        values.update(changes)
        return SkillLoadProbeRequest(**values)  # type: ignore[arg-type]
