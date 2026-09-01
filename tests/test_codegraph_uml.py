from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _cli_harness import run_cli
from omh.codegraph import build_codegraph, build_uml_model, render_plan, render_plantuml
from omh.codegraph.uml import OTHER_UNIT_ID, render_uml_text


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _sample_repo(root: Path) -> None:
    _write(root / "app" / "__init__.py", "")
    _write(
        root / "app" / "core" / "__init__.py",
        "from .engine import Engine, run\n",
    )
    _write(
        root / "app" / "core" / "engine.py",
        """
class Engine:
    def start(self):
        return "ok"

class EngineError(Exception):
    pass

def run():
    return Engine().start()

def _private_helper():
    return None
""".lstrip(),
    )
    _write(
        root / "app" / "cli" / "__init__.py",
        "",
    )
    _write(
        root / "app" / "cli" / "main.py",
        """
from app.core.engine import Engine, run
from app.core.engine import Engine as E2

def main():
    return run()
""".lstrip(),
    )
    _write(
        root / "app" / "web" / "views.py",
        """
from app.core.engine import run

def index():
    return run()
""".lstrip(),
    )
    _write(
        root / "tests" / "test_engine.py",
        """
from app.core.engine import run

def test_run():
    assert run() == "ok"
""".lstrip(),
    )


