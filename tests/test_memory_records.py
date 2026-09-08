"""The provider's record budget covers the complete serialized section."""
import subprocess
import sys
from pathlib import Path
import unittest
from xml.etree import ElementTree

from omh.plugin_bundle.omh.memory_records import render_memory_records


class MemoryRecordBudgetTests(unittest.TestCase):
    def record(self, **values):
        return {"record_id": "mem_a", "record_type": "fact", "summary": "a < b & c",
                "approved_at": "2026-09-01T00:00:00Z", **values}

    def check_section(self, records, budget=2400, limit=6):
        text, count = render_memory_records(records, budget_chars=budget, limit=limit)
        self.assertLessEqual(len(text), max(0, budget))
        if not text:
            self.assertEqual(count, 0)
            return None
        root = ElementTree.fromstring(text)
        self.assertEqual(root.tag, "memory_records")
        self.assertEqual(count, len(root.findall("record")))
        self.assertLessEqual(count, max(0, limit))
        omissions = root.findall("omitted")
        self.assertLessEqual(len(omissions), 2)
        self.assertEqual(count + sum(int(item.attrib["count"]) for item in omissions), len(records))
        return root

    def test_large_store_aggregates_every_omission(self):
        root = self.check_section([self.record(record_id=f"mem_{i}") for i in range(10000)])
        self.assertEqual(len(root.findall("record")), 6)
        self.assertEqual(root.find("omitted").attrib,
                         {"count": "9994", "reason": "record_limit_reached"})

    def test_all_small_budgets_are_empty_or_complete_structures(self):
        for budget in [-100, *range(350)]:
            for limit in [-1, 0, 1, 6]:
                with self.subTest(budget=budget, limit=limit):
                    self.check_section([self.record()] * 10, budget, limit)
        self.assertEqual(render_memory_records([], budget_chars=0), ("", 0))

    def test_escaping_and_long_metadata_cannot_escape_the_budget(self):
        records = [self.record(record_id='"<&' * 5000, record_type='"<&' * 5000),
                   self.record(summary="&" * 500), self.record()]
        root = self.check_section(records)
        self.assertEqual(len(root.findall("record")), 1)
        record = root.find("record")
        self.assertEqual(record.attrib, {"id": "mem_a", "type": "fact", "approved": "2026-09-01"})
        self.assertEqual(record.text, "a < b & c")
        self.assertEqual(root.find("omitted").attrib,
                         {"count": "2", "reason": "render_budget_exhausted"})

    def test_one_record_fits_at_exact_serialized_boundary(self):
        records = [self.record()]
        text, count = render_memory_records(records)
        self.assertEqual(count, 1)
        self.assertEqual(render_memory_records(records, budget_chars=len(text)), (text, 1))
        self.assertEqual(render_memory_records(records, budget_chars=len(text)-1)[1], 0)

    def test_normal_records_preserve_order_and_escaped_provenance(self):
        records = [self.record(record_id='mem_"<&', record_type='fact"<&'), self.record(record_id="mem_b")]
        root = self.check_section(records)
        self.assertEqual([r.attrib["id"] for r in root], ['mem_"<&', "mem_b"])
        self.assertEqual(root[0].attrib["type"], 'fact"<&')
        self.assertEqual(len(root.findall("omitted")), 0)

    def test_standalone_bundle_uses_the_same_bounded_renderer(self):
        bundle = Path(__file__).resolve().parents[1] / "src" / "plugin_bundle"
        script = '''
import sys
sys.path.insert(0, sys.argv[1])
from omh.memory_records import render_memory_records
from xml.etree import ElementTree
text, count = render_memory_records([{"record_id": "mem_a", "summary": "fact"}] * 10000)
assert len(text) <= 2400
assert count == len(ElementTree.fromstring(text).findall("record")) == 6
assert "omh.workflows" not in sys.modules
print("standalone bounded records: OK")
'''
        result = subprocess.run([sys.executable, "-I", "-S", "-c", script, str(bundle)],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("standalone bounded records: OK", result.stdout)

    def test_mixed_omission_reasons_are_counted_truthfully(self):
        root = self.check_section([self.record(summary="&" * 500), self.record(), self.record()], limit=1)
        self.assertEqual({e.attrib["reason"]: int(e.attrib["count"]) for e in root.findall("omitted")},
                         {"render_budget_exhausted": 1, "record_limit_reached": 1})
