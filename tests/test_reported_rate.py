from __future__ import annotations

import unittest

from _local_package import load_local_package

load_local_package()

from omh.quality.common_request_coverage import build_common_request_coverage_demo  # noqa: E402
from omh.quality.hermes_ux_quality import build_hermes_ux_quality_demo  # noqa: E402
from omh.quality.reported_rate import (  # noqa: E402
    NO_OBSERVATIONS_BASIS,
    OBSERVATIONS_BASIS,
    REPORTED_RATE_SCHEMA_VERSION,
    format_reported_rate,
    meets_target,
    reported_rate,
    reported_rate_shape_errors,
)
from omh.quality.routing_accuracy import build_routing_accuracy_demo  # noqa: E402
from omh.workflows.use_cases import use_case_readiness  # noqa: E402


class ReportedRateContractTests(unittest.TestCase):
    def test_empty_denominator_reports_unmeasured_never_zero_percent(self) -> None:
        # Given: a corpus with no cases in it.
        rate = reported_rate(
            numerator=0,
            denominator=0,
            numerator_of=("passing_case",),
            denominator_of="demo cases",
        )

        # Then: the rate is unmeasured, not a confident 0% — 0% would assert
        # that every case was measured and every case failed.
        self.assertIsNone(rate.percent)
        self.assertEqual(rate.basis, NO_OBSERVATIONS_BASIS)
        self.assertFalse(meets_target(rate, 0.0))
        self.assertIn(NO_OBSERVATIONS_BASIS, format_reported_rate(rate))
        self.assertEqual(reported_rate_shape_errors(rate.to_payload()), ())

    def test_measured_rate_names_numerator_denominator_and_exclusions(self) -> None:
        rate = reported_rate(
            numerator=3,
            denominator=4,
            numerator_of=("resolved", "handed_off"),
            denominator_of="routing cases",
            excluded=("infra_error",),
        )

        payload = rate.to_payload()
        self.assertEqual(payload["schema_version"], REPORTED_RATE_SCHEMA_VERSION)
        self.assertEqual(payload["percent"], 75.0)
        self.assertEqual(payload["numerator_of"], ["resolved", "handed_off"])
        self.assertEqual(payload["denominator_of"], "routing cases")
        self.assertEqual(payload["excluded"], ["infra_error"])
        self.assertEqual(payload["basis"], OBSERVATIONS_BASIS)
        self.assertEqual(reported_rate_shape_errors(payload), ())
        self.assertEqual(
            format_reported_rate(rate),
            "75.0% — 3/4 routing cases (resolved + handed_off); excluded: infra_error",
        )

    def test_a_rate_must_name_what_it_divided(self) -> None:
        for kwargs in (
            {"numerator_of": ()},
            {"denominator_of": "  "},
        ):
            with self.subTest(kwargs=kwargs):
                base = {
                    "numerator": 1,
                    "denominator": 2,
                    "numerator_of": ("passing_case",),
                    "denominator_of": "demo cases",
                }
                base.update(kwargs)
                with self.assertRaises(ValueError):
                    reported_rate(**base)  # type: ignore[arg-type]

    def test_shape_errors_reject_a_zero_denominator_reported_as_a_number(self) -> None:
        payload = reported_rate(
            numerator=0,
            denominator=0,
            numerator_of=("passing_case",),
            denominator_of="demo cases",
        ).to_payload()
        payload["percent"] = 0.0
        self.assertTrue(
            any("must report percent null" in error for error in reported_rate_shape_errors(payload))
        )


class ReportedRatePayloadDisclosureTests(unittest.TestCase):
    """Every percentage OMH prints about itself carries its denominator."""

    def _assert_disclosed(self, payload: object, label: str) -> None:
        self.assertIsInstance(payload, dict)
        errors = reported_rate_shape_errors(payload)
        self.assertEqual(errors, (), f"{label}: {errors}")

    def test_common_request_coverage_discloses_its_denominator(self) -> None:
        summary = build_common_request_coverage_demo()["summary"]
        rate = summary["coverage_rate"]
        self._assert_disclosed(rate, "common_request_coverage.summary")
        self.assertEqual(rate["percent"], summary["coverage_percent"])
        self.assertEqual(rate["denominator"], summary["case_count"])
        self.assertEqual(rate["numerator"], summary["passing_count"])

    def test_common_request_family_rows_disclose_their_denominators(self) -> None:
        payload = build_common_request_coverage_demo()
        families = payload["families"]
        self.assertTrue(families)
        for family in families:
            with self.subTest(family=family["family"]):
                self._assert_disclosed(family["coverage_rate"], "family row")
                self.assertEqual(family["coverage_rate"]["denominator"], family["case_count"])

    def test_ux_quality_score_discloses_its_gate_denominator(self) -> None:
        payload = build_hermes_ux_quality_demo()
        rate = payload["score_rate"]
        self._assert_disclosed(rate, "hermes_ux_quality.score_rate")
        self.assertEqual(rate["denominator"], payload["summary"]["gate_count"])
        self.assertEqual(rate["numerator"], payload["summary"]["passing_gate_count"])

    def test_routing_accuracy_names_the_composed_coverage_numerator(self) -> None:
        payload = build_routing_accuracy_demo()
        summary = payload["summary"]
        self._assert_disclosed(summary["resolved_rate"], "routing_accuracy.resolved_rate")
        self._assert_disclosed(summary["covered_rate"], "routing_accuracy.covered_rate")
        # Coverage sums two outcome buckets; the payload says which, so a JSON
        # consumer cannot read it as a single bucket.
        self.assertEqual(summary["covered_rate"]["numerator_of"], ["resolved", "handed_off"])
        self.assertEqual(summary["resolved_rate"]["numerator_of"], ["resolved"])
        self.assertEqual(
            summary["covered_rate"]["numerator"],
            summary["resolved"] + summary["handed_off"],
        )
        for bucket in payload["languages"]:
            with self.subTest(language=bucket["language"]):
                self._assert_disclosed(bucket["resolved_rate"], "language resolved_rate")
                self._assert_disclosed(bucket["covered_rate"], "language covered_rate")

    def test_use_case_readiness_exposes_its_adjusted_denominator(self) -> None:
        from omh.paths import OmhPaths
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            paths = OmhPaths(Path(tmp) / "omh", Path(tmp) / "hermes")
            payload = use_case_readiness(paths)

        rate = payload["score_rate"]
        self._assert_disclosed(rate, "use_case_readiness.score_rate")
        # Non-blocking gates are excluded from both sides, so the denominator is
        # NOT len(gates) — the payload has to say that, and now does.
        self.assertEqual(rate["denominator"], len(payload["gates"]) - payload["warning_count"])
        self.assertEqual(rate["denominator_of"], "blocking readiness gates")
        if payload["warning_count"]:
            self.assertEqual(rate["excluded"], ["non_blocking_gate"])


if __name__ == "__main__":
    unittest.main()
