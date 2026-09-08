"""CLI leaf registration for project-local browser skill promotion.

The parent web-qa command owns registration; this module deliberately does not
modify shared parsers.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable

from ..install.installer import OmhError
from ..workflows.browser_skill_promotion import (
    BrowserSkillPromotionError, approve_browser_skill_lifecycle,
    approve_browser_skill_removal, approve_browser_skill_rollback,
    browser_skill_promotion_status, promote_approved_browser_skill,
    retry_browser_skill_promotion, review_browser_skill_lifecycle,
    review_browser_skill_removal, review_browser_skill_rollback,
)
from ..workflows.browser_skill_promotion_approval import BrowserSkillPromotionApprovalError
from ..workflows.browser_skill_promotion_plan import BrowserSkillPromotionPlanError
from ..workflows.browser_workflow_learning import BrowserTraceError
from .common import _print_json


def _run(action: Callable[[], dict[str, object]]) -> int:
    try:
        _print_json(action())
    except (BrowserSkillPromotionError, BrowserSkillPromotionApprovalError, BrowserSkillPromotionPlanError, BrowserTraceError) as exc:
        raise OmhError(str(exc)) from exc
    return 0


def add_browser_skill_promotion_commands(parent: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    promotion = parent.add_parser("promotion", help="Review and explicitly promote approved browser traces into this project.")
    commands = promotion.add_subparsers(dest="browser_skill_promotion_command", required=True)
    diff = commands.add_parser("diff", help="Render the exact promotion diff and native preflight.")
    _trace_skill(diff); diff.add_argument("--operation", choices=("install", "update"), default="install"); diff.set_defaults(func=lambda args: _run(lambda: review_browser_skill_lifecycle(args.project_root, args.trace_id, args.skill_name, operation=args.operation)))
    approve = commands.add_parser("approve", help="Persist exact-diff promotion approval.")
    _trace_skill(approve); approve.add_argument("--reviewed-diff-digest", required=True); approve.add_argument("--reviewer", required=True); approve.add_argument("--operation", choices=("install", "update"), default="install"); approve.set_defaults(func=lambda args: _run(lambda: approve_browser_skill_lifecycle(args.project_root, args.trace_id, args.skill_name, reviewed_diff_digest=args.reviewed_diff_digest, reviewer_identity=args.reviewer, operation=args.operation)))
    promote = commands.add_parser("promote", help="Activate exactly one approved promotion receipt.")
    _project_only(promote); promote.add_argument("--receipt-id", required=True); promote.set_defaults(func=lambda args: _run(lambda: promote_approved_browser_skill(args.project_root, args.receipt_id)))
    status = commands.add_parser("status", help="Validate actual entry/manifest/approval truth and optionally deactivate drift.")
    _project_skill(status); status.add_argument("--no-source-check", action="store_true"); status.set_defaults(func=lambda args: _run(lambda: browser_skill_promotion_status(args.project_root, args.skill_name, check_source=not args.no_source_check)))
    rollback = commands.add_parser("rollback", help="Review or approve a retained generation rollback.")
    _project_skill(rollback); rollback.add_argument("--generation", required=True); rollback.add_argument("--reviewed-diff-digest"); rollback.add_argument("--reviewer"); rollback.set_defaults(func=_rollback)
    remove = commands.add_parser("remove", help="Review or approve removal of only a verified managed SKILL.md.")
    _project_skill(remove); remove.add_argument("--reviewed-diff-digest"); remove.add_argument("--reviewer"); remove.set_defaults(func=_remove)
    retry = commands.add_parser("retry", help="Explicitly resume an incomplete approved pre-entry promotion.")
    _project_only(retry); retry.add_argument("--receipt-id", required=True); retry.set_defaults(func=lambda args: _run(lambda: retry_browser_skill_promotion(args.project_root, args.receipt_id)))


def _rollback(args: argparse.Namespace) -> int:
    if bool(args.reviewed_diff_digest) != bool(args.reviewer):
        raise OmhError("rollback approval requires both --reviewed-diff-digest and --reviewer")
    if args.reviewed_diff_digest:
        return _run(lambda: approve_browser_skill_rollback(args.project_root, args.skill_name, args.generation, reviewed_diff_digest=args.reviewed_diff_digest, reviewer_identity=args.reviewer))
    return _run(lambda: review_browser_skill_rollback(args.project_root, args.skill_name, args.generation))


def _remove(args: argparse.Namespace) -> int:
    if bool(args.reviewed_diff_digest) != bool(args.reviewer):
        raise OmhError("removal approval requires both --reviewed-diff-digest and --reviewer")
    if args.reviewed_diff_digest:
        return _run(lambda: approve_browser_skill_removal(args.project_root, args.skill_name, reviewed_diff_digest=args.reviewed_diff_digest, reviewer_identity=args.reviewer))
    return _run(lambda: review_browser_skill_removal(args.project_root, args.skill_name))


def _project_only(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-root", default=".")

def _project_skill(parser: argparse.ArgumentParser) -> None:
    _project_only(parser)
    parser.add_argument("--skill-name", required=True)
def _trace_skill(parser: argparse.ArgumentParser) -> None:
    _project_skill(parser); parser.add_argument("--trace-id", required=True)
