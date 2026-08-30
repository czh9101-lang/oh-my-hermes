from __future__ import annotations

import re
import unittest
from pathlib import Path

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh.tools import BUILTIN_TOOL_NAMES, builtin_tool_schemas  # noqa: E402
from omh.quality.schema_overlap import (  # noqa: E402
    ENUM_SEMANTIC_RESIDUE_MIN_CHARS,
    KEEP_REASONS,
    REVIEWED_OVERLAP_DECISIONS,
    SCHEMA_OVERLAP_SCHEMA_VERSION,
    VERDICT_KEEP,
    VERDICT_PRUNE_CANDIDATE,
    format_schema_overlap_report,
    schema_overlap_findings,
    schema_overlap_payload,
    stale_overlap_decisions,
    unreviewed_overlap_findings,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TOOLS_DIR = _REPO_ROOT / "src" / "plugin_bundle" / "omh" / "tools"
_REGISTER = _REPO_ROOT / "src" / "plugin_bundle" / "omh" / "__init__.py"


class ToolSurfaceCoverageTests(unittest.TestCase):
    """The probe is only honest if it walks every registered tool.

    Both checks below read committed repository sources, which is why every
    read here passes `encoding="utf-8"` explicitly. `Path.read_text()` with no
    encoding uses `locale.getencoding()` -- cp1252 on Windows -- and the tool
    sources carry non-ASCII on purpose (`run_summary_tool.py` documents its
    localized output in Korean), so the default would decode-error there and
    nowhere else.
    """

    def test_collector_covers_every_schema_defined_under_tools(self) -> None:
        declared = {
            match.group(1)
            for path in sorted(_TOOLS_DIR.glob("*.py"))
            for match in re.finditer(
                r"^(OMH_[A-Z_]+_SCHEMA)\s*=\s*\{",
                path.read_text(encoding="utf-8"),
                re.MULTILINE,
            )
        }
        collected = {
            f"OMH_{str(schema['name']).removeprefix('omh_').upper()}_SCHEMA"
            for schema in builtin_tool_schemas()
        }
        # Two names differ from the mechanical derivation; assert the count and
        # the tool names instead of guessing at constant spelling.
        self.assertEqual(len(builtin_tool_schemas()), len(declared), sorted(declared - collected))

    def test_collector_and_name_list_match_the_registered_tools(self) -> None:
        registered = set(
            re.findall(
                r'ctx\.register_tool\(\s*"([a-z_]+)"',
                _REGISTER.read_text(encoding="utf-8"),
            )
        )
        self.assertEqual(set(BUILTIN_TOOL_NAMES), registered)
        self.assertEqual(
            [str(schema["name"]) for schema in builtin_tool_schemas()],
            sorted(BUILTIN_TOOL_NAMES),
        )


class SchemaOverlapProbeTests(unittest.TestCase):
    def test_every_overlap_carries_a_reviewed_verdict(self) -> None:
        # Given: the probe over the live tool surface.
        payload = schema_overlap_payload()

        # Then: nothing is unclassified and no decision outlives its finding.
        self.assertEqual(
            payload["unreviewed"],
            [],
            format_schema_overlap_report(payload),
        )
        self.assertEqual(payload["stale_decisions"], [], format_schema_overlap_report(payload))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["schema_version"], SCHEMA_OVERLAP_SCHEMA_VERSION)

    def test_reviewed_decisions_state_a_verdict_and_a_reason(self) -> None:
        for finding_id, reason in sorted(REVIEWED_OVERLAP_DECISIONS.items()):
            with self.subTest(finding=finding_id):
                self.assertTrue(
                    reason.startswith(("keep — ", "prune candidate — ")),
                    f"{finding_id}: a decision names its verdict and why",
                )
                self.assertGreater(len(reason), 60, f"{finding_id}: a reason is not a label")

    def test_probe_reports_candidates_and_never_deletes(self) -> None:
        # The probe's whole output is a report; nothing it returns is a mutation
        # and nothing in the payload authorises one.
        payload = schema_overlap_payload()
        self.assertIn("never an automatic delete", str(payload["claim_boundary"]))
        self.assertIn("git blame", str(payload["claim_boundary"]))
        self.assertEqual(
            payload["finding_count"],
            payload["prune_candidate_count"] + payload["keep_count"],
        )
        for finding in payload["findings"]:
            with self.subTest(finding=finding["id"]):
                self.assertIn(finding["verdict"], {VERDICT_KEEP, VERDICT_PRUNE_CANDIDATE})
                if finding["verdict"] == VERDICT_KEEP:
                    self.assertIn(finding["keep_reason"], KEEP_REASONS)
                else:
                    self.assertEqual(finding["keep_reason"], "")