class UmlModelTests(unittest.TestCase):
    def _graph(self, root: Path):
        return build_codegraph(root, generated_at="2026-01-01T00:00:00Z")

    def test_package_model_folds_files_and_ranks_interface_by_fan_in(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _sample_repo(root)
            model = build_uml_model(self._graph(root))

        self.assertEqual(model["schema_version"], "codebase_uml/v1")
        self.assertEqual(model["source_schema_version"], "omh_codegraph/v1")
        node_ids = [node["id"] for node in model["nodes"]]
        self.assertEqual(node_ids, ["app", "app/cli", "app/core", "app/web"])
        core = next(node for node in model["nodes"] if node["id"] == "app/core")
        # `run` is imported by two other units, `Engine` by one; the error type
        # and the private helper never make the interface.
        self.assertEqual(core["interface"][:2], ["run()", "Engine"])
        self.assertNotIn("_private_helper()", core["interface"])
        self.assertEqual(core["interface"][-1], "EngineError")
        self.assertEqual(core["file_count"], 2)
        edges = {(edge["from"], edge["to"]): edge["weight"] for edge in model["edges"]}
        self.assertEqual(edges[("app/cli", "app/core")], 1)
        self.assertEqual(edges[("app/web", "app/core")], 1)
        self.assertEqual(model["omissions"]["test_files_skipped"], 1)
        self.assertEqual(model["layout"]["direction"], "top to bottom")
        self.assertIn("Static local analysis is not execution/review/CI/merge evidence", model["claim_boundary"])

    def test_include_tests_keeps_test_units(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _sample_repo(root)
            model = build_uml_model(self._graph(root), include_tests=True)
        self.assertIn("tests", [node["id"] for node in model["nodes"]])
        self.assertEqual(model["omissions"]["test_files_skipped"], 0)

    def test_module_level_names_files_and_collapses_init(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _sample_repo(root)
            model = build_uml_model(self._graph(root), level="module")
        node_ids = [node["id"] for node in model["nodes"]]
        self.assertIn("app/core/engine", node_ids)
        self.assertIn("app/core", node_ids)  # the package facade __init__
        self.assertIn("app/cli/main", node_ids)
        self.assertEqual(model["layout"]["strong_edge_weight"], 2)

    def test_focus_keeps_prefix_and_direct_neighbours(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _sample_repo(root)
            model = build_uml_model(self._graph(root), focus="app/web")
        node_ids = [node["id"] for node in model["nodes"]]
        self.assertEqual(node_ids, ["app/core", "app/web"])
        self.assertEqual(model["omissions"]["units_outside_focus"], 2)
        self.assertEqual(model["view"]["focus"], "app/web")

    def test_node_cap_folds_lowest_degree_units_and_drops_their_edges(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _sample_repo(root)
            model = build_uml_model(self._graph(root), max_nodes=3)
        node_ids = [node["id"] for node in model["nodes"]]
        self.assertIn(OTHER_UNIT_ID, node_ids)
        self.assertEqual(len(node_ids), 3)
        self.assertIn("app/core", node_ids)
        folded = model["omissions"]["units_folded"]
        self.assertEqual(len(folded), 2)
        other = next(node for node in model["nodes"] if node["id"] == OTHER_UNIT_ID)
        self.assertEqual(other["folded_unit_count"], 2)
        for edge in model["edges"]:
            self.assertNotEqual(edge["from"], OTHER_UNIT_ID)
            self.assertNotEqual(edge["to"], OTHER_UNIT_ID)
        self.assertGreaterEqual(model["omissions"]["edges_into_folded_units"], 1)

    def test_focus_units_survive_the_fold_before_neighbours(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _sample_repo(root)
            model = build_uml_model(self._graph(root), level="module", focus="app/core", max_nodes=3)
        node_ids = [node["id"] for node in model["nodes"]]
        # Two in-focus modules plus the fold; the neighbours outside the focus fold first.
        self.assertEqual(node_ids, [OTHER_UNIT_ID, "app/core", "app/core/engine"])
        self.assertNotIn("app/cli/main", node_ids)

    def test_edge_budget_prunes_lightest_edges(self) -> None:
        graph = {
            "schema_version": "omh_codegraph/v1",
            "repo_root": "/r",
            "generated_at": "2026-01-01T00:00:00Z",
            "files": [
                {"path": f"{name}/mod.py", "imports": [], "defines": [], "entrypoint_tags": []} for name in "abc"
            ],
            "symbols": [],
            "edges": [
                *[{"from": "a/mod.py", "to": "b/mod.py", "kind": "imports_internal"}] * 1,
                {"from": "a/mod.py", "to": "c/mod.py", "kind": "imports_internal"},
                {"from": "b/mod.py", "to": "c/mod.py", "kind": "imports_internal"},
                {"from": "c/mod.py", "to": "a/mod.py", "kind": "imports_internal"},
                {"from": "b/mod.py", "to": "a/mod.py", "kind": "imports_internal"},
                {"from": "c/mod.py", "to": "b/mod.py", "kind": "imports_internal"},
                {"from": "a/mod.py", "to": "b/mod.py", "kind": "imports_internal"},
            ],
        }
        # Duplicate edge keys collapse in the scanner; emulate weights via a 4-node graph instead.
        graph["files"].append({"path": "d/x.py", "imports": [], "defines": [], "entrypoint_tags": []})
        graph["files"].append({"path": "d/y.py", "imports": [], "defines": [], "entrypoint_tags": []})
        graph["edges"].extend(
            [
                {"from": "d/x.py", "to": "a/mod.py", "kind": "imports_internal"},
                {"from": "d/y.py", "to": "a/mod.py", "kind": "imports_internal"},
            ]
        )
        model = build_uml_model(graph, depth=1)
        # 4 nodes -> budget 8; 8 distinct edges -> nothing pruned.
        self.assertEqual(model["omissions"]["edges_pruned"], 0)
        heavy = next(edge for edge in model["edges"] if edge["from"] == "d" and edge["to"] == "a")
        self.assertEqual(heavy["weight"], 2)

    def test_alias_collisions_are_deduplicated(self) -> None:
        graph = {
            "files": [
                {"path": "src/a-b/mod.py", "imports": [], "defines": [], "entrypoint_tags": []},
                {"path": "src/a_b/mod.py", "imports": [], "defines": [], "entrypoint_tags": []},
            ],
            "symbols": [],
            "edges": [],
        }
        model = build_uml_model(graph)
        aliases = [node["alias"] for node in model["nodes"]]
        self.assertEqual(len(aliases), len(set(aliases)))

    def test_invalid_options_raise_value_error(self) -> None:
        graph = {"files": [], "symbols": [], "edges": []}
        with self.assertRaises(ValueError):
            build_uml_model(graph, level="class")
        with self.assertRaises(ValueError):
            build_uml_model(graph, depth=0)
        with self.assertRaises(ValueError):
            build_uml_model(graph, max_nodes=1)


class PlantUmlRenderTests(unittest.TestCase):
    def test_source_carries_layout_hardening_theme_and_interface(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _sample_repo(root)
            model = build_uml_model(build_codegraph(root, generated_at="2026-01-01T00:00:00Z"))
        source = render_plantuml(model)
        self.assertTrue(source.startswith("@startuml\n"))
        self.assertTrue(source.endswith("@enduml\n"))
        for directive in (
            "top to bottom direction",
            "skinparam linetype ortho",
            "skinparam packageStyle rectangle",
            "skinparam classAttributeIconSize 0",
            "hide circle",
            "hide empty members",
            "skinparam backgroundColor #FFFFFF",
        ):
            self.assertIn(directive, source)
        self.assertIn('class "app/core" as u_app_core <<package>>', source)
        self.assertIn("{method} +run()", source)
        self.assertIn("{method} +Engine", source)
        self.assertIn("u_app_cli ..> u_app_core", source)
        self.assertIn("not architecture proof", source)
        self.assertNotIn("!pragma layout smetana", source)

    def test_smetana_pragma_and_mono_theme_are_explicit(self) -> None:
        model = build_uml_model({"files": [], "symbols": [], "edges": []})
        source = render_plantuml(model, theme="mono", layout_engine="smetana", title="Empty")
        self.assertIn("!pragma layout smetana", source)
        self.assertIn("title Empty", source)
        self.assertIn("BorderColor #111111", source)

    def test_folded_node_renders_last_with_count(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _sample_repo(root)
            model = build_uml_model(build_codegraph(root), max_nodes=2)
        source = render_plantuml(model)
        self.assertIn('class "+3 more units" as u_other <<folded>>', source)
        self.assertLess(source.index("u_app_core"), source.index("u_other"))
        self.assertIn("edges into folded units not drawn", source)

    def test_unknown_theme_or_engine_raise(self) -> None:
        model = build_uml_model({"files": [], "symbols": [], "edges": []})
        with self.assertRaises(ValueError):
            render_plantuml(model, theme="neon")
        with self.assertRaises(ValueError):
            render_plantuml(model, layout_engine="auto")


class RenderPlanTests(unittest.TestCase):
    def test_plantuml_cli_is_preferred_and_dot_selects_engine(self) -> None:
        which = {"plantuml": "/usr/local/bin/plantuml", "java": "/usr/bin/java", "dot": "/usr/local/bin/dot"}.get
        plan = render_plan(source_path="/tmp/x.puml", environ={}, which=which)
        self.assertEqual(plan["schema_version"], "uml_render_plan/v1")
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["renderer"], "plantuml")
        self.assertEqual(plan["layout_engine"], "dot")
        self.assertEqual(plan["command"][0], "plantuml")
        self.assertIn("-DPLANTUML_LIMIT_SIZE=8192", plan["command"])
        self.assertIn("-tpng", plan["command"])
        self.assertEqual(plan["expected_output_path"], "/tmp/x.png")
        self.assertEqual(plan["blockers"], [])
        self.assertIn("not render evidence", plan["claim_boundary"])

    def test_jar_path_from_env_with_java_falls_back_to_smetana_without_dot(self) -> None:
        with TemporaryDirectory() as tmp:
            jar = Path(tmp) / "plantuml.jar"
            jar.write_bytes(b"")
            which = {"java": "/opt/jdk/bin/java"}.get
            plan = render_plan(
                source_path="diagram.puml",
                output_format="svg",
                environ={"PLANTUML_JAR": str(jar)},
                which=which,
            )
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["renderer"], "java_jar")
        self.assertEqual(plan["layout_engine"], "smetana")
        self.assertEqual(plan["command"][:2], ["java", "-Djava.awt.headless=true"])
        self.assertIn("-tsvg", plan["command"])
        self.assertTrue(any("smetana" in note for note in plan["notes"]))
        self.assertTrue(any("SVG does not preview inline" in note for note in plan["notes"]))

    def test_missing_renderer_blocks_with_install_hint(self) -> None:
        plan = render_plan(source_path="d.puml", environ={}, which=lambda name: None)
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(plan["renderer"], "missing")
        self.assertEqual(plan["command"], [])
        self.assertTrue(any("brew install plantuml" in blocker for blocker in plan["blockers"]))

    def test_explicit_dot_without_graphviz_blocks(self) -> None:
        which = {"plantuml": "/usr/local/bin/plantuml"}.get
        plan = render_plan(source_path="d.puml", layout_engine="dot", environ={}, which=which)
        self.assertEqual(plan["status"], "blocked")
        self.assertTrue(any("--layout smetana" in blocker for blocker in plan["blockers"]))

    def test_macos_java_stub_is_flagged(self) -> None:
        with TemporaryDirectory() as tmp:
            jar = Path(tmp) / "plantuml.jar"
            jar.write_bytes(b"")
            which = {"java": "/usr/bin/java", "dot": "/opt/homebrew/bin/dot"}.get
            plan = render_plan(source_path="d.puml", environ={"PLANTUML_JAR": str(jar)}, which=which)
        self.assertTrue(any("launcher stub" in note for note in plan["notes"]))


class UmlCommandTests(unittest.TestCase):
    def test_stdout_is_plantuml_source_by_default(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _sample_repo(root)
            status, stdout, stderr = run_cli(["codegraph", "uml", "--repo", str(root)], output_json=False)
        self.assertEqual(status, 0, stderr)
        self.assertEqual(stderr, "")
        self.assertTrue(stdout.startswith("@startuml\n"))
        self.assertIn("@enduml", stdout)

    def test_output_writes_source_and_prints_summary_with_render_plan(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _sample_repo(root)
            target = root / "out" / "arch.puml"
            status, stdout, stderr = run_cli(
                ["codegraph", "uml", "--repo", str(root), "--output", str(target), "--focus", "app/cli"],
                output_json=False,
            )
            self.assertEqual(status, 0, stderr)
            self.assertTrue(target.exists())
            self.assertTrue(target.read_text(encoding="utf-8").startswith("@startuml"))
        self.assertIn("OMH codebase UML", stdout)
        self.assertIn("focus app/cli", stdout)
        self.assertIn("Render:", stdout)
        self.assertIn("not render evidence", stdout)

    def test_json_payload_carries_model_source_and_plan(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _sample_repo(root)
            status, stdout, stderr = run_cli(
                ["codegraph", "uml", "--repo", str(root), "--level", "module", "--max-nodes", "3", "--json"],
                output_json=False,
            )
        self.assertEqual(status, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["schema_version"], "codebase_uml/v1")
        self.assertEqual(payload["model"]["view"]["level"], "module")
        self.assertEqual(payload["model"]["layout"]["node_count"], 3)
        self.assertTrue(payload["plantuml"].startswith("@startuml"))
        self.assertEqual(payload["render_plan"]["schema_version"], "uml_render_plan/v1")
        self.assertEqual(payload["source_path"], "")
        self.assertIn("layout_engine", payload["render_plan"])

    def test_invalid_repo_is_a_clean_error(self) -> None:
        status, _stdout, stderr = run_cli(["codegraph", "uml", "--repo", "/definitely/missing/repo"], output_json=False)
        self.assertNotEqual(status, 0)
        self.assertIn("does not exist", stderr)

    def test_text_summary_lists_omissions(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _sample_repo(root)
            model = build_uml_model(build_codegraph(root), max_nodes=2)
        plan = render_plan(source_path="x.puml", environ={}, which=lambda name: None)
        text = render_uml_text({"model": model, "render_plan": plan, "source_path": "x.puml"})
        self.assertIn("3 units folded", text)
        self.assertIn("blocker:", text)


if __name__ == "__main__":
    unittest.main()
