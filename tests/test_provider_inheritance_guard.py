"""A dispatch may not inherit a provider that cannot serve the model it pins.

Observed live: a Fable alias was pinned with no provider, inherited an
`openai-codex` session, and every dispatch returned HTTP 400. It happened
twice before the operator worked out that the model needed a different
provider and hunted its wire id down by hand. Nothing in OMH had checked the
one thing it could check -- that the provider about to serve the run is one
the catalog says can serve that model -- even though the alias and the
provider are both in scope at the moment the route is written.

These tests pin the guard and, just as importantly, its silence: it refuses
only what is known wrong, and says who could serve the model instead.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from omh.plugin_bundle.omh.tools.delegate_route_tool import omh_delegate_route_handler


class ProviderInheritanceGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.config = self.home / "config.yaml"
        self.omh_home = self.home / ".omh"

    def _session_on(self, provider: str) -> None:
        self.config.write_text(f"model:\n  provider: {provider}\n", encoding="utf-8")

    def _entitlements(self, providers: dict[str, str]) -> None:
        path = self.omh_home / "routing" / "providers.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({
                "schema_version": "provider_entitlements/v1",
                "providers": providers,
                "subscription_clis": [],
            }),
            encoding="utf-8",
        )

    def _call(self, **args) -> dict:
        return json.loads(
            omh_delegate_route_handler(
                {"hermes_home": str(self.home), "omh_home": str(self.omh_home), **args}
            )
        )

    def test_the_incident_is_refused_with_the_provider_that_could_serve_it(self) -> None:
        self._session_on("openai-codex")
        self._entitlements({"og": "gateway", "openai-codex": "openai-codex"})
        result = self._call(action="set", model="claude-fable-5-1", reasoning_effort="high")
        self.assertEqual(result["status"], "error")
        self.assertIn("openai-codex", result["error"])
        self.assertIn("does not serve it", result["error"])
        self.assertEqual(result["inherited_provider"], "openai-codex")
        # The refusal is only half the help; the answer is the other half.
        self.assertEqual(result["providers_serving_alias"], ["og"])
        self.assertIn("og", result["error"])
        # Nothing was written: a refused route must not leave a half-route.
        self.assertNotIn("delegation:", self.config.read_text(encoding="utf-8"))

    def test_it_still_catches_the_incident_with_no_entitlements_recorded(self) -> None:
        # The provider id is itself a catalog family name, so the check holds
        # on an install that never ran the entitlement interview -- which is
        # the install the incident happened on.
        self._session_on("openai-codex")
        result = self._call(action="set", model="claude-fable-5-1")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["providers_serving_alias"], [])
        self.assertIn("routing/model-providers.json", result["error"])

    def test_an_explicit_provider_is_never_second_guessed(self) -> None:
        self._session_on("openai-codex")
        result = self._call(
            action="set",
            model="anthropic/claude-fable-5-1",
            provider="og",
            reasoning_effort="high",
        )
        self.assertEqual(result["status"], "routed")

    def test_a_provider_that_serves_the_alias_dispatches(self) -> None:
        self._session_on("anthropic")
        result = self._call(action="set", model="claude-fable-5-1")
        self.assertEqual(result["status"], "routed")

    def test_silence_on_every_unknown(self) -> None:
        # Nothing is refused on a guess. Each of these leaves the answer
        # genuinely unknown, and an unknown provider must never be treated as
        # a wrong one.
        cases = {
            "no session provider recorded": ("", "claude-fable-5-1", {}),
            "a model the catalog never described": ("openai-codex", "some-local-model", {}),
            "a provider id nothing records": ("my-own-thing", "claude-fable-5-1", {}),
        }
        for label, (session_provider, model, providers) in cases.items():
            with self.subTest(case=label):
                self.config.write_text(
                    f"model:\n  provider: {session_provider}\n" if session_provider else "model:\n",
                    encoding="utf-8",
                )
                if providers:
                    self._entitlements(providers)
                result = self._call(action="set", model=model)
                self.assertEqual(result["status"], "routed", result)

    def test_a_multi_vendor_session_provider_is_left_alone(self) -> None:
        # A relay serves models of every family, so inheriting one is not a
        # known-wrong dispatch even when the alias names other families.
        self._session_on("og")
        self._entitlements({"og": "gateway"})
        result = self._call(action="set", model="claude-fable-5-1")
        self.assertEqual(result["status"], "routed")


if __name__ == "__main__":
    unittest.main()
