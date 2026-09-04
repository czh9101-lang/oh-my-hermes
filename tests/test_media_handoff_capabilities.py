from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from omh.coding.coding_delegation import build_coding_delegation_payload
from omh.local_store import utc_now
from omh.coding.executor_capability_snapshots import (
    build_executor_capability_snapshot,
    complete_executor_capability_snapshot,
    executor_capability_snapshot_path,
    executor_capability_snapshot_compatibility,
    validate_executor_capability_snapshot,
    write_executor_capability_snapshot,
)
from omh.coding.media_handoff_capabilities import (
    HANDOFF_INPUT_REPRESENTATIONS,
    build_executor_modality_decision,
    demo_media_handoff_decisions,
)


_ROUTE = {"provider": "openai", "wire_model": "gpt-5", "endpoint_mode": "default"}
_PRIMARY_RECOMMENDATION = {
    "owner": "maestro",
    "status": "resolved",
    "selected": {
        "provider": "openai",
        "model_id": "gpt-5",
        "endpoint_mode": "default",
        "model_alias": "display-gpt-5",
    },
    "projection": {"kind": "maestro_ordered_chain", "chain": []},
}


def _snapshot(status: str = "host_observed") -> dict[str, object]:
    return {
        "schema_version": "executor_capability_snapshot/v2",
        "executor": "codex",
        "recorded_at": "2026-09-03T00:00:00Z",
        "capabilities": {
            "input_modality_image": {
                "status": status,
                "scope": _ROUTE,
                "evidence_ref": "operator:provider-docs",
                "observed_at": "2026-09-03T00:00:00Z",
            }
        },
    }


def _primary_handoff_decision(
    *,
    capability: str = "input_modality_image",
    scope: dict[str, str] | None = None,
    input_representation: str = "raw_media:image",
    transformation: dict[str, str] | None = None,
    recommendation: dict[str, object] | None = None,
) -> dict[str, object]:
    # The delegation payload judges freshness against the real clock (it has
    # no `now` seam), so the fixture must be stamped at test time: a fixed
    # date crossed the 24-hour evidence horizon the day after it was written.
    stamp = utc_now()
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        snapshot = build_executor_capability_snapshot(
            executor="codex",
            capabilities={
                capability: {
                    "status": "host_observed",
                    "scope": scope or _ROUTE,
                    "evidence_ref": "operator:primary-handoff-fixture",
                    "observed_at": stamp,
                }
            },
            recorded_at=stamp,
        )
        write_executor_capability_snapshot(
            executor_capability_snapshot_path(directory, "codex"),
            snapshot,
        )
        payload = build_coding_delegation_payload(
            "Implement the image handoff with regression coverage.",
            executor_target="codex",
            input_representation=input_representation,
            transformation=transformation,
            model_recommendation=recommendation or _PRIMARY_RECOMMENDATION,
            capability_snapshot_directory=directory,
        )
    return payload["executor_handoff"]["executor_modality_decision"]  # type: ignore[index]


