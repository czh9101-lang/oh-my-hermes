"""Temporary PR #1419 diagnostic; remove with windows-journal-diagnostics.yml.

Run from the target checkout, not necessarily the script's checkout:
  python <script> --revision <full HEAD sha> --case cold --storage default --out trace.json

One untouched original test runs once in a fresh, bounded child. Both storage
arms use invocation-owned roots: TemporaryDirectory(dir=None) versus an explicit
directory under RUNNER_TEMP. Only the child's TemporaryDirectory constructor is
redirected; TEMP/TMP, tempfile.tempdir, production and suite settings are not.
No prewarming, GC control, clock replacement, timing subtraction or new limits.
Native calls delegate unchanged, including the test's original fault subclass.
Spans are inclusive and nested/overlapping; NEVER sum them as elapsed time.
Profiling adds cost. Callback-body cost is only a lower bound, not a correction.
Fresh isolated jobs cannot rule out contamination in the original full shard.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import ctypes
from ctypes import wintypes
import gc
import hashlib
import importlib
from itertools import count
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


CASES = {
    "cold": "test_windows_commit_failure_blocks_handler_and_does_not_mint_attempt",
    "contention": "test_writer_lock_fails_within_the_bound_without_appending",
}
CONTRACTS = (
    "src/plugin_bundle/omh/egress_attempt_receipts.py",
    "src/plugin_bundle/omh/egress_attempts.py",
    "tests/test_egress_attempt_receipts.py",
    "tests/_local_package.py",
)


def drive_identity(path: Path) -> dict[str, object]:
    """Only drive identity/capacity; never a username, volume label or full path."""
    result: dict[str, object] = {"device_id": path.stat().st_dev,
              "capacity_bytes": shutil.disk_usage(path).total,
              "free_bytes": shutil.disk_usage(path).free}
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        volume = ctypes.create_unicode_buffer(261)
        filesystem = ctypes.create_unicode_buffer(261)
        serial, maximum, flags = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
        get_path = kernel.GetVolumePathNameW
        get_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        get_path.restype = wintypes.BOOL
        get_info = kernel.GetVolumeInformationW
        get_info.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
                            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                            ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
        get_info.restype = wintypes.BOOL
        if not get_path(str(path.resolve()), volume, len(volume)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not get_info(volume.value, None, 0, ctypes.byref(serial), ctypes.byref(maximum),
                        ctypes.byref(flags), filesystem, len(filesystem)):
            raise ctypes.WinError(ctypes.get_last_error())
        result.update(drive=Path(volume.value).drive, volume_serial=serial.value,
                      filesystem=filesystem.value, filesystem_flags=flags.value)
    return result


def failure_metadata(error) -> dict[str, object]:
    """Do not export exception text, source lines, frame locals or test payloads."""
    kind, _, traceback = error
    frames = []
    while traceback is not None:
        code = traceback.tb_frame.f_code
        frames.append({"file": Path(code.co_filename).name,
                       "function": code.co_name, "line": traceback.tb_lineno})
        traceback = traceback.tb_next
    return {"type": kind.__name__, "frames": frames}


class MetadataResult(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.problems = []

    def addFailure(self, test, err):
        self.problems.append({"outcome": "failure", **failure_metadata(err)})
        super().addFailure(test, err)

    def addError(self, test, err):
        self.problems.append({"outcome": "error", **failure_metadata(err)})
        super().addError(test, err)


class Trace:
    def __init__(self, test_name: str):
        self.test_name = test_name
        self.origin = time.perf_counter_ns()
        self.events = []
        self.event_ids = count()
        self.stack = []
        self.timer_marks = []
        self.deadline = None
        self.callback_count = 0
        self.callback_ns = 0
        self.gc_starts = {}
        self.python_names = {
            "open_attempt", "_connect", "_validate_attempt_input", "_validate_row",
            "before_handler", "sync", "mkdir", "exists", "_registered_handler",
            "register", "commit", "execute", "rollback", "close", "setUp",
        }

    def begin(self, key, label, now, frame):
        span = {"id": next(self.event_ids), "parent_id": self.stack[-1][1]["id"] if self.stack else None,
                "operation": label, "start_ns": now - self.origin,
                "duration_ns": None, "thread_cpu_ns": None, "raised": None,
                "file": Path(frame.f_code.co_filename).name, "line": frame.f_lineno}
        self.events.append(span)
        self.stack.append((key, span, time.thread_time_ns()))

    def end(self, key, now, raised=None):
        if self.stack and self.stack[-1][0] == key:
            _, span, cpu = self.stack.pop()
            span.update(duration_ns=now - self.origin - span["start_ns"],
                        thread_cpu_ns=time.thread_time_ns() - cpu, raised=raised)

    def profile(self, frame, event, arg):
        now = time.perf_counter_ns()
        self.callback_count += 1
        name = frame.f_code.co_name
        if event in ("call", "return"):
            if name in self.python_names or name.startswith("assert") or name == self.test_name:
                key = (id(frame), "python")
                if event == "call":
                    label = "Python " + name
                    if name == "execute" and frame.f_code.co_filename == __file__:
                        # SQL is static test/source syntax; omit parameters and literal values.
                        sql = frame.f_locals["sql"].strip().split()
                        label = "SQL " + " ".join(sql[:2])
                    self.begin(key, label, now, frame)
                    if name == "assertLess" and frame.f_back.f_code.co_name == self.test_name:
                        elapsed, limit = frame.f_locals["a"], frame.f_locals["b"]
                        self.deadline = {"elapsed_seconds": elapsed, "limit_seconds": limit,
                                         "comparison_passed": elapsed < limit,
                                         "caller_line": frame.f_back.f_lineno}
                else:
                    # Python profile return does not identify exception unwinds; use
                    # the original TestResult and its failure locations, not guessed passes.
                    self.end(key, now)
        elif event in ("c_call", "c_return", "c_exception"):
            native_name = getattr(arg, "__name__", "")
            owner = getattr(arg, "__self__", None)
            label = None
            if isinstance(owner, (sqlite3.Connection, sqlite3.Cursor)):
                label = "native sqlite " + native_name
            elif getattr(arg, "__module__", "") in ("posix", "nt") and native_name in {
                "mkdir", "stat", "lstat", "open", "close", "fsync", "unlink", "rmdir",
            }:
                label = "native fs " + native_name
            elif native_name == "connect" and getattr(arg, "__module__", "") == "_sqlite3":
                label = "native sqlite connect"
            if label:
                key = (id(frame), label)
                if event == "c_call":
                    self.begin(key, label, now, frame)
                else:
                    self.end(key, now, event == "c_exception")
            if event == "c_return" and arg is time.monotonic and name == self.test_name:
                self.timer_marks.append({"boundary": "start" if not self.timer_marks else "stop",
                                         "perf_counter_ns": now - self.origin,
                                         "profile_callback_body_ns_so_far": self.callback_ns,
                                         "source_line": frame.f_lineno})
        self.callback_ns += time.perf_counter_ns() - now

    def gc_event(self, phase, info):
        now = time.perf_counter_ns()
        generation = info["generation"]
        if phase == "start":
            self.gc_starts[generation] = now
        else:
            start = self.gc_starts.pop(generation, None)
            if start is not None:
                self.events.append({"id": next(self.event_ids), "parent_id": None,
                                    "operation": f"GC generation {generation}",
                                    "start_ns": start - self.origin, "duration_ns": now - start,
                                    "collected": info["collected"]})


def clock_samples() -> dict[str, object]:
    # AFTER the only measured test. No sleeps, timer changes, or test warmups.
    samples = {}
    for name in ("monotonic", "perf_counter"):
        clock = getattr(time, name)
        values = [clock() for _ in range(2048)]
        deltas = [b - a for a, b in zip(values, values[1:])]
        positive = [delta for delta in deltas if delta > 0]
        samples[name] = {"zero_deltas": deltas.count(0), "negative_deltas": sum(d < 0 for d in deltas),
                         "minimum_positive_seconds": min(positive) if positive else None,
                         "sample_count": len(values)}
    return samples


def worker(args) -> int:
    root = Path.cwd()
    trace = Trace(CASES[args.case])
    result = MetadataResult()
    connect = sqlite3.connect
    temporary_directory = tempfile.TemporaryDirectory
    temporary_paths = []

    def traced_connect(*positional, **keywords):
        base = keywords.pop("factory", sqlite3.Connection)

        class TracedConnection(base):
            def execute(self, sql, *values, **options):
                return super().execute(sql, *values, **options)

        return connect(*positional, **keywords, factory=TracedConnection)

    with ExitStack() as cleanup:
        def owned_temporary(*positional, **keywords):
            keywords["dir"] = args.scratch_root
            holder = temporary_directory(*positional, **keywords)
            temporary_paths.append(Path(holder.name))
            cleanup.callback(holder.cleanup)
            return holder

        cleanup.enter_context(patch.object(tempfile, "TemporaryDirectory", new=owned_temporary))
        sys.path.insert(0, str(root / "tests"))
        tests = importlib.import_module("test_egress_attempt_receipts")
        test = tests.EgressAttemptStoreTests(CASES[args.case])
        threads_before = threading.active_count()
        with patch.object(sqlite3, "connect", new=traced_connect):
            gc.callbacks.append(trace.gc_event)
            sys.setprofile(trace.profile)
            try:
                test.run(result)
            finally:
                sys.setprofile(None)
                gc.callbacks.remove(trace.gc_event)
        threads_after = threading.active_count()

    report = {
        "test": CASES[args.case], "successful": result.wasSuccessful(),
        "tests_run": result.testsRun, "problems": result.problems, "skips": len(result.skipped),
        "original_deadline_assertion": trace.deadline, "original_timer_boundaries": trace.timer_marks,
        "timer_boundary_precision": "perf_counter stamps observe original monotonic c_return events; exact original elapsed is the assertLess operand, not their difference.",
        "assertion_boundary": "Original test aborts at first failed assertion; later assertions are NOT claimed executed.",
        "events": trace.events, "unfinished_profile_spans": len(trace.stack),
        "instrumentation": {"profile_callback_count": trace.callback_count,
                            "profile_callback_body_ns_lower_bound": trace.callback_ns,
                            "cost_boundary": "Includes callback-body cost only, across setup/test/cleanup; excludes dispatch, subclass and GC callback costs. No subtraction. No uninstrumented control."},
        "post_test_clock_samples": clock_samples(),
        "temporary_fixture_roots_removed": all(not path.exists() for path in temporary_paths),
        "thread_count_before": threads_before, "thread_count_after": threads_after,
    }
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if result.wasSuccessful() and not result.skipped else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True, help="Full immutable SHA of the target checkout")
    parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--storage", choices=("default", "runner-temp"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--scratch-root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.scratch_root is not None:
        try:
            return worker(args)
        except Exception:
            args.out.write_text(json.dumps({"diagnostic_error": failure_metadata(sys.exc_info())},
                                           indent=2) + "\n", encoding="utf-8")
            return 2

    args.out = args.out.resolve()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    report = {"schema_version": "temporary_windows_journal_diagnostic/v1",
              "revision": args.revision, "case": args.case, "storage": args.storage,
              "python": sys.version, "python_implementation": platform.python_implementation(),
              "sqlite": sqlite3.sqlite_version, "native_system": platform.system(),
              "native_release": platform.release(), "machine": platform.machine(),
              "clocks": {name: vars(time.get_clock_info(name)) for name in ("monotonic", "perf_counter", "thread_time")},
              "diagnostic_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "claim_boundary": "Native results only for the named system. Inclusive spans overlap; never sum. One cold test, not a reproduction of full-shard history. Storage comparison is evidence, not threshold approval."}
    exit_code = 2
    try:
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], timeout=10).decode().strip()
        if args.revision != revision:
            raise ValueError("Expected full immutable checkout SHA")
        hashes = {}
        for path in CONTRACTS:
            committed = subprocess.check_output(["git", "show", f"{revision}:{path}"], timeout=10)
            # Checkout may use CRLF on Windows; validate normalized text, hash git bytes.
            if Path(path).read_bytes().replace(b"\r\n", b"\n") != committed.replace(b"\r\n", b"\n"):
                raise ValueError("Target contract differs from its immutable git object")
            hashes[path] = hashlib.sha256(committed).hexdigest()
        report["contract_sha256"] = hashes
        parent = None if args.storage == "default" else os.environ["RUNNER_TEMP"]
        with tempfile.TemporaryDirectory(prefix="omh-journal-diagnostic-", dir=parent) as temporary:
            scratch = Path(temporary)
            report["selected_drive"] = drive_identity(scratch)
            report["default_temp_drive"] = drive_identity(Path(tempfile.gettempdir()))
            if "RUNNER_TEMP" in os.environ:
                report["runner_temp_drive"] = drive_identity(Path(os.environ["RUNNER_TEMP"]))
                report["default_temp_equals_runner_temp"] = Path(tempfile.gettempdir()).resolve() == Path(os.environ["RUNNER_TEMP"]).resolve()
            child_out = scratch / "worker.json"
            command = [sys.executable, str(Path(__file__).resolve()), "--revision", revision,
                       "--case", args.case, "--storage", args.storage,
                       "--out", str(child_out), "--scratch-root", str(scratch)]
            try:
                child = subprocess.run(command, timeout=60, capture_output=True, check=False)
                exit_code = child.returncode
                report["child_exit_code"] = exit_code
                # Never export stdout/stderr, which could contain paths or payloads.
                report["child_output_bytes"] = {"stdout": len(child.stdout), "stderr": len(child.stderr)}
                if child_out.exists():
                    report["trace"] = json.loads(child_out.read_text(encoding="utf-8"))
                else:
                    report["diagnostic_error"] = "child_did_not_write_metadata"
                    exit_code = 2
            except subprocess.TimeoutExpired:
                # subprocess.run kills and waits for this only child before root cleanup.
                report["diagnostic_error"] = "child_timeout_60_seconds"
                exit_code = 2
        report["invocation_root_removed"] = not scratch.exists()
    except Exception:
        report["diagnostic_error"] = failure_metadata(sys.exc_info())
        exit_code = 2
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"case": args.case, "storage": args.storage, "exit_code": exit_code}))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
