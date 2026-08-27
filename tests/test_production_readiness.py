from __future__ import annotations

import unittest

from production_readiness_test_authentication import ReadinessMatrixTestsMixin as AuthenticationMixin
from production_readiness_test_boundaries import ReadinessMatrixTestsMixin as BoundariesMixin
from production_readiness_test_concurrency import ReadinessMatrixTestsMixin as ConcurrencyMixin
from production_readiness_test_contract import OperationsArtifactContractTestsMixin
from production_readiness_test_matrix import ReadinessMatrixTestsMixin as MatrixMixin
from production_readiness_test_security import ReadinessMatrixTestsMixin as SecurityMixin
from production_readiness_test_structural import ReadinessMatrixTestsMixin as StructuralMixin


class OperationsArtifactContractTests(OperationsArtifactContractTestsMixin, unittest.TestCase):
    pass


class ReadinessMatrixTests(
    MatrixMixin,
    StructuralMixin,
    AuthenticationMixin,
    SecurityMixin,
    ConcurrencyMixin,
    BoundariesMixin,
    unittest.TestCase,
):
    pass
