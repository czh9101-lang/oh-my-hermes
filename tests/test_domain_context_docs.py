from __future__ import annotations

import unittest
from pathlib import Path

from omh.skills.render import workflow_reference_markdown, workflow_reference_payload


ROOT = Path(__file__).resolve().parents[1]
DOMAIN_WORKFLOW_INPUTS = {
    "curriculum-design": "learners",
    "finance-analysis": "period",
    "legal-compliance-review": "jurisdiction",
    "localization-review": "locale",
    "people-ops": "role or people-process outcome",
    "product-brief": "product evidence",
    "sales-development": "account or segment",
    "support-operations": "support case",
}


class DomainContextDocumentationTests(unittest.TestCase):
    def test_workflow_reference_is_generated_from_catalog_contracts(self) -> None:
        workflows = (ROOT / "docs" / "WORKFLOWS.md").read_text(encoding="utf-8")
        self.assertEqual(workflows, workflow_reference_markdown())

        payload = workflow_reference_payload()
        self.assertEqual(payload["schema_version"], "workflow_catalog/v1")
        skills = {skill["name"]: skill for skill in payload["skills"]}
        for name, required_input in DOMAIN_WORKFLOW_INPUTS.items():
            with self.subTest(workflow=name):
                skill = skills[name]
                self.assertIn(required_input, skill["required_inputs"])
                questions = skill["expert_questions"]
                if skill.get("procedure_steps"):
                    self.assertEqual(
                        {question["required_input"] for question in questions},
                        set(skill["required_inputs"]),
                    )
                else:
                    self.assertEqual(len(questions), 1)
                self.assertEqual(questions[0]["required_input"], required_input)
                for question in questions:
                    self.assertEqual(set(question["questions"]), {"en", "ko"})
                    self.assertTrue(all(question["questions"].values()))


if __name__ == "__main__":
    unittest.main()
