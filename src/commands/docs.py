from __future__ import annotations

import argparse
from pathlib import Path

from ..catalogs.roles import roles_reference_markdown
from ..installer import OmhError
from ..local_store import atomic_write_text
from ..skill_pack import builtin_skill_reference_templates, builtin_skill_templates
from ..skills.catalog import omh_skill_display_name
from ..skills.context_cost import skill_context_cost_markdown, skill_context_cost_payload
from ..skills.render import workflow_reference_markdown, workflow_reference_payload
from ..skills.validation import harness_inspection_payload, harness_summary_payload, validate_catalog_contract
from .common import _print_json


def cmd_docs_workflows(args: argparse.Namespace) -> int:
    if args.json:
        if args.check:
            raise OmhError("docs workflows --json cannot be combined with --check")
        if args.output:
            raise OmhError("docs workflows --json cannot be combined with --output")
        _print_json(workflow_reference_payload())
        return 0
    content = workflow_reference_markdown()
    output = Path(args.output).expanduser().resolve() if args.output else Path("docs/WORKFLOWS.md").resolve()
    if args.check:
        try:
            current = output.read_text(encoding="utf-8")
        except OSError as exc:
            raise OmhError(f"workflow docs check failed: {exc}") from exc
        if current != content:
            raise OmhError(f"workflow docs are stale: {output}")
        tap_skills = _tap_skills_check_payload(Path("skills"))
        if not tap_skills["ok"]:
            stale = ", ".join(tap_skills["missing"] + tap_skills["stale"] + tap_skills["extra"])
            raise OmhError(f"tap skills are stale: {stale}")
        _print_json({"ok": True, "checked": str(output), "tap_skills": tap_skills})
        return 0
    if args.output:
        atomic_write_text(output, content)
        _print_json({"written": str(output)})
        return 0
    print(content.rstrip())
    return 0


def cmd_docs_roles(args: argparse.Namespace) -> int:
    content = roles_reference_markdown()
    output = Path(args.output).expanduser().resolve() if args.output else Path("docs/ROLES.md").resolve()
    if args.check:
        try:
            current = output.read_text(encoding="utf-8")
        except OSError as exc:
            raise OmhError(f"role docs check failed: {exc}") from exc
        if current != content:
            raise OmhError(f"role docs are stale: {output}")
        _print_json({"ok": True, "checked": str(output)})
        return 0
    if args.output:
        atomic_write_text(output, content)
        _print_json({"written": str(output)})
        return 0
    print(content.rstrip())
    return 0


def cmd_docs_capability_families(args: argparse.Namespace) -> int:
    from ..capabilities.families import standalone_capability_families_json

    content = standalone_capability_families_json()
    output = (
        Path(args.output).expanduser().resolve()
        if args.output
        else _default_capability_families_path()
    )
    if args.check:
        try:
            current = output.read_text(encoding="utf-8")
        except OSError as exc:
            raise OmhError(f"capability families sidecar check failed: {exc}") from exc
        if current != content:
            raise OmhError(f"capability families sidecar is stale: {output}")
        _print_json({"ok": True, "checked": str(output)})
        return 0
    atomic_write_text(output, content)
    _print_json({"written": str(output)})
    return 0


def cmd_docs_ulw_inventory(args: argparse.Namespace) -> int:
    from ..skills.catalog import ulw_inventory_payload

    if args.json:
        if args.check:
            raise OmhError("docs ulw-inventory --json cannot be combined with --check")
        _print_json(ulw_inventory_payload())
        return 0
    return _sync_ulw_region(
        path=Path(args.path).expanduser().resolve(),
        check=args.check,
        label="README ULW inventory region",
    )


def cmd_docs_ulw_site(args: argparse.Namespace) -> int:
    return _sync_ulw_region(
        path=Path(args.path).expanduser().resolve(),
        check=args.check,
        label="site ULW region",
        site=True,
    )


def _sync_ulw_region(*, path: Path, check: bool, label: str, site: bool = False) -> int:
    from ..catalogs.ulw_surfaces import readme_with_generated_region, site_with_generated_region

    regenerate = readme_with_generated_region if not site else site_with_generated_region
    try:
        current = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OmhError(f"{label} check failed: {exc}") from exc
    try:
        updated = regenerate(current)
    except ValueError as exc:
        raise OmhError(f"{label} check failed: {exc}") from exc
    if check:
        if current != updated:
            raise OmhError(f"{label} is stale: {path}")
        _print_json({"ok": True, "checked": str(path)})
        return 0
    if updated != current:
        atomic_write_text(path, updated)
    _print_json({"written": str(path)})
    return 0


def cmd_docs_skill_context_cost(args: argparse.Namespace) -> int:
    if args.json:
        _print_json(skill_context_cost_payload())
        return 0
    print(skill_context_cost_markdown().rstrip())
    return 0


def cmd_docs_skill_lint(args: argparse.Namespace) -> int:
    """Return one structural pass/fail verdict for every tracked skill.

    Exit codes are the contract: 0 pass, 1 violations, 2 invocation or internal
    error. The unsupported-format branch raises `OmhError` so it exits 2 through
    the same path every other invocation error uses.
    """
    from ..quality.skill_governance import skill_structure_lint_report

    if args.format != "json":
        raise OmhError(f"unsupported skill-lint format: {args.format}")
    report = skill_structure_lint_report()
    _print_json(report)
    return 0 if report["ok"] else 1


def _default_capability_families_path() -> Path:
    from ..plugin_bundle.omh import tools as plugin_tools

    return (Path(plugin_tools.__file__).resolve().parent / "capability_families.json").resolve()


