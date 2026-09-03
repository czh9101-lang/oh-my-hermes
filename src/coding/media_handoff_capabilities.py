"""Route-scoped media input decisions for prepared coding handoffs.

This module stores metadata only.  It never receives attachment bytes or local
paths and never probes an executor/provider.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

try:  # Supports the registered direct-source demo import as well as omh.coding.
    from .executor_capability_snapshots import INPUT_MODALITY_CAPABILITY_NAMES
    from .pre_handoff_readiness import capability_evidence_is_fresh
except ImportError:  # pragma: no cover - exercised by the direct-source command.
    from omh.coding.executor_capability_snapshots import INPUT_MODALITY_CAPABILITY_NAMES
    from omh.coding.pre_handoff_readiness import capability_evidence_is_fresh

HANDOFF_INPUT_REPRESENTATIONS: Final = (
    "text_only",
    "raw_media",
    "local_file_reference",
    "extracted_text",
    "ocr_output",
    "transcript",
    "normalized_other",
)
_MEDIA_MODALITIES: Final = frozenset({"image", "audio", "video", "document"})
_TEXT_REPRESENTATIONS: Final = frozenset({"text_only", "extracted_text", "ocr_output", "transcript", "normalized_other"})
DECISION_SCHEMA_VERSION: Final = "executor_modality_decision/v1"
DECISION_CLAIM_BOUNDARY: Final = (
    "This route-scoped capability decision is metadata-only prepared context. It is not proof of attachment "
    "receipt, provider acceptance, dispatch, execution, verification, review, CI, or merge."
)


def normalize_input_representation(value: object) -> tuple[dict[str, str], ...]:
    """Parse declared representation(s), never infer them from task prose."""
    values: Sequence[object] = value if isinstance(value, (list, tuple)) else (value,)
    rows: list[dict[str, str]] = []
    for item in values:
        if isinstance(item, Mapping):
            representation = str(item.get("representation", "") or "").strip()
            modality = str(item.get("modality", "") or "").strip()
        else:
            parts = str(item or "").strip().split(":", 1)
            representation = parts[0].strip()
            modality = parts[1].strip() if len(parts) == 2 else ""
        if representation not in HANDOFF_INPUT_REPRESENTATIONS:
            raise ValueError("input representation must be one of: " + ", ".join(HANDOFF_INPUT_REPRESENTATIONS))
        if representation == "raw_media":
            if modality not in _MEDIA_MODALITIES:
                raise ValueError("raw_media requires one of: image, audio, video, document")
        elif modality and modality != "text":
            raise ValueError("non-raw input representations may only declare text modality")
        rows.append({"representation": representation, "modality": modality or ("text" if representation in _TEXT_REPRESENTATIONS else "")})
    return tuple(rows)


def modality_requirements(value: object, route: Mapping[str, object] | None = None) -> tuple[dict[str, str], ...]:
    route_data = route if isinstance(route, Mapping) else {}
    provider = str(route_data.get("provider", route_data.get("model_family", "")) or "").strip()
    wire_model = str(route_data.get("wire_model", route_data.get("selected_model", "")) or "").strip()
    endpoint_mode = str(route_data.get("endpoint_mode", "default") or "default").strip()
    requirements: list[dict[str, str]] = []
    for row in normalize_input_representation(value):
        # Ordinary text handoffs predate modality evidence and carry no media
        # attachment.  This gate is intentionally additive: it applies only
        # where a declared media representation or transformed media crosses
        # an executor boundary.
        if row["representation"] == "text_only":
            continue
        modality = row["modality"]
        if not modality:
            continue
        capability = f"input_modality_{modality}"
        if capability not in INPUT_MODALITY_CAPABILITY_NAMES:
            continue
        requirement = {
            "capability": capability,
            "representation": row["representation"],
            "modality": modality,
            "provider": provider,
            "wire_model": wire_model,
            "endpoint_mode": endpoint_mode,
        }
        if requirement not in requirements:
            requirements.append(requirement)
    return tuple(requirements)


def build_executor_modality_decision(
    *,
    input_representation: object = "text_only",
    snapshot: Mapping[str, object] | None,
    route: Mapping[str, object] | None = None,
    now: str = "",
    transformation: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Fail closed for media until fresh, exact-route evidence says otherwise."""
    requirements = modality_requirements(input_representation, route)
    normalized_route = {
        "executor": str((snapshot or {}).get("executor", "") or ""),
        "provider": requirements[0]["provider"] if requirements else "",
        "wire_model": requirements[0]["wire_model"] if requirements else "",
    }
    transform = dict(transformation or {})
    transform_status = str(transform.get("status", "") or "")
    if any(row["representation"] in {"ocr_output", "transcript"} for row in normalize_input_representation(input_representation)) and transform_status != "observed":
        return _decision(requirements, normalized_route, "modality_transformation_unobserved", transformation=transform)
    entries = (snapshot or {}).get("capabilities", {})
    entries = entries if isinstance(entries, Mapping) else {}
    verdict = "dispatch"
    evidence_ref = ""
    observed_at = ""
    freshness = "not_required"
    fallback_reason = ""
    for requirement in requirements:
        entry = entries.get(requirement["capability"])
        entry = entry if isinstance(entry, Mapping) else {}
        scope = entry.get("scope") if isinstance(entry.get("scope"), Mapping) else {}
        exact_route = all(str(scope.get(key, "")) == requirement[key] for key in ("provider", "wire_model", "endpoint_mode"))
        status = str(entry.get("status", "unknown") or "unknown")
        observed_at = str(entry.get("observed_at", "") or "")
        evidence_ref = str(entry.get("evidence_ref", "") or "")
        fresh = bool(observed_at and capability_evidence_is_fresh(observed_at, now))
        freshness = "fresh" if fresh else "stale_or_unknown"
        if status == "unavailable" and exact_route and fresh:
            verdict, fallback_reason = "modality_unsupported", "fresh route evidence records this modality as unavailable"
            break
        if status != "host_observed" or not exact_route or not fresh:
            verdict, fallback_reason = "modality_unknown", "fresh exact-route modality evidence is required before media dispatch"
            break
    return _decision(requirements, normalized_route, verdict, evidence_ref, observed_at, freshness, transform, fallback_reason)


