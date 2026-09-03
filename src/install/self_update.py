"""Staged command-package updates; subprocess execution stays in this module."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any, Callable

try:
    from ..core.errors import OmhError
    from ..system.local_store import FileLockTimeout, file_lock
    from ..system.paths import managed_command_venv_dir, resolve_paths
    from .self_update_platform import SelfUpdatePlatform
    from .self_update_state import collect_garbage, commit_activation, generation_entry, interrupted_activation, load_state, mark_activation, mark_switched, migrate_legacy, pointer_target, record_pointer, recovery_target, restore_known_good, save_state, state_path, switch_current
    from .self_update_state_validation import require_generation_path
except ImportError:  # pragma: no cover - direct-source installer smoke.
    from core.errors import OmhError
    from system.local_store import FileLockTimeout, file_lock
    from system.paths import managed_command_venv_dir, resolve_paths
    from install.self_update_platform import SelfUpdatePlatform
    from install.self_update_state import collect_garbage, commit_activation, generation_entry, interrupted_activation, load_state, mark_activation, mark_switched, migrate_legacy, pointer_target, record_pointer, recovery_target, restore_known_good, save_state, state_path, switch_current
    from install.self_update_state_validation import require_generation_path

RESULT_SCHEMA_VERSION = "command_package_self_update/v1"
REENTRY_TIMEOUT_SECONDS = 600.0
Runner = Callable[..., subprocess.CompletedProcess[str]]


def _result(candidate: Path) -> dict[str, Any]:
    return {"schema_version": RESULT_SCHEMA_VERSION, "ok": False, "phase": "lock", "candidate": generation_entry(candidate), "staging": {"status": "skipped"}, "verification": {"status": "skipped"}, "migration": {"status": "skipped"}, "activation": {"status": "skipped"}, "post_activation": {"status": "skipped"}, "rollback": {"performed": False, "restored": ""}, "recovery": {"status": "skipped", "action": ""}, "cleanup": {"collected": []}}


def _python(generation: Path, platform: SelfUpdatePlatform) -> str:
    executable = "python.exe" if platform.is_windows else "python"
    return str(platform.scripts_dir(generation / "venv") / executable)


def _run(runner: Runner, command: list[str], *, env: dict[str, str] | None = None, timeout: float | None = None, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return runner(command, text=True, stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE if capture else None, env=env, timeout=timeout)


def _detail(value: subprocess.CompletedProcess[str], fallback: str) -> str:
    return str(value.stderr or value.stdout or fallback).strip()[:2000]


def _stage(candidate: Path, package_url: str, base_python: str, *, runner: Runner, json_mode: bool, platform: SelfUpdatePlatform) -> tuple[bool, str]:
    candidate.parent.mkdir(parents=True, exist_ok=True)
    try:
        built = _run(runner, [base_python, "-m", "venv", str(candidate / "venv")], capture=json_mode)
        if built.returncode:
            return False, _detail(built, "candidate venv creation failed")
        command = [_python(candidate, platform), "-m", "pip", "install", "--disable-pip-version-check", "--no-cache-dir", "--upgrade", package_url]
        if json_mode:
            command.insert(command.index("install") + 1, "-q")
        installed = _run(runner, command, capture=json_mode)
    except OSError as exc:
        return False, f"candidate staging failed: {exc}"
    return (True, "") if installed.returncode == 0 else (False, _detail(installed, "candidate pip install failed"))


def _smoke(candidate: Path, expected_version: str, *, runner: Runner, platform: SelfUpdatePlatform) -> tuple[bool, str]:
    env = dict(os.environ, OMH_SELF_UPDATE_GENERATION=str(candidate))
    with TemporaryDirectory(prefix="omh-self-update-") as temporary:
        env["OMH_HOME"] = str(Path(temporary) / "omh")
        env["HERMES_HOME"] = str(Path(temporary) / "hermes")
        python = _python(candidate, platform)
        checks = (([python, "-c", "import omh.cli"], "import"), ([python, "-m", "omh.cli", "--version"], "version"), ([python, "-m", "omh.cli", "update", "--command-package-updated", "--no-interactive", "--json"], "workflow-pack"))
        try:
            for command, name in checks:
                checked = _run(runner, command, env=env)
                if checked.returncode:
                    return False, f"{name} smoke failed: {_detail(checked, name + ' smoke failed')}"
                if name == "version" and expected_version and expected_version not in str(checked.stdout):
                    return False, f"version smoke failed: expected {expected_version}"
        except OSError as exc:
            return False, f"smoke execution failed: {exc}"
    skills = candidate / "skills"
    if not skills.is_dir() or not any(skills.iterdir()):
        return False, "workflow-pack smoke failed: candidate pack is empty"
    return True, ""



def _reentry_argv() -> list[str]:
    argv = [item for item in sys.argv[1:] if item != "--recover-known-good"]
    return argv if "--command-package-updated" in argv else [*argv, "--command-package-updated"]


def _reenter(root: Path, generation: Path, args: Any, runner: Runner, platform: SelfUpdatePlatform) -> tuple[bool, str]:
    trusted = require_generation_path(root, generation, None, "re-entry generation")
    pointed = pointer_target(root, platform=platform)
    if pointed is None or require_generation_path(root, pointed, None, "current pointer target") != trusted:
        raise OmhError("cannot re-enter an untrusted self-update generation")
    env = dict(os.environ, OMH_UPDATE_COMMAND_PACKAGE_REENTERED="1", OMH_SELF_UPDATE_GENERATION=str(trusted))
    try:
        checked = _run(runner, [_python(root / "current", platform), "-m", "omh.cli", *_reentry_argv()], env=env, timeout=REENTRY_TIMEOUT_SECONDS, capture=False)
    except (OSError, subprocess.TimeoutExpired):
        return False, "post-activation re-entry timed out"
    return (checked.returncode == 0, _detail(checked, "post-activation re-entry failed"))


def _running_generation(root: Path) -> Path | None:
    executable = Path(sys.executable).expanduser().resolve()
    generations = root / "generations"
    for generation in generations.iterdir() if generations.exists() else ():
        try:
            executable.relative_to(generation.resolve() / "venv")
            return generation
        except ValueError:
            continue
    return None


def _cleanup_candidate(root: Path, candidate: Path) -> None:
    trusted = require_generation_path(root, candidate, None, "candidate cleanup target")
    shutil.rmtree(trusted, ignore_errors=True)


def _switch(root: Path, target: Path, platform: SelfUpdatePlatform) -> None:
    if platform.is_windows:
        switch_current(root, target, platform=platform)
    else:
        switch_current(root, target)


def run_installer_self_update(args: Any, plan: dict[str, object], *, runner: Runner = subprocess.run, platform: SelfUpdatePlatform | None = None) -> dict[str, Any]:
    """Run stage, verification, pointer activation, rollback, and retention."""
    release = plan.get("release")
    package_url = str(getattr(release, "package_url", "") or "")
    expected_version = str(getattr(release, "version", "") or getattr(args, "version", "") or "")
    legacy = Path(str(plan.get("venv_dir") or managed_command_venv_dir() or Path(sys.executable).parent.parent))
    root = legacy.parent
    identifier = f"{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}-{hashlib.sha256(package_url.encode()).hexdigest()[:10]}"
    candidate = root / "generations" / identifier
    result = _result(candidate)
    paths = resolve_paths(getattr(args, "omh_home", None), getattr(args, "hermes_home", None), scope=getattr(args, "scope", None))
    selected = platform or SelfUpdatePlatform.host()
    try:
        with file_lock(state_path(root), timeout_seconds=0.0, private=True) as lock:
            if not lock.get("enforced"):
                raise OmhError("atomic self-update requires an enforced file lock; no changes were made")
            state = load_state(root, legacy)
            had_marker = isinstance(state.get("activation_in_progress"), dict)
            interrupted = interrupted_activation(root, state, platform=selected)
            if had_marker and interrupted is None:
                result["recovery"] = {"status": "ok", "action": "discarded_unswitched_candidate"}
            if interrupted is not None:
                return _recover_interrupted(root, state, interrupted, args, runner, selected, result)
            if bool(getattr(args, "recover_known_good", False)):
                return _recover_known_good(root, state, args, runner, selected, result)
            if not package_url:
                raise OmhError("cannot update command package because no package URL is available")
            staged, reason = _stage(candidate, package_url, str(plan.get("python") or sys.executable), runner=runner, json_mode=bool(getattr(args, "json", False)), platform=selected)
            result.update(phase="staging", staging={"status": "ok" if staged else "failed", "reason": reason})
            if not staged:
                _cleanup_candidate(root, candidate)
                return result
            verified, reason = _smoke(candidate, expected_version, runner=runner, platform=selected)
            result.update(phase="verification", verification={"status": "ok" if verified else "failed", "reason": reason})
            if not verified:
                _cleanup_candidate(root, candidate)
                return result
            try:
                migrate_legacy(root, state, paths, selected)
            except (OSError, ValueError, OmhError) as exc:
                _cleanup_candidate(root, candidate)
                result.update(phase="migration", migration={"status": "failed", "reason": str(exc)})
                return result
            return _activate(root, state, candidate, args, runner, selected, result)
    except FileLockTimeout as exc:
        raise OmhError("another omh update is already in progress; no changes were made") from exc


def _activate(root: Path, state: dict[str, Any], candidate: Path, args: Any, runner: Runner, platform: SelfUpdatePlatform, result: dict[str, Any]) -> dict[str, Any]:
    result["migration"] = {"status": "ok", "reason": "old pair migrated onto current"}
    previous = Path(str(state["active"]["path"]))
    mark_activation(state, candidate, previous)
    save_state(root, state)
    try:
        _switch(root, candidate, platform)
    except OmhError as exc:
        result.update(phase="activation", activation={"status": "failed", "reason": str(exc)})
        return result
    mark_switched(state)
    save_state(root, state)
    launcher = "present" if state["migration"].get("launcher_on_pointer") else "absent_manual_instruction"
    result["activation"] = {"status": "ok", "pointer": str(root / "current"), "launcher": launcher}
    passed, reason = _reenter(root, candidate, args, runner, platform)
    result.update(phase="post_activation", post_activation={"status": "ok" if passed else "failed", "reason": reason})
    if not passed:
        _switch(root, previous, platform)
        _reenter(root, previous, args, runner, platform)
        state["activation_in_progress"] = None
        record_pointer(state, root, previous)
        save_state(root, state)
        result["rollback"] = {"performed": True, "restored": previous.name}
        return result
    commit_activation(root, state, candidate)
    collect_garbage(root, state, result, running_generation=_running_generation(root))
    save_state(root, state)
    result.update(ok=True, phase="cleanup")
    return result


def _recover_interrupted(root: Path, state: dict[str, Any], interrupted: tuple[Path, Path], args: Any, runner: Runner, platform: SelfUpdatePlatform, result: dict[str, Any]) -> dict[str, Any]:
    candidate, previous = interrupted
    passed, _reason = _reenter(root, candidate, args, runner, platform)
    if passed:
        commit_activation(root, state, candidate)
        save_state(root, state)
        result.update(ok=True, phase="recovery", recovery={"status": "ok", "action": "completed_interrupted_activation"})
        return result
    _switch(root, previous, platform)
    _reenter(root, previous, args, runner, platform)
    state["activation_in_progress"] = None
    record_pointer(state, root, previous)
    save_state(root, state)
    result.update(phase="recovery", recovery={"status": "failed", "action": "rolled_back_interrupted_activation"}, rollback={"performed": True, "restored": previous.name})
    return result


def _recover_known_good(root: Path, state: dict[str, Any], args: Any, runner: Runner, platform: SelfUpdatePlatform, result: dict[str, Any]) -> dict[str, Any]:
    target = recovery_target(root, state)
    previous = Path(str(state["active"]["path"]))
    mark_activation(state, target, previous)
    save_state(root, state)
    _switch(root, target, platform)
    passed, reason = _reenter(root, target, args, runner, platform)
    if not passed:
        _switch(root, previous, platform)
        state["activation_in_progress"] = None
        save_state(root, state)
        result.update(phase="recovery", recovery={"status": "failed", "action": "restore_failed", "reason": reason})
        return result
    restore_known_good(root, state, target)
    save_state(root, state)
    result.update(ok=True, phase="recovery", recovery={"status": "ok", "action": "selected_known_good", "selected": target.name}, activation={"status": "skipped", "reason": "recovery"}, post_activation={"status": "ok", "reason": "known-good restored"})
    return result
