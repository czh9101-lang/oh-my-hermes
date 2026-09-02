from __future__ import annotations

from typing import Any

from ..surfaces.work_artifact_copy import (
    WORK_ARTIFACT_COPY_MANIFEST_SCHEMA_VERSION,
    build_work_artifact_copy_manifest,
    select_work_artifact,
)

LIST_ACTION = "list_work_artifacts"
SELECT_ACTION = "select_work_artifact"


def build_work_artifact_copy_action(
    status_payload: dict[str, Any],
    *,
    artifact_id: str = "",
) -> dict[str, Any]:
    """List the current work item's copyable artifacts, or hand back exactly one.

    Reads an already-built ``wrapper_session_result/v1`` payload; it performs no
    session I/O, records nothing, and leaves ``next_action`` at ``show_status``
    so copying a block never advances the session toward dispatch or evidence.
    """

    briefing = _object(status_payload.get("coding_briefing"))
    manifest = build_work_artifact_copy_manifest(
        briefing,
        prompt_handoff=_object(status_payload.get("prompt_handoff")),
    )
    base = {
        "schema_version": WORK_ARTIFACT_COPY_MANIFEST_SCHEMA_VERSION,
        "session_id": str(status_payload.get("session_id", "")),
        "next_action": "show_status",
        "claim_boundary": str(manifest["claim_boundary"]),
    }
    if artifact_id:
        return {**base, "action": SELECT_ACTION, "artifact": select_work_artifact(manifest, artifact_id)}
    return {
        **base,
        "action": LIST_ACTION,
        # The listing is an index: ids, labels, and availability only. Text
        # comes back one selected block at a time, so a picker cannot spill
        # every artifact into chat at once.
        "artifacts": [_listed(entry) for entry in manifest["artifacts"]],
    }


def _listed(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in entry.items() if key != "text"}


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
