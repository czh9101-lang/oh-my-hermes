from __future__ import annotations

import json
import re
import unittest

from _cli_harness import run_cli
from omh.catalogs.design_data import (
    DESIGN_DATA_CONTEXTS,
    DESIGN_DATA_KINDS,
    DESIGN_DATA_SCHEMA_VERSION,
    PALETTE_ROLE_KEYS,
    color_palettes,
    font_pairings,
    query_design_data,
    ux_guidelines,
)
from omh.commands.main import build_parser


HEX_PATTERN = re.compile(r"^#[0-9A-F]{6}$")


class DesignDataIntegrityTests(unittest.TestCase):
    def test_row_counts_stay_in_the_curated_range(self) -> None:
        self.assertEqual(len(color_palettes()), 16)
        self.assertEqual(len(font_pairings()), 12)
        self.assertEqual(len(ux_guidelines()), 22)

    def test_row_names_are_unique_within_each_kind(self) -> None:
        for rows in (color_palettes(), font_pairings(), ux_guidelines()):
            names = [row.name for row in rows]
            self.assertEqual(len(names), len(set(names)), names)

    def test_every_row_declares_known_non_empty_contexts(self) -> None:
        for rows in (color_palettes(), font_pairings(), ux_guidelines()):
            for row in rows:
                self.assertTrue(row.contexts, row.name)
                self.assertEqual(tuple(sorted(row.contexts)), row.contexts, row.name)
                for context in row.contexts:
                    self.assertIn(context, DESIGN_DATA_CONTEXTS, row.name)

    def test_every_context_is_covered_by_every_kind(self) -> None:
        for kind in DESIGN_DATA_KINDS:
            covered = {context for row in _rows_for(kind) for context in row.contexts}
            self.assertEqual(covered, set(DESIGN_DATA_CONTEXTS), kind)

    def test_palette_roles_are_complete_and_uppercase_hex(self) -> None:
        for palette in color_palettes():
            roles = dict(palette.roles)
            self.assertEqual(tuple(role for role, _ in palette.roles), PALETTE_ROLE_KEYS, palette.name)
            self.assertIn(palette.mode, ("light", "dark"), palette.name)
            self.assertTrue(palette.note.strip(), palette.name)
            for role, value in roles.items():
                self.assertRegex(value, HEX_PATTERN, f"{palette.name}/{role}")

    def test_font_pairings_carry_fallbacks_and_cjk_notes(self) -> None:
        for pairing in font_pairings():
            self.assertIn(",", pairing.display_stack, pairing.name)
            self.assertIn(",", pairing.body_stack, pairing.name)
            self.assertTrue(pairing.cjk_note.strip(), pairing.name)
            self.assertTrue(pairing.note.strip(), pairing.name)

    def test_korean_body_floor_is_stated_somewhere_in_each_kind(self) -> None:
        self.assertTrue(any("14px" in pairing.cjk_note for pairing in font_pairings()))
        self.assertTrue(any("14px" in row.guideline for row in ux_guidelines()))

    def test_ux_rows_carry_a_guideline_and_a_one_line_rationale(self) -> None:
        for row in ux_guidelines():
            self.assertTrue(row.guideline.strip(), row.name)
            self.assertTrue(row.rationale.strip(), row.name)
            self.assertNotIn("\n", row.rationale, row.name)


class DesignDataQueryTests(unittest.TestCase):
    def test_query_payload_shape(self) -> None:
        payload = query_design_data("palette", "fintech")

        self.assertEqual(payload["schema_version"], DESIGN_DATA_SCHEMA_VERSION)
        self.assertEqual(payload["kind"], "palette")
        self.assertEqual(payload["context"], "fintech")
        self.assertEqual(payload["available_contexts"], list(DESIGN_DATA_CONTEXTS))
        self.assertEqual(payload["count"], len(payload["rows"]))
        self.assertTrue(payload["rows"])
        for row in payload["rows"]:
            self.assertIn("fintech", row["contexts"])
            self.assertEqual(sorted(row["roles"]), sorted(PALETTE_ROLE_KEYS))

    def test_context_filter_narrows_the_row_set(self) -> None:
        everything = query_design_data("ux")
        filtered = query_design_data("ux", "mobile")

        self.assertEqual(everything["context"], "")
        self.assertEqual(everything["count"], len(ux_guidelines()))
        self.assertLess(filtered["count"], everything["count"])
        self.assertTrue(filtered["count"])

    def test_rows_are_sorted_by_name_for_byte_stable_output(self) -> None:
        for kind in DESIGN_DATA_KINDS:
            names = [row["name"] for row in query_design_data(kind)["rows"]]
            self.assertEqual(names, sorted(names), kind)

    def test_unknown_kind_and_context_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            query_design_data("colours")
        with self.assertRaises(ValueError):
            query_design_data("palette", "spaceship")


class DesignDataCliTests(unittest.TestCase):
    def test_design_command_is_wired_into_root_help(self) -> None:
        help_text = build_parser().format_help()

        self.assertIn("design", help_text)
        self.assertIn("Query curated local design reference data", help_text)

    def test_cli_json_matches_the_query_payload(self) -> None:
        status, stdout, _ = run_cli(["design", "data", "--kind", "font", "--context", "mobile", "--json"])

        self.assertEqual(status, 0)
        self.assertEqual(json.loads(stdout), query_design_data("font", "mobile"))

    def test_cli_summary_is_deterministic_and_english(self) -> None:
        first = run_cli(["design", "data", "--kind", "palette", "--context", "dev-tool"], output_json=False)
        second = run_cli(["design", "data", "--kind", "palette", "--context", "dev-tool"], output_json=False)

        self.assertEqual(first[0], 0)
        self.assertEqual(first[1], second[1])
        self.assertIn("Design reference data: palette (dev-tool)", first[1])
        self.assertIn("Slate Console", first[1])
        self.assertIn("For machine-readable output, rerun with `--json`.", first[1])

    def test_cli_rejects_an_unknown_context_and_lists_the_valid_ones(self) -> None:
        status, _, stderr = run_cli(["design", "data", "--kind", "ux", "--context", "spaceship"])

        self.assertEqual(status, 2)
        self.assertIn("unknown design context: spaceship", stderr)
        self.assertIn("fintech", stderr)

    def test_cli_summary_without_context_covers_every_row(self) -> None:
        status, stdout, _ = run_cli(["design", "data", "--kind", "ux"], output_json=False)

        self.assertEqual(status, 0)
        self.assertIn("Design reference data: ux (all contexts)", stdout)
        self.assertIn(f"Rows: {len(ux_guidelines())}", stdout)


def _rows_for(kind: str):
    if kind == "palette":
        return color_palettes()
    if kind == "font":
        return font_pairings()
    return ux_guidelines()


if __name__ == "__main__":
    unittest.main()
