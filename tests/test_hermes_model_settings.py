from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _local_package import load_local_package

load_local_package()
from omh.paths import OmhPaths
from omh.surfaces.hermes_model_settings import (
    HERMES_AUX_ALIASES,
    HERMES_MODEL_SETTINGS_SCHEMA_VERSION,
    read_hermes_model_settings,
)


class HermesModelSettingsTests(unittest.TestCase):
    def _read(self, config_text: str) -> dict[str, object]:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        hermes_home = root / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(config_text, encoding="utf-8")
        return read_hermes_model_settings(OmhPaths(root / ".omh", hermes_home))

    def test_reads_live_shaped_default_and_all_inherit_auxiliaries(self) -> None:
        result = self._read(
            "model:\n  default: anthropic/claude-opus-4.6\n  provider: auto\n"
            "  base_url: https://example.invalid\nagent:\n  reasoning_effort: medium\n"
        )
        self.assertEqual(result["schema_version"], HERMES_MODEL_SETTINGS_SCHEMA_VERSION)
        self.assertTrue(result["observed"])
        self.assertEqual(result["reason"], "")
        self.assertEqual(result["provider"], "auto")
        self.assertEqual(
            result["aliases"][0],
            {
                "alias": "main",
                "model": "anthropic/claude-opus-4.6",
                "effort": "medium",
                "source": "config.model.default",
                "configured": True,
                "label": "anthropic/claude-opus-4.6:medium",
            },
        )
        self.assertEqual(result["configured_count"], 1)
        self.assertEqual(result["inherit_count"], 14)

    def test_reads_known_auxiliary_model_and_effort_without_resolving_inheritance(self) -> None:
        result = self._read(
            "model:\n  default: main-model\nauxiliary:\n"
            "  vision:\n    model:\n    reasoning_effort: high\n"
            "  web_extract:\n    model: google/gemini-2.5-flash\n    reasoning_effort: low\n"
            "  invented_alias:\n    model: must-not-appear\n"
        )
        aliases = {entry["alias"]: entry for entry in result["aliases"]}
        self.assertEqual(aliases["vision"]["label"], "inherit")
        self.assertFalse(aliases["vision"]["configured"])
        self.assertEqual(aliases["vision"]["effort"], "high")
        self.assertEqual(aliases["web_extract"]["model"], "google/gemini-2.5-flash")
        self.assertEqual(aliases["web_extract"]["label"], "google/gemini-2.5-flash:low")
        self.assertTrue(aliases["web_extract"]["configured"])
        self.assertNotIn("invented_alias", aliases)
        self.assertEqual(result["configured_count"], 2)
        self.assertEqual(result["inherit_count"], 13)

    def test_strips_quotes_and_comments_from_configured_scalars(self) -> None:
        result = self._read(
            "model:\n  default: 'anthropic/model#revision'  # main note\n"
            "# top-level comment does not end the model block\n"
            '  provider: "auto" # provider note\n'
            "agent:\n  reasoning_effort: medium # effort note\n"
            "auxiliary:\n  web_extract:\n    # task comment\n"
            "    model: \"gpt-5.6-sol\"  # inline note\n"
        )
        self.assertEqual(result["provider"], "auto")
        self.assertEqual(result["aliases"][0]["model"], "anthropic/model#revision")
        self.assertEqual(result["aliases"][0]["effort"], "medium")
        web_extract = next(entry for entry in result["aliases"] if entry["alias"] == "web_extract")
        self.assertEqual(web_extract["model"], "gpt-5.6-sol")

    def test_missing_config_degrades_to_config_unreadable(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = read_hermes_model_settings(OmhPaths(root / ".omh", root / ".hermes"))
        self.assertFalse(result["observed"])
        self.assertEqual(result["reason"], "config_unreadable")
        self.assertEqual(result["aliases"], [])
        self.assertEqual(result["configured_count"], 0)
        self.assertEqual(result["inherit_count"], 0)

    def test_malformed_utf8_config_degrades_to_config_unreadable(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            hermes_home = root / ".hermes"
            hermes_home.mkdir()
            (hermes_home / "config.yaml").write_bytes(b"model:\n  default: \xff\n")

            result = read_hermes_model_settings(OmhPaths(root / ".omh", hermes_home))

        self.assertFalse(result["observed"])
        self.assertEqual(result["reason"], "config_unreadable")
        self.assertEqual(result["aliases"], [])

    def test_block_scalar_degrades_instead_of_publishing_yaml_syntax(self) -> None:
        result = self._read("model:\n  default: >-\n    gpt-5.6-sol\n")

        self.assertFalse(result["observed"])
        self.assertEqual(result["reason"], "config_unreadable")
        self.assertEqual(result["aliases"], [])

    def test_yaml_null_scalars_mean_inherit(self) -> None:
        result = self._read(
            "model:\n  default: null\nauxiliary:\n"
            "  web_extract:\n    model: ~\n"
        )

        aliases = {entry["alias"]: entry for entry in result["aliases"]}
        self.assertTrue(result["observed"])
        self.assertFalse(aliases["main"]["configured"])
        self.assertEqual(aliases["main"]["label"], "inherit")
        self.assertFalse(aliases["web_extract"]["configured"])
        self.assertEqual(result["configured_count"], 0)
        self.assertEqual(result["inherit_count"], 15)

    def test_tab_indentation_degrades_as_unreadable(self) -> None:
        result = self._read("model:\n\tdefault: gpt-5.6-sol\n")

        self.assertFalse(result["observed"])
        self.assertEqual(result["reason"], "config_unreadable")

    def test_flow_mapping_degrades_as_unreadable(self) -> None:
        result = self._read("model: {default: gpt-5.6-sol}\n")

        self.assertFalse(result["observed"])
        self.assertEqual(result["reason"], "config_unreadable")

    def test_nested_tracked_scalar_degrades_as_unreadable(self) -> None:
        configs = (
            "model:\n  default:\n    name: gpt-5.6-sol\n",
            "auxiliary:\n  web_extract:\n    model:\n      name: gpt-5.6-sol\n",
        )
        for config_text in configs:
            with self.subTest(config_text=config_text):
                result = self._read(config_text)

                self.assertFalse(result["observed"])
                self.assertEqual(result["reason"], "config_unreadable")

    def test_delegation_block_is_read_beside_the_aliases(self) -> None:
        # `delegation:` is what every delegate_task child inherits when no OMH
        # route is set; it is reported as its own entry so the alias tuple
        # stays the exact auxiliary contract.
        result = self._read(
            "model:\n  default: gpt-5.6-sol\nagent:\n  reasoning_effort: medium\n"
            "delegation:\n  model: 'gpt-6-astra'\n  reasoning_effort: 'high'\n  provider: 'openai-codex'\n"
            "  max_iterations: 250\n"
        )
        self.assertEqual(
            result["delegation"],
            {
                "alias": "delegation",
                "model": "gpt-6-astra",
                "effort": "high",
                "provider": "openai-codex",
                "source": "config.delegation.model",
                "configured": True,
                "label": "gpt-6-astra:high",
            },
        )
        self.assertEqual(tuple(entry["alias"] for entry in result["aliases"]), ("main",) + HERMES_AUX_ALIASES)
        absent = self._read("model:\n  default: gpt-5.6-sol\n")
        self.assertFalse(absent["delegation"]["configured"])
        self.assertEqual(absent["delegation"]["label"], "inherit")

    def test_alias_order_is_the_exact_hermes_contract(self) -> None:
        result = self._read("model:\n  default:\n")
        self.assertEqual(
            tuple(entry["alias"] for entry in result["aliases"]),
            ("main",) + HERMES_AUX_ALIASES,
        )
        self.assertEqual(len(HERMES_AUX_ALIASES), 14)
        self.assertEqual(result["configured_count"], 0)
        self.assertEqual(result["inherit_count"], 15)


if __name__ == "__main__":
    unittest.main()
