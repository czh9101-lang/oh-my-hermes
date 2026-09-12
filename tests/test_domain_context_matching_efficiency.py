from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from project_identity_fixture import project_identity
from omh.workflows import domain_intelligence_profile_resolution as resolution


class DomainContextMatchingEfficiencyTests(unittest.TestCase):
    def test_resolution_normalizes_bounded_message_once_across_maximum_mappings(
        self,
    ) -> None:
        message = "x" * 4096
        scope = {
            "kind": "project",
            "ref": project_identity(Path.cwd()),
            "ref_authority": "operator_or_wrapper_supplied",
            "identity_claim": "not_authenticated_identity_evidence",
        }
        profiles = tuple(
            {
                "scope": scope,
                "vocabulary_mappings": [
                    {
                        "phrase": f"needle {profile_index} {mapping_index}",
                        "canonical": "absent",
                    }
                    for mapping_index in range(40)
                ],
            }
            for profile_index in range(1024)
        )
        real_normalize = resolution._normalize_match_text
        message_normalizations = 0
        phrase_normalizations = 0

        def record_normalization(value: object) -> str:
            nonlocal message_normalizations, phrase_normalizations
            if value is message:
                message_normalizations += 1
            else:
                phrase_normalizations += 1
            return real_normalize(value)

        with patch.object(
            resolution,
            "_normalize_match_text",
            side_effect=record_normalization,
        ):
            target = resolution.resolve_domain_clarification_target(
                profiles,
                message,
                project_root=Path.cwd(),
                locale="en",
            )

        self.assertIsNone(target)
        self.assertEqual(message_normalizations, 1)
        self.assertEqual(phrase_normalizations, 1024 * 40)


if __name__ == "__main__":
    unittest.main()
