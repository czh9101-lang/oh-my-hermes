#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# Run: uv run python tools/benchmarks/egress_attempts.py
"""Measure the real store and non-egress guard against the issue's fixed caps."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import sqlite3
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter_ns
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from omh.plugin_bundle.omh.egress_attempt_receipts import AttemptStore
from omh.plugin_bundle.omh.egress_attempts import Guard, Target


def _p95(samples: list[float]) -> float:
    return sorted(samples)[math.ceil(len(samples) * 0.95) - 1]


def main() -> int:
    non_egress: list[float] = []
    appends: list[float] = []
    digest = hashlib.sha256(b"synthetic benchmark metadata").hexdigest()
    with TemporaryDirectory(prefix="omh-egress-benchmark-") as temporary:
        home = Path(temporary) / ".omh"
        guard = Guard(
            home,
            {"send_probe": Target("send_probe", "message_send", "chat_channel", "channel", "body")},
            {}, "", lambda _name: None,
        )
        failure = AssertionError("non-egress must perform no receipt filesystem operations")
        with (
            patch("builtins.open", side_effect=failure),
            patch("io.open", side_effect=failure),
            patch("os.open", side_effect=failure),
            patch("os.stat", side_effect=failure),
            patch.object(sqlite3, "connect", side_effect=failure),
        ):
            for index in range(2000):
                started = perf_counter_ns()
                result = guard.pre(tool_name="read_file", session_id="benchmark", tool_call_id=str(index))
                non_egress.append((perf_counter_ns() - started) / 1_000_000)
                if result is not None:
                    raise AssertionError("non-egress behavior changed")
        store = AttemptStore(home)
        for index in range(200):
            started = perf_counter_ns()
            result = store.open_attempt(
                session_id="benchmark", tool_call_id=f"call-{index}", tool_name="send_probe",
                action_class="message_send", destination_class="chat_channel",
                request_fingerprint=digest, effect_id=digest, destination_digest=digest,
                payload_digest=digest, payload_bytes=32,
            )
            appends.append((perf_counter_ns() - started) / 1_000_000)
            if result["disposition"] != "created":
                raise AssertionError("benchmark call identity unexpectedly replayed")
        rows = store.public_rows()
        max_row_bytes = max(len(json.dumps(row, sort_keys=True, separators=(",", ":")).encode()) for row in rows)
        database = sqlite3.connect(store.database_path)
        try:
            plan = database.execute(
                "EXPLAIN QUERY PLAN SELECT attempt_id FROM egress_attempts WHERE session_ref=? AND tool_call_ref=?",
                ("session", "call"),
            ).fetchall()
        finally:
            database.close()
        indexed = all("SEARCH" in str(row[3]) and "INDEX" in str(row[3]) for row in plan)
    report: dict[str, object] = {
        "schema_version": "egress_attempt_benchmark/v1",
        "machine": {"platform": platform.platform(), "architecture": platform.machine(), "logical_cpus": os.cpu_count()},
        "sampling": {"method": "nearest_rank_p95", "warmup_excluded": 0, "cold_append_included": True},
        "non_egress": {"samples": len(non_egress), "p95_ms": _p95(non_egress), "receipt_io_operations": 0, "limit_ms": 0.1},
        "append": {"samples": len(appends), "p95_ms": _p95(appends), "cold_ms": appends[0], "limit_ms": 25.0},
        "storage": {"attempt_rows": len(rows), "max_row_bytes": max_row_bytes, "row_limit_bytes": 1024, "indexed_call_lookup": indexed},
        "claim_boundary": "Local metadata-store and guard timing only; no external handler or remote delivery was measured.",
    }
    passed = _p95(non_egress) < 0.1 and _p95(appends) <= 25 and max_row_bytes <= 1024 and indexed
    report["ratchets_pass"] = passed
    print(json.dumps(report, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