class OverlapRuleTests(unittest.TestCase):
    """Each rule is exercised on a synthetic schema, both directions."""

    def _findings(self, properties: dict, required: list[str] | None = None) -> dict[str, str]:
        schema = {
            "name": "demo_tool",
            "description": "Demo.",
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
            },
        }
        return {finding.rule: finding.verdict for finding in schema_overlap_findings([schema])}

    def test_bare_type_restatement_is_a_prune_candidate(self) -> None:
        self.assertEqual(
            self._findings({"limit": {"type": "integer", "description": "An integer."}}),
            {"OVERLAP_TYPE_RESTATED": VERDICT_PRUNE_CANDIDATE},
        )

    def test_a_description_that_says_what_the_type_is_for_is_not_a_finding(self) -> None:
        self.assertEqual(
            self._findings(
                {"limit": {"type": "integer", "description": "How many route candidates to score."}}
            ),
            {},
        )

    def test_enum_listing_is_a_candidate_and_enum_semantics_are_a_keep(self) -> None:
        listing = {"mode": {"type": "string", "enum": ["fast", "full"], "description": "fast or full."}}
        self.assertEqual(
            self._findings(listing), {"OVERLAP_ENUM_RESTATED": VERDICT_PRUNE_CANDIDATE}
        )

        semantics = {
            "mode": {
                "type": "string",
                "enum": ["fast", "full"],
                "description": (
                    "fast scores only the explicit trigger table and returns within one turn; "
                    "full scores the whole catalog and may ask a clarifying question first."
                ),
            }
        }
        self.assertEqual(self._findings(semantics), {"OVERLAP_ENUM_RESTATED": VERDICT_KEEP})

    def test_the_enum_residue_floor_is_where_the_verdict_flips(self) -> None:
        members = ["fast", "full"]
        # Residue is what survives after the member names are struck out, so
        # "or" and its separator already count: pad to one below the floor.
        padding = "x" * (ENUM_SEMANTIC_RESIDUE_MIN_CHARS - 1 - len("or "))
        below = {"mode": {"type": "string", "enum": members, "description": f"fast or full {padding}"}}
        above = {"mode": {"type": "string", "enum": members, "description": f"fast or full {padding}y"}}
        self.assertEqual(self._findings(below)["OVERLAP_ENUM_RESTATED"], VERDICT_PRUNE_CANDIDATE)
        self.assertEqual(self._findings(above)["OVERLAP_ENUM_RESTATED"], VERDICT_KEEP)

    def test_a_bare_clamp_restatement_is_a_candidate_and_bound_meaning_is_a_keep(self) -> None:
        bare = {"depth": {"type": "integer", "minimum": 0, "description": "Nesting, at least 0."}}
        self.assertEqual(
            self._findings(bare), {"OVERLAP_BOUND_RESTATED": VERDICT_PRUNE_CANDIDATE}
        )

        meaningful = {
            "depth": {
                "type": "integer",
                "minimum": 0,
                "description": "Nesting level; 0 = top-level task.",
            }
        }
        self.assertEqual(self._findings(meaningful), {"OVERLAP_BOUND_RESTATED": VERDICT_KEEP})

    def test_a_bare_default_restatement_is_a_candidate_and_direction_is_a_keep(self) -> None:
        bare = {
            "source": {"type": "string", "default": "hermes", "description": "Default is hermes."}
        }
        self.assertEqual(
            self._findings(bare), {"OVERLAP_DEFAULT_RESTATED": VERDICT_PRUNE_CANDIDATE}
        )

        # `gitignore: true` does not say "respects gitignore": the direction a
        # default points is the thing the schema cannot carry.
        direction = {
            "gitignore": {
                "type": "boolean",
                "default": "true",
                "description": "Defaults to the true setting, which means ignored paths are skipped.",
            }
        }
        self.assertEqual(self._findings(direction), {"OVERLAP_DEFAULT_RESTATED": VERDICT_KEEP})

    def test_unconditional_required_is_a_candidate_and_conditional_required_is_a_keep(self) -> None:
        unconditional = {"role": {"type": "string", "description": "Role name. Required."}}
        self.assertEqual(
            self._findings(unconditional, ["role"]),
            {"OVERLAP_REQUIRED_RESTATED": VERDICT_PRUNE_CANDIDATE},
        )

        conditional = {
            "role": {"type": "string", "description": "Role name. Required only when action=read."}
        }
        self.assertEqual(
            self._findings(conditional, ["role"]),
            {"OVERLAP_REQUIRED_RESTATED": VERDICT_KEEP},
        )


class OverlapGateFailureTests(unittest.TestCase):
    def test_an_unreviewed_finding_fails_with_paste_ready_instructions(self) -> None:
        schema = {
            "name": "unreviewed_tool",
            "description": "Demo.",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "An integer."}},
            },
        }
        findings = schema_overlap_findings([schema])
        unreviewed = unreviewed_overlap_findings(findings)
        self.assertEqual([finding.id for finding in unreviewed], ["unreviewed_tool.limit:OVERLAP_TYPE_RESTATED"])

        report = format_schema_overlap_report(
            {
                "finding_count": len(findings),
                "prune_candidate_count": len(findings),
                "keep_count": 0,
                "findings": [finding.to_payload() for finding in findings],
                "unreviewed": [finding.to_payload() for finding in unreviewed],
                "stale_decisions": [],
                "claim_boundary": "",
            }
        )
        self.assertIn("REVIEWED_OVERLAP_DECISIONS", report)
        self.assertIn("src/quality/schema_overlap.py", report)
        self.assertIn("unreviewed_tool.limit:OVERLAP_TYPE_RESTATED", report)

    def test_a_decision_whose_overlap_is_gone_is_reported_stale(self) -> None:
        self.assertEqual(stale_overlap_decisions([]), sorted(REVIEWED_OVERLAP_DECISIONS))


if __name__ == "__main__":
    unittest.main()
