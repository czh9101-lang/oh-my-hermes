#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

# ─── How to run ───
# 1. Install uv (if not installed):
#      curl -LsSf https://astral.sh/uv/install.sh | sh
# 2. Run directly (runner environment required):
#      uv run fake_adapter.py SCENARIO
# 3. Or make executable and run:
#      chmod +x fake_adapter.py && ./fake_adapter.py SCENARIO
# ─────────────────

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import pwd
import signal
import socket
import stat
import subprocess
import sys
from typing import Callable, TypeAlias, TypeVar


JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
_T = TypeVar("_T")


def _digest(value: dict[str, JsonValue]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _denied(operation: Callable[[], _T]) -> bool:
    try:
        operation()
    except OSError:
        return True
    return False


def _write_result(*, child_result: str = "pass", artifact_type: str = "cross_harness_adapter_fixture_result/v1", stale: bool = False, process_status: str = "exit", exit_code: int | None = 0) -> None:
    request = json.loads(Path(os.environ["OMH_ADAPTER_REQUEST"]).read_text(encoding="utf-8"))
    result: dict[str, JsonValue] = {
        "schema_version": "cross_harness_adapter_result/v1",
        "request_digest": "0" * 64 if stale else os.environ["OMH_REQUEST_DIGEST"],
        "fixture_id": request["fixture_id"],
        "adapter_id": request["adapter_id"],
        "capability_id": request["capability_id"],
        "evidence_class": "runtime",
        "observation_state": "observed",
        "actual_machine": {"sandboxed": True},
        "facts": {"expected_block_observed": True},
        "skill_events": [{"id": "fixture-skill", "result": "pass"}],
        "tool_events": [{"id": "fixture-tool", "result": "pass"}],
        "child_results": [{"id": "fixture-child", "result": child_result}],
        "artifact_type": artifact_type,
        "artifact_hash": "0" * 64,
        "process_status": process_status,
        "exit_code": exit_code,
        "side_effects": [],
    }
    result["artifact_hash"] = _digest({key: value for key, value in result.items() if key != "artifact_hash"})
    output = Path(os.environ["OMH_ADAPTER_OUTPUT"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, sort_keys=True), encoding="utf-8")


def _spawn_descendant(*, inherit_stdio: bool) -> None:
    pid_path = Path(os.environ["OMH_DESCENDANT_PID"])
    read_fd, write_fd = os.pipe()
    child_code = (
        "import os,signal,sys,tempfile;"
        "target=sys.argv[1];ready=int(sys.argv[2]);"
        "fd,tmp=tempfile.mkstemp(dir=os.path.dirname(target) or '.');"
        "os.write(fd,str(os.getpid()).encode());os.fsync(fd);os.close(fd);"
        "os.replace(tmp,target);os.write(ready,b'1');os.close(ready);signal.pause()"
    )
    stream = None if inherit_stdio else subprocess.DEVNULL
    subprocess.Popen(
        (sys.executable, "-c", child_code, str(pid_path), str(write_fd)),
        stdin=subprocess.DEVNULL,
        stdout=stream,
        stderr=stream,
        close_fds=True,
        pass_fds=(write_fd,),
    )
    os.close(write_fd)
    try:
        if os.read(read_fd, 1) != b"1":
            raise RuntimeError("descendant_pid_not_published")
    finally:
        os.close(read_fd)


def _abort() -> None:
    if sys.platform.startswith("linux"):
        import ctypes

        # Piped core handlers ignore RLIMIT_CORE=0. Keep real SIGABRT without
        # making this synthetic crash wait for the host's dump collector.
        prctl = ctypes.CDLL(None, use_errno=True).prctl
        prctl.argtypes = (ctypes.c_int, *([ctypes.c_ulong] * 4))
        prctl.restype = ctypes.c_int
        if prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
    os.abort()


def main() -> int:
    scenario = sys.argv[1]
    if scenario == "--version":
        canary = os.environ.get("OMH_VERSION_CANARY")
        if canary and not _denied(lambda: Path(canary).write_text("changed", encoding="utf-8")):
            return 12
        print("fake-adapter 1.0")
        return 0
    launch_probe = os.environ.get("OMH_LAUNCH_PROBE")
    if launch_probe:
        Path(launch_probe).write_text("launched", encoding="utf-8")
    if scenario == "timeout":
        _write_result(process_status="timeout", exit_code=None)
        read_fd, write_fd = os.pipe()
        heartbeat = Path(os.environ["HOME"]).parent / "work" / "descendant-heartbeat"
        child_code = "import os,signal,sys;open(sys.argv[1],'wb').write(b'alive');os.write(int(sys.argv[2]),b'1');signal.pause()"
        descendant = subprocess.Popen((sys.executable, "-c", child_code, str(heartbeat), str(write_fd)), close_fds=True, pass_fds=(write_fd,))
        if pid_path := os.environ.get("OMH_DESCENDANT_PID"):
            Path(pid_path).write_text(str(descendant.pid), encoding="utf-8")
        os.close(write_fd)
        os.read(read_fd, 1)
        os.close(read_fd)
        signal.pause()
    if scenario == "crash":
        _write_result(process_status="crash", exit_code=None)
        _abort()
    if scenario == "crash-descendant":
        _write_result(process_status="crash", exit_code=None)
        _spawn_descendant(inherit_stdio=False)
        _abort()
    if scenario == "descendant-exit-closed":
        _spawn_descendant(inherit_stdio=False)
    if scenario == "descendant-exit-inherited":
        _spawn_descendant(inherit_stdio=True)
    if scenario == "session-escape-denied":
        child_code = "import os,sys; operation=getattr(os,sys.argv[1]);\ntry: operation() if sys.argv[1]=='setsid' else operation(0,0)\nexcept PermissionError: sys.exit(0)\nsys.exit(13)"
        for operation in ("setsid", "setpgid"):
            completed = subprocess.run((sys.executable, "-c", child_code, operation), check=False)
            if completed.returncode != 0:
                return 13
    if scenario == "misleading-zero":
        return 0
    if scenario == "environment-clean" and "AWS_SECRET_ACCESS_KEY" in os.environ:
        return 3
    if scenario == "credential-read-denied":
        probe = os.environ.get("OMH_READ_PROBE")
        target = Path(probe) if probe else Path(pwd.getpwuid(os.getuid()).pw_dir) / ".ssh" / "id_rsa"
        if not _denied(target.read_bytes):
            return 4
    if scenario == "network-denied":
        if not _denied(lambda: socket.create_connection(("127.0.0.1", int(os.environ["OMH_NETWORK_PORT"])), timeout=0.2)):
            return 5
    if scenario == "network-allowed":
        connection = socket.create_connection(("127.0.0.1", int(os.environ["OMH_NETWORK_PORT"])), timeout=0.2)
        connection.close()
    if scenario == "outside-write-denied":
        if not _denied(lambda: Path(os.environ["OMH_OUTSIDE_PROBE"]).write_text("escape", encoding="utf-8")):
            return 6
    if scenario == "noisy":
        print("x" * 8192)
        print("y" * 8192, file=sys.stderr)
    if scenario == "oversized-output":
        print("x" * 1_100_000)
        print("y" * 1_100_000, file=sys.stderr)
    if scenario == "fd-denied":
        if not _denied(lambda: os.fstat(int(os.environ["OMH_FD_PROBE"]))):
            return 7
    if scenario == "dirty-worktree-denied":
        target = Path(os.environ["OMH_DIRTY_PROBE"])
        if not _denied(target.read_bytes):
            return 8
        if not _denied(lambda: target.write_text("changed", encoding="utf-8")):
            return 9
    if scenario == "scratch-write":
        target = Path(os.environ["HOME"]).parent / "work" / "product.bin"
        target.write_bytes(b"observed")
        if target.read_bytes() != b"observed":
            return 10
    if scenario == "inventory-symlink":
        target = Path(os.environ["OMH_FILE_CANARY"])
        (Path(os.environ["HOME"]).parent / "work" / "linked-canary").symlink_to(target)
    if scenario == "artifact-symlink":
        target = Path(os.environ["OMH_FILE_CANARY"])
        output = Path(os.environ["OMH_ADAPTER_OUTPUT"])
        output.symlink_to(target)
        if not stat.S_ISLNK(output.lstat().st_mode):
            return 11
        return 0
    if scenario == "wrong-artifact":
        _write_result(artifact_type="wrong/v1")
    elif scenario == "stale-artifact":
        _write_result(stale=True)
    elif scenario == "preseed-stale":
        _write_result()
        os.utime(os.environ["OMH_ADAPTER_OUTPUT"], (1, 1))
    elif scenario == "partial-child":
        _write_result(child_result="partial")
    elif scenario == "flaky" and os.environ["OMH_REPETITION_INDEX"] == "2":
        _write_result(child_result="fail")
    else:
        _write_result()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
