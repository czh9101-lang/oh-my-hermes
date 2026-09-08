"""Reference-aware hints through a managed bundle with no importable core OMH."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from _cli_harness import run_cli
from omh.skills.catalog import routable_definitions
from test_routing_reference_regions import HINT_DIRECT_CONTROLS, protected_hint_forms


STANDALONE_PROBE = r'''
import importlib.util
import json
from pathlib import Path
import sys

plugin_dir = Path(sys.argv[1])
assert importlib.util.find_spec('omh') is None
spec = importlib.util.spec_from_file_location(
    '_reference_plugin', plugin_dir / '__init__.py',
    submodule_search_locations=[str(plugin_dir)],
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

class Context:
    def __init__(self):
        self.tools = {}
    def register_tool(self, name, *args, **kwargs):
        self.tools[name] = args[2]
    def register_hook(self, name, handler):
        pass

context = Context()
module.register(context)
from _reference_plugin.awareness import awareness_route_hint
results = []
for message in json.load(sys.stdin):
    brief = json.loads(context.tools['omh_context']({'message': message}))
    results.append({
        'awareness': awareness_route_hint(message),
        'tool_hint': brief['route_hint'],
        'message': brief['message'],
        'backend': brief['source_backend'],
    })
assert not any(name == 'omh' or name.startswith('omh.') for name in sys.modules)
print(json.dumps(results))
'''


class InstalledPluginReferenceHintTests(unittest.TestCase):
    def test_managed_standalone_plugin_keeps_references_inert_and_controls_active(self) -> None:
        cases = []
        for definition in routable_definitions():
            text = f'{definition.name} {" ".join(definition.triggers)} {definition.description}'
            for reference in protected_hint_forms(text):
                cases.append(('For reference:\n' + reference, ''))
        for message, expected in HINT_DIRECT_CONTROLS:
            cases.append((message, expected))
            for reference in protected_hint_forms('research jit-learn workflow-learning $ulw-work'):
                cases.extend(((message + '\n' + reference, expected), (reference + '\n' + message, expected)))
        with TemporaryDirectory(prefix='omh-installed-reference-') as temporary:
            root = Path(temporary)
            plugin_dir = root / 'hermes' / 'plugins' / 'omh'
            status, _, stderr = run_cli([
                '--omh-home', str(root / 'omh'), '--hermes-home', str(root / 'hermes'),
                'setup', '--with-plugin',
            ])
            self.assertEqual(status, 0, stderr)
            result = subprocess.run(
                [sys.executable, '-I', '-S', '-B', '-c', STANDALONE_PROBE, str(plugin_dir)],
                input=json.dumps([message for message, _ in cases]),
                cwd=root, env={**os.environ, 'HOME': str(root / 'home'),
                               'OMH_HOME': str(root / 'omh'), 'HERMES_HOME': str(root / 'hermes')},
                text=True, capture_output=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = json.loads(result.stdout)
            self.assertEqual(len(rows), len(cases))
            for (message, expected), row in zip(cases, rows, strict=True):
                with self.subTest(message=message):
                    self.assertEqual(row['backend'], 'standalone_plugin_bundle_fallback')
                    self.assertEqual(row['message']['length'], len(message))
                    self.assertEqual(row['message']['sha256'], hashlib.sha256(message.encode()).hexdigest())
                for surface in ('awareness', 'tool_hint'):
                    with self.subTest(message=message, surface=surface):
                        hint = row[surface]
                        self.assertEqual(hint['primary_workflow'], expected)
                        self.assertEqual(hint['message_length'], len(message))
                        self.assertEqual(hint['message_sha256'], hashlib.sha256(message.encode()).hexdigest())
                        if not expected:
                            self.assertEqual(hint['hints'], [])


if __name__ == '__main__':
    unittest.main()
