"""Agent-facing preparation and assessment for source-bound quality evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from ..installer import OmhError
from ..quality.evidence_records import assess_quality_evidence, build_quality_evidence_package
from ..quality.working_tree_fingerprint import working_tree_content_fingerprint
from ..quality.language_diagnostic_evidence import (
    LANGUAGE_DIAGNOSTIC_CHECK_STATES,
    LANGUAGE_DIAGNOSTIC_OWNERS,
    LanguageDiagnosticEvidenceError,
    build_language_diagnostic_evidence,
    language_diagnostic_claim_support,
)
from .common import _paths, _print_json, _wants_json


def cmd_quality_evidence_prepare(args: argparse.Namespace) -> int:
    """Prepare QA, review, and claim requirements without executing them."""
    try:
        package = build_quality_evidence_package(
            repository_id=args.repository,
            commit_sha=args.commit,
            tree_sha=args.tree,
            title=args.title,
            executor_target=args.executor,
            scenarios=_json_list(args.scenarios_json, args.scenarios_file, "scenarios"),
            review_requirements=_json_list(args.reviews_json, args.reviews_file, "review requirements"),
            claim_requirements=_json_list(args.claims_json, args.claims_file, "claim requirements"),
            self_critique_questions=_json_strings(args.self_critique_json, args.self_critique_file, "self-critique questions"),
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise OmhError(str(exc)) from exc
    _print_json(package)
    return 0


def cmd_quality_evidence_assess(args: argparse.Namespace) -> int:
    """Assess a package and optional observations; never execute external work."""
    try:
        package = _json_object(args.package)
        observations = _json_list(args.observations_json, args.observations_file, "observations")
        assessment = assess_quality_evidence(
            package,
            observations,
            omh_home=_paths(args).omh_home,
            current_fingerprint=working_tree_content_fingerprint(),
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise OmhError(str(exc)) from exc
    _print_json(assessment)
    return 0


def cmd_quality_evidence_language_diagnostics(args: argparse.Namespace) -> int:
    """Render one supplied language-diagnostic observation as a scoped record.

    Read-only in both directions: OMH starts no language server and writes no
    file. The diagnostics come from a caller that ran the provider, and the
    verdict is derived from them rather than accepted from them.
    """
    try:
        record = build_language_diagnostic_evidence(
            owner=args.owner,
            provider=args.provider,
            workspace_id=args.workspace,
            baseline_revision=args.baseline_revision,
            end_revision=args.end_revision,
            diagnostics_revision=args.diagnostics_revision,
            check_state=args.check_state,
            config_digest=args.config_digest,
            changed_paths=_json_strings(args.changed_paths_json, args.changed_paths_file, "changed paths"),
            introduced=_json_list(args.introduced_json, args.introduced_file, "introduced diagnostics"),
            resolved=_json_list(args.resolved_json, args.resolved_file, "resolved diagnostics"),
            observed_at=args.observed_at,
            evidence_refs=_json_strings(args.evidence_refs_json, None, "evidence refs"),
        )
    except (OSError, json.JSONDecodeError, TypeError, LanguageDiagnosticEvidenceError, ValueError) as exc:
        raise OmhError(str(exc)) from exc
    support = language_diagnostic_claim_support(record)
    if _wants_json(args):
        _print_json({"record": record, "claim_support": support})
    else:
        _print_language_diagnostic_summary(record, support)
    return 0


def _print_language_diagnostic_summary(record: Mapping[str, Any], support: Mapping[str, Any]) -> None:
    print("OMH language diagnostic evidence")
    print(f"  Verdict: {record['verdict']}")
    print(f"  {record['summary_label']}")
    print("Check")
    print(f"  Owner: {record['owner']}    Provider: {record['provider']}    State: {record['check_state']}")
    print(f"  Workspace: {record['workspace_id'] or '(not stated)'}")
    print(
        f"  Interval: {record['baseline_revision'] or '(not stated)'}"
        f" -> {record['end_revision'] or '(not stated)'}"
        f"    Diagnostics at: {record['diagnostics_revision'] or '(not stated)'}"
    )
    print(f"  Freshness: {record['freshness']}    Attribution: {record['attribution']}")
    print(
        f"  Introduced: {record['introduced_count']}"
        f"    Resolved: {record['resolved_count']}"
        f"    Changed paths: {record['changed_path_count']}"
    )
    print("Boundary")
    supported = support.get("supported_claims") or []
    unsupported = support.get("unsupported_claims") or []
    print(f"  Supports: {', '.join(str(item) for item in supported) or 'nothing'}")
    print(f"  Does not support: {', '.join(str(item) for item in unsupported)}")
    print(f"  {record['claim_boundary']}")


def _add_quality_evidence_commands(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the operator-only quality evidence control-plane commands."""
    quality = sub.add_parser(
        "quality-evidence",
        help="Prepare or assess source-bound quality evidence (operator/backend; no execution).",
        description=(
            "Operator/backend commands for prepared quality evidence. Preparation and assessment "
            "do not run tests, review, CI, merge, or any runtime dispatch."
        ),
    )
    commands = quality.add_subparsers(dest="quality_evidence_command", required=True)
    prepare = commands.add_parser(
        "prepare",
        help="Create a prepared_not_observed package from source and JSON requirements.",
        description="Create requirements only; this does not execute tests or perform review/CI.",
    )
    prepare.add_argument("--repository", "--repository-id", dest="repository", required=True)
    prepare.add_argument("--commit", "--commit-sha", dest="commit", required=True)
    prepare.add_argument("--tree", "--tree-sha", dest="tree", required=True)
    prepare.add_argument("--title", required=True)
    prepare.add_argument("--executor", "--executor-target", dest="executor", required=True)
    _add_json_input(prepare, "scenarios", "QA scenarios")
    _add_json_input(prepare, "reviews", "review requirements")
    _add_json_input(prepare, "claims", "claim requirements")
    prepare.add_argument("--self-critique-json", "--self-critique", dest="self_critique_json", help="Inline self-critique question JSON array.")
    prepare.add_argument("--self-critique-file", dest="self_critique_file", help="Path to self-critique question JSON array.")
    prepare.set_defaults(func=cmd_quality_evidence_prepare)

    assess = commands.add_parser(
        "assess",
        help="Assess a package plus optional observations without claiming execution.",
        description="Assessment checks source-bound evidence consistency; it does not run tests or merge.",
    )
    assess.add_argument("--package", required=True, help="Path to a prepared quality evidence package JSON.")
    assess.add_argument("--observations-json", "--observations", dest="observations_json", help="Inline observations JSON.")
    assess.add_argument("--observations-file", help="Path to observations JSON.")
    assess.set_defaults(func=cmd_quality_evidence_assess)

    language = commands.add_parser(
        "language-diagnostics",
        help="Scope a supplied language-server diagnostic delta as a language-diagnostic check.",
        description=(
            "Render supplied diagnostics as a language_diagnostic_evidence/v1 record. OMH starts no "
            "language server and observes nothing; a clean result is labelled only as a fresh "
            "language-diagnostic check and never as verification, tests, review, CI, or merge evidence."
        ),
    )
    language.add_argument("--owner", required=True, choices=LANGUAGE_DIAGNOSTIC_OWNERS, help="Surface that observed the check.")
    language.add_argument("--provider", required=True, help="Diagnostic provider label, such as a language-server name.")
    language.add_argument("--workspace", default="", help="Workspace identifier the interval is attributed to.")
    language.add_argument("--baseline-revision", default="", help="Revision the interval starts at.")
    language.add_argument("--end-revision", default="", help="Revision the interval ends at.")
    language.add_argument("--diagnostics-revision", default="", help="Revision the diagnostics were observed at.")
    language.add_argument("--check-state", default="observed", choices=LANGUAGE_DIAGNOSTIC_CHECK_STATES)
    language.add_argument("--config-digest", default="", help="Digest of the provider configuration in effect.")
    language.add_argument("--observed-at", default="", help="Caller-supplied observation timestamp.")
    _add_json_input(language, "changed-paths", "changed workspace-relative paths")
    _add_json_input(language, "introduced", "introduced diagnostics")
    _add_json_input(language, "resolved", "resolved diagnostics")
    language.add_argument("--evidence-refs-json", "--evidence-refs", dest="evidence_refs_json", help="Inline bounded evidence reference JSON array.")
    language.add_argument("--json", action="store_true", help="Print the machine-readable record and claim support.")
    language.set_defaults(func=cmd_quality_evidence_language_diagnostics)


