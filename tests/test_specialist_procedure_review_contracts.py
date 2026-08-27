from __future__ import annotations

import unittest

from _local_package import load_local_package
from specialist_procedure_contract_support import DOMAIN_REVIEW_CONTRACTS, TARGETS

load_local_package()
from omh.skills.catalog import builtin_definitions


class SpecialistProcedureContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.definitions = {definition.name: definition for definition in builtin_definitions()}
        self.finance = self.definitions["finance-analysis"]

    def test_domain_review_contracts_define_outputs_order_and_check_results(self) -> None:
        self.assertEqual(set(DOMAIN_REVIEW_CONTRACTS), TARGETS)
        for name, expected in DOMAIN_REVIEW_CONTRACTS.items():
            definition = self.definitions[name]
            checks = {check.check_id: check for check in definition.procedure_checks}
            with self.subTest(name=name):
                self.assertEqual(definition.expected_outputs, expected["outputs"])
                self.assertEqual(tuple(step.step_id for step in definition.procedure_steps), expected["steps"])
                self.assertEqual(set(checks), set(expected["checks"]))
                for check_id, required_fields in expected["checks"].items():
                    self.assertEqual(set(checks[check_id].required_result_fields), required_fields)


if __name__ == "__main__":
    unittest.main()
