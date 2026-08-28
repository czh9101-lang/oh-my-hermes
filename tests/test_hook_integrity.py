"""#803: the native OMH hooks stay reviewed, tamper-evident, and revocable.

Three acceptance criteria, pinned here rather than inferred from the plugin
distribution tests:

1. Every managed hook matches a reviewed digest and a declared event scope.
2. A changed or a revoked hook leaves the managed projection *and* produces a
   repair message naming the capability that went with it.
3. Installing a hook is never recorded as observing Hermes invoke it.

The tampering tests write through `atomic_write_text` for the same reason the
bundle installer does: it writes with `newline=""`, so a file's bytes on disk
stay what the test wrote. Default text mode translates `\\n` to CRLF on
Windows, and every assertion below compares a digest of those exact bytes.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _local_package import load_local_package

load_local_package()

from omh.capabilities.hooks import hook_manifest
from omh.commands import setup as setup_commands
from omh.hashutil import sha256_file, sha256_text
from omh.install.hook_integrity import (
    DIGEST_AGGREGATE_STATES,
    HOOK_HOST_TARGET,
    HOOK_INTEGRITY_RECORD_KEYS,
    HOOK_INTEGRITY_SCHEMA_VERSION,
    HOOK_INTEGRITY_STATUS_KEYS,
    HOOK_REVIEWS,
    HOOK_REVOCATION_LEDGER_SCHEMA_VERSION,
    REVOCATION_REASON_LIMIT,
    VALID_HOOK_EVENTS,
    build_hook_integrity_status,
    read_hook_revocations,
    revocation_ledger_path,
    validate_hook_integrity_status,
)
from omh.local_store import atomic_write_text, ensure_dir
from omh.maintenance.doctor import run_doctor
from omh.paths import resolve_paths
from omh.plugin_bundle.omh.metadata import OPTIONAL_HOOKS, PROVIDED_HOOKS, REQUIRED_HOOKS
from omh.plugin_pack import install_plugin_bundle


def _paths(tmp: str):
    return resolve_paths(Path(tmp) / ".omh", Path(tmp) / ".hermes")


def _installed_paths(tmp: str):
    paths = _paths(tmp)
    install_plugin_bundle(paths)
    return paths


def _hook_file(paths, name: str) -> Path:
    source_path = str(HOOK_REVIEWS[name]["source_path"])
    return paths.hermes_plugin_dir.joinpath(*source_path.split("/"))


def _tamper(paths, name: str) -> None:
    path = _hook_file(paths, name)
    atomic_write_text(path, path.read_text(encoding="utf-8") + "\n# local edit\n")


def _revoke(paths, name: str, reason: str) -> None:
    ensure_dir(paths.runtime_dir)
    atomic_write_text(
        revocation_ledger_path(paths),
        json.dumps(
            {
                "schema_version": HOOK_REVOCATION_LEDGER_SCHEMA_VERSION,
                "revoked": [{"hook": name, "reason": reason}],
            }
        ),
    )


def _record(status: dict, name: str) -> dict:
    return next(item for item in status["records"] if item["name"] == name)


def _excluded(status: dict, name: str) -> dict:
    return next(item for item in status["excluded_hooks"] if item["name"] == name)


class ReviewedRecordTests(unittest.TestCase):
    """AC1: every managed hook carries a reviewed digest and an event scope."""

    def test_every_declared_hook_is_reviewed_with_a_digest_and_a_scope(self) -> None:
        with TemporaryDirectory() as tmp:
            status = build_hook_integrity_status(_paths(tmp))

            self.assertEqual(validate_hook_integrity_status(status), [])
            self.assertEqual(status["schema_version"], HOOK_INTEGRITY_SCHEMA_VERSION)
            self.assertEqual(sorted(status["managed_hooks"]), sorted(PROVIDED_HOOKS))
            for record in status["records"]:
                with self.subTest(hook=record["name"]):
                    self.assertEqual(sorted(record), sorted(HOOK_INTEGRITY_RECORD_KEYS))
                    self.assertEqual(len(record["reviewed_digest"]), 64)
                    self.assertTrue(record["event_scope"])
                    self.assertTrue(set(record["event_scope"]).issubset(VALID_HOOK_EVENTS))
                    self.assertEqual(record["review"], "reviewed")
                    self.assertEqual(record["host_target"], HOOK_HOST_TARGET)
                    self.assertGreater(record["reviewed_timeout_ms"], 0)

    def test_the_record_refines_the_hook_manifest_rather_than_replacing_it(self) -> None:
        # One hook concept, two projections. If these vocabularies ever drift,
        # a hook could be reviewed here and absent there, or the reverse.
        with TemporaryDirectory() as tmp:
            status = build_hook_integrity_status(_paths(tmp))

            manifest_hooks = {item["name"] for item in hook_manifest()["plugin_hooks"]}
            self.assertEqual({record["name"] for record in status["records"]}, manifest_hooks)
            self.assertEqual(
                status["hook_manifest_schema_version"], hook_manifest()["schema_version"]
            )

    def test_host_registration_follows_the_required_and_optional_split(self) -> None:
        with TemporaryDirectory() as tmp:
            status = build_hook_integrity_status(_paths(tmp))

            by_name = {record["name"]: record for record in status["records"]}
            for name in REQUIRED_HOOKS:
                self.assertEqual(by_name[name]["host_registration"], "required")
            for name in OPTIONAL_HOOKS:
                self.assertEqual(by_name[name]["host_registration"], "optional")

    def test_the_reviewed_digest_is_the_digest_the_installer_writes(self) -> None:
        # The reviewed value has to be the one `plugin_pack` already computes,
        # or "matches" would mean two different things in two places.
        with TemporaryDirectory() as tmp:
            paths = _installed_paths(tmp)

            status = build_hook_integrity_status(paths)

            self.assertEqual(status["digest_state"], "matches")
            for record in status["records"]:
                with self.subTest(hook=record["name"]):
                    installed = _hook_file(paths, record["name"])
                    self.assertEqual(record["reviewed_digest"], sha256_file(installed))
                    self.assertEqual(
                        record["reviewed_digest"],
                        sha256_text(installed.read_text(encoding="utf-8")),
                    )
                    self.assertEqual(record["digest"], "matches")

    def test_a_managed_hook_without_a_digest_or_a_scope_fails_validation(self) -> None:
        with TemporaryDirectory() as tmp:
            status = build_hook_integrity_status(_paths(tmp))

            no_digest = json.loads(json.dumps(status))
            _record(no_digest, "pre_llm_call")["reviewed_digest"] = ""
            self.assertTrue(
                any("reviewed sha256 digest" in error for error in validate_hook_integrity_status(no_digest))
            )

            no_scope = json.loads(json.dumps(status))
            _record(no_scope, "pre_llm_call")["event_scope"] = []
            self.assertTrue(
                any("non-empty event scope" in error for error in validate_hook_integrity_status(no_scope))
            )

    def test_an_unknown_event_name_is_refused(self) -> None:
        # `VALID_HOOK_EVENTS` is the vocabulary. A record that widens its own
        # matcher must not arrive as a reviewed fact.
        with TemporaryDirectory() as tmp:
            status = build_hook_integrity_status(_paths(tmp))
            widened = json.loads(json.dumps(status))
            _record(widened, "pre_tool_call")["event_scope"] = ["pre_tool_call", "post_verify"]

            errors = validate_hook_integrity_status(widened)

            self.assertTrue(any("outside VALID_HOOK_EVENTS" in error for error in errors))
            self.assertTrue(any("post_verify" in error for error in errors))

    def test_the_status_shape_is_pinned(self) -> None:
        with TemporaryDirectory() as tmp:
            status = build_hook_integrity_status(_paths(tmp))

            self.assertEqual(sorted(status), sorted(HOOK_INTEGRITY_STATUS_KEYS))
            self.assertIn(status["digest_state"], DIGEST_AGGREGATE_STATES)
            self.assertEqual(validate_hook_integrity_status("not a status"), ["hook_integrity_status must be an object"])


class ChangedHookTests(unittest.TestCase):
    """AC2, tamper half: a changed hook leaves the projection with a repair."""

    def test_a_changed_hook_is_dropped_and_names_its_capability(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _installed_paths(tmp)
            _tamper(paths, "pre_llm_call")

            status = build_hook_integrity_status(paths)

            self.assertEqual(validate_hook_integrity_status(status), [])
            self.assertNotIn("pre_llm_call", status["managed_hooks"])
            self.assertEqual(status["digest_state"], "changed")
            record = _record(status, "pre_llm_call")
            self.assertFalse(record["trusted"])
            self.assertEqual(record["digest"], "changed")
            self.assertNotEqual(record["installed_digest"], record["reviewed_digest"])
            excluded = _excluded(status, "pre_llm_call")
            self.assertIn("route hint", excluded["capability"])
            self.assertIn("unavailable", excluded["repair"])
            self.assertIn("omh setup --force", excluded["repair"])
            # The other three hooks are untouched: one bad file must not take
            # the whole hook surface down.
            self.assertEqual(len(status["managed_hooks"]), len(PROVIDED_HOOKS) - 1)

    def test_a_deleted_hook_file_is_dropped_and_names_its_capability(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _installed_paths(tmp)
            _hook_file(paths, "pre_verify").unlink()

            status = build_hook_integrity_status(paths)

            self.assertEqual(validate_hook_integrity_status(status), [])
            self.assertNotIn("pre_verify", status["managed_hooks"])
            self.assertEqual(_record(status, "pre_verify")["digest"], "missing")
            self.assertIn("verification nudge", _excluded(status, "pre_verify")["capability"])

    def test_a_repaired_hook_returns_to_the_projection(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _installed_paths(tmp)
            original = _hook_file(paths, "pre_tool_call").read_text(encoding="utf-8")
            _tamper(paths, "pre_tool_call")
            self.assertNotIn("pre_tool_call", build_hook_integrity_status(paths)["managed_hooks"])

            atomic_write_text(_hook_file(paths, "pre_tool_call"), original)

            status = build_hook_integrity_status(paths)
            self.assertIn("pre_tool_call", status["managed_hooks"])
            self.assertEqual(status["excluded_hooks"], [])
            self.assertEqual(status["digest_state"], "matches")

    def test_an_uninstalled_bundle_is_not_treated_as_tampering(self) -> None:
        # Nothing has changed a hook that was never installed. Calling that a
        # fault would fail doctor on every machine before its first setup.
        with TemporaryDirectory() as tmp:
            status = build_hook_integrity_status(_paths(tmp))

            self.assertFalse(status["plugin_installed"])
            self.assertEqual(status["digest_state"], "not_installed")
            self.assertEqual(status["excluded_hooks"], [])
            self.assertEqual(sorted(status["managed_hooks"]), sorted(PROVIDED_HOOKS))


class RevocationTests(unittest.TestCase):
    """AC2, revocation half: revocation is explicit data with its own repair."""

    def test_a_revoked_hook_is_dropped_and_names_its_capability(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _installed_paths(tmp)
            _revoke(paths, "pre_verify", "operator policy")

            status = build_hook_integrity_status(paths)

            self.assertEqual(validate_hook_integrity_status(status), [])
            self.assertNotIn("pre_verify", status["managed_hooks"])
            self.assertEqual(status["revocation_state"], "revoked_present")
            self.assertEqual(status["revocation_ledger"], "loaded")
            record = _record(status, "pre_verify")
            self.assertEqual(record["revocation"], "revoked")
            self.assertEqual(record["revocation_reason"], "operator policy")
            # Revocation is data, not absence: the hook is still projected, it
            # is projected as withdrawn.
            self.assertEqual(record["digest"], "matches")
            excluded = _excluded(status, "pre_verify")
            self.assertIn("verification nudge", excluded["capability"])
            self.assertIn("revocation ledger", excluded["repair"])

    def test_a_revoked_hook_stays_revoked_until_the_ledger_is_repaired(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _installed_paths(tmp)
            _revoke(paths, "pre_llm_call", "operator policy")

            # Reinstalling restores the bytes and changes nothing about the
            # operator's decision.
            install_plugin_bundle(paths, force=True)
            self.assertNotIn("pre_llm_call", build_hook_integrity_status(paths)["managed_hooks"])

            revocation_ledger_path(paths).unlink()

            status = build_hook_integrity_status(paths)
            self.assertIn("pre_llm_call", status["managed_hooks"])
            self.assertEqual(status["revocation_state"], "none")
            self.assertEqual(status["revocation_ledger"], "absent")

    def test_revocation_outranks_a_changed_digest_in_the_repair(self) -> None:
        # Telling an operator to reinstall a hook they deliberately withdrew is
        # the wrong instruction, even when the file also drifted.
        with TemporaryDirectory() as tmp:
            paths = _installed_paths(tmp)
            _tamper(paths, "on_session_end")
            _revoke(paths, "on_session_end", "operator policy")

            status = build_hook_integrity_status(paths)

            self.assertIn("revoked locally", _record(status, "on_session_end")["exclusion_reason"])
            self.assertIn("revocation ledger", status["next_action"])
            self.assertNotIn("omh setup --force", status["next_action"])

    def test_an_unreadable_ledger_reports_itself_instead_of_revoking_everything(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _installed_paths(tmp)
            ensure_dir(paths.runtime_dir)
            atomic_write_text(revocation_ledger_path(paths), "{ not json")

            revoked, state = read_hook_revocations(paths)
            status = build_hook_integrity_status(paths)

            self.assertEqual(revoked, {})
            self.assertEqual(state, "unreadable")
            self.assertEqual(status["revocation_ledger"], "unreadable")
            self.assertEqual(sorted(status["managed_hooks"]), sorted(PROVIDED_HOOKS))
            self.assertIn("Repair or remove", status["next_action"])

    def test_a_ledger_from_an_unknown_schema_version_is_not_read_as_empty(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _installed_paths(tmp)
            ensure_dir(paths.runtime_dir)
            atomic_write_text(
                revocation_ledger_path(paths),
                json.dumps({"schema_version": "omh_hook_revocations/v99", "revoked": []}),
            )

            revoked, state = read_hook_revocations(paths)

            self.assertEqual(revoked, {})
            self.assertEqual(state, "unreadable")

    def test_a_long_revocation_reason_is_bounded(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _installed_paths(tmp)
            _revoke(paths, "pre_verify", "x" * (REVOCATION_REASON_LIMIT + 50))

            record = _record(build_hook_integrity_status(paths), "pre_verify")

            self.assertEqual(len(record["revocation_reason"]), REVOCATION_REASON_LIMIT)


class ObservationBoundaryTests(unittest.TestCase):
    """AC3: installation is never proof of load or invocation."""

    def test_installing_the_bundle_never_sets_an_observed_flag(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _paths(tmp)
            before = build_hook_integrity_status(paths)
            result = install_plugin_bundle(paths)
            after = build_hook_integrity_status(paths)

            # The installer observed its own copy, and that is all it observed.
            self.assertTrue(result["observed"])
            self.assertIn("does not prove Hermes loaded or used the plugin", result["observed_scope"])
            for status in (before, after):
                self.assertFalse(status["observed_in_this_environment"])
                for record in status["records"]:
                    self.assertFalse(record["observed_in_this_environment"])

    def test_the_claim_boundary_separates_review_from_invocation(self) -> None:
        with TemporaryDirectory() as tmp:
            status = build_hook_integrity_status(_installed_paths(tmp))

            boundary = status["claim_boundary"]
            self.assertIn("not evidence that Hermes loaded, registered, or invoked", boundary)
            self.assertIn("omh_plugin_host_observation", boundary)
            self.assertIn("unobserved", status["next_action"])

    def test_an_observed_flag_set_to_true_fails_validation(self) -> None:
        with TemporaryDirectory() as tmp:
            status = build_hook_integrity_status(_installed_paths(tmp))

            lying_status = {**status, "observed_in_this_environment": True}
            self.assertTrue(
                any("observed_in_this_environment must be False" in error
                    for error in validate_hook_integrity_status(lying_status))
            )

            lying_record = json.loads(json.dumps(status))
            _record(lying_record, "pre_llm_call")["observed_in_this_environment"] = True
            self.assertTrue(
                any("observed_in_this_environment must be False" in error
                    for error in validate_hook_integrity_status(lying_record))
            )


class DoctorSurfaceTests(unittest.TestCase):
    """The surface an operator actually reaches: `omh doctor`."""

    def test_doctor_reports_the_six_axes_and_stays_green_on_a_bare_home(self) -> None:
        with TemporaryDirectory() as tmp:
            check = next(
                item for item in run_doctor(_paths(tmp)) if item.name == "plugin_hook_integrity"
            )

            self.assertTrue(check.ok)
            for axis in ("managed=", "digest=", "event_scope=", "review=", "host_target=", "revocation="):
                self.assertIn(axis, check.message)
            self.assertIn("observed=False", check.message)

    def test_doctor_stays_green_for_a_freshly_installed_bundle(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _installed_paths(tmp)

            check = next(item for item in run_doctor(paths) if item.name == "plugin_hook_integrity")

            self.assertTrue(check.ok)
            self.assertIn("digest=matches", check.message)

    def test_doctor_names_the_lost_capability_for_a_changed_hook(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _installed_paths(tmp)
            _tamper(paths, "pre_llm_call")

            check = next(item for item in run_doctor(paths) if item.name == "plugin_hook_integrity")

            self.assertFalse(check.ok)
            self.assertIn("pre_llm_call", check.message)
            self.assertIn("route hint", check.message)
            self.assertIn("omh setup --force", check.next_action)
            self.assertTrue(check.remediation)

    def test_doctor_names_the_lost_capability_for_a_revoked_hook(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = _installed_paths(tmp)
            _revoke(paths, "pre_tool_call", "operator policy")

            check = next(item for item in run_doctor(paths) if item.name == "plugin_hook_integrity")

            self.assertFalse(check.ok)
            self.assertIn("unknown-role warning", check.message)
            self.assertIn("revocation ledger", check.next_action)

    def test_the_check_lands_in_an_operator_visible_group(self) -> None:
        # A check outside every group is a check no operator ever reads.
        with TemporaryDirectory() as tmp:
            paths = _installed_paths(tmp)
            _tamper(paths, "on_session_end")

            summary = setup_commands._doctor_operator_summary(run_doctor(paths))

            group = next(item for item in summary["groups"] if item["name"] == "optional_surfaces")
            self.assertIn("plugin_hook_integrity", group["failed"])


if __name__ == "__main__":
    unittest.main()
