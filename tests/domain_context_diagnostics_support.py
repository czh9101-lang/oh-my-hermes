from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from project_identity_fixture import project_identity
from omh.workflows.domain_routing_context import DomainRoutingResolution

from test_domain_routing_context import _binding


def _ensure_store(root: Path) -> None:
    (root / ".omh" / "memory" / "domain-intelligence").mkdir(
        parents=True,
        exist_ok=True,
    )


def _profile(
    root: Path,
    *,
    canonical: str = "pipeline_review",
    hints: list[object] | None = None,
) -> dict[str, object]:
    return {
        "scope": {
            "kind": "project",
            "ref": project_identity(root),
            "ref_authority": "operator_or_wrapper_supplied",
            "identity_claim": "not_authenticated_identity_evidence",
        },
        "vocabulary_mappings": [
            {"phrase": "pipeline review", "canonical": canonical}
        ],
        "workflow_hints": ["sales-development"] if hints is None else hints,
    }


class DomainDiagnosticResolverMixin:
    def _resolve(
        self,
        root: Path,
        profiles: tuple[dict[str, object], ...],
        message: object = "Please do a pipeline review",
    ) -> DomainRoutingResolution:
        from omh.workflows.domain_routing_context import (
            resolve_domain_routing_context_result,
        )

        _ensure_store(root)
        with (
            patch(
                "omh.workflows.domain_intelligence_profile_snapshot."
                "read_validated_domain_profiles_at",
                return_value=profiles,
            ),
            _binding(root) as binding,
        ):
            return resolve_domain_routing_context_result(
                binding,
                message,
                locale="en",
            )


def _find_resolution_results(value: object) -> list[DomainRoutingResolution]:
    if isinstance(value, DomainRoutingResolution):
        return [value]
    if isinstance(value, dict):
        return [
            result
            for nested in value.values()
            for result in _find_resolution_results(nested)
        ]
    if isinstance(value, (list, tuple)):
        return [
            result
            for nested in value
            for result in _find_resolution_results(nested)
        ]
    return []