def _add_json_input(parser: argparse.ArgumentParser, name: str, label: str) -> None:
    # A multi-word flag is hyphenated on the command line and underscored in the
    # namespace; argparse only derives that for positionals, so `dest` is set
    # here or `--changed-paths-json` lands on an unreachable attribute name.
    dest = name.replace("-", "_")
    parser.add_argument(f"--{name}-json", f"--{name}", dest=f"{dest}_json", help=f"Inline {label} JSON array.")
    parser.add_argument(f"--{name}-file", dest=f"{dest}_file", help=f"Path to {label} JSON array.")


def _json_object(path: str) -> dict[str, object]:
    value = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("package JSON must be an object")
    return value


def _json_list(inline: str | None, path: str | None, label: str) -> list[Mapping[str, object]]:
    if inline is not None and path is not None:
        raise ValueError(f"{label} accepts either inline JSON or a file, not both")
    raw = inline if inline is not None else Path(path).expanduser().read_text(encoding="utf-8") if path else "[]"
    value = json.loads(raw)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{label} JSON must be an array of objects")
    return value


def _json_strings(inline: str | None, path: str | None, label: str) -> list[str]:
    if inline is not None and path is not None:
        raise ValueError(f"{label} accepts either inline JSON or a file, not both")
    raw = inline if inline is not None else Path(path).expanduser().read_text(encoding="utf-8") if path else "[]"
    value = json.loads(raw)
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{label} JSON must be an array of nonblank strings")
    return value
