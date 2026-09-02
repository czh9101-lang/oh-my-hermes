"""Provider entitlements: the setup interview's answer and how it reshapes chains.

Chains stay provider-neutral alias lists; the entitlement document is the one
place a machine's own accounts are described. Shaping reorders, never drops.
"""

from __future__ import annotations

import argparse
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()

from omh.coding.model_recommendations import SHIPPED_MODEL_RECOMMENDATIONS  # noqa: E402
from omh.coding.model_routing import CLAUDE_FRONTIER_CHAIN_MODELS  # noqa: E402
from omh.commands import setup as setup_module  # noqa: E402
from omh.config_adapter import configured_provider_ids  # noqa: E402
from omh.plugin_bundle.omh.hermes_delegation import (  # noqa: E402
    HERMES_MIXTURE_ALIAS_PROVIDER_FAMILIES,
    HERMES_MIXTURE_CATEGORY_CHAINS,
    PROVIDER_ENTITLEMENTS_SCHEMA_VERSION,
    PROVIDER_FAMILY_VOCABULARY,
    alias_is_served,
    effective_mixture_category_chains,
    entitlement_shaped_chain,
    load_provider_entitlements,
    parse_provider_entitlements,
    provider_entitlements_path,
)


def _entitlements(providers: dict[str, str], clis: list[str] | None = None) -> dict[str, object]:
    return {"providers": providers, "subscription_clis": list(clis or [])}