class MediaHandoffCapabilityContractTests(unittest.TestCase):
    def test_v2_route_scoped_image_observation_is_accepted(self) -> None:
        self.assertEqual(validate_executor_capability_snapshot(_snapshot()), [])

    def test_v1_projects_explicit_unknown_modality_rows(self) -> None:
        legacy = {
            "schema_version": "executor_capability_snapshot/v1",
            "executor": "codex",
            "recorded_at": "2026-09-03T00:00:00Z",
            "capabilities": {"parallel_agents": {"status": "unknown"}},
        }
        self.assertEqual(validate_executor_capability_snapshot(legacy), [])
        self.assertEqual(
            executor_capability_snapshot_compatibility(legacy),
            {"compatible": True, "projected_from": "v1", "modality_rows": "unknown"},
        )
        self.assertEqual(
            complete_executor_capability_snapshot(legacy)["capabilities"]["input_modality_image"],
            {"status": "unknown"},
        )

    def test_decision_is_fail_closed_except_for_fresh_exact_route_evidence(self) -> None:
        supported = build_executor_modality_decision(
            input_representation="raw_media:image", snapshot=_snapshot(), route=_ROUTE, now="2026-09-03T01:00:00Z"
        )
        unknown = build_executor_modality_decision(
            input_representation="raw_media:image", snapshot={"executor": "codex", "capabilities": {}}, route=_ROUTE, now="2026-09-03T01:00:00Z"
        )
        unsupported = build_executor_modality_decision(
            input_representation="raw_media:image", snapshot=_snapshot("unavailable"), route=_ROUTE, now="2026-09-03T01:00:00Z"
        )
        self.assertEqual(supported["verdict"], "dispatch")
        self.assertEqual(unknown["verdict"], "modality_unknown")
        self.assertEqual(unsupported["verdict"], "modality_unsupported")
        self.assertEqual(set(HANDOFF_INPUT_REPRESENTATIONS), {"text_only", "raw_media", "local_file_reference", "extracted_text", "ocr_output", "transcript", "normalized_other"})

    def test_local_file_reference_requires_modality_and_route_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "local_file_reference requires"):
            build_executor_modality_decision(
                input_representation="local_file_reference",
                snapshot=_snapshot(),
                route=_ROUTE,
                now="2026-09-03T01:00:00Z",
            )

        supported = build_executor_modality_decision(
            input_representation="local_file_reference:image",
            snapshot=_snapshot(),
            route=_ROUTE,
            now="2026-09-03T01:00:00Z",
        )
        unknown = build_executor_modality_decision(
            input_representation="local_file_reference:image",
            snapshot={"executor": "codex", "capabilities": {}},
            route=_ROUTE,
            now="2026-09-03T01:00:00Z",
        )

        self.assertEqual(supported["verdict"], "dispatch")
        self.assertEqual(unknown["verdict"], "modality_unknown")
        self.assertEqual(
            supported["required_representations"],
            [
                {
                    "capability": "input_modality_image",
                    "representation": "local_file_reference",
                    "modality": "image",
                    **_ROUTE,
                }
            ],
        )

    def test_primary_handoff_binds_fresh_media_evidence_to_its_selected_route(self) -> None:
        decision = _primary_handoff_decision()

        self.assertEqual(
            decision["route"],
            {"executor": "codex", **_ROUTE},
        )
        self.assertEqual(
            decision["required_representations"],
            [
                {
                    "capability": "input_modality_image",
                    "representation": "raw_media",
                    "modality": "image",
                    **_ROUTE,
                }
            ],
        )
        self.assertEqual(decision["verdict"], "dispatch")

        for mismatch in (
            {**_ROUTE, "provider": "anthropic"},
            {**_ROUTE, "wire_model": "gpt-5-mini"},
        ):
            with self.subTest(scope=mismatch):
                self.assertEqual(_primary_handoff_decision(scope=mismatch)["verdict"], "modality_unknown")

        missing_endpoint = {
            **_PRIMARY_RECOMMENDATION,
            "selected": {key: value for key, value in _PRIMARY_RECOMMENDATION["selected"].items() if key != "endpoint_mode"},
        }
        self.assertEqual(
            _primary_handoff_decision(recommendation=missing_endpoint)["verdict"],
            "modality_unknown",
        )

        transformed = _primary_handoff_decision(
            capability="input_modality_text",
            input_representation="ocr_output",
            transformation={"kind": "ocr", "status": "observed", "evidence_ref": "operator:ocr-fixture"},
        )
        self.assertEqual(transformed["verdict"], "dispatch")
        self.assertEqual(
            transformed["transformation"],
            {"kind": "ocr", "status": "observed", "evidence_ref": "operator:ocr-fixture"},
        )

    def test_transformed_media_requires_observed_transformation_and_demo_is_private(self) -> None:
        unobserved = build_executor_modality_decision(
            input_representation="ocr_output", snapshot=_snapshot(), route=_ROUTE,
            transformation={"kind": "ocr", "status": "prepared_not_observed", "evidence_ref": ""},
        )
        self.assertEqual(unobserved["verdict"], "modality_transformation_unobserved")
        demo = demo_media_handoff_decisions()
        self.assertEqual(demo["schema_version"], "omh_media_handoff_decision_demo/v1")
        self.assertEqual(demo["supported"]["verdict"], "dispatch")
        self.assertNotEqual(demo["unknown"]["verdict"], "dispatch")
        self.assertNotEqual(demo["fallback_rechecked"]["verdict"], "dispatch")
        serialized = json.dumps(demo, sort_keys=True)
        self.assertNotIn("/Users/", serialized)
        self.assertNotIn("bytes", serialized)

    def test_transformation_evidence_is_schema_bound_private_and_kind_matched(self) -> None:
        unsafe_transformations = (
            {
                "kind": "ocr",
                "status": "observed",
                "evidence_ref": "/Users/alice/.ssh/id_rsa",
                "api_key": "sk-live-secret",
            },
            {
                "kind": "transcription",
                "status": "observed",
                "evidence_ref": "operator:wrong-kind",
            },
            {
                "kind": "ocr",
                "status": "observed",
                "evidence_ref": "",
            },
        )

        for transformation in unsafe_transformations:
            with self.subTest(transformation=transformation):
                decision = _primary_handoff_decision(
                    capability="input_modality_text",
                    input_representation="ocr_output",
                    transformation=transformation,
                )

                self.assertEqual(decision["verdict"], "modality_transformation_unobserved")
                self.assertEqual(set(decision["transformation"]), {"kind", "status", "evidence_ref"})
                self.assertNotEqual(decision["transformation"]["status"], "observed")
                self.assertEqual(decision["transformation"]["evidence_ref"], "")
                serialized = json.dumps(decision, sort_keys=True)
                self.assertNotIn("/Users/", serialized)
                self.assertNotIn("sk-live-secret", serialized)
                self.assertNotIn("api_key", serialized)


if __name__ == "__main__":
    unittest.main()
