"""Contract for the ULW inventory producer and its generated projections.

`ulw_inventory_payload()` is the single producer for the ULW engine list and
per-engine lifecycle state (plan #954 Stage 0, §9). These tests pin:

1. the parity gate between the producer and `ULW_ENGINE_SKILL_NAMES`;
2. the materialized `SurfaceExposure` rows changing nothing except
   carrying an explicit `lifecycle_stage`;
3. the drift metrics reading `len()` off the producer;
4. the generated site region and English README table being current, and every
   `data-i18n="ulw.*"` key in the generated region resolving in all four
   locales of `site/i18n.js`;
5. the localized README tables staying count-gated (rows and numeral) while
   their prose stays hand-maintained (plan Q4).
"""

from __future__ import annotations

import re
import unittest
from dataclasses import asdict

from _local_package import load_local_package

load_local_package()
from omh.catalogs.ulw_surfaces import (
    ULW_README_REGION_BEGIN,
    ULW_README_REGION_END,
    ULW_SITE_REGION_BEGIN,
    ULW_SITE_REGION_END,
    readme_with_generated_region,
    site_with_generated_region,
    ulw_readme_region,
    ulw_site_region,
)
from omh.maintenance.drift import count_metrics, repo_root_default
from omh.skills.catalog import (
    ULW_INVENTORY_SCHEMA_VERSION,
    _default_surface_exposure,
    _surface_exposure_by_name,
    skill_exposure_payload,
    ulw_inventory_payload,
)
from omh.skills.catalog_types import SURFACE_LIFECYCLE_STAGES, ULW_ENGINE_SKILL_NAMES

REPO_ROOT = repo_root_default()

# The generated-table row shape shared by all four READMEs. A bare `ulw-`
# substring count would also match prose lines, so the gate matches rows only.
README_ROW_PATTERN = re.compile(r"^\| ⚡ `ulw-", re.MULTILINE)


RETIRED_ENGINES = ("team", "ralph", "ultragoal", "ultraprocess")


def _all_engines(payload: dict[str, object]) -> list[dict[str, object]]:
    return (
        list(payload["canonical_engines"])
        + list(payload["alias_engines"])
        + list(payload["retired_engines"])
    )


class UlwInventoryProducerTests(unittest.TestCase):
    def test_ulw_inventory_matches_engine_name_set(self) -> None:
        """§9.3 parity gate: one producer, no second membership source."""
        payload = ulw_inventory_payload()
        names = {engine["canonical"] for engine in _all_engines(payload)}
        self.assertEqual(names, set(ULW_ENGINE_SKILL_NAMES))
        self.assertEqual(len(_all_engines(payload)), len(ULW_ENGINE_SKILL_NAMES))

    def test_counts_and_stages_are_read_off_the_engine_lists(self) -> None:
        payload = ulw_inventory_payload()
        self.assertEqual(payload["schema_version"], ULW_INVENTORY_SCHEMA_VERSION)
        self.assertEqual(payload["counts"]["canonical"], len(payload["canonical_engines"]))
        self.assertEqual(payload["counts"]["alias"], len(payload["alias_engines"]))
        self.assertEqual(payload["counts"]["retired"], len(payload["retired_engines"]))
        self.assertEqual(payload["counts"]["total"], len(_all_engines(payload)))
        # Stage 5 contract (#954, window=0): the canonical engines, four
        # retired engines enumerated separately (never silently dropped), no
        # alias/warning stage in between.
        self.assertEqual(payload["counts"], {"canonical": 9, "alias": 0, "retired": 4, "total": 13})
        self.assertEqual(
            {engine["canonical"] for engine in payload["retired_engines"]},
            set(RETIRED_ENGINES),
        )
        for engine in payload["canonical_engines"]:
            with self.subTest(engine=engine["canonical"]):
                self.assertIn(engine["lifecycle_stage"], SURFACE_LIFECYCLE_STAGES)
                self.assertEqual(engine["lifecycle_stage"], "canonical")
                self.assertIsNone(engine["target_home"])
                self.assertIsNone(engine["migration_release"])
        for engine in payload["retired_engines"]:
            with self.subTest(engine=engine["canonical"]):
                self.assertEqual(engine["lifecycle_stage"], "retired")
                self.assertEqual(engine["target_home"], "ultrawork")
                self.assertTrue(engine["migration_release"])

    def test_canonical_explicit_exposure_rows_change_nothing(self) -> None:
        """§9.2 criterion: each canonical materialized row equals the default
        on every field except `lifecycle_stage`, which is explicit
        `canonical`."""
        by_name = _surface_exposure_by_name()
        for name in sorted(set(ULW_ENGINE_SKILL_NAMES) - set(RETIRED_ENGINES)):
            with self.subTest(name=name):
                self.assertIn(name, by_name, "engine fell back to the implicit default row")
                explicit = asdict(by_name[name])
                default = asdict(_default_surface_exposure(name))
                self.assertEqual(explicit.pop("lifecycle_stage"), "canonical")
                default.pop("lifecycle_stage")
                self.assertEqual(explicit, default)

    def test_retired_exposure_rows_use_the_reference_only_shape(self) -> None:
        """Retirement is an exposure change, not a deletion (P2): projections
        narrow to `workflow_reference` with install visibility off, and
        `compatibility_alias` stays true so a stale workflow hint resolves as
        a compatibility concern (§9.1)."""
        by_name = _surface_exposure_by_name()
        for name in RETIRED_ENGINES:
            with self.subTest(name=name):
                exposure = by_name[name]
                self.assertEqual(exposure.projections, ("workflow_reference",))
                self.assertFalse(exposure.install_visibility)
                self.assertTrue(exposure.compatibility_alias)
                self.assertEqual(exposure.lifecycle_stage, "retired")
                self.assertEqual(exposure.target_home, "ultrawork")
                self.assertTrue(exposure.migration_release)

    def test_exposure_payload_carries_lifecycle_fields(self) -> None:
        for name in sorted(ULW_ENGINE_SKILL_NAMES):
            with self.subTest(name=name):
                payload = skill_exposure_payload(name)
                if name in RETIRED_ENGINES:
                    self.assertEqual(payload["lifecycle_stage"], "retired")
                    self.assertEqual(payload["target_home"], "ultrawork")
                    self.assertTrue(payload["migration_release"])
                else:
                    self.assertEqual(payload["lifecycle_stage"], "canonical")
                    self.assertIsNone(payload["target_home"])
                    self.assertIsNone(payload["migration_release"])

    def test_drift_metrics_read_len_off_the_producer(self) -> None:
        metrics = {metric.name: metric for metric in count_metrics()}
        canonical = metrics["ulw_canonical_engine_count"]
        alias = metrics["ulw_alias_engine_count"]
        retired = metrics["ulw_retired_engine_count"]
        self.assertEqual(canonical.expected, 9)
        self.assertEqual(canonical.live(), 9)
        self.assertEqual(alias.expected, 0)
        self.assertEqual(alias.live(), 0)
        self.assertEqual(retired.expected, 4)
        self.assertEqual(retired.live(), 4)
        for metric in (canonical, alias, retired):
            for site in metric.sites:
                with self.subTest(metric=metric.name, site=site):
                    self.assertTrue((REPO_ROOT / site).is_file())


