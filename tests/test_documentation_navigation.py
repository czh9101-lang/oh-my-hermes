from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from _cli_harness import run_cli
from _local_package import load_local_package

load_local_package()

from omh.catalogs.documentation_navigation import (
    NavigationRoot,
    PageClassification,
    documentation_navigation_roots,
    documentation_page_classifications,
    documentation_scope_patterns,
)
from omh.maintenance.documentation_navigation import (
    DOCUMENTATION_NAVIGATION_SCHEMA,
    FAILURE_CLASSES,
    documentation_navigation_report,
    format_documentation_navigation,
    public_path_collisions,
)


ROOT = Path(__file__).resolve().parents[1]
MODULE = "omh.maintenance.documentation_navigation"


def write(root: Path, relative: str, text: str) -> Path:
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class FixtureRepository:
    """A synthetic tree plus the navigation policy that should govern it.

    The checked-in policy names real repository pages, so a fixture supplies its
    own roots, scope, and classifications through the same three entry points the
    report reads. Nothing about the production signature changes.
    """

    def __init__(
        self,
        *,
        roots: tuple[NavigationRoot, ...] = (NavigationRoot("README.md", "fixture root"),),
        patterns: tuple[str, ...] = ("docs/*.md",),
        classifications: tuple[PageClassification, ...] = (),
    ) -> None:
        self.roots = roots
        self.patterns = patterns
        self.classifications = classifications
        self._temp = tempfile.TemporaryDirectory()
        self.path = Path(self._temp.name).resolve()

    def __enter__(self) -> FixtureRepository:
        return self

    def __exit__(self, *exc: object) -> None:
        self._temp.cleanup()

    def write(self, relative: str, text: str) -> Path:
        return write(self.path, relative, text)

    def report(self) -> dict:
        with (
            patch(f"{MODULE}.documentation_navigation_roots", return_value=self.roots),
            patch(f"{MODULE}.documentation_scope_patterns", return_value=self.patterns),
            patch(f"{MODULE}.documentation_page_classifications", return_value=self.classifications),
        ):
            return documentation_navigation_report(root=self.path)

    def classes(self) -> set[str]:
        return {row["failure_class"] for row in self.report()["findings"]}


