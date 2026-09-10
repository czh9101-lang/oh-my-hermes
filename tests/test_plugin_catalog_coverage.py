"""Plugin catalog coverage against a supplied host snapshot (issue #1449).

The cases here are grouped by the question each one keeps honest rather than
by function, because the failures worth catching are all confusions between
two answers that look alike: a rediscovered entry reading as a new one, a
stale snapshot reading as the current catalog, the packaged repository catalog
reading as host coverage, and catalog presence reading as readiness.

No global plugin count is pinned anywhere in this file. Every routing case
builds the entries it is about, so admitting a new capability shape never
forces an unrelated number to move.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _cli_harness import run_cli
from _credential_fixtures import AWS_ACCESS_KEY_ID
from _local_package import load_local_package


load_local_package()
from omh.catalogs.awesome_hermes_agent import (  # noqa: E402
    PACKAGED_COVERAGE_SCOPE,
    awesome_hermes_coverage_payload,
    awesome_hermes_summary,
)
from omh.catalogs.awesome_hermes_agent_outcomes import awesome_hermes_plugin_outcomes  # noqa: E402
from omh.workflows.plugin_catalog_coverage import (  # noqa: E402
    COVERAGE_CLASSES,
    GENERIC_REVIEW_ROUTE,
    HELD_REVIEW_ROUTE,
    PLUGIN_CATALOG_COVERAGE_SCHEMA_VERSION,
    PluginCatalogCoverageError,
    build_plugin_catalog_coverage,
    plugin_catalog_coverage_unavailable,
    render_plugin_catalog_coverage,
)
from omh.workflows.plugin_catalog_snapshots import (  # noqa: E402
    MAX_DIAGNOSTICS,
    MAX_SNAPSHOT_BYTES,
    MAX_SNAPSHOT_ENTRIES,
    PLUGIN_CATALOG_SNAPSHOT_SCHEMA_VERSION,
    PluginCatalogSnapshotError,
    load_plugin_catalog_snapshot,
    validate_plugin_catalog_snapshot,
)


OBSERVED_AT = "2026-09-10T09:00:00Z"
NOW = "2026-09-10T10:00:00Z"
MUCH_LATER = "2026-09-30T10:00:00Z"

# Every module that would turn the adapter into a fetcher, an installer, or a
# loader. The contract's whole premise is that the supplied file is the only
# input, and an import here would make that claim unenforceable.
NETWORK_AND_EXECUTION_MODULES = (
    "http",
    "httpx",
    "importlib",
    "requests",
    "socket",
    "ssl",
    "subprocess",
    "urllib",
)


def entry(
    plugin_id: str,
    *,
    content_revision: str = "rev-1",
    tier: str = "verified",
    declared_capabilities: tuple[str, ...] = ("provider",),
    required_env_names: tuple[str, ...] = (),
    platform_limits: tuple[str, ...] = (),
    host_version_constraint: str = "",
    host_version_status: str = "satisfied",
    removal_status: str = "active",
) -> dict[str, object]:
    return {
        "plugin_id": plugin_id,
        "content_revision": content_revision,
        "tier": tier,
        "declared_capabilities": list(declared_capabilities),
        "required_env_names": list(required_env_names),
        "platform_limits": list(platform_limits),
        "host_version_constraint": host_version_constraint,
        "host_version_status": host_version_status,
        "removal_status": removal_status,
    }


def snapshot(
    entries: list[dict[str, object]],
    *,
    catalog_revision: str = "catalog-rev-1",
    observed_at: str = OBSERVED_AT,
    profile_ref: str = "default",
    producer_kind: str = "hermes_host",
) -> dict[str, object]:
    return {
        "schema_version": PLUGIN_CATALOG_SNAPSHOT_SCHEMA_VERSION,
        "producer": {"kind": producer_kind, "ref": "hermes-host-a", "host_version": "0.13.2"},
        "profile_ref": profile_ref,
        "observed_at": observed_at,
        "catalog_revision": catalog_revision,
        "entry_count": len(entries),
        "entries": entries,
    }


def rows_by_id(payload: dict[str, object]) -> dict[str, dict[str, object]]:
    return {str(row["plugin_id"]): row for row in payload["entries"]}


class SnapshotIdentityTests(unittest.TestCase):
    """AC1: one answer, bound to one producer, time, and catalog revision."""

    def test_a_valid_snapshot_produces_sorted_output_bound_to_its_identity(self) -> None:
        payload = build_plugin_catalog_coverage(
            snapshot([entry("zeta-provider"), entry("alpha-connector", declared_capabilities=("connector",))]),
            now=NOW,
        )

        self.assertEqual(payload["schema_version"], PLUGIN_CATALOG_COVERAGE_SCHEMA_VERSION)
        self.assertEqual(payload["snapshot_state"], "current")
        self.assertEqual(payload["snapshot"]["producer_kind"], "hermes_host")
        self.assertEqual(payload["snapshot"]["producer_ref"], "hermes-host-a")
        self.assertEqual(payload["snapshot"]["observed_at"], OBSERVED_AT)
        self.assertEqual(payload["snapshot"]["catalog_revision"], "catalog-rev-1")
        self.assertEqual([row["plugin_id"] for row in payload["entries"]], ["alpha-connector", "zeta-provider"])
        self.assertEqual(payload["entry_count"], 2)

    def test_the_same_inputs_produce_a_byte_identical_answer(self) -> None:
        inputs = snapshot([entry("beta-memory", declared_capabilities=("memory",)), entry("alpha-provider")])

        first = build_plugin_catalog_coverage(inputs, now=NOW)
        second = build_plugin_catalog_coverage(inputs, now=NOW)

        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))

    def test_entry_order_in_the_file_does_not_change_the_answer(self) -> None:
        forward = [entry("alpha-provider"), entry("beta-connector", declared_capabilities=("connector",))]
        reversed_order = list(reversed(forward))

        first = build_plugin_catalog_coverage(snapshot(forward), now=NOW)
        second = build_plugin_catalog_coverage(snapshot(reversed_order), now=NOW)

        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))


class ChangeClassificationTests(unittest.TestCase):
    """AC2 and AC3: a rediscovered entry is not a new one."""

    def test_one_admitted_entry_reports_exactly_one_added_row(self) -> None:
        previous = snapshot([entry("alpha-provider")])
        current = snapshot(
            [entry("alpha-provider"), entry("beta-connector", declared_capabilities=("connector",))],
            catalog_revision="catalog-rev-2",
        )

        payload = build_plugin_catalog_coverage(current, previous=previous, now=NOW)

        self.assertEqual(payload["summary"]["change_class_counts"]["added"], 1)
        self.assertEqual(payload["summary"]["change_class_counts"]["unchanged"], 1)
        self.assertEqual(payload["summary"]["change_class_counts"]["changed"], 0)
        self.assertEqual(payload["summary"]["change_class_counts"]["removed"], 0)
        self.assertEqual(rows_by_id(payload)["beta-connector"]["change_class"], "added")

    def test_replaying_the_same_snapshot_reports_no_new_row(self) -> None:
        current = snapshot([entry("alpha-provider"), entry("beta-connector", declared_capabilities=("connector",))])

        payload = build_plugin_catalog_coverage(current, previous=current, now=NOW)

        counts = payload["summary"]["change_class_counts"]
        self.assertEqual(counts["unchanged"], 2)
        self.assertEqual(counts["added"], 0)
        self.assertEqual(counts["changed"], 0)
        self.assertEqual(counts["removed"], 0)

    def test_a_reviewed_revision_change_is_changed_and_keeps_both_revisions(self) -> None:
        previous = snapshot([entry("alpha-provider", content_revision="rev-1")])
        current = snapshot(
            [entry("alpha-provider", content_revision="rev-2")],
            catalog_revision="catalog-rev-2",
        )

        payload = build_plugin_catalog_coverage(current, previous=previous, now=NOW)
        row = rows_by_id(payload)["alpha-provider"]

        self.assertEqual(row["change_class"], "changed")
        self.assertNotEqual(row["change_class"], "added")
        self.assertEqual(row["previous_content_revision"], "rev-1")
        self.assertEqual(row["content_revision"], "rev-2")
        self.assertEqual(row["changed_fields"], ["content_revision"])
        self.assertEqual(payload["previous_snapshot"]["catalog_revision"], "catalog-rev-1")

    def test_a_capability_change_is_changed_and_names_the_field_that_moved(self) -> None:
        previous = snapshot([entry("alpha-plugin", declared_capabilities=("provider",))])
        current = snapshot([entry("alpha-plugin", declared_capabilities=("connector", "provider"))])

        row = rows_by_id(build_plugin_catalog_coverage(current, previous=previous, now=NOW))["alpha-plugin"]

        self.assertEqual(row["change_class"], "changed")
        self.assertEqual(row["changed_fields"], ["declared_capabilities"])

    def test_an_entry_that_left_the_catalog_reports_removed_and_keeps_its_prior_revision(self) -> None:
        previous = snapshot([entry("alpha-provider"), entry("beta-connector", declared_capabilities=("connector",))])
        current = snapshot([entry("alpha-provider")], catalog_revision="catalog-rev-2")

        row = rows_by_id(build_plugin_catalog_coverage(current, previous=previous, now=NOW))["beta-connector"]

        self.assertEqual(row["change_class"], "removed")
        self.assertEqual(row["coverage_class"], "removed")
        self.assertFalse(row["present_in_snapshot"])
        self.assertEqual(row["previous_content_revision"], "rev-1")
        self.assertEqual(row["content_revision"], "")

    def test_without_a_prior_snapshot_every_row_is_a_first_sighting(self) -> None:
        payload = build_plugin_catalog_coverage(snapshot([entry("alpha-provider")]), now=NOW)

        self.assertIsNone(payload["previous_snapshot"])
        self.assertEqual(payload["summary"]["change_class_counts"]["added"], 1)
        self.assertIn("no prior snapshot supplied", render_plugin_catalog_coverage(payload))


class RemovalAndCompatibilityHoldTests(unittest.TestCase):
    """AC4 and AC5: withdrawal and incompatibility are held, never ready."""

    def test_a_removed_entry_is_held_and_routed_to_review(self) -> None:
        payload = build_plugin_catalog_coverage(
            snapshot([entry("alpha-provider", removal_status="removed")]),
            now=NOW,
        )
        row = rows_by_id(payload)["alpha-provider"]

        self.assertEqual(row["coverage_class"], "removed")
        self.assertEqual(row["adoption_guidance"], "held")
        self.assertEqual(row["route"], HELD_REVIEW_ROUTE)
        self.assertIn("catalog_removal", row["holds"])
        self.assertEqual(payload["held_entries"], ["alpha-provider"])

    def test_a_removal_hold_never_claims_an_installed_copy_changed(self) -> None:
        payload = build_plugin_catalog_coverage(
            snapshot([entry("alpha-provider", removal_status="removed")]),
            now=NOW,
        )
        row = rows_by_id(payload)["alpha-provider"]

        self.assertIn("installed_copy_state", row["unavailable_evidence"])
        boundary = str(payload["claim_boundary"])
        self.assertIn("says nothing about whether an installed copy exists", boundary)
        for forbidden in ("disabled the plugin", "uninstalled the plugin"):
            self.assertNotIn(forbidden, boundary)

    def test_an_unmet_host_version_constraint_is_incompatible_and_held(self) -> None:
        payload = build_plugin_catalog_coverage(
            snapshot(
                [
                    entry(
                        "alpha-provider",
                        host_version_constraint=">=0.14",
                        host_version_status="unsatisfied",
                    )
                ]
            ),
            now=NOW,
        )
        row = rows_by_id(payload)["alpha-provider"]

        self.assertEqual(row["coverage_class"], "incompatible")
        self.assertEqual(row["adoption_guidance"], "held")
        self.assertIn("host_version_unsatisfied", row["holds"])
        self.assertEqual(row["host_version_constraint"], ">=0.14")

    def test_catalog_presence_never_produces_a_ready_state(self) -> None:
        payload = build_plugin_catalog_coverage(
            snapshot(
                [
                    entry("alpha-provider", tier="official"),
                    entry("beta-connector", declared_capabilities=("connector",), tier="official"),
                ]
            ),
            now=NOW,
        )

        for row in payload["entries"]:
            with self.subTest(plugin_id=row["plugin_id"]):
                self.assertNotIn("ready", str(row["adoption_guidance"]))
                self.assertIn("host_observed_behavior", row["unavailable_evidence"])
                self.assertIn("plugin_execution", row["unavailable_evidence"])
                self.assertIn("host_permission_grant", row["unavailable_evidence"])

    def test_an_unknown_host_version_status_names_the_missing_evaluation(self) -> None:
        payload = build_plugin_catalog_coverage(
            snapshot([entry("alpha-provider", host_version_constraint=">=0.13", host_version_status="unknown")]),
            now=NOW,
        )
        row = rows_by_id(payload)["alpha-provider"]

        self.assertEqual(row["coverage_class"], "mapped")
        self.assertIn("host_version_evaluation", row["unavailable_evidence"])


class OutcomeRoutingTests(unittest.TestCase):
    """AC6 and AC9: one class per entry, and each capability shape has an owner.

    Each case names the entries it is about, so no global plugin count is
    pinned and admitting a new shape moves nothing else.
    """

    def test_each_owned_capability_routes_to_its_workflow(self) -> None:
        expected = {
            "provider": "provider-profile-posture",
            "connector": "external-connector-readiness",
            "observability": "ops-observability-card",
            "memory": "memory-sync",
            "media": "media-input-operator",
        }
        entries = [
            entry(f"{capability}-plugin", declared_capabilities=(capability,)) for capability in sorted(expected)
        ]

        rows = rows_by_id(build_plugin_catalog_coverage(snapshot(entries), now=NOW))

        for capability, workflow in expected.items():
            with self.subTest(capability=capability):
                row = rows[f"{capability}-plugin"]
                self.assertEqual(row["coverage_class"], "mapped")
                self.assertEqual(row["route"], workflow)
                self.assertEqual(row["owner_boundary"], "omh")
                self.assertEqual(row["adoption_guidance"], "route_to_owner")

    def test_a_multi_capability_entry_takes_one_primary_route_and_lists_the_rest(self) -> None:
        payload = build_plugin_catalog_coverage(
            snapshot([entry("wide-plugin", declared_capabilities=("connector", "memory", "provider"))]),
            now=NOW,
        )
        row = rows_by_id(payload)["wide-plugin"]

        self.assertEqual(row["route"], "provider-profile-posture")
        self.assertEqual(row["additional_routes"], ["external-connector-readiness", "memory-sync"])

    def test_a_desktop_only_entry_keeps_its_owner_and_names_the_platform_gap(self) -> None:
        payload = build_plugin_catalog_coverage(
            snapshot(
                [entry("desktop-media", declared_capabilities=("media",), platform_limits=("desktop",))]
            ),
            now=NOW,
        )
        row = rows_by_id(payload)["desktop-media"]

        self.assertEqual(row["coverage_class"], "mapped")
        self.assertEqual(row["route"], "media-input-operator")
        self.assertEqual(row["platform_limits"], ["desktop"])
        self.assertIn("platform_limited", row["advisories"])
        self.assertIn("host_platform_match", row["unavailable_evidence"])

    def test_a_host_core_only_entry_stays_on_the_host_side_of_the_boundary(self) -> None:
        payload = build_plugin_catalog_coverage(
            snapshot([entry("loader-core", declared_capabilities=("host_core",))]),
            now=NOW,
        )
        row = rows_by_id(payload)["loader-core"]

        self.assertEqual(row["owner_boundary"], "hermes_host")
        self.assertEqual(row["adoption_guidance"], "host_owned")
        self.assertEqual(row["route"], GENERIC_REVIEW_ROUTE)
        self.assertIn("host_owned_mechanics", row["advisories"])

    def test_an_unmapped_capability_combination_reaches_generic_review(self) -> None:
        payload = build_plugin_catalog_coverage(
            snapshot([entry("odd-plugin", declared_capabilities=("bus-daemon", "shell-rewrite"))]),
            now=NOW,
        )
        row = rows_by_id(payload)["odd-plugin"]

        self.assertEqual(row["coverage_class"], "generic_review")
        self.assertEqual(row["route"], GENERIC_REVIEW_ROUTE)
        self.assertEqual(payload["unresolved_entries"], ["odd-plugin"])

    def test_an_entry_that_declares_nothing_is_unknown_rather_than_absent(self) -> None:
        payload = build_plugin_catalog_coverage(
            snapshot([entry("silent-plugin", declared_capabilities=())]),
            now=NOW,
        )
        row = rows_by_id(payload)["silent-plugin"]

        self.assertEqual(row["coverage_class"], "unknown")
        self.assertEqual(row["route"], GENERIC_REVIEW_ROUTE)
        self.assertIn("silent-plugin", payload["unresolved_entries"])

    def test_every_entry_gets_exactly_one_coverage_class_and_the_counts_add_up(self) -> None:
        entries = [
            entry("a-provider"),
            entry("b-connector", declared_capabilities=("connector",)),
            entry("c-core", declared_capabilities=("host_core",)),
            entry("d-odd", declared_capabilities=("bus-daemon",)),
            entry("e-silent", declared_capabilities=()),
            entry("f-removed", removal_status="removed"),
            entry("g-blocked", host_version_status="unsatisfied"),
        ]

        payload = build_plugin_catalog_coverage(snapshot(entries), now=NOW)

        counts = payload["summary"]["coverage_class_counts"]
        self.assertEqual(set(counts), set(COVERAGE_CLASSES))
        self.assertEqual(sum(counts.values()), len(entries))
        for row in payload["entries"]:
            with self.subTest(plugin_id=row["plugin_id"]):
                self.assertIn(row["coverage_class"], COVERAGE_CLASSES)
                self.assertTrue(row["route"])

    def test_a_deprecated_entry_still_routes_but_carries_the_advisory(self) -> None:
        payload = build_plugin_catalog_coverage(
            snapshot([entry("fading-provider", removal_status="deprecated")]),
            now=NOW,
        )
        row = rows_by_id(payload)["fading-provider"]

        self.assertEqual(row["coverage_class"], "mapped")
        self.assertIn("catalog_deprecation", row["advisories"])

    def test_a_declared_capability_is_recorded_as_a_claim_not_as_behavior(self) -> None:
        payload = build_plugin_catalog_coverage(
            snapshot([entry("alpha-provider", declared_capabilities=("provider",))]),
            now=NOW,
        )
        row = rows_by_id(payload)["alpha-provider"]

        self.assertEqual(row["declared_capabilities"], ["provider"])
        self.assertIn("Declared capabilities are catalog claims, not observed plugin behavior", str(payload["claim_boundary"]))


class FailClosedTests(unittest.TestCase):
    """AC7: bad input produces bounded diagnostics, never a partial answer."""

    def test_a_malformed_snapshot_is_refused(self) -> None:
        with self.assertRaises(PluginCatalogCoverageError):
            build_plugin_catalog_coverage({"schema_version": "nope"}, now=NOW)

    def test_an_unsupported_field_is_named_without_echoing_its_value(self) -> None:
        broken = snapshot([entry("alpha-provider")])
        broken["entries"][0]["source_code"] = "def exfiltrate(): ..."

        diagnostics = validate_plugin_catalog_snapshot(broken)

        self.assertTrue(any("unsupported fields: source_code" in item for item in diagnostics))
        self.assertFalse(any("exfiltrate" in item for item in diagnostics))

    def test_a_duplicate_identity_is_refused(self) -> None:
        duplicated = snapshot([entry("alpha-provider"), entry("alpha-provider", content_revision="rev-2")])

        with self.assertRaisesRegex(PluginCatalogCoverageError, "repeats an identity"):
            build_plugin_catalog_coverage(duplicated, now=NOW)

    def test_a_cross_profile_snapshot_is_refused_rather_than_reported(self) -> None:
        with self.assertRaisesRegex(PluginCatalogCoverageError, "different profile"):
            build_plugin_catalog_coverage(
                snapshot([entry("alpha-provider")], profile_ref="staging"),
                profile_ref="default",
                now=NOW,
            )

    def test_a_previous_snapshot_from_another_profile_is_refused(self) -> None:
        with self.assertRaisesRegex(PluginCatalogCoverageError, "different profile"):
            build_plugin_catalog_coverage(
                snapshot([entry("alpha-provider")]),
                previous=snapshot([entry("alpha-provider")], profile_ref="staging"),
                now=NOW,
            )

    def test_an_oversized_entry_list_is_refused_rather_than_truncated(self) -> None:
        oversized = snapshot([entry(f"plugin-{index:04d}") for index in range(MAX_SNAPSHOT_ENTRIES + 1)])

        diagnostics = validate_plugin_catalog_snapshot(oversized)

        self.assertTrue(any(str(MAX_SNAPSHOT_ENTRIES) in item for item in diagnostics))
        with self.assertRaises(PluginCatalogCoverageError):
            build_plugin_catalog_coverage(oversized, now=NOW)

    def test_an_oversized_file_is_refused_before_it_is_parsed(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(" " * (MAX_SNAPSHOT_BYTES + 1), encoding="utf-8")

            with self.assertRaisesRegex(PluginCatalogSnapshotError, "byte bound"):
                load_plugin_catalog_snapshot(path)

    def test_a_missing_file_is_refused_with_a_bounded_reason(self) -> None:
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(PluginCatalogSnapshotError, "could not be read"):
                load_plugin_catalog_snapshot(Path(directory) / "absent.json")

    def test_diagnostics_are_bounded_for_a_snapshot_that_is_broken_everywhere(self) -> None:
        broken = snapshot([entry(f"plugin-{index}", tier="invented") for index in range(MAX_DIAGNOSTICS + 10)])

        diagnostics = validate_plugin_catalog_snapshot(broken)

        self.assertLessEqual(len(diagnostics), MAX_DIAGNOSTICS + 1)
        self.assertIn("further diagnostics were not listed", diagnostics[-1])

    def test_a_credential_value_in_an_environment_name_list_is_refused(self) -> None:
        carrying = snapshot([entry("alpha-provider", required_env_names=(AWS_ACCESS_KEY_ID,))])

        diagnostics = validate_plugin_catalog_snapshot(carrying)

        self.assertTrue(any("credential value rather than a variable name" in item for item in diagnostics))
        self.assertFalse(any(AWS_ACCESS_KEY_ID in item for item in diagnostics))

    def test_an_environment_variable_name_that_reads_like_a_secret_is_accepted(self) -> None:
        named = snapshot([entry("alpha-provider", required_env_names=("ALPHA_API_KEY", "GITHUB_TOKEN"))])

        self.assertEqual(validate_plugin_catalog_snapshot(named), [])
        row = rows_by_id(build_plugin_catalog_coverage(named, now=NOW))["alpha-provider"]
        self.assertEqual(row["required_env_names"], ["ALPHA_API_KEY", "GITHUB_TOKEN"])
        self.assertIn("requires_environment_names", row["advisories"])

    def test_a_constraint_string_carrying_a_body_is_refused(self) -> None:
        carrying = snapshot([entry("alpha-provider", host_version_constraint="```\nrm -rf /\n```")])

        diagnostics = validate_plugin_catalog_snapshot(carrying)

        self.assertTrue(any("one bounded line" in item for item in diagnostics))


class FreshnessTests(unittest.TestCase):
    """AC7 and proposal 4: absence and staleness never inherit presence."""

    def test_a_stale_snapshot_holds_every_entry_it_carries(self) -> None:
        payload = build_plugin_catalog_coverage(snapshot([entry("alpha-provider")]), now=MUCH_LATER)

        self.assertEqual(payload["snapshot_state"], "stale")
        self.assertEqual(payload["freshness"]["state"], "stale")
        row = rows_by_id(payload)["alpha-provider"]
        self.assertEqual(row["adoption_guidance"], "held")
        self.assertIn("stale_snapshot", row["holds"])

    def test_a_missing_snapshot_reports_unavailable_and_carries_no_entries(self) -> None:
        payload = plugin_catalog_coverage_unavailable("no snapshot was supplied")

        self.assertEqual(payload["snapshot_state"], "unavailable")
        self.assertEqual(payload["entries"], [])
        self.assertEqual(payload["entry_count"], 0)
        self.assertEqual(payload["snapshot"]["catalog_revision"], "")
        self.assertEqual(payload["diagnostics"], ["no snapshot was supplied"])

    def test_an_unavailable_answer_does_not_borrow_the_packaged_catalog(self) -> None:
        payload = plugin_catalog_coverage_unavailable("no snapshot was supplied")

        self.assertFalse(payload["packaged_reference"]["used_as_host_coverage"])
        self.assertEqual(payload["packaged_reference"]["coverage_scope"], PACKAGED_COVERAGE_SCOPE)
        packaged_count = int(awesome_hermes_summary()["plugin_count"])
        self.assertNotEqual(payload["entry_count"], packaged_count)
        self.assertEqual(payload["summary"]["coverage_class_counts"]["mapped"], 0)

    def test_an_unreadable_reference_clock_reports_unknown_rather_than_fresh(self) -> None:
        payload = build_plugin_catalog_coverage(snapshot([entry("alpha-provider")]), now="not-a-timestamp")

        self.assertEqual(payload["freshness"]["state"], "unknown")
        self.assertEqual(payload["snapshot_state"], "stale")


class PackagedInputsStayReadableTests(unittest.TestCase):
    """AC8: legacy inputs keep working and say what they are."""

    def test_the_packaged_catalog_summary_is_labelled_snapshot_limited(self) -> None:
        payload = awesome_hermes_summary()

        self.assertEqual(payload["coverage_scope"], PACKAGED_COVERAGE_SCOPE)
        self.assertIn("not the active host catalog", str(payload["coverage_scope_note"]))
        self.assertGreater(int(payload["plugin_count"]), 0)

    def test_the_packaged_coverage_payload_is_labelled_snapshot_limited(self) -> None:
        payload = awesome_hermes_coverage_payload(subsection="Plugins")

        self.assertEqual(payload["coverage_scope"], PACKAGED_COVERAGE_SCOPE)
        self.assertTrue(payload["items"])

    def test_the_seven_entry_outcome_matrix_still_reads_and_is_labelled(self) -> None:
        payload = awesome_hermes_plugin_outcomes()

        self.assertEqual(payload["coverage_scope"], PACKAGED_COVERAGE_SCOPE)
        self.assertIn("plugin_catalog_snapshot/v1", str(payload["coverage_scope_note"]))
        self.assertTrue(payload["outcomes"])


class ReadOnlyAdapterTests(unittest.TestCase):
    """AC10: the adapter reads a supplied file and does nothing else."""

    def test_the_contract_modules_import_nothing_that_could_fetch_or_execute(self) -> None:
        from omh.commands import plugin_catalog as adapter
        from omh.workflows import plugin_catalog_coverage, plugin_catalog_snapshots

        for module in (adapter, plugin_catalog_coverage, plugin_catalog_snapshots):
            source = Path(str(module.__file__)).read_text(encoding="utf-8")
            imported = {
                line.split()[1].split(".")[0]
                for line in source.splitlines()
                if line.startswith("import ") or line.startswith("from ")
            }
            for forbidden in NETWORK_AND_EXECUTION_MODULES:
                with self.subTest(module=module.__name__, forbidden=forbidden):
                    self.assertNotIn(forbidden, imported)

    def test_the_adapter_does_not_read_or_write_the_supplied_file_beyond_reading(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            payload = snapshot([entry("alpha-provider")])
            path.write_text(json.dumps(payload), encoding="utf-8")
            before = path.read_bytes()

            status, stdout, stderr = run_cli(
                ["ecosystem", "plugin-catalog", "coverage", "--input", str(path), "--now", NOW]
            )

            self.assertEqual(status, 0, stderr)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(sorted(item.name for item in Path(directory).iterdir()), ["snapshot.json"])
            self.assertEqual(json.loads(stdout)["snapshot_state"], "current")


class CliSurfaceTests(unittest.TestCase):
    """The operator entry point, including how it fails."""

    def _write(self, directory: str, name: str, payload: dict[str, object]) -> str:
        path = Path(directory) / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def test_the_cli_reports_one_added_row_across_two_snapshots(self) -> None:
        with TemporaryDirectory() as directory:
            previous = self._write(directory, "previous.json", snapshot([entry("alpha-provider")]))
            current = self._write(
                directory,
                "current.json",
                snapshot(
                    [entry("alpha-provider"), entry("beta-connector", declared_capabilities=("connector",))],
                    catalog_revision="catalog-rev-2",
                ),
            )

            status, stdout, stderr = run_cli(
                [
                    "ecosystem",
                    "plugin-catalog",
                    "coverage",
                    "--input",
                    current,
                    "--previous",
                    previous,
                    "--now",
                    NOW,
                ]
            )

            self.assertEqual(status, 0, stderr)
            payload = json.loads(stdout)
            self.assertEqual(payload["summary"]["change_class_counts"]["added"], 1)
            self.assertEqual(payload["snapshot"]["catalog_revision"], "catalog-rev-2")

    def test_the_cli_without_a_snapshot_reports_unavailable_and_exits_non_zero(self) -> None:
        status, stdout, stderr = run_cli(["ecosystem", "plugin-catalog", "coverage"])

        self.assertEqual(status, 1, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["snapshot_state"], "unavailable")
        self.assertEqual(payload["entries"], [])

    def test_the_cli_refuses_a_cross_profile_snapshot(self) -> None:
        with TemporaryDirectory() as directory:
            path = self._write(directory, "snapshot.json", snapshot([entry("alpha-provider")], profile_ref="staging"))

            status, stdout, stderr = run_cli(
                ["ecosystem", "plugin-catalog", "coverage", "--input", path, "--profile", "default", "--now", NOW]
            )

            self.assertNotEqual(status, 0)
            self.assertEqual(stdout, "")
            self.assertIn("different profile", stderr)

    def test_the_plain_text_status_names_the_snapshot_and_the_holds(self) -> None:
        with TemporaryDirectory() as directory:
            path = self._write(
                directory,
                "snapshot.json",
                snapshot([entry("alpha-provider", removal_status="removed")]),
            )

            status, stdout, stderr = run_cli(
                ["ecosystem", "plugin-catalog", "coverage", "--input", path, "--now", NOW],
                output_json=False,
            )

            self.assertEqual(status, 0, stderr)
            self.assertIn("catalog-rev-1", stdout)
            self.assertIn("alpha-provider: catalog_removal", stdout)
            self.assertIn("snapshot_limited", stdout)

    def test_the_packaged_outcome_surfaces_still_answer_and_say_what_they_cover(self) -> None:
        status, stdout, stderr = run_cli(["ecosystem", "awesome-hermes", "outcomes", "--json"])

        self.assertEqual(status, 0, stderr)
        self.assertEqual(json.loads(stdout)["coverage_scope"], PACKAGED_COVERAGE_SCOPE)


if __name__ == "__main__":
    unittest.main()
