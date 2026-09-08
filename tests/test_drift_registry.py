from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _cli_harness import run_cli
from _local_package import load_local_package

load_local_package()
from omh.maintenance.drift import (
    BudgetMetric,
    CountMetric,
    GeneratedArtifact,
    budget_metrics,
    count_metrics,
    drift_report,
    format_drift_report,
    generated_artifacts,
    repo_root_default,
)


REPO_ROOT = repo_root_default()


class DriftRegistryHonestyTests(unittest.TestCase):
    """Keep the registry from becoming a second stale source of truth.

    The registry claims two things it cannot prove on its own: that its expected
    value matches reality, and that the sites it lists really do hardcode that
    value. Both are checked here, so a registry that drifts fails loudly instead
    of quietly under-reporting.
    """

    def test_every_count_metric_matches_its_live_producer(self) -> None:
        for metric in count_metrics():
            with self.subTest(metric=metric.name):
                self.assertEqual(
                    metric.live(),
                    metric.expected,
                    f"{metric.name} drifted; run `omh release drift` for the full set",
                )

    def test_every_declared_site_exists(self) -> None:
        for metric in count_metrics():
            for site in metric.sites:
                with self.subTest(metric=metric.name, site=site):
                    self.assertTrue((REPO_ROOT / site).is_file(), f"declared site is missing: {site}")
        for metric in budget_metrics():
            with self.subTest(metric=metric.name, site=metric.limit_site):
                self.assertTrue((REPO_ROOT / metric.limit_site).is_file())

    def test_every_declared_site_actually_hardcodes_the_value(self) -> None:
        """A site that no longer mentions the number is a site that stopped mattering."""
        for metric in count_metrics():
            token = re.compile(rf"(?<![\d_]){metric.expected}(?![\d_])")
            for site in metric.sites:
                with self.subTest(metric=metric.name, site=site):
                    text = (REPO_ROOT / site).read_text(encoding="utf-8")
                    self.assertRegex(
                        text,
                        token,
                        f"{site} no longer contains {metric.expected}; drop it from "
                        f"{metric.name}.sites or point it at the right file",
                    )

    def test_every_budget_limit_is_declared_where_the_registry_says(self) -> None:
        for metric in budget_metrics():
            with self.subTest(metric=metric.name):
                text = (REPO_ROOT / metric.limit_site).read_text(encoding="utf-8")
                self.assertRegex(text, re.compile(rf"(?<![\d_]){metric.limit}(?![\d_])"))

    def test_metric_names_are_unique(self) -> None:
        names = [m.name for m in count_metrics()] + [m.name for m in budget_metrics()]
        names += [a.name for a in generated_artifacts()]
        self.assertEqual(len(names), len(set(names)))