def _decision(
    requirements: Sequence[Mapping[str, str]], route: Mapping[str, str], verdict: str,
    evidence_ref: str = "", observed_at: str = "", freshness: str = "not_required",
    transformation: Mapping[str, object] | None = None, fallback_reason: str = "",
) -> dict[str, object]:
    return {
        "schema_version": DECISION_SCHEMA_VERSION,
        "required_representations": [dict(row) for row in requirements],
        "route": dict(route),
        "verdict": verdict,
        "evidence_ref": evidence_ref,
        "evidence_observed_at": observed_at,
        "freshness": freshness,
        "transformation": dict(transformation or {"kind": "", "status": "not_required", "evidence_ref": ""}),
        "fallback_reason": fallback_reason,
        "remaining_user_action": "" if verdict == "dispatch" else "record fresh route-scoped capability evidence or use observed transformed text",
        "claim_boundary": DECISION_CLAIM_BOUNDARY,
    }


def demo_media_handoff_decisions() -> dict[str, object]:
    """Deterministic public demonstration of supported and fail-closed routes."""
    route = {"provider": "demo", "wire_model": "vision-1", "endpoint_mode": "default"}
    supported = {"executor": "demo", "capabilities": {"input_modality_image": {"status": "host_observed", "scope": route, "evidence_ref": "operator:demo-image", "observed_at": "2026-09-03T00:00:00Z"}, "input_modality_text": {"status": "host_observed", "scope": route, "evidence_ref": "operator:demo-text", "observed_at": "2026-09-03T00:00:00Z"}}}
    return {
        "schema_version": "omh_media_handoff_decision_demo/v1",
        "supported": build_executor_modality_decision(input_representation="raw_media:image", snapshot=supported, route=route, now="2026-09-03T01:00:00Z"),
        "unknown": build_executor_modality_decision(input_representation="raw_media:image", snapshot={"executor": "demo", "capabilities": {}}, route=route, now="2026-09-03T01:00:00Z"),
        "unsupported": build_executor_modality_decision(input_representation="raw_media:image", snapshot={"executor": "demo", "capabilities": {"input_modality_image": {**supported["capabilities"]["input_modality_image"], "status": "unavailable"}}}, route=route, now="2026-09-03T01:00:00Z"),
        "transformed": build_executor_modality_decision(input_representation="ocr_output", snapshot=supported, route=route, now="2026-09-03T01:00:00Z", transformation={"kind": "ocr", "status": "observed", "evidence_ref": "operator:demo-ocr"}),
        "fallback_rechecked": build_executor_modality_decision(input_representation="raw_media:image", snapshot={"executor": "fallback", "capabilities": {}}, route={"provider": "fallback", "wire_model": "text-1", "endpoint_mode": "default"}, now="2026-09-03T01:00:00Z"),
    }