def _write(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


class ParityTests(unittest.TestCase):
    """The plugin bundle cannot import src/coding, so it embeds two mirrors."""

    def _catalog_families(self) -> dict[str, tuple[str, ...]]:
        families: dict[str, tuple[str, ...]] = {}
        for section in ("categories", "role_suggestions", "domain_affinities", "last_resort"):
            for chain in SHIPPED_MODEL_RECOMMENDATIONS[section].values():
                for candidate in chain:
                    alias = str(candidate["model_alias"])
                    listed = tuple(candidate["preferred_provider_families"])
                    self.assertEqual(families.get(alias, listed), listed, alias)
                    families[alias] = listed
        return families

    def test_alias_families_mirror_the_catalog(self) -> None:
        self.assertEqual(HERMES_MIXTURE_ALIAS_PROVIDER_FAMILIES, self._catalog_families())

    def test_family_vocabulary_is_the_catalog_union(self) -> None:
        union = sorted({family for families in self._catalog_families().values() for family in families})
        self.assertEqual(list(PROVIDER_FAMILY_VOCABULARY), union)


class ParseTests(unittest.TestCase):
    def test_valid_document(self) -> None:
        parsed, status = parse_provider_entitlements(
            {
                "schema_version": PROVIDER_ENTITLEMENTS_SCHEMA_VERSION,
                "providers": {"og": "gateway", "zai": "zai"},
                "subscription_clis": ["claude-code", "claude-code"],
            }
        )
        self.assertEqual(status, "applied")
        self.assertEqual(parsed, _entitlements({"og": "gateway", "zai": "zai"}, ["claude-code"]))

    def test_invalid_documents_yield_none(self) -> None:
        cases = [
            ([], "must be a JSON object"),
            ({"schema_version": "nope"}, "schema_version"),
            ({"schema_version": PROVIDER_ENTITLEMENTS_SCHEMA_VERSION, "extra": 1}, "unsupported fields"),
            ({"schema_version": PROVIDER_ENTITLEMENTS_SCHEMA_VERSION, "providers": []}, "providers must be"),
            ({"schema_version": PROVIDER_ENTITLEMENTS_SCHEMA_VERSION, "providers": {"bad id": "zai"}}, "plain identifier"),
            ({"schema_version": PROVIDER_ENTITLEMENTS_SCHEMA_VERSION, "providers": {"og": "warp"}}, "kind must be one of"),
            ({"schema_version": PROVIDER_ENTITLEMENTS_SCHEMA_VERSION, "subscription_clis": "claude-code"}, "must be a list"),
            ({"schema_version": PROVIDER_ENTITLEMENTS_SCHEMA_VERSION, "subscription_clis": ["cursor"]}, "must be one of"),
        ]
        for raw, fragment in cases:
            with self.subTest(raw=raw):
                parsed, status = parse_provider_entitlements(raw)
                self.assertIsNone(parsed)
                self.assertIn(fragment, status)

    def test_loader_reports_absent_and_unreadable(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertEqual(load_provider_entitlements(tmp), (None, "absent"))
            provider_entitlements_path(tmp).parent.mkdir(parents=True)
            provider_entitlements_path(tmp).write_text("{", encoding="utf-8")
            self.assertEqual(load_provider_entitlements(tmp), (None, "invalid: unreadable JSON"))


class ServingRuleTests(unittest.TestCase):
    def test_vendor_provider_serves_only_its_families(self) -> None:
        zai = _entitlements({"zai": "zai"})
        self.assertTrue(alias_is_served("glm-5.3", zai))
        self.assertFalse(alias_is_served("claude-fable-5-1", zai))
        self.assertFalse(alias_is_served("kimi-k3", zai))

    def test_gateway_and_unknown_serve_everything(self) -> None:
        for kind in ("gateway", "unknown", "openrouter", "opencode"):
            with self.subTest(kind=kind):
                self.assertTrue(alias_is_served("claude-fable-5-1", _entitlements({"p": kind})))

    def test_fail_open_without_providers_or_catalog_knowledge(self) -> None:
        self.assertTrue(alias_is_served("claude-fable-5-1", _entitlements({})))
        self.assertTrue(alias_is_served("some-new-model", _entitlements({"zai": "zai"})))

    def test_explicit_route_decides_before_families(self) -> None:
        routes = {"claude-fable-5": ("og", "anthropic/claude-fable-5")}
        # Routed to a confirmed provider: served even for a vendor-only account.
        self.assertTrue(alias_is_served("claude-fable-5", _entitlements({"og": "zai"}), routes))
        # Routed to a provider the operator did not confirm: unserved even
        # though a gateway is present.
        self.assertFalse(alias_is_served("claude-fable-5", _entitlements({"other": "gateway"}), routes))

    def test_shaping_is_a_stable_partition(self) -> None:
        zai = _entitlements({"zai": "zai"})
        shaped = entitlement_shaped_chain(HERMES_MIXTURE_CATEGORY_CHAINS["quick"], zai)
        aliases = [alias for alias, _ in shaped]
        self.assertEqual(aliases[:2], ["glm-5.3-flash", "glm-5.2-ultrafast"])
        self.assertEqual(sorted(aliases), sorted(alias for alias, _ in HERMES_MIXTURE_CATEGORY_CHAINS["quick"]))
        unserved = [alias for alias, _ in HERMES_MIXTURE_CATEGORY_CHAINS["quick"] if alias not in aliases[:2]]
        self.assertEqual(aliases[2:], unserved)

    def test_effective_chains_apply_entitlements_after_overrides(self) -> None:
        with TemporaryDirectory() as tmp:
            _write(
                Path(tmp) / "routing" / "model-chains.json",
                {
                    "schema_version": "mixture_chain_overrides/v1",
                    "categories": {"quick": [{"model": "claude-opus-5", "reasoning_effort": "low"}, {"model": "glm-5.3", "reasoning_effort": "low"}]},
                },
            )
            _write(
                provider_entitlements_path(tmp),
                {"schema_version": PROVIDER_ENTITLEMENTS_SCHEMA_VERSION, "providers": {"zai": "zai"}, "subscription_clis": []},
            )
            chains = effective_mixture_category_chains(tmp)
            self.assertEqual(chains["quick"], (("glm-5.3", "low"), ("claude-opus-5", "low")))
            # An untouched category is shaped from the shipped default.
            self.assertEqual(chains["unspecified-low"][0][0], "glm-5.3")

    def test_absent_document_leaves_chains_untouched(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertEqual(effective_mixture_category_chains(tmp), dict(HERMES_MIXTURE_CATEGORY_CHAINS))


class ConfigReaderTests(unittest.TestCase):
    def test_reads_provider_keys_and_default_provider(self) -> None:
        config = (
            "model:\n"
            "  provider: openai-codex\n"
            "  default: gpt-5.6-sol\n"
            "providers:\n"
            "  og:\n"
            "    base_url: https://example.invalid/v1\n"
            "  # commented: out\n"
            "  zai:\n"
            "    api_mode: chat_completions\n"
            "plugins:\n"
            "  enabled: [omh]\n"
        )
        self.assertEqual(configured_provider_ids(config), ["openai-codex", "og", "zai"])

    def test_empty_and_absent_sections(self) -> None:
        self.assertEqual(configured_provider_ids(""), [])
        self.assertEqual(configured_provider_ids("providers:\n  og:\n    base_url: x\nmodel:\n  provider: og\n"), ["og"])


class SetupInterviewTests(unittest.TestCase):
    def _paths(self, root: Path):
        args = argparse.Namespace(omh_home=str(root / ".omh"), hermes_home=str(root / ".hermes"), scope=None)
        return setup_module._paths(args)

    def _config(self, paths, text: str) -> None:
        paths.hermes_config_path.parent.mkdir(parents=True, exist_ok=True)
        paths.hermes_config_path.write_text(text, encoding="utf-8")

    def test_interview_records_answers_and_seeds_claude_code(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            self._config(paths, "model:\n  provider: og\nproviders:\n  og:\n    base_url: x\n  zai:\n    base_url: y\n")
            args = argparse.Namespace()
            detected = {
                "claude-code": {"binary_present": True, "login_marker": "present"},
                "codex": {"binary_present": True, "login_marker": "absent"},
            }
            # record? yes; hold og? yes; hold zai? no; claude-code? yes; codex? no
            answers = iter([True, True, False, True, False])
            with patch.object(setup_module, "_detect_external_cli_profiles", return_value=detected), patch.object(
                setup_module, "_ask_yes_no", side_effect=lambda *a, **k: next(answers)
            ), patch.object(setup_module, "_ask_single_choice", return_value="gateway"), patch.object(
                setup_module, "_use_color", return_value=False
            ):
                setup_module._ask_provider_entitlements(args, paths, "en")

            document = json.loads(provider_entitlements_path(paths.omh_home).read_text(encoding="utf-8"))
            self.assertEqual(
                document,
                {
                    "providers": {"og": "gateway"},
                    "schema_version": PROVIDER_ENTITLEMENTS_SCHEMA_VERSION,
                    "subscription_clis": ["claude-code"],
                },
            )
            prefs = json.loads((paths.omh_home / "routing" / "dispatch-models.json").read_text(encoding="utf-8"))
            self.assertEqual(prefs["profiles"], {"claude-code": CLAUDE_FRONTIER_CHAIN_MODELS[0]})
            # A second call in the same run asks nothing more.
            with patch.object(setup_module, "_ask_yes_no") as yes_no:
                setup_module._ask_provider_entitlements(args, paths, "en")
            yes_no.assert_not_called()

    def test_declining_writes_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._paths(root)
            self._config(paths, "providers:\n  og:\n    base_url: x\n")
            args = argparse.Namespace()
            with patch.object(
                setup_module, "_detect_external_cli_profiles", return_value={"claude-code": {"binary_present": False}, "codex": {"binary_present": False}}
            ), patch.object(setup_module, "_ask_yes_no", return_value=False), patch.object(
                setup_module, "_use_color", return_value=False
            ):
                setup_module._ask_provider_entitlements(args, paths, "en")
            self.assertFalse(provider_entitlements_path(paths.omh_home).exists())

    def test_nothing_detected_asks_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            args = argparse.Namespace()
            with patch.object(
                setup_module, "_detect_external_cli_profiles", return_value={"claude-code": {"binary_present": False}, "codex": {"binary_present": False}}
            ), patch.object(setup_module, "_ask_yes_no") as yes_no:
                setup_module._ask_provider_entitlements(args, paths, "en")
            yes_no.assert_not_called()
            self.assertIsNone(args._provider_entitlements)

    def test_claude_code_seed_never_overwrites_an_existing_value(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            path = paths.omh_home / "routing" / "dispatch-models.json"
            _write(path, {"schema_version": "omh_dispatch_model_preferences/v1", "profiles": {"claude-code": "opus"}})
            self.assertEqual(setup_module._seed_claude_code_dispatch_head(paths)["status"], "already_present")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["profiles"], {"claude-code": "opus"})
            _write(path, {"schema_version": "omh_dispatch_model_preferences/v1", "profiles": {"codex": "gpt-5-codex"}})
            self.assertEqual(setup_module._seed_claude_code_dispatch_head(paths)["status"], "seeded")
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["profiles"],
                {"claude-code": CLAUDE_FRONTIER_CHAIN_MODELS[0], "codex": "gpt-5-codex"},
            )


if __name__ == "__main__":
    unittest.main()
