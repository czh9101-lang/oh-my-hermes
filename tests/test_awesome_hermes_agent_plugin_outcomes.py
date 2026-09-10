from __future__ import annotations

from importlib.resources import files
import json
import unittest

from _cli_harness import run_cli
from omh.catalogs.awesome_hermes_agent import PACKAGED_COVERAGE_SCOPE
from omh.catalogs.awesome_hermes_agent_outcomes import (
    PLUGIN_OUTCOME_SCHEMA_VERSION,
    _parse_plugin_outcomes,
    awesome_hermes_plugin_outcomes,
)


def stored_matrix() -> dict[str, object]:
    """The envelope as it sits on disk.

    `_parse_plugin_outcomes` validates the stored envelope, whose key set is
    closed. `awesome_hermes_plugin_outcomes` returns a projection of it that
    also carries the coverage-scope label, so feeding the projection back into
    the parser tests a shape the parser never receives.
    """
    resource = files("omh.catalogs").joinpath("awesome_hermes_agent_plugin_outcomes.json")
    return json.loads(resource.read_text(encoding="utf-8"))


class AwesomeHermesAgentPluginOutcomeTests(unittest.TestCase):
    def test_selected_plugin_outcomes_distinguish_native_and_contract_only_capabilities(self) -> None:
        payload = awesome_hermes_plugin_outcomes()

        self.assertEqual(payload["schema_version"], PLUGIN_OUTCOME_SCHEMA_VERSION)
        outcomes = {outcome["plugin_id"]: outcome for outcome in payload["outcomes"]}
        self.assertEqual(
            set(outcomes),
            {
                "clawrouter-hermes",
                "cronalytics",
                "hermes-doppler",
                "hermes-plugin-guard",
                "hermes-plugin-slash-prompts",
                "robrain",
                "rtk-hermes",
            },
        )
        expected_capabilities = {
            "clawrouter-hermes": ["provider-profile-posture"],
            "cronalytics": ["run-efficiency"],
            "hermes-doppler": ["provider-profile-posture"],
            "hermes-plugin-guard": ["plugin-risk-audit"],
            "hermes-plugin-slash-prompts": ["prompt-import-readiness"],
            "robrain": ["decision-recall"],
            "rtk-hermes": ["run-efficiency"],
        }
        for plugin_id, capability_ids in expected_capabilities.items():
            outcome = outcomes[plugin_id]
            self.assertEqual(outcome["owner_boundary"], "omh")
            self.assertEqual(outcome["claim_status"], "prepared_not_observed")
            self.assertEqual(outcome["native_capability_ids"], capability_ids)
            self.assertTrue({reference["kind"] for reference in outcome["evidence_refs"]}.intersection({"code", "test"}))
        self.assertEqual(outcomes["rtk-hermes"]["implementation_state"], "contract_only")
        self.assertIn("does not intercept shell calls", outcomes["rtk-hermes"]["rationale"])
        for plugin_id in set(outcomes) - {"rtk-hermes"}:
            self.assertEqual(outcomes[plugin_id]["implementation_state"], "omh_native")

    def test_loader_rejects_native_claims_without_implementation_evidence(self) -> None:
        invalid = stored_matrix()
        invalid["outcomes"][0]["evidence_refs"] = []

        with self.assertRaisesRegex(ValueError, "code or test evidence"):
            _parse_plugin_outcomes(invalid)

    def test_the_matrix_is_labelled_as_a_packaged_snapshot_not_host_coverage(self) -> None:
        payload = awesome_hermes_plugin_outcomes()

        self.assertEqual(payload["coverage_scope"], PACKAGED_COVERAGE_SCOPE)
        self.assertIn("not the active host catalog", str(payload["coverage_scope_note"]))

    def test_ecosystem_cli_emits_the_outcomes_matrix(self) -> None:
        status, stdout, stderr = run_cli(["ecosystem", "awesome-hermes", "outcomes", "--json"])

        self.assertEqual(status, 0, stderr)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(payload["schema_version"], PLUGIN_OUTCOME_SCHEMA_VERSION)
        self.assertEqual(len(payload["outcomes"]), 7)
        self.assertIn("claim_boundary", payload)


if __name__ == "__main__":
    unittest.main()