def _tap_skills_check_payload(skills_root: Path) -> dict[str, object]:
    templates = {omh_skill_display_name(template.name): template for template in builtin_skill_templates()}
    reference_templates = {
        Path(omh_skill_display_name(template.skill_name)) / template.relative_path: template
        for template in builtin_skill_reference_templates()
    }
    paths = {path.parent.name: path for path in skills_root.glob("*/SKILL.md")}
    reference_paths = {path.relative_to(skills_root): path for path in skills_root.glob("*/references/*.md")}
    missing = sorted(name for name in templates if name not in paths)
    missing.extend(path.as_posix() for path in sorted(reference_templates) if path not in reference_paths)
    extra = sorted(name for name in paths if name not in templates)
    extra.extend(path.as_posix() for path in sorted(reference_paths) if path not in reference_templates)
    stale: list[str] = []
    for name, path in sorted(paths.items()):
        if name in templates and path.read_text(encoding="utf-8") != templates[name].content:
            stale.append(name)
    for rel_path, path in sorted(reference_paths.items()):
        template = reference_templates.get(rel_path)
        if template and path.read_text(encoding="utf-8") != template.content:
            stale.append(rel_path.as_posix())
    return {
        "ok": not missing and not stale and not extra,
        "root": str(skills_root.resolve()),
        "expected": len(templates) + len(reference_templates),
        "checked": len(paths) + len(reference_paths),
        "missing": missing,
        "stale": stale,
        "extra": extra,
    }


def cmd_harness_list(args: argparse.Namespace) -> int:
    _print_json(harness_summary_payload())
    return 0


def cmd_harness_inspect(args: argparse.Namespace) -> int:
    try:
        _print_json(harness_inspection_payload(args.name))
    except KeyError as exc:
        raise OmhError(f"unknown harness: {args.name}") from exc
    return 0


def cmd_harness_validate(args: argparse.Namespace) -> int:
    result = validate_catalog_contract()
    _print_json(result)
    return 0 if result["ok"] else 1


def _add_docs_commands(sub) -> None:
    docs = sub.add_parser("docs", help="Render or check generated OMH workflow reference docs.")
    docs_sub = docs.add_subparsers(dest="docs_command", required=True)

    docs_workflows = docs_sub.add_parser("workflows")
    docs_workflows.add_argument("--output", default=None)
    docs_workflows.add_argument("--check", action="store_true")
    docs_workflows.add_argument("--json", action="store_true", help="Print machine-readable workflow and harness catalog metadata.")
    docs_workflows.set_defaults(func=cmd_docs_workflows)

    docs_roles = docs_sub.add_parser("roles")
    docs_roles.add_argument("--output", default=None)
    docs_roles.add_argument("--check", action="store_true")
    docs_roles.set_defaults(func=cmd_docs_roles)

    docs_capability_families = docs_sub.add_parser(
        "capability-families",
        help="Write or check the generated plugin-bundle capability-family sidecar JSON.",
    )
    docs_capability_families.add_argument("--output", default=None)
    docs_capability_families.add_argument("--check", action="store_true")
    docs_capability_families.set_defaults(func=cmd_docs_capability_families)

    docs_ulw_inventory = docs_sub.add_parser(
        "ulw-inventory",
        help="Write or check the generated ULW table region of the English README, or print the inventory payload.",
    )
    docs_ulw_inventory.add_argument("--path", default="README.md")
    docs_ulw_inventory.add_argument("--check", action="store_true")
    docs_ulw_inventory.add_argument(
        "--json",
        action="store_true",
        help="Print the machine-readable omh_ulw_inventory/v1 payload.",
    )
    docs_ulw_inventory.set_defaults(func=cmd_docs_ulw_inventory)

    docs_ulw_site = docs_sub.add_parser(
        "ulw-site",
        help="Write or check the generated ULW region of site/index.html.",
    )
    docs_ulw_site.add_argument("--path", default="site/index.html")
    docs_ulw_site.add_argument("--check", action="store_true")
    docs_ulw_site.set_defaults(func=cmd_docs_ulw_site)

    docs_skill_context_cost = docs_sub.add_parser(
        "skill-context-cost",
        help="Report always-loaded skill-body size and cross-skill repetition for the core and full profiles.",
    )
    docs_skill_context_cost.add_argument(
        "--json",
        action="store_true",
        help="Print the machine-readable omh_skill_context_cost/v1 payload.",
    )
    docs_skill_context_cost.set_defaults(func=cmd_docs_skill_context_cost)

    docs_skill_lint = docs_sub.add_parser(
        "skill-lint",
        help=(
            "Check every tracked skill for structure only: parsed frontmatter, machine-consumed "
            "fields, resolvable harness, generated parity, trigger format, executable consumers, "
            "and the context budget. Offline and pass/fail; never scores, ranks, or reviews wording."
        ),
    )
    docs_skill_lint.add_argument(
        "--format",
        default="json",
        choices=("json",),
        help="Output format for the omh_skill_structure_lint/v1 report.",
    )
    docs_skill_lint.set_defaults(func=cmd_docs_skill_lint)


def _add_harness_commands(sub) -> None:
    harness = sub.add_parser("harness", help="List, inspect, and validate workflow harness contracts.")
    harness_sub = harness.add_subparsers(dest="harness_command", required=True)

    harness_list = harness_sub.add_parser("list")
    harness_list.set_defaults(func=cmd_harness_list)

    harness_inspect = harness_sub.add_parser("inspect")
    harness_inspect.add_argument("name")
    harness_inspect.set_defaults(func=cmd_harness_inspect)

    harness_validate = harness_sub.add_parser("validate")
    harness_validate.set_defaults(func=cmd_harness_validate)
