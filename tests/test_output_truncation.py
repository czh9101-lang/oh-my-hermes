from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _local_package import load_local_package

load_local_package()

from omh.coding._hermes_child_process import (  # noqa: E402
    BoundedStreamCapture,
    MAX_CAPTURE_BYTES,
    bounded_redacted_output,
    capture_truncation_record,
)
from omh.install.release_smoke_core import bounded_text  # noqa: E402
from omh.system.output_truncation import (  # noqa: E402
    OUTPUT_SPILL_REF_SCHEMA_VERSION,
    OUTPUT_TRUNCATION_SCHEMA_VERSION,
    SPILL_STATUSES,
    TRUNCATION_REASON_CODES,
    resolve_spill_reference,
    spill_evidence_ref,
    truncate_output,
    truncation_notice,
    write_output_spill,
)
from omh.system.paths import OmhPaths  # noqa: E402


class OutputTruncationContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory(prefix="omh-output-truncation-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = OmhPaths(omh_home=self.root / ".omh", hermes_home=self.root / ".hermes")

    def test_record_round_trips_every_contract_field(self) -> None:
        bounded = truncate_output(
            "x" * 5000,
            limit_bytes=100,
            source="unit test capture",
            keep="tail",
            spill_dir=self.paths.runtime_output_spills_dir,
        )
        record = bounded.record
        self.assertEqual(record["schema_version"], OUTPUT_TRUNCATION_SCHEMA_VERSION)
        self.assertTrue(record["truncated"])
        self.assertIn(record["reason_code"], TRUNCATION_REASON_CODES)
        self.assertEqual(record["reason_code"], "output_cap")
        self.assertEqual(record["source"], "unit test capture")
        self.assertEqual(record["limit_bytes"], 100)
        self.assertEqual(record["original_bytes"], 5000)
        self.assertEqual(record["kept_bytes"], 100)
        self.assertEqual(
            record["kept_ranges"],
            [{"position": "tail", "start_byte": 4900, "end_byte": 5000}],
        )
        self.assertIn(record["spill_status"], SPILL_STATUSES)
        self.assertEqual(record["spill_status"], "written")
        self.assertTrue(record["continuation_hint"])
        self.assertEqual(bounded.kept_text, "x" * 100)

    def test_exactly_at_the_cap_is_not_truncated(self) -> None:
        at_cap = truncate_output("y" * 300, limit_bytes=300, source="boundary")
        over_cap = truncate_output("y" * 301, limit_bytes=300, source="boundary")
        self.assertFalse(at_cap.truncated)
        self.assertEqual(at_cap.record["reason_code"], "not_truncated")
        self.assertEqual(at_cap.text, "y" * 300)
        self.assertEqual(truncation_notice(at_cap.record), "")
        self.assertTrue(over_cap.truncated)
        self.assertEqual(over_cap.record["reason_code"], "output_cap")

    def test_spill_file_digest_and_length_match_the_back_reference(self) -> None:
        full = "line\n" * 4000
        bounded = truncate_output(
            full,
            limit_bytes=200,
            source="spill digest",
            spill_dir=self.paths.runtime_output_spills_dir,
        )
        spill = bounded.record["spill"]
        self.assertEqual(spill["schema_version"], OUTPUT_SPILL_REF_SCHEMA_VERSION)
        self.assertEqual(spill["sha256"], sha256(full.encode("utf-8")).hexdigest())
        self.assertEqual(spill["byte_count"], len(full.encode("utf-8")))
        spill_path = Path(spill["path"])
        self.assertTrue(spill_path.is_file())
        self.assertEqual(spill_path.read_text(encoding="utf-8"), full)
        # Deterministic and content-addressed: the same output re-spills onto
        # the same file rather than allocating a second one.
        again = write_output_spill(self.paths.runtime_output_spills_dir, full)
        self.assertEqual(again["path"], spill["path"])

    def test_back_reference_resolves_exactly_what_was_cut(self) -> None:
        full = "".join(f"row {index}\n" for index in range(3000))
        bounded = truncate_output(
            full,
            limit_bytes=120,
            source="back reference",
            keep="tail",
            spill_dir=self.paths.runtime_output_spills_dir,
        )
        spill = bounded.record["spill"]
        self.assertEqual(resolve_spill_reference(spill), full)
        # The kept tail is the exact suffix of the resolved content, so a
        # reader can splice the two without guessing where the cut fell.
        self.assertTrue(full.endswith(bounded.kept_text))
        self.assertEqual(
            spill_evidence_ref(bounded.record),
            f"output_spill:{spill['path']}:sha256:{spill['sha256']}:{spill['byte_count']}",
        )

    def test_resolution_refuses_a_reference_whose_file_changed(self) -> None:
        bounded = truncate_output(
            "z" * 4000,
            limit_bytes=50,
            source="tamper",
            spill_dir=self.paths.runtime_output_spills_dir,
        )
        spill = dict(bounded.record["spill"])
        Path(spill["path"]).write_text("z" * 3999, encoding="utf-8")
        with self.assertRaises(ValueError):
            resolve_spill_reference(spill)

    def test_no_pointer_is_emitted_when_the_spill_write_failed(self) -> None:
        blocked = self.root / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        bounded = truncate_output(
            "q" * 4000, limit_bytes=40, source="failed spill", spill_dir=blocked / "spills"
        )
        self.assertNotIn("spill", bounded.record)
        self.assertEqual(bounded.record["spill_status"], "store_unavailable")
        self.assertIn("not recoverable", bounded.record["continuation_hint"])

    def test_notices_are_ellipsis_free_and_name_the_recovery_path(self) -> None:
        bounded = truncate_output(
            "e" * 9000,
            limit_bytes=64,
            source="ellipsis guarantee",
            spill_dir=self.paths.runtime_output_spills_dir,
        )
        self.assertNotIn("...", bounded.text)
        self.assertNotIn("…", bounded.text)
        notice = truncation_notice(bounded.record)
        self.assertIn("reason=output_cap", notice)
        self.assertIn("original_bytes=9000", notice)
        self.assertIn("kept_bytes=64", notice)
        self.assertIn("kept_ranges=tail 8936-9000", notice)
        self.assertIn(bounded.record["spill"]["path"], notice)
        self.assertIn(bounded.record["spill"]["sha256"], notice)

    def test_compact_notice_defers_the_pointer_to_evidence_refs(self) -> None:
        bounded = truncate_output(
            "c" * 9000,
            limit_bytes=64,
            source="compact",
            spill_dir=self.paths.runtime_output_spills_dir,
        )
        compact = truncation_notice(bounded.record, compact=True)
        self.assertIn("continuation=evidence_refs", compact)
        self.assertNotIn(bounded.record["spill"]["path"], compact)
        self.assertNotIn("...", compact)
        # Short enough to survive the observation journal's 500-character
        # summary bound alongside a 300-byte tail and the run prefix.
        self.assertLess(len(compact), 160)

    def test_multibyte_output_never_keeps_a_partial_character(self) -> None:
        text = "한글" * 400
        tail = truncate_output(text, limit_bytes=101, source="utf8 tail", keep="tail")
        head = truncate_output(text, limit_bytes=101, source="utf8 head", keep="head")
        self.assertTrue(text.endswith(tail.kept_text))
        self.assertTrue(text.startswith(head.kept_text))
        self.assertNotIn("�", tail.kept_text)
        self.assertNotIn("�", head.kept_text)
        self.assertEqual(tail.record["kept_bytes"], len(tail.kept_text.encode("utf-8")))
        self.assertEqual(head.record["kept_bytes"], len(head.kept_text.encode("utf-8")))


class BoundedCaptureContractTests(unittest.TestCase):
    def test_capture_cap_reports_the_counted_original_and_no_spill(self) -> None:
        capture = BoundedStreamCapture(b"k" * MAX_CAPTURE_BYTES, True, 1_048_576)
        record = capture_truncation_record(capture, source="hermes child stdout capture")
        self.assertEqual(record["reason_code"], "capture_cap")
        self.assertEqual(record["original_bytes"], 1_048_576)
        self.assertEqual(record["kept_bytes"], MAX_CAPTURE_BYTES)
        self.assertEqual(record["spill_status"], "content_not_retained")
        self.assertNotIn("spill", record)

    def test_uncounted_capture_reports_the_original_as_unknown(self) -> None:
        record = capture_truncation_record(
            BoundedStreamCapture(b"partial", True), source="hermes child stderr capture"
        )
        self.assertIsNone(record["original_bytes"])
        self.assertIn("original_bytes=unknown", truncation_notice(record))

    def test_bounded_output_renders_the_notice_instead_of_a_bare_marker(self) -> None:
        rendered = bounded_redacted_output(
            BoundedStreamCapture(b"m" * MAX_CAPTURE_BYTES, True, 999_999),
            secrets=set(),
            source="hermes child stdout capture",
        )
        self.assertNotIn("...", rendered)
        self.assertIn("[output truncated:", rendered)
        self.assertIn("reason=capture_cap", rendered)
        self.assertIn("original_bytes=999999", rendered)
        self.assertIn("never retained", rendered)

    def test_untruncated_capture_renders_no_notice(self) -> None:
        rendered = bounded_redacted_output(
            BoundedStreamCapture(b"short", False, 5), secrets=set()
        )
        self.assertEqual(rendered, "short")


class ReleaseSmokeExcerptTests(unittest.TestCase):
    def test_excerpt_under_the_bound_is_returned_verbatim(self) -> None:
        self.assertEqual(bounded_text("ok\n"), "ok\n")

    def test_excerpt_over_the_bound_carries_a_reason_code_not_an_ellipsis(self) -> None:
        rendered = bounded_text("v" * 4000, 100)
        self.assertNotIn("...[truncated]", rendered)
        self.assertNotIn("...", rendered)
        self.assertIn("reason=output_cap", rendered)
        self.assertIn("original_bytes=4000", rendered)
        self.assertIn("kept_bytes=100", rendered)
        self.assertIn("kept_ranges=head 0-100", rendered)
        self.assertTrue(rendered.startswith("v" * 100))


if __name__ == "__main__":
    unittest.main()