class UlwGeneratedSurfaceTests(unittest.TestCase):
    def test_english_readme_region_is_current(self) -> None:
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(ULW_README_REGION_BEGIN, text)
        self.assertIn(ULW_README_REGION_END, text)
        self.assertEqual(readme_with_generated_region(text), text)

    def test_site_region_is_current(self) -> None:
        text = (REPO_ROOT / "site" / "index.html").read_text(encoding="utf-8")
        self.assertIn(ULW_SITE_REGION_BEGIN, text)
        self.assertIn(ULW_SITE_REGION_END, text)
        self.assertEqual(site_with_generated_region(text), text)

    def test_generated_readme_rows_derive_from_the_producer(self) -> None:
        region = ulw_readme_region()
        expected_rows = len(ulw_inventory_payload()["canonical_engines"])
        self.assertEqual(len(README_ROW_PATTERN.findall(region)), expected_rows)
        for engine in ulw_inventory_payload()["canonical_engines"]:
            with self.subTest(engine=engine["canonical"]):
                self.assertIn(f"`{engine['display_name']}`", region)

    def test_generated_site_region_lists_every_canonical_engine_once(self) -> None:
        region = ulw_site_region()
        for engine in ulw_inventory_payload()["canonical_engines"]:
            with self.subTest(engine=engine["canonical"]):
                slug = f'<code class="ulw-row__slug">{engine["display_name"]}</code>'
                self.assertEqual(region.count(slug), 1)

    def test_site_region_i18n_keys_resolve_in_all_four_locales(self) -> None:
        region = ulw_site_region()
        keys = sorted(set(re.findall(r'data-i18n="(ulw\.[^"]+)"', region)))
        self.assertGreaterEqual(len(keys), 4)
        i18n = (REPO_ROOT / "site" / "i18n.js").read_text(encoding="utf-8")
        for key in keys:
            with self.subTest(key=key):
                marker = f'"{key}":'
                self.assertIn(marker, i18n, f"{key} missing from site/i18n.js")
                block_start = i18n.index("{", i18n.index(marker))
                depth = 0
                end = block_start
                for index in range(block_start, len(i18n)):
                    if i18n[index] == "{":
                        depth += 1
                    elif i18n[index] == "}":
                        depth -= 1
                        if depth == 0:
                            end = index
                            break
                block = i18n[block_start : end + 1]
                for locale in ("en:", "ko:", "ja:", "zh:"):
                    self.assertIn(locale, block, f"{key} does not resolve for {locale[:-1]}")

    def test_localized_readme_tables_stay_count_gated(self) -> None:
        """Plan Q4: localized prose is hand-maintained; rows and the count
        numeral are gated against the producer."""
        expected = len(ulw_inventory_payload()["canonical_engines"])
        # zh spells the count as a word; ko and ja carry the numeral and are
        # declared drift sites for `ulw_canonical_engine_count`.
        for name, numeral in (
            ("README.ko.md", f"{expected}개"),
            ("README.ja.md", f"{expected} 個"),
            ("README.zh.md", "九个"),
        ):
            with self.subTest(readme=name):
                text = (REPO_ROOT / name).read_text(encoding="utf-8")
                self.assertEqual(len(README_ROW_PATTERN.findall(text)), expected, name)
                self.assertIn(numeral, text)


if __name__ == "__main__":
    unittest.main()
