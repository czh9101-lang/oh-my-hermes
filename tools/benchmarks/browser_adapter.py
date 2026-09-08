#!/usr/bin/env python3
"""Bounded #1391 benchmark with deterministic controls and opt-in host timing.

Run: uv run python tools/benchmarks/browser_adapter.py --repetitions 20
Pass --host-qa with an explicit local QA driver to measure real host timing.
The control adapter is an in-memory host, not a real-browser speed claim.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from statistics import median
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event
from time import perf_counter_ns

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from _browser_adapter_support import Adapter, request
from omh.workflows.browser_adapter import acquisition_request, capability_snapshot
from omh.workflows.browser_lease_store import BrowserLeaseStore, BrowserSessionManager


def measure(callback, repetitions):
    values = []
    for _ in range(repetitions):
        started = perf_counter_ns()
        callback()
        values.append(perf_counter_ns() - started)
    return {"samples": repetitions, "min_ns": min(values), "median_ns": median(values), "max_ns": max(values)}


def controls(repetitions):
    with TemporaryDirectory(prefix="omh-browser-benchmark-") as tmp:
        adapter = Adapter()
        store = BrowserLeaseStore(Path(tmp) / "omh")
        manager = BrowserSessionManager(store, adapter, clock=lambda: 1000)
        lease = manager.acquire("owner", request())
        assert lease["status"] == "active"
        identity = acquisition_request("owner", adapter, request())
        pure = measure(lambda: capability_snapshot(adapter.cap, identity, 1000), repetitions)
        calls = adapter.calls.copy()
        before = (store.path.read_bytes(), store.path.stat().st_mtime_ns)

        def repeat():
            assert manager.acquire("owner", request()) == lease

        repeated = measure(repeat, repetitions)
        assert calls == adapter.calls
        assert before == (store.path.read_bytes(), store.path.stat().st_mtime_ns)
        action = {"operation": "act", "action": "read", "lease_id": lease["lease_id"],
                  "tab_id": "tab-1", "revision": 0, "handle": lease["page"]["elements"][0]["handle"]}

        def stale():
            assert manager.operate("owner", action)["reason"] == "stale_state"

        stale_result = measure(stale, repetitions)
        assert adapter.calls["act"] == 0
        manager.release("owner", lease["lease_id"])
        calls = adapter.calls.copy()
        cleanup = measure(lambda: manager.cleanup("owner"), repetitions)
        assert adapter.calls == calls and not adapter.live
        adapter.entered, adapter.proceed = Event(), Event()
        started = perf_counter_ns()
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(manager.acquire, "owner", request(task="concurrent"))
            assert adapter.entered.wait(5)
            try:
                assert manager.acquire("owner", request(task="concurrent"))["status"] == "pending"
            finally:
                adapter.proceed.set()
            concurrent = future.result(timeout=5)
        concurrency_ns = perf_counter_ns() - started
        manager.release("owner", concurrent["lease_id"])
        adapter.entered, adapter.proceed = None, None
        adapter.crash = True
        started = perf_counter_ns()
        try:
            manager.acquire("owner", request(task="crash"))
        except RuntimeError:
            crashed = True
        else:
            crashed = False
        assert crashed
        start_count = adapter.calls["start"]
        restarted = BrowserSessionManager(store, adapter, clock=lambda: 1000)
        for _ in range(repetitions):
            assert restarted.acquire("owner", request(task="crash"))["status"] == "unknown"
        assert adapter.calls["start"] == start_count
        restarted.cleanup("owner")
        assert not adapter.live
        crash_ns = perf_counter_ns() - started
        return {"adapter": "deterministic in-memory host control, not real browser latency",
                "pure_capability_validation": pure, "repeated_completed_task": repeated,
                "stale_handle": stale_result, "idempotent_cleanup": cleanup,
                "concurrency_reservation": {"samples": 1, "total_ns": concurrency_ns, "duplicate_starts": 0},
                "crash_recovery": {"samples": repetitions, "total_ns": crash_ns, "automatic_restarts": 0},
                "retained_store_bytes": store.path.stat().st_size, "callbacks": dict(adapter.calls), "live_resources": 0}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repetitions", type=int, default=20)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--host-qa", type=Path, help="Explicit local host QA driver; omitted runs controls only")
    options = parser.parse_args()
    if not 1 <= options.repetitions <= 100:
        parser.error("repetitions must be 1..100")
    if options.output_dir and options.host_qa is None:
        parser.error("--output-dir requires --host-qa")
    if options.host_qa is not None and not options.host_qa.is_file():
        parser.error("--host-qa must identify an existing local file")
    control = controls(options.repetitions)
    real = {"status": "not_run", "reason": "host_qa_not_supplied"}
    if options.host_qa is not None:
        qa_args = ["--output-dir", str(options.output_dir.resolve())] if options.output_dir else []
        with TemporaryDirectory(prefix="omh-browser-benchmark-") as temporary:
            env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": temporary,
                   "HERMES_HOME": str(Path(temporary) / "hermes"), "OMH_HOME": str(Path(temporary) / "omh")}
            result = subprocess.run([sys.executable, "-B", str(options.host_qa.resolve()), *qa_args],
                                    capture_output=True, text=True, env=env, timeout=65)
        if result.returncode:
            sys.stderr.write(result.stderr)
            raise SystemExit(result.returncode)
        real = json.loads(result.stdout)
        assert real["status"] == "observed"
    print(json.dumps({"schema_version": "browser_adapter_benchmark/v1", "control_arms": control,
                      "real_host": real, "measurement_definition": {
                          "host_callback_ns": "Measured capabilities/start/observe/act/release callback wall time, including IPC and browser startup/navigation",
                          "omh_overhead_ns": "Registered handler wall time minus measured host callbacks; includes validation, bounded store IO, and serialization",
                          "excluded": "Fixture mutation and screenshot are reported separately, not OMH validation overhead",
                          "percentages": "none"}}, sort_keys=True))


if __name__ == "__main__":
    main()
