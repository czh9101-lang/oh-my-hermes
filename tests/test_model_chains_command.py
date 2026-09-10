"""Contracts for `omh model-chains` — the supported editor over the
mixture chain override document.

show prints the effective per-category state with origins; set writes one
category through the same validation the plugin reader enforces and --clear
returns it to the shipped default; interview walks every category with
numbered options on a terminal and refuses (with the scriptable path) on a
pipe. Every path converges on `<omh-home>/routing/model-chains.json`.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from _cli_harness import run_cli

from omh.commands.model_chains import _ultrafast_variant
from omh.plugin_bundle.omh.hermes_delegation import HERMES_MIXTURE_CATEGORY_CHAINS


def _base(root: Path) -> list[str]:
    return ["--omh-home", str(root / ".omh")]


def _chains_path(root: Path) -> Path:
    return root / ".omh" / "routing" / "model-chains.json"


class ModelChainsShowTests(unittest.TestCase):
    def test_show_lists_every_category_with_default_origin(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            status, stdout, stderr = run_cli(_base(root) + ["model-chains", "show"], output_json=False)
            self.assertEqual((status, stderr), (0, ""))
            for category in HERMES_MIXTURE_CATEGORY_CHAINS:
                self.assertIn(f"  {category}: ", stdout)
            self.assertIn("[absent]", stdout)
            self.assertNotIn("(override)", stdout)

    def test_show_json_reports_state_schema_and_origins(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            status, stdout, stderr = run_cli(
                _base(root) + ["model-chains", "show", "--json"], output_json=False
            )
            self.assertEqual((status, stderr), (0, ""))
            payload = json.loads(stdout)
            self.assertEqual(payload["schema_version"], "model_chain_state/v1")
            self.assertEqual(
                {row["category"] for row in payload["categories"]},
                set(HERMES_MIXTURE_CATEGORY_CHAINS),
            )
            self.assertTrue(all(row["origin"] == "default" for row in payload["categories"]))


class ModelChainsSetTests(unittest.TestCase):
    def test_set_writes_one_category_and_show_marks_the_override(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            status, stdout, stderr = run_cli(
                _base(root)
                + ["model-chains", "set", "quick", "kimi-k3-ultrafast:low, glm-5.2-ultrafast:low"],
                output_json=False,
            )
            self.assertEqual((status, stderr), (0, ""))
            self.assertIn("quick: kimi-k3-ultrafast:low, glm-5.2-ultrafast:low (override)", stdout)
            document = json.loads(_chains_path(root).read_text(encoding="utf-8"))
            self.assertEqual(document["schema_version"], "mixture_chain_overrides/v1")
            self.assertEqual(
                document["categories"]["quick"][0],
                {"model": "kimi-k3-ultrafast", "reasoning_effort": "low"},
            )

            status, stdout, _ = run_cli(_base(root) + ["model-chains", "show"], output_json=False)
            self.assertEqual(status, 0)
            self.assertIn("quick: kimi-k3-ultrafast:low, glm-5.2-ultrafast:low (override)", stdout)
            # Untouched categories keep the shipped default.
            self.assertNotIn("architect: claude-fable-5-1:xhigh, gpt-6-astra:xhigh, kimi-k3:xhigh (override)", stdout)

    def test_clear_returns_the_category_to_the_shipped_default(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_cli(_base(root) + ["model-chains", "set", "deep", "gpt-5.6-terra:xhigh"], output_json=False)
            status, stdout, stderr = run_cli(
                _base(root) + ["model-chains", "set", "deep", "--clear"], output_json=False
            )
            self.assertEqual((status, stderr), (0, ""))
            self.assertIn("deep: gpt-5.6-terra:high, deepseek-flash:high (default)", stdout)
            document = json.loads(_chains_path(root).read_text(encoding="utf-8"))
            self.assertNotIn("deep", document["categories"])

    def test_set_refuses_unknown_categories_and_non_token_models(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            status, _, stderr = run_cli(
                _base(root) + ["model-chains", "set", "warp-drive", "kimi-k3"], output_json=False
            )
            self.assertEqual(status, 2)
            self.assertIn("unknown category", stderr)
            status, _, stderr = run_cli(
                _base(root) + ["model-chains", "set", "quick", "bad model!"], output_json=False
            )
            self.assertEqual(status, 2)
            self.assertIn("not a plain model identifier", stderr)
            self.assertFalse(_chains_path(root).exists())

    def test_set_preserves_other_override_categories(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_cli(_base(root) + ["model-chains", "set", "quick", "kimi-k3-ultrafast:low"], output_json=False)
            run_cli(_base(root) + ["model-chains", "set", "deep", "gpt-5.6-terra:xhigh"], output_json=False)
            document = json.loads(_chains_path(root).read_text(encoding="utf-8"))
            self.assertEqual(set(document["categories"]), {"quick", "deep"})


class ModelChainsInterviewTests(unittest.TestCase):
    def test_interview_refuses_without_a_terminal_and_names_the_scriptable_path(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            status, _, stderr = run_cli(_base(root) + ["model-chains", "interview"], output_json=False)
            self.assertEqual(status, 2)
            self.assertIn("model-chains set", stderr)

    def test_interview_applies_numbered_choices_and_custom_entry(self) -> None:
        # Category order is the shipped dict order. Without an override the
        # options are 1) keep, [2) Ultrafast when the chain has a swappable
        # member], last) custom. Keep everything except `quick` (Ultrafast)
        # and `writing` (custom entry).
        answers = []
        for name, chain in HERMES_MIXTURE_CATEGORY_CHAINS.items():
            has_ultrafast = any(model in ("glm-5.2", "kimi-k3") for model, _ in chain)
            if name == "quick":
                answers.append("2" if has_ultrafast else "1")
            elif name == "writing":
                answers.extend(["3" if has_ultrafast else "2", "qwen3-coder:high, kimi-k3:high"])
            else:
                answers.append("")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            responses = iter(answers)
            with (
                patch("omh.commands.model_chains._stdin_is_tty", return_value=True),
                patch("builtins.input", side_effect=lambda *_: next(responses)),
            ):
                status, stdout, stderr = run_cli(
                    _base(root) + ["model-chains", "interview"], output_json=False
                )
            self.assertEqual((status, stderr), (0, ""), stdout)
            document = json.loads(_chains_path(root).read_text(encoding="utf-8"))
            self.assertEqual(set(document["categories"]), {"quick", "writing"})
            quick_models = [entry["model"] for entry in document["categories"]["quick"]]
            self.assertIn("kimi-k3-ultrafast", quick_models)
            self.assertNotIn("kimi-k3", quick_models)
            self.assertEqual(
                document["categories"]["writing"],
                [
                    {"model": "qwen3-coder", "reasoning_effort": "high"},
                    {"model": "kimi-k3", "reasoning_effort": "high"},
                ],
            )

    def test_ultrafast_option_never_invents_an_unknown_variant(self) -> None:
        # The offer follows the -ultrafast suffix only when that token is a known
        # model (shipped chains or price table). Since 2026-09-11 no shipped chain
        # names an Ultrafast tier, so the offer rests on the retained price rows
        # (`glm-5.2-ultrafast`, `kimi-k3-ultrafast`). deepseek-v3.2-ultrafast exists
        # in neither source, so no swap is offered; an already-suffixed member never
        # re-swaps.
        self.assertEqual(
            _ultrafast_variant((("glm-5.2", "low"), ("kimi-k3", "high"))),
            (("glm-5.2-ultrafast", "low"), ("kimi-k3-ultrafast", "high")),
        )
        self.assertIsNone(_ultrafast_variant((("deepseek-v3.2", "high"),)))
        self.assertIsNone(_ultrafast_variant((("glm-5.2-ultrafast", "low"),)))


if __name__ == "__main__":
    unittest.main()