class DriftReportTests(unittest.TestCase):
    def _count(self, name: str, live: int, expected: int) -> CountMetric:
        return CountMetric(
            name=name,
            describe=name,
            live=lambda: live,
            expected=expected,
            sites=("tests/test_drift_registry.py",),
        )

    def test_a_clean_repo_reports_no_drift(self) -> None:
        payload = drift_report()
        self.assertTrue(payload["ok"], payload["drift"])
        self.assertEqual(payload["drift_count"], 0)
        self.assertGreater(payload["checked_count"], 0)

    def test_every_simultaneous_drift_is_reported_in_one_pass(self) -> None:
        """The whole point: one run, the complete list.

        This mirrors the observed session, where a single skill addition
        invalidated counts, a budget, and generated docs at once and the suite
        revealed them a few at a time across twelve cycles.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            (root / "docs" / "STALE.md").write_text("old", encoding="utf-8")

            payload = drift_report(
                repo_root=root,
                include_tap_skills=False,
                counts=(
                    self._count("first_count", live=60, expected=59),
                    self._count("second_count", live=180, expected=179),
                    self._count("third_count", live=90, expected=90),
                ),
                budgets=(
                    BudgetMetric(
                        name="over_budget",
                        describe="over budget",
                        live=lambda: 24_305,
                        limit=24_000,
                        limit_site="src/maintenance/release.py",
                    ),
                    BudgetMetric(
                        name="within_budget",
                        describe="within budget",
                        live=lambda: 10,
                        limit=24_000,
                        limit_site="src/maintenance/release.py",
                    ),
                ),
                artifacts=(
                    GeneratedArtifact(
                        name="stale_doc",
                        path="docs/STALE.md",
                        render=lambda: "new",
                        regenerate="regenerate me",
                    ),
                    GeneratedArtifact(
                        name="missing_doc",
                        path="docs/GONE.md",
                        render=lambda: "anything",
                        regenerate="regenerate me too",
                    ),
                ),
            )

            self.assertFalse(payload["ok"])
            self.assertEqual(payload["checked_count"], 7)
            reported = {item["name"] for item in payload["drift"]}
            self.assertEqual(
                reported,
                {"first_count", "second_count", "over_budget", "stale_doc", "missing_doc"},
            )

    def test_generated_skill_files_are_checked_by_default(self) -> None:
        """Regression: the registry once shipped without this and stayed silent.

        Editing a skill reference template left `skills/*/references/*.md` stale.
        `docs workflows --check` caught it; `release drift` reported no drift,
        which is the failure this command exists to prevent.
        """
        self.assertIn("tap_skills", drift_report()["checked"])

    def test_stale_generated_skill_files_are_reported(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = drift_report(repo_root=root, counts=(), budgets=(), artifacts=())
            reported = {item["name"] for item in payload["drift"]}
            self.assertIn("tap_skills", reported, "an empty skills/ tree must count as drift")

    def test_count_drift_names_the_replacement_and_its_sites(self) -> None:
        payload = drift_report(
            counts=(self._count("skill_count", live=60, expected=59),),
            budgets=(),
            artifacts=(),
            include_tap_skills=False,
        )
        item = payload["drift"][0]
        assert item["kind"] == "count"
        self.assertEqual(item["kind"], "count")
        self.assertEqual(item["expected"], 59)
        self.assertEqual(item["live"], 60)
        self.assertIn("tests/test_drift_registry.py", item["sites"])
        self.assertIn("59", item["fix"])
        self.assertIn("60", item["fix"])

    def test_budget_drift_reports_the_overage(self) -> None:
        payload = drift_report(
            counts=(),
            include_tap_skills=False,
            budgets=(
                BudgetMetric(
                    name="registry_bytes",
                    describe="registry",
                    live=lambda: 24_305,
                    limit=24_000,
                    limit_site="src/maintenance/release.py",
                ),
            ),
            artifacts=(),
        )
        item = payload["drift"][0]
        assert item["kind"] == "budget"
        self.assertEqual(item["kind"], "budget")
        self.assertEqual(item["over_by"], 305)
        self.assertIn("305", item["fix"])

    def test_generated_drift_carries_the_regenerate_command(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            (root / "docs" / "ROLES.md").write_text("stale", encoding="utf-8")
            payload = drift_report(
                repo_root=root,
                include_tap_skills=False,
                counts=(),
                budgets=(),
                artifacts=(
                    GeneratedArtifact(
                        name="roles_doc",
                        path="docs/ROLES.md",
                        render=lambda: "fresh",
                        regenerate="uv run python -m omh.cli docs roles --output docs/ROLES.md",
                    ),
                ),
            )
            item = payload["drift"][0]
            assert item["kind"] == "generated"
            self.assertEqual(item["state"], "stale")
            self.assertIn("docs roles --output", item["fix"])

    def test_the_human_format_lists_every_finding_with_its_fix(self) -> None:
        payload = drift_report(
            counts=(self._count("a_count", live=2, expected=1), self._count("b_count", live=4, expected=3)),
            budgets=(),
            artifacts=(),
            include_tap_skills=False,
        )
        text = format_drift_report(payload)
        self.assertIn("a_count", text)
        self.assertIn("b_count", text)
        self.assertEqual(text.count("fix:"), 2)

    def test_the_clean_format_says_how_much_was_checked(self) -> None:
        text = format_drift_report(drift_report(counts=(), budgets=(), artifacts=(), include_tap_skills=False))
        self.assertIn("No drift", text)


class DriftCommandTests(unittest.TestCase):
    def test_the_command_passes_on_a_clean_tree(self) -> None:
        status, stdout, stderr = run_cli(["release", "drift", "--json"])
        self.assertEqual(stderr, "")
        self.assertEqual(status, 0)
        payload = json.loads(stdout)
        self.assertTrue(payload["ok"], payload["drift"])

    def test_the_human_output_is_the_default(self) -> None:
        status, stdout, stderr = run_cli(["release", "drift"], output_json=False)
        self.assertEqual(stderr, "")
        self.assertEqual(status, 0)
        self.assertIn("No drift", stdout)


if __name__ == "__main__":
    unittest.main()
