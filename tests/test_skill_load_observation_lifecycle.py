from __future__ import annotations

import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from _skill_load_observation_support import SkillLoadObservationTestCase
from omh.coding import skill_load_observation as skill_load_module
from omh.coding.skill_load_observation import SkillLoadProbeRequest, probe_skill_load


class SkillLoadObservationTests(SkillLoadObservationTestCase):
    def test_atomic_replace_after_fingerprint_never_executes_unbound_bytes(self) -> None:
        original = self.executable(self.root / "all.py", "1")
        replacement = self.executable(self.root / "replacement.py", "9")
        real_run = skill_load_module._run_inventory_process

        def replace_then_run(*args: object, **kwargs: object):
            os.replace(replacement, original)
            return real_run(*args, **kwargs)  # type: ignore[arg-type]

        request = SkillLoadProbeRequest(
            expected_skills=("alpha", "beta"), hermes=str(original), timeout_seconds=5.0,
            env={"PATH": os.defpath},
        )
        with patch.object(skill_load_module, "_run_inventory_process", replace_then_run):
            payload = probe_skill_load(request, confirmed=True)
        self.assertEqual(payload["probe_status"], "observed")
        self.assertEqual(payload["runtime_fingerprint"], "1" * 64)

    def test_atomic_replace_and_restore_cannot_evade_executable_binding(self) -> None:
        original = self.executable(self.root / "all.py", "1")
        replacement = self.executable(self.root / "replacement.py", "9")
        backup = self.root / "original-backup.py"
        real_run = skill_load_module._run_inventory_process

        def swap_run_restore(*args: object, **kwargs: object):
            os.replace(original, backup)
            os.replace(replacement, original)
            try:
                return real_run(*args, **kwargs)  # type: ignore[arg-type]
            finally:
                os.replace(original, replacement)
                os.replace(backup, original)

        request = SkillLoadProbeRequest(
            expected_skills=("alpha", "beta"), hermes=str(original), timeout_seconds=5.0,
            env={"PATH": os.defpath},
        )
        with patch.object(skill_load_module, "_run_inventory_process", swap_run_restore):
            payload = probe_skill_load(request, confirmed=True)
        self.assertEqual(original.read_text(encoding="utf-8").count('"1" * 64'), 1)
        self.assertEqual(payload["probe_status"], "observed")
        self.assertEqual(payload["runtime_fingerprint"], "1" * 64)

    def test_symlink_target_replace_after_fingerprint_never_executes_replacement(self) -> None:
        target = self.executable(self.root / "all.py", "1")
        replacement = self.executable(self.root / "replacement.py", "9")
        link = self.root / "selected-hermes"
        link.symlink_to(target)
        real_run = skill_load_module._run_inventory_process

        def replace_target_then_run(*args: object, **kwargs: object):
            os.replace(replacement, target)
            return real_run(*args, **kwargs)  # type: ignore[arg-type]

        request = SkillLoadProbeRequest(
            expected_skills=("alpha", "beta"), hermes=str(link), timeout_seconds=5.0,
            env={"PATH": os.defpath},
        )
        with patch.object(skill_load_module, "_run_inventory_process", replace_target_then_run):
            payload = probe_skill_load(request, confirmed=True)
        self.assertEqual(payload["probe_status"], "observed")
        self.assertEqual(payload["runtime_fingerprint"], "1" * 64)

    def test_executable_snapshot_is_removed_after_success_timeout_and_error(self) -> None:
        snapshots: list[Path] = []
        real_run = skill_load_module._run_inventory_process

        def capture_then_run(*args: object, **kwargs: object):
            snapshots.append(Path(str(args[1])))
            return real_run(*args, **kwargs)  # type: ignore[arg-type]

        with patch.object(skill_load_module, "_run_inventory_process", capture_then_run):
            self.assertEqual(
                probe_skill_load(self.request("all"), confirmed=True)["probe_status"],
                "observed",
            )
            self.assertEqual(
                probe_skill_load(self.request("error"), confirmed=True)["reason_code"],
                "inventory_process_error",
            )
            sleeper = self.root / "sleep.py"
            sleeper.write_text(
                "#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n",
                encoding="utf-8",
            )
            sleeper.chmod(0o755)
            timed_out = probe_skill_load(
                self.request("all", hermes=str(sleeper), timeout_seconds=0.05),
                confirmed=True,
            )
            self.assertEqual(timed_out["reason_code"], "inventory_timeout")

        def capture_then_raise(*args: object, **_kwargs: object):
            snapshots.append(Path(str(args[1])))
            raise RuntimeError("synthetic process adapter failure")

        with patch.object(skill_load_module, "_run_inventory_process", capture_then_raise):
            with self.assertRaisesRegex(RuntimeError, "synthetic process adapter failure"):
                probe_skill_load(self.request("all"), confirmed=True)

        self.assertEqual(len(snapshots), 4)
        for snapshot in snapshots:
            self.assertFalse(snapshot.exists())
            self.assertFalse(snapshot.parent.exists())

    def test_path_selected_executable_fingerprint_binds_the_binary_that_ran(self) -> None:
        fingerprints: list[str] = []
        for label, runtime_digit in (("first", "1"), ("second", "2")):
            binary_dir = self.root / label
            binary_dir.mkdir()
            executable = binary_dir / ("hermes.py" if os.name == "nt" else "hermes")
            self.executable(executable, runtime_digit)
            env = {"PATH": os.pathsep.join((str(binary_dir), os.defpath))}
            if os.name == "nt":
                env["PATHEXT"] = ".PY"
            payload = probe_skill_load(
                SkillLoadProbeRequest(
                    expected_skills=("alpha", "beta"),
                    hermes="hermes",
                    env=env,
                ),
                confirmed=True,
            )
            self.assertEqual(payload["probe_status"], "observed")
            self.assertEqual(payload["runtime_fingerprint"], runtime_digit * 64)
            fingerprints.append(str(payload["tool_fingerprint"]))

        self.assertNotEqual(fingerprints[0], fingerprints[1])

    def test_explicit_symlink_binds_the_resolved_executable(self) -> None:
        target = self.root / "all.py"
        target.write_bytes(self.hermes.read_bytes())
        target.chmod(0o755)
        link = self.root / "selected-hermes"
        link.symlink_to(target)
        direct = probe_skill_load(self.request("all", hermes=str(target)), confirmed=True)
        selected = probe_skill_load(self.request("all", hermes=str(link)), confirmed=True)
        self.assertEqual(direct["probe_status"], "observed")
        self.assertEqual(selected["probe_status"], "observed")
        self.assertEqual(direct["tool_fingerprint"], selected["tool_fingerprint"])

    def test_unresolvable_or_unreadable_executable_fails_closed_without_path_leak(self) -> None:
        secret_path = self.root / "SECRET_EXECUTABLE_PATH" / "hermes"
        missing = probe_skill_load(
            SkillLoadProbeRequest(expected_skills=("alpha",), hermes=str(secret_path)),
            confirmed=True,
        )
        self.assertEqual(
            (missing["probe_status"], missing["reason_code"]),
            ("probe_error", "inventory_executable_unavailable"),
        )
        self.assertNotIn("SECRET_EXECUTABLE_PATH", json.dumps(missing))
        self.assertNotIn("observed_skills", missing)

        executable = self.request("all")
        with patch.object(skill_load_module.os, "read", side_effect=PermissionError):
            unreadable = probe_skill_load(executable, confirmed=True)
        self.assertEqual(
            (unreadable["probe_status"], unreadable["reason_code"]),
            ("probe_error", "inventory_executable_unavailable"),
        )

    def test_confirmation_is_mandatory_and_default_construction_starts_nothing(self) -> None:
        request = self.request("all")
        self.assertFalse((self.root / "called").exists())
        with self.assertRaisesRegex(RuntimeError, "confirmation"):
            probe_skill_load(request, confirmed=False)

    def test_child_recursion_marker_refuses_probe_before_spawn(self) -> None:
        with patch.dict(os.environ, {"OMH_ISOLATED_HERMES_DEPTH": "1"}):
            with self.assertRaisesRegex(RuntimeError, "depth is limited"):
                probe_skill_load(self.request("all"), confirmed=True)

    def test_timeout_fails_closed(self) -> None:
        sleeper = self.root / "sleep.py"
        sleeper.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n", encoding="utf-8")
        sleeper.chmod(0o755)
        payload = probe_skill_load(self.request("all", hermes=str(sleeper), timeout_seconds=0.05), confirmed=True)
        self.assertEqual((payload["probe_status"], payload["reason_code"]), ("probe_error", "inventory_timeout"))


if __name__ == "__main__":
    unittest.main()
