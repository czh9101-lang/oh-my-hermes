from __future__ import annotations

import argparse
from pathlib import Path

from ..codegraph import (
    build_codegraph,
    build_handoff_context,
    codegraph_artifact_path,
    render_build_text,
    render_handoff_text,
    render_summary_text,
    summarize_codegraph,
    write_codegraph_artifact,
)
from ..codegraph.uml import (
    DEFAULT_DEPTH,
    DEFAULT_MAX_INTERFACE,
    DEFAULT_MAX_NODES,
    UML_LAYOUT_ENGINES,
    UML_LEVELS,
    UML_THEMES,
    build_uml_model,
    render_plan,
    render_plantuml,
    render_uml_text,
)
from ..installer import OmhError
from .common import _print_json, _wants_json


def cmd_codegraph_build(args: argparse.Namespace) -> int:
    try:
        graph = build_codegraph(args.repo)
        if args.write:
            artifact_path = codegraph_artifact_path(Path(graph["repo_root"]))
            graph["artifact_path"] = str(artifact_path)
            write_codegraph_artifact(graph)
    except (OSError, ValueError) as exc:
        raise OmhError(str(exc)) from exc
    if _wants_json(args):
        _print_json(graph)
    else:
        print(render_build_text(graph))
    return 0


def cmd_codegraph_summary(args: argparse.Namespace) -> int:
    try:
        graph = build_codegraph(args.repo)
    except ValueError as exc:
        raise OmhError(str(exc)) from exc
    summary = summarize_codegraph(graph)
    if _wants_json(args):
        _print_json(summary)
    else:
        print(render_summary_text(summary))
    return 0


def cmd_codegraph_handoff(args: argparse.Namespace) -> int:
    try:
        graph = build_codegraph(args.repo)
        context = build_handoff_context(graph, task=args.task)
    except ValueError as exc:
        raise OmhError(str(exc)) from exc
    if _wants_json(args):
        _print_json(context)
    else:
        print(render_handoff_text(context))
    return 0


def cmd_codegraph_uml(args: argparse.Namespace) -> int:
    try:
        graph = build_codegraph(args.repo)
        model = build_uml_model(
            graph,
            level=args.level,
            depth=args.depth,
            focus=args.focus,
            max_nodes=args.max_nodes,
            max_interface=args.max_interface,
            include_tests=args.include_tests,
        )
        source_path = str(Path(args.output).expanduser()) if args.output else "codebase.puml"
        plan = render_plan(source_path=source_path, output_format=args.format, layout_engine=args.layout)
        source = render_plantuml(model, theme=args.theme, layout_engine=plan["layout_engine"], title=args.title)
        if args.output:
            output_path = Path(source_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(source, encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise OmhError(str(exc)) from exc
    payload = {
        "schema_version": model["schema_version"],
        "model": model,
        "plantuml": source,
        "source_path": source_path if args.output else "",
        "render_plan": plan,
        "claim_boundary": model["claim_boundary"],
    }
    if _wants_json(args):
        _print_json(payload)
    elif args.output:
        print(render_uml_text(payload))
    else:
        print(source, end="")
    return 0


def _add_codegraph_commands(sub) -> None:
    codegraph = sub.add_parser(
        "codegraph",
        help="Build static local codegraph artifacts for prepared coding context.",
    )
    codegraph_sub = codegraph.add_subparsers(dest="codegraph_command", required=True)

    build = codegraph_sub.add_parser("build", help="Build a static local Python AST codegraph.")
    build.add_argument("--repo", default=".", help="Repository root to scan.")
    build.add_argument("--write", action="store_true", help="Write .omh/codegraph/codegraph.json.")
    build.add_argument("--json", action="store_true", help="Print the full codegraph artifact as JSON.")
    build.set_defaults(func=cmd_codegraph_build)

    summary = codegraph_sub.add_parser("summary", help="Print a compact static codegraph summary.")
    summary.add_argument("--repo", default=".", help="Repository root to scan.")
    summary.add_argument("--json", action="store_true", help="Print the summary payload as JSON.")
    summary.set_defaults(func=cmd_codegraph_summary)

    handoff = codegraph_sub.add_parser("handoff", help="Build compact prepared context for coding agents.")
    handoff.add_argument("--repo", default=".", help="Repository root to scan.")
    handoff.add_argument("--task", required=True, help="Task description used to rank relevant files and symbols.")
    handoff.add_argument("--json", action="store_true", help="Print the handoff context payload as JSON.")
    handoff.set_defaults(func=cmd_codegraph_handoff)

    uml = codegraph_sub.add_parser(
        "uml",
        help="Emit an interface-level, layout-hardened PlantUML diagram of the repository.",
    )
    uml.add_argument("--repo", default=".", help="Repository root to scan.")
    uml.add_argument("--level", choices=UML_LEVELS, default="package", help="Unit granularity for boxes.")
    uml.add_argument("--depth", type=int, default=DEFAULT_DEPTH, help="Directory depth that defines a package unit.")
    uml.add_argument("--focus", default="", help="Path prefix to keep, plus its direct import neighbours.")
    uml.add_argument("--max-nodes", type=int, default=DEFAULT_MAX_NODES, help="Node cap; overflow folds into one unit.")
    uml.add_argument(
        "--max-interface", type=int, default=DEFAULT_MAX_INTERFACE, help="Public symbols listed per unit."
    )
    uml.add_argument("--include-tests", action="store_true", help="Keep test files as units.")
    uml.add_argument("--theme", choices=UML_THEMES, default="omh", help="Color theme for the rendered image.")
    uml.add_argument(
        "--layout", choices=UML_LAYOUT_ENGINES, default="auto", help="Layout engine; auto picks dot when present."
    )
    uml.add_argument("--format", choices=("png", "svg"), default="png", help="Target image format for the render plan.")
    uml.add_argument("--title", default="", help="Diagram title override.")
    uml.add_argument("--output", default="", help="Write the PlantUML source here instead of stdout.")
    uml.add_argument("--json", action="store_true", help="Print model, source, and render plan as JSON.")
    uml.set_defaults(func=cmd_codegraph_uml)
