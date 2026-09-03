from __future__ import annotations

import json
import unittest

from omh.coding.executor_capability_snapshots import (
    complete_executor_capability_snapshot,
    executor_capability_snapshot_compatibility,
    validate_executor_capability_snapshot,
)
from omh.coding.media_handoff_capabilities import (
    HANDOFF_INPUT_REPRESENTATIONS,
    build_executor_modality_decision,
    demo_media_handoff_decisions,
)


_ROUTE = {"provider": "openai", "wire_model": "gpt-5", "endpoint_mode": "default"}


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


if __name__ == "__main__":
    unittest.main()
