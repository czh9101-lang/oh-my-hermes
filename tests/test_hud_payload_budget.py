from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _local_package import load_local_package

load_local_package()

from omh.plugin_bundle.omh.runtime_reader import read_omh_hud  # noqa: E402


class HudPayloadBudgetTests(unittest.TestCase):
    def test_adversarial_plugin_inventory_stays_below_widget_buffer(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            omh_home = root / ".omh"
            hermes_home = root / ".hermes"
            plugin_dir = hermes_home / "plugins" / "omh"
            plugin_dir.mkdir(parents=True)
            (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
            tools = "\n".join(f"  - tool-{index:05d}" for index in range(10_000))
            (plugin_dir / "plugin.yaml").write_text(
                f"version: 1.0.0\nprovides_tools:\n{tools}\n",
                encoding="utf-8",
            )

            payload = read_omh_hud(
                omh_home,
                hermes_home,
                status={"runs": []},
            )
            encoded = json.dumps(payload).encode("utf-8") + b"\n"

            self.assertLess(len(encoded), 65_536)
            self.assertLessEqual(
                len(payload["plugin"]["capabilities"]["advertised_tools"]),
                128,
            )
            self.assertEqual(payload["privacy"], "metadata_only")
            self.assertIn("graph", payload)
