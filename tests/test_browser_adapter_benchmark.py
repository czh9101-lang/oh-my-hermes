from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType
import unittest
from unittest.mock import patch

from _local_package import load_local_package

load_local_package()

BENCHMARK = Path(__file__).resolve().parents[1] / "tools" / "benchmarks" / "browser_adapter.py"


def benchmark_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("browser_benchmark_probe", BENCHMARK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BrowserAdapterBenchmarkTests(unittest.TestCase):
    def test_default_runs_without_ignored_host_qa_artifacts(self) -> None:
        benchmark = benchmark_module()
        output = StringIO()
        with TemporaryDirectory() as checkout:
            setattr(benchmark, "ROOT", Path(checkout))
            with patch.object(sys, "argv", [str(BENCHMARK), "--repetitions", "1"]):
                with redirect_stdout(output):
                    try:
                        benchmark.main()
                    except (RuntimeError, SystemExit) as error:
                        self.fail(f"The default benchmark requires private checkout artifacts: {error}")
            result = json.loads(output.getvalue())
        self.assertFalse(Path(checkout).exists())
        self.assertEqual(result["real_host"], {"status": "not_run", "reason": "host_qa_not_supplied"})
        self.assertEqual(result["control_arms"]["live_resources"], 0)
        self.assertEqual(result["control_arms"]["concurrency_reservation"]["duplicate_starts"], 0)
        self.assertEqual(result["control_arms"]["crash_recovery"]["automatic_restarts"], 0)

    def test_explicit_host_driver_runs_with_owned_temporary_homes(self) -> None:
        benchmark = benchmark_module()
        output = StringIO()
        with TemporaryDirectory() as checkout:
            setattr(benchmark, "ROOT", Path(checkout))
            driver = Path(checkout) / "driver.py"
            driver.write_text(
                "import json, os\n"
                "print(json.dumps({'status': 'observed', 'surface': 'synthetic_driver_contract', "
                "'hermes_home': os.environ['HERMES_HOME'], "
                "'temporary_root': os.environ['TMPDIR']}))\n",
                encoding="utf-8",
            )
            with patch.object(
                sys, "argv", [str(BENCHMARK), "--repetitions", "1", "--host-qa", str(driver)]
            ):
                with redirect_stdout(output):
                    benchmark.main()
            result = json.loads(output.getvalue())
        self.assertFalse(Path(checkout).exists())
        self.assertEqual(result["real_host"]["surface"], "synthetic_driver_contract")
        self.assertFalse(Path(result["real_host"]["hermes_home"]).exists())
        self.assertFalse(Path(result["real_host"]["temporary_root"]).exists())


if __name__ == "__main__":
    unittest.main()
