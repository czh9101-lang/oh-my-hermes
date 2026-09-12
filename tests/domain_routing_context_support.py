from __future__ import annotations

import importlib
from pathlib import Path
from project_identity_fixture import project_identity, seed_repository_remote


CLAIM_BOUNDARY = (
    "Reviewed domain context only selects one wrapper clarification question; it is not "
    "routing, plan approval, execution, review, CI, merge, authentication, or Hermes "
    "internal-memory evidence."
)


def _contract_module():
    return importlib.import_module("omh.workflows.domain_routing_context")


def _sales_target(*, locale: str = "ko", question: str | None = None):
    contract = _contract_module()
    return contract.DomainClarificationTarget(
        workflow_hint="sales-development",
        required_input="account or segment",
        question_locale=locale,
        question_text=(
            question
            if question is not None
            else "이 영업 작업은 어떤 계정 또는 고객 세그먼트에 집중해야 하나요?"
        ),
    )


def _resolver():
    return _contract_module().resolve_domain_routing_context


def _repository(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / ".git").mkdir()
    seed_repository_remote(root)
    return root


def _approve_profile(
    root: Path,
    *,
    domain_id: str,
    phrase: str = "pipeline review",
    canonical: str = "pipeline_review",
    workflow_hints: list[str] | None = None,
    scope_kind: str = "project",
    scope_ref: str | None = None,
) -> dict[str, object]:
    from omh.paths import resolve_paths
    from omh.workflows.domain_intelligence import (
        approve_domain_candidate,
        capture_domain_candidate,
    )

    paths = resolve_paths(root / ".omh", root / ".hermes")
    candidate = capture_domain_candidate(
        paths,
        scope_kind=scope_kind,
        scope_ref=scope_ref or project_identity(root),
        domain_id=domain_id,
        mappings=[(phrase, canonical)],
        workflow_hints=(
            ["sales-development"] if workflow_hints is None else workflow_hints
        ),
    )["candidate"]
    profile = approve_domain_candidate(paths, str(candidate["candidate_id"]))["profile"]
    for dirname in ("profiles", "reviews", "history"):
        (_store(root) / dirname).mkdir(parents=True, exist_ok=True)
    return profile


def _binding(root: Path):
    module = importlib.import_module("omh.workflows.domain_project_context")
    binding = module.bind_cli_project(root)
    if binding is None:
        raise AssertionError("fixture failed to mint a project binding")
    return binding


def _store(root: Path) -> Path:
    return root / ".omh" / "memory" / "domain-intelligence"
