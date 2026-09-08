from __future__ import annotations

import importlib.util
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()

_SPEC = importlib.util.spec_from_file_location(
    "tracker_ingress_benchmark",
    Path(__file__).resolve().parents[1] / "tools" / "benchmarks" / "tracker_ingress.py",
)
assert _SPEC is not None and _SPEC.loader is not None
benchmark = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(benchmark)


class TrackerBenchmarkTests(unittest.TestCase):
    def test_failed_performance_ratchet_returns_nonzero(self) -> None:
        with patch.object(benchmark, "_measure", side_effect=[6.0, 1.0, 2.0]):
            with redirect_stdout(StringIO()):
                status = benchmark.main()
        self.assertNotEqual(status, 0)


if __name__ == "__main__":
    unittest.main()
