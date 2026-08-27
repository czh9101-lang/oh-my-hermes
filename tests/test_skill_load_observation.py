from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import textwrap
import unittest
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()

from omh.coding import skill_load_observation as skill_load_module  # noqa: E402
from omh.coding.skill_load_observation import (  # noqa: E402
    InventoryNonceLedger,
    SkillLoadProbeRequest,
    expected_skills_digest,
    probe_skill_load,
    skill_load_observation_is_fresh,
    validate_skill_load_observation,
)
from omh.evidence.labels import SKILL_LOAD_PROBE_LABELS, SKILL_LOAD_STATE_LABELS  # noqa: E402
from omh.quality.harness_quality import skill_load_evidence_state  # noqa: E402


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


class SkillLoadObservationTests(unittest.TestCase):
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

    def test_fake_protocol_all_partial_none_unexpected_and_not_applicable(self) -> None:
        cases = (
            ("all", ("alpha", "beta"), "all_loaded", (), ()),
            ("partial", ("alpha", "beta"), "partially_loaded", ("beta",), ()),
            ("none", ("alpha", "beta"), "none_loaded", ("alpha", "beta"), ()),
            ("unexpected", ("alpha", "beta"), "all_loaded", (), ("gamma",)),
            ("unexpected-only", ("alpha", "beta"), "none_loaded", ("alpha", "beta"), ("gamma",)),
            ("not-applicable", (), "not_applicable", (), ()),
        )
        for mode, expected, state, missing, unexpected in cases:
            with self.subTest(mode=mode):
                payload = probe_skill_load(self.request(mode, expected), confirmed=True)
                self.assertEqual(payload["probe_status"], "observed")
                self.assertEqual(payload["load_state"], state)
                self.assertEqual(payload["expected_skills"], list(expected))
                self.assertEqual(payload["missing_skills"], list(missing))
                self.assertEqual(payload["unexpected_skills"], list(unexpected))
                self.assertEqual(validate_skill_load_observation(payload), [])

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

    def test_unsupported_and_process_error_are_inventory_free_and_redacted(self) -> None:
        for mode, status, reason in (
            ("unsupported", "unsupported", "inventory_protocol_unavailable"),
            ("error", "probe_error", "inventory_process_error"),
        ):
            with self.subTest(mode=mode):
                payload = probe_skill_load(self.request(mode), confirmed=True)
                self.assertEqual((payload["probe_status"], payload["reason_code"]), (status, reason))
                for key in ("load_state", "expected_skills", "observed_skills", "missing_skills", "unexpected_skills", "inventory_digest"):
                    self.assertNotIn(key, payload)
                self.assertNotIn("SECRET", json.dumps(payload))

    def test_malformed_binding_duplicate_expiry_and_forbidden_fields_fail_closed(self) -> None:
        reasons = {
            "malformed": "inventory_response_malformed",
            "duplicate": "inventory_response_malformed",
            "nonce-mismatch": "inventory_nonce_mismatch",
            "digest-mismatch": "inventory_expected_digest_mismatch",
            "inventory-digest-mismatch": "inventory_digest_mismatch",
            "tool-mismatch": "inventory_response_malformed",
            "expired": "inventory_response_expired",
            "stale": "inventory_response_stale",
            "forbidden": "inventory_response_malformed",
        }
        for mode, reason in reasons.items():
            with self.subTest(mode=mode):
                payload = probe_skill_load(self.request(mode), confirmed=True)
                self.assertEqual(payload["probe_status"], "probe_error")
                self.assertEqual(payload["reason_code"], reason)
                self.assertNotIn("observed_skills", payload)
                self.assertNotIn("SECRET", json.dumps(payload))

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

    def test_replay_is_rejected_by_bounded_nonce_ledger(self) -> None:
        ledger = InventoryNonceLedger(capacity=2)
        fixed_nonce = "a" * 64
        first = probe_skill_load(self.request("all", nonce=fixed_nonce), confirmed=True, nonce_ledger=ledger)
        second = probe_skill_load(self.request("all", nonce=fixed_nonce), confirmed=True, nonce_ledger=ledger)
        self.assertEqual(first["probe_status"], "observed")
        self.assertEqual((second["probe_status"], second["reason_code"]), ("probe_error", "inventory_nonce_replayed"))

    def test_observation_expiry_and_schema_forbidden_keys(self) -> None:
        payload = probe_skill_load(self.request("all"), confirmed=True)
        self.assertTrue(skill_load_observation_is_fresh(payload))
        future = datetime.now(timezone.utc) + timedelta(days=1)
        self.assertFalse(skill_load_observation_is_fresh(payload, now=future))
        for forbidden in ("prompt", "raw_log", "credential", "cpu", "ram", "resource_score"):
            errors = validate_skill_load_observation({**payload, forbidden: "x"})
            self.assertTrue(errors, forbidden)

    def test_expected_digest_is_sorted_and_stable(self) -> None:
        self.assertEqual(expected_skills_digest(("beta", "alpha")), expected_skills_digest(("alpha", "beta")))
        self.assertEqual(len(expected_skills_digest(("alpha",))), 64)

    def test_labels_and_harness_consumer_preserve_probe_axes(self) -> None:
        observed = probe_skill_load(self.request("partial"), confirmed=True)
        unsupported = probe_skill_load(self.request("unsupported"), confirmed=True)
        self.assertEqual(skill_load_evidence_state(observed), "partially_loaded")
        self.assertEqual(skill_load_evidence_state(unsupported), "unsupported")
        self.assertEqual(set(SKILL_LOAD_PROBE_LABELS), {"observed", "unsupported", "probe_error"})
        self.assertEqual(set(SKILL_LOAD_STATE_LABELS), {"all_loaded", "partially_loaded", "none_loaded", "not_applicable"})


if __name__ == "__main__":
    unittest.main()