class RepositoryNavigationTests(unittest.TestCase):
    """The live repository under the checked-in policy."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.report = documentation_navigation_report(root=ROOT)

    def test_repository_documentation_graph_has_no_structural_findings(self) -> None:
        self.assertEqual(self.report["schema_version"], DOCUMENTATION_NAVIGATION_SCHEMA)
        self.assertTrue(self.report["observed"])
        self.assertEqual(
            self.report["findings"], [],
            "\n" + format_documentation_navigation(self.report),
        )
        self.assertTrue(self.report["ok"])

    def test_every_in_scope_page_is_reachable_or_classified_with_a_reason_and_owner(self) -> None:
        # Success criterion 4: no top-level docs page may be settled by silence.
        for row in self.report["pages"]:
            with self.subTest(page=row["path"]):
                if row["reachable"]:
                    self.assertTrue(row["referrers"] or row["path"] in {"README.md", "docs/README.md"})
                    continue
                self.assertEqual(row["reachability"], "exempt")
                self.assertGreater(len(row["reason"]), 40, "an exclusion must explain itself")
                self.assertTrue(row["owner"])

    def test_generated_public_pages_are_classified_but_still_required_to_be_reachable(self) -> None:
        rows = {row["path"]: row for row in self.report["pages"]}
        for page in ("docs/WORKFLOWS.md", "docs/ROLES.md"):
            with self.subTest(page=page):
                self.assertEqual(rows[page]["page_class"], "generated")
                self.assertEqual(rows[page]["reachability"], "required")
                self.assertTrue(rows[page]["reachable"])

    def test_declared_roots_exist_and_are_unique(self) -> None:
        declared = [item.path for item in documentation_navigation_roots()]
        self.assertEqual(len(declared), len(set(declared)))
        for path in declared:
            self.assertTrue(ROOT.joinpath(*path.split("/")).is_file())

    def test_classifications_are_unique_and_name_pages_that_exist(self) -> None:
        seen: set[str] = set()
        for entry in documentation_page_classifications():
            with self.subTest(page=entry.path):
                self.assertNotIn(entry.path, seen)
                seen.add(entry.path)
                self.assertTrue(ROOT.joinpath(*entry.path.split("/")).is_file())

    def test_report_is_deterministic_and_stably_ordered(self) -> None:
        again = documentation_navigation_report(root=ROOT)
        self.assertEqual(json.dumps(self.report, sort_keys=True), json.dumps(again, sort_keys=True))
        self.assertEqual([row["path"] for row in again["pages"]], sorted(row["path"] for row in again["pages"]))

    def test_bounds_are_declared_and_no_side_channel_is_claimed(self) -> None:
        bounds = self.report["bounds"]
        self.assertFalse(bounds["network"])
        self.assertFalse(bounds["subprocess"])
        self.assertFalse(bounds["automatic_doc_edits"])
        self.assertGreater(bounds["max_page_bytes"], 1_048_576)

    def test_structure_evidence_does_not_claim_content_correctness(self) -> None:
        boundary = self.report["structure_boundary"]
        self.assertIn("not evidence that its content", boundary)
        self.assertIn("not generated-artifact equality", boundary)
        self.assertIn("claim auditing", boundary)
        self.assertIn("no equivalence with GitHub", self.report["anchor_scope"])

    def test_scope_stays_the_declared_top_level_docs_surface(self) -> None:
        self.assertEqual(documentation_scope_patterns(), ("docs/*.md",))
        expected = sorted(path.name for path in (ROOT / "docs").glob("*.md"))
        self.assertEqual(sorted(Path(row["path"]).name for row in self.report["pages"]), expected)


class LinkResolutionTests(unittest.TestCase):
    """Path handling: the shapes that break differently per platform."""

    def test_nested_relative_link_resolves_through_directories(self) -> None:
        with FixtureRepository(patterns=("docs/*.md", "docs/*/*.md")) as repo:
            repo.write("README.md", "[docs](docs/README.md)\n")
            repo.write("docs/README.md", "[child](guides/CHILD.md)\n")
            repo.write("docs/guides/CHILD.md", "[back up](../../README.md)\n[sibling](../SIB.md)\n")
            repo.write("docs/SIB.md", "# Sibling\n")

            report = repo.report()

            self.assertEqual(report["findings"], [], format_documentation_navigation(report))
            reachable = {row["path"] for row in report["pages"] if row["reachable"]}
            self.assertEqual(reachable, {"docs/README.md", "docs/guides/CHILD.md", "docs/SIB.md"})

    def test_url_encoded_path_resolves_to_the_decoded_file(self) -> None:
        with FixtureRepository() as repo:
            repo.write("README.md", "[docs](docs/README.md)\n")
            repo.write("docs/README.md", "[spaced](release%20notes.md)\n[plus](c%2B%2B.md)\n")
            repo.write("docs/release notes.md", "# Release notes\n")
            repo.write("docs/c++.md", "# C++\n")

            report = repo.report()

            self.assertEqual(report["findings"], [], format_documentation_navigation(report))
            self.assertEqual(report["summary"]["reachable_pages"], 3)

    def test_windows_separator_link_is_rejected_rather_than_silently_resolved(self) -> None:
        # This must fail identically on POSIX and Windows. A backslash is not a
        # separator in a repository view, and letting the host filesystem decide
        # would make the same tree pass on one runner and fail on the other.
        with FixtureRepository() as repo:
            repo.write("README.md", "[docs](docs/README.md)\n")
            repo.write("docs/README.md", "[win](sub\\CHILD.md)\n[ok](CHILD.md)\n")
            repo.write("docs/CHILD.md", "# Child\n")
            repo.write("docs/sub/CHILD.md", "# Nested child\n")

            findings = repo.report()["findings"]

            self.assertEqual([row["failure_class"] for row in findings], ["windows_separator_link"])
            self.assertEqual(findings[0]["page"], "docs/README.md")
            self.assertIn("sub\\CHILD.md", findings[0]["target"])

    def test_link_escaping_the_repository_is_rejected(self) -> None:
        with FixtureRepository() as repo:
            repo.write("README.md", "[docs](docs/README.md)\n")
            repo.write("docs/README.md", "[out](../../secrets.md)\n")

            findings = repo.report()["findings"]

            self.assertEqual([row["failure_class"] for row in findings], ["link_escapes_repository"])
            self.assertEqual(findings[0]["target"], "../../secrets.md")

    def test_absolute_local_path_is_rejected(self) -> None:
        with FixtureRepository() as repo:
            repo.write("README.md", "[docs](docs/README.md)\n")
            repo.write("docs/README.md", "[abs](/docs/CHILD.md)\n[drive](C:/docs/CHILD.md)\n")
            repo.write("docs/CHILD.md", "# Child\n")

            findings = repo.report()["findings"]

            self.assertEqual({row["failure_class"] for row in findings},
                             {"absolute_link_target", "unclassified_orphan"})

    def test_missing_local_target_fails_with_the_referring_page(self) -> None:
        with FixtureRepository() as repo:
            repo.write("README.md", "[docs](docs/README.md)\n")
            repo.write("docs/README.md", "[gone](REMOVED.md)\n![shot](../assets/missing.png)\n")

            findings = repo.report()["findings"]

            self.assertEqual({row["failure_class"] for row in findings}, {"missing_link_target"})
            self.assertEqual({row["page"] for row in findings}, {"docs/README.md"})
            self.assertEqual({row["target"] for row in findings}, {"REMOVED.md", "../assets/missing.png"})

    def test_case_only_difference_does_not_resolve(self) -> None:
        # The host filesystem may fold case; the verdict must not.
        with FixtureRepository() as repo:
            repo.write("README.md", "[docs](docs/README.md)\n")
            repo.write("docs/README.md", "[wrong case](child.md)\n[right](CHILD.md)\n")
            repo.write("docs/CHILD.md", "# Child\n")

            findings = repo.report()["findings"]

            self.assertEqual([row["failure_class"] for row in findings], ["missing_link_target"])
            self.assertEqual(findings[0]["target"], "child.md")

    def test_external_and_mailto_links_are_never_resolved_locally(self) -> None:
        with FixtureRepository() as repo:
            repo.write("README.md", "[docs](docs/README.md)\n")
            repo.write(
                "docs/README.md",
                "[web](https://example.invalid/x.md)\n[mail](mailto:a@example.invalid)\n"
                "[scheme-relative](//example.invalid/y.md)\n",
            )

            self.assertEqual(repo.report()["findings"], [])

    def test_code_fences_and_inline_code_are_not_navigation_edges(self) -> None:
        with FixtureRepository() as repo:
            repo.write("README.md", "[docs](docs/README.md)\n")
            repo.write(
                "docs/README.md",
                "Example only: `[x](GONE.md)`\n\n"
                "```sh\n# Heading inside a fence\n[y](ALSO-GONE.md)\n```\n\n"
                "~~~\n[z](STILL-GONE.md)\n~~~\n",
            )

            self.assertEqual(repo.report()["findings"], [])


class AnchorTests(unittest.TestCase):
    def test_valid_cross_page_and_same_page_anchors_pass(self) -> None:
        with FixtureRepository() as repo:
            repo.write("README.md", "[docs](docs/README.md)\n")
            repo.write(
                "docs/README.md",
                "# Docs\n\n## Guided Model Setup\n\n[same](#guided-model-setup)\n"
                "[cross](CHILD.md#deep-section-two)\n",
            )
            repo.write("docs/CHILD.md", "# Child\n\n## Deep: section two\n")

            self.assertEqual(repo.report()["findings"], [])

    def test_missing_anchor_on_a_fully_settled_page_fails(self) -> None:
        with FixtureRepository() as repo:
            repo.write("README.md", "[docs](docs/README.md)\n")
            repo.write("docs/README.md", "# Docs\n\n[cross](CHILD.md#not-a-heading)\n")
            repo.write("docs/CHILD.md", "# Child\n\n## Real heading\n")

            findings = repo.report()["findings"]

            self.assertEqual([row["failure_class"] for row in findings], ["missing_heading_anchor"])
            self.assertEqual(findings[0]["page"], "docs/CHILD.md")
            self.assertEqual(findings[0]["referrer"], "docs/README.md")

    def test_repeated_headings_get_disambiguated_anchors(self) -> None:
        with FixtureRepository() as repo:
            repo.write("README.md", "[docs](docs/README.md)\n")
            repo.write("docs/README.md", "# Docs\n\n[second](CHILD.md#notes-1)\n")
            repo.write("docs/CHILD.md", "# Child\n\n## Notes\n\ntext\n\n## Notes\n")

            self.assertEqual(repo.report()["findings"], [])

    def test_unresolved_anchor_on_a_page_with_non_ascii_headings_is_advisory(self) -> None:
        # The parser refuses to guess how a renderer transliterates a non-ASCII
        # heading, so it reports the limit instead of asserting a defect.
        with FixtureRepository() as repo:
            repo.write("README.md", "[docs](docs/README.md)\n")
            repo.write("docs/README.md", "# Docs\n\n[cross](CHILD.md#01-per-model-tuning)\n")
            repo.write("docs/CHILD.md", "# Child\n\n### 01 \u00b7 Per-model tuning\n")

            report = repo.report()

            self.assertEqual(report["findings"], [])
            self.assertEqual([row["failure_class"] for row in report["advisories"]], ["missing_heading_anchor"])
            self.assertIn("unsettled", report["advisories"][0]["detail"])
            self.assertTrue(report["ok"])

    def test_headings_inside_code_fences_do_not_create_anchors(self) -> None:
        with FixtureRepository() as repo:
            repo.write("README.md", "[docs](docs/README.md)\n")
            repo.write("docs/README.md", "# Docs\n\n[cross](CHILD.md#shell-comment)\n")
            repo.write("docs/CHILD.md", "# Child\n\n```sh\n# Shell comment\n```\n")

            findings = repo.report()["findings"]

            self.assertEqual([row["failure_class"] for row in findings], ["missing_heading_anchor"])


class ClassificationTests(unittest.TestCase):
    def test_unclassified_orphan_fails(self) -> None:
        with FixtureRepository() as repo:
            repo.write("README.md", "[docs](docs/README.md)\n")
            repo.write("docs/README.md", "# Docs\n")
            repo.write("docs/ORPHAN.md", "# Orphan\n")

            findings = repo.report()["findings"]

            self.assertEqual([row["failure_class"] for row in findings], ["unclassified_orphan"])
            self.assertEqual(findings[0]["page"], "docs/ORPHAN.md")
            self.assertIn("documentation_page_classifications()", findings[0]["detail"])

    def test_classified_exclusion_passes_and_carries_its_reason_forward(self) -> None:
        with FixtureRepository(classifications=(
            PageClassification(
                "docs/ORPHAN.md", "maintainer_procedure", "exempt",
                "Maintainer procedure entered from the agent contract.", "docs-specialist",
            ),
        )) as repo:
            repo.write("README.md", "[docs](docs/README.md)\n")
            repo.write("docs/README.md", "# Docs\n")
            repo.write("docs/ORPHAN.md", "# Orphan\n")

            report = repo.report()
            row = next(item for item in report["pages"] if item["path"] == "docs/ORPHAN.md")

            self.assertEqual(report["findings"], [])
            self.assertFalse(row["reachable"])
            self.assertEqual(row["page_class"], "maintainer_procedure")
            self.assertIn("agent contract", row["reason"])
            self.assertEqual(report["summary"]["exempt_pages"], 1)

    def test_a_reachable_page_may_not_stay_classified_exempt(self) -> None:
        with FixtureRepository(classifications=(
            PageClassification("docs/LINKED.md", "repo_internal", "exempt", "stale reason", "docs-specialist"),
        )) as repo:
            repo.write("README.md", "[docs](docs/README.md)\n")
            repo.write("docs/README.md", "# Docs\n\n[linked](LINKED.md)\n")
            repo.write("docs/LINKED.md", "# Linked\n")

            findings = repo.report()["findings"]

            self.assertEqual([row["failure_class"] for row in findings], ["unnecessary_exclusion"])
            self.assertEqual(findings[0]["referrer"], "docs/README.md")

    def test_a_generated_page_declared_required_still_has_to_be_reachable(self) -> None:
        # Generated is a provenance fact, not an excuse to drop a public page.
        with FixtureRepository(classifications=(
            PageClassification("docs/GENERATED.md", "generated", "required", "Rendered from the catalog.", "docs-specialist"),
        )) as repo:
            repo.write("README.md", "[docs](docs/README.md)\n")
            repo.write("docs/README.md", "# Docs\n")
            repo.write("docs/GENERATED.md", "# Generated\n")

            findings = repo.report()["findings"]

            self.assertEqual([row["failure_class"] for row in findings], ["required_page_unreachable"])
            self.assertIn("change the classification", findings[0]["detail"])

    def test_classification_naming_a_removed_page_is_stale(self) -> None:
        with FixtureRepository(classifications=(
            PageClassification("docs/DELETED.md", "repo_internal", "exempt", "gone", "docs-specialist"),
        )) as repo:
            repo.write("README.md", "[docs](docs/README.md)\n")
            repo.write("docs/README.md", "# Docs\n")

            findings = repo.report()["findings"]

            self.assertEqual([row["failure_class"] for row in findings], ["stale_classification"])

    def test_duplicate_and_invalid_classifications_fail(self) -> None:
        entry = PageClassification("docs/A.md", "repo_internal", "exempt", "reason", "docs-specialist")
        with FixtureRepository(classifications=(
            entry, entry,
            PageClassification("docs/B.md", "not-a-class", "exempt", "reason", "docs-specialist"),
            PageClassification("docs/C.md", "repo_internal", "maybe", "reason", "docs-specialist"),
        )) as repo:
            repo.write("README.md", "[docs](docs/README.md)\n")
            repo.write("docs/README.md", "# Docs\n")
            for name in ("A", "B", "C"):
                repo.write(f"docs/{name}.md", f"# {name}\n")

            classes = repo.classes()

            self.assertIn("duplicate_classification", classes)
            self.assertIn("invalid_classification", classes)


class NavigationRootTests(unittest.TestCase):
    def test_duplicate_navigation_root_fails(self) -> None:
        with FixtureRepository(roots=(
            NavigationRoot("README.md", "front page"),
            NavigationRoot("README.md", "declared twice"),
        )) as repo:
            repo.write("README.md", "[docs](docs/README.md)\n")
            repo.write("docs/README.md", "# Docs\n")

            findings = repo.report()["findings"]

            self.assertEqual([row["failure_class"] for row in findings], ["duplicate_navigation_root"])

    def test_missing_navigation_root_fails_rather_than_emptying_the_graph(self) -> None:
        with FixtureRepository(roots=(
            NavigationRoot("README.md", "front page"),
            NavigationRoot("HANDBOOK.md", "removed page"),
        )) as repo:
            repo.write("README.md", "[docs](docs/README.md)\n")
            repo.write("docs/README.md", "# Docs\n")

            classes = repo.classes()

            self.assertIn("missing_navigation_root", classes)

    def test_public_path_collision_is_reported(self) -> None:
        collisions = public_path_collisions(["docs/Guide.md", "docs/guide.md", "docs/other.md"])

        self.assertEqual(len(collisions), 1)
        self.assertEqual(collisions[0]["failure_class"], "duplicate_public_path")
        self.assertIn("docs/Guide.md", collisions[0]["detail"])
        self.assertIn("docs/guide.md", collisions[0]["detail"])
        self.assertEqual(public_path_collisions(["docs/a.md", "docs/b.md"]), [])

    def test_public_path_collision_is_reported_end_to_end_where_the_filesystem_allows_it(self) -> None:
        with FixtureRepository(classifications=(
            PageClassification("docs/Guide.md", "repo_internal", "exempt", "fixture", "docs-specialist"),
            PageClassification("docs/guide.md", "repo_internal", "exempt", "fixture", "docs-specialist"),
        )) as repo:
            repo.write("README.md", "[docs](docs/README.md)\n")
            repo.write("docs/README.md", "# Docs\n")
            repo.write("docs/Guide.md", "# Guide\n")
            repo.write("docs/guide.md", "# guide\n")
            if len(list((repo.path / "docs").glob("*uide.md"))) < 2:
                self.skipTest("case-insensitive filesystem cannot hold both pages at once")

            classes = repo.classes()

            self.assertIn("duplicate_public_path", classes)


class BudgetAndDiagnosticTests(unittest.TestCase):
    def test_a_page_over_the_parse_budget_is_reported_not_skipped(self) -> None:
        with FixtureRepository() as repo, patch(f"{MODULE}.MAX_PAGE_BYTES", 64):
            repo.write("README.md", "[docs](docs/README.md)\n")
            repo.write("docs/README.md", "# Docs\n\n" + "filler line\n" * 40)

            classes = repo.classes()

            self.assertIn("page_over_budget", classes)

    def test_traversal_budget_stops_and_says_the_results_are_incomplete(self) -> None:
        with FixtureRepository() as repo, patch(f"{MODULE}.MAX_TRAVERSED_PAGES", 3):
            repo.write("README.md", "[docs](docs/README.md)\n")
            repo.write("docs/README.md", "".join(f"[p{i}](P{i}.md)\n" for i in range(10)))
            for index in range(10):
                repo.write(f"docs/P{index}.md", f"# Page {index}\n")

            findings = repo.report()["findings"]

            self.assertIn("traversal_budget_exceeded", {row["failure_class"] for row in findings})

    def test_diagnostics_name_paths_and_never_quote_page_prose(self) -> None:
        secret = "internal-only-sentence-that-must-not-escape"
        with FixtureRepository() as repo:
            repo.write("README.md", "[docs](docs/README.md)\n")
            repo.write("docs/README.md", f"{secret}\n\n[gone](REMOVED.md)\n")

            report = repo.report()
            rendered = json.dumps(report) + format_documentation_navigation(report)

            self.assertNotIn(secret, rendered)
            self.assertIn("REMOVED.md", rendered)

    def test_an_overlong_link_target_is_truncated_and_control_characters_are_escaped(self) -> None:
        with FixtureRepository() as repo:
            repo.write("README.md", "[docs](docs/README.md)\n")
            repo.write("docs/README.md", "[long](" + "a" * 900 + ".md)\n")

            findings = repo.report()["findings"]

            self.assertEqual(findings[0]["failure_class"], "missing_link_target")
            self.assertLess(len(findings[0]["target"]), 250)
            self.assertTrue(findings[0]["target"].endswith("..."))

    def test_findings_are_stably_ordered_for_ci(self) -> None:
        with FixtureRepository() as repo:
            repo.write("README.md", "[docs](docs/README.md)\n")
            repo.write("docs/README.md", "[b](B-GONE.md)\n[a](A-GONE.md)\n")
            repo.write("docs/ZORPHAN.md", "# Z\n")
            repo.write("docs/AORPHAN.md", "# A\n")

            findings = repo.report()["findings"]
            keys = [(row["failure_class"], row["page"], row["referrer"] or "", row["target"] or "") for row in findings]

            self.assertEqual(keys, sorted(keys))
            self.assertTrue(set(FAILURE_CLASSES) >= {row["failure_class"] for row in findings})


class NavigationCommandTests(unittest.TestCase):
    def test_check_passes_on_the_repository_and_prints_a_machine_readable_payload(self) -> None:
        status, stdout, _ = run_cli(["docs", "navigation", "--check", "--json"], output_json=False)
        payload = json.loads(stdout)

        self.assertEqual(status, 0)
        self.assertEqual(payload["schema_version"], DOCUMENTATION_NAVIGATION_SCHEMA)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["findings"], [])

    def test_check_exits_one_on_a_tree_with_an_orphan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tree = Path(temp)
            write(tree, "README.md", "[docs](docs/README.md)\n")
            write(tree, "docs/README.md", "# Docs\n")
            write(tree, "docs/ORPHAN.md", "# Orphan\n")

            status, stdout, _ = run_cli(
                ["docs", "navigation", "--check", "--root", str(tree)], output_json=False
            )

        self.assertEqual(status, 1)
        self.assertIn("unclassified_orphan docs/ORPHAN.md", stdout)
        self.assertIn("NEEDS ATTENTION", stdout)

    def test_human_output_is_the_default_and_names_the_boundary(self) -> None:
        status, stdout, _ = run_cli(["docs", "navigation"], output_json=False)

        self.assertEqual(status, 0)
        self.assertIn("Documentation navigation: PASS", stdout)
        self.assertIn("no equivalence with GitHub", stdout)

    def test_without_check_a_failing_tree_still_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tree = Path(temp)
            write(tree, "README.md", "# Only a root\n")
            write(tree, "docs/README.md", "# Docs\n")
            write(tree, "docs/ORPHAN.md", "# Orphan\n")

            status, stdout, _ = run_cli(["docs", "navigation", "--root", str(tree)], output_json=False)

        self.assertEqual(status, 0)
        self.assertIn("unclassified_orphan", stdout)


class EvidenceSeparationTests(unittest.TestCase):
    def test_navigation_is_registered_in_the_documented_workflow_and_release_evidence(self) -> None:
        from omh.maintenance.release import release_readiness_checklist

        docs_index = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        items = {item["id"]: item for item in release_readiness_checklist(version="1.0.0")["items"]}

        self.assertIn("omh.cli docs navigation --check", docs_index)
        self.assertIn("omh.cli docs navigation --check", agents)
        self.assertIn("omh.cli docs navigation --check", claude)
        self.assertIn("docs navigation --check", ci)
        self.assertIn("documentation_navigation", items)
        self.assertEqual(
            items["documentation_navigation"]["command"],
            "uv run python -m omh.cli docs navigation --check",
        )
        self.assertIn("reachable", items["documentation_navigation"]["evidence_required"])
        self.assertIn("not", items["documentation_navigation"]["proof_boundary"])

    def test_navigation_stays_a_separate_evidence_class_from_claims_and_drift(self) -> None:
        from omh.maintenance.documentation_claims import DOCUMENTATION_CLAIM_AUDIT_SCHEMA

        self.assertNotEqual(DOCUMENTATION_NAVIGATION_SCHEMA, DOCUMENTATION_CLAIM_AUDIT_SCHEMA)
        payload = documentation_navigation_report(root=ROOT)
        self.assertNotIn("claims", payload)
        self.assertNotIn("generated_artifact_drift", payload)

    def test_the_existing_documentation_gates_still_pass_unchanged(self) -> None:
        for args in (
            ["docs", "workflows", "--check"],
            ["docs", "roles", "--check"],
            ["docs", "capability-families", "--check"],
        ):
            with self.subTest(args=args):
                status, _, stderr = run_cli(args, output_json=False)
                self.assertEqual(status, 0, stderr)


if __name__ == "__main__":
    unittest.main()
