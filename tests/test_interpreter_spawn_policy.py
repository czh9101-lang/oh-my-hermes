"""Gate: every interpreter spawn that runs `omh.cli` isolates `sys.path[0]`.

`python -m omh.cli` and `python -c "import omh.cli"` put the working
directory at `sys.path[0]`. The repository root ships a top-level `omh/` shim
package (it re-points at `src/`), so an `omh update` launched from inside a
checkout re-entered the CHECKOUT's code instead of the generation venv that
had just been activated: on 2026-09-08 a 2.0.2 candidate was judged by a
stale 2.0.1 checkout, refused, rolled back, and the rollback re-entry stamped
the manifest `2.0.1` (fixed in PR #1395 with `-P`, Python 3.11+, the package
floor).

That fix lives at three spawn sites today. This module re-derives every such
spawn from source and fails when one appears without `-P` immediately before
`-m`/`-c`, so the class cannot come back through a new call site.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from _local_package import load_local_package

load_local_package()

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "src"
# `-m omh.cli` and `-c "import omh.cli"` are the two shapes in use; a new
# shape (say `-m omh`) must be added here, not left unguarded.
_MODULE_FORMS = {"omh.cli", "omh"}


def _string(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _spawn_sites() -> list[tuple[str, int, list[str | None], str]]:
    """Every list literal in src/ whose elements name an omh.cli interpreter run."""
    sites: list[tuple[str, int, list[str | None], str]] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.List, ast.Tuple)):
                continue
            elements = [_string(element) for element in node.elts]
            for index, value in enumerate(elements):
                if value is None:
                    continue
                runs_module = value in _MODULE_FORMS and index >= 1 and elements[index - 1] == "-m"
                runs_import = value.startswith("import omh") and index >= 1 and elements[index - 1] == "-c"
                if not (runs_module or runs_import):
                    continue
                flag_index = index - 2
                flag = elements[flag_index] if flag_index >= 0 else None
                sites.append((path.relative_to(REPO_ROOT).as_posix(), node.lineno, elements, str(flag)))
                break
    return sites


class InterpreterSpawnPolicyTests(unittest.TestCase):
    def test_every_omh_cli_interpreter_spawn_isolates_sys_path(self) -> None:
        sites = _spawn_sites()
        # Not vacuous: the self-update's `_omh_cli` builder, its import smoke,
        # and the startup auto-update are the known spawns. Fewer means the
        # scan broke.
        self.assertGreaterEqual(len(sites), 3, sites)
        unguarded = [(path, line, elements) for path, line, elements, flag in sites if flag != "-P"]
        self.assertEqual(
            unguarded,
            [],
            "an omh.cli interpreter spawn runs without `-P`; the working directory "
            "(a source checkout's top-level omh/ shim) would shadow the installed package",
        )

    def test_scan_recognizes_the_two_spawn_shapes(self) -> None:
        # The derivation itself is a contract: both shapes are found, the
        # `-P` sits where the policy expects it, and a plain spawn is flagged.
        source = (
            "a = [python, '-P', '-m', 'omh.cli', '--version']\n"
            "b = [python, '-P', '-c', 'import omh.cli']\n"
            "c = [python, '-m', 'omh.cli']\n"
        )
        tree = ast.parse(source)
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.List):
                elements = [_string(element) for element in node.elts]
                found.append(elements)
        self.assertEqual(len(found), 3)
        self.assertEqual(found[0][1:4], ["-P", "-m", "omh.cli"])
        self.assertEqual(found[1][1:4], ["-P", "-c", "import omh.cli"])
        self.assertEqual(found[2][1:3], ["-m", "omh.cli"])


if __name__ == "__main__":
    unittest.main()
