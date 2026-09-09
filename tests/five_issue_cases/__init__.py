"""Tracked case producer contract for the parent-owned five-issue QA command.

Each sibling module exports run_case(case_id: str) -> CaseResult. It executes
its deciding observable, then cleans invocation-owned resources in finally.
Return only safe metadata, never raw fixture inputs/output or exception text.
A foundation result must use scope='foundation'; it cannot pass surface QA.
Native-required cases must explicitly report native availability. Fixture
provenance never represents vendor execution. The router checks completeness,
not the truth of the deciding observation; independent parent QA owns that.
"""
from __future__ import annotations

from typing import Literal, Protocol, TypeAlias, TypedDict, runtime_checkable

JsonValue: TypeAlias = (
    str | int | float | bool | None | list['JsonValue'] | dict[str, 'JsonValue']
)


class Provenance(TypedDict):
    kind: Literal['local', 'fixture', 'native']
    scope: Literal['foundation', 'surface']
    native_required: bool
    native_available: bool


class Cleanup(TypedDict):
    owned_resources: list[str]
    terminated_processes: list[int]
    removed_paths: list[str]
    verified_absent: bool
    errors: list[str]


CaseResult = TypedDict('CaseResult', {
    'case': str,
    'commands': list[list[str]],
    'inputs_metadata': dict[str, JsonValue],
    'observations': dict[str, JsonValue],
    'pass': bool,
    'blocked_reason': str | None,
    'provenance': Provenance,
    'cleanup': Cleanup,
})


@runtime_checkable
class CaseProducer(Protocol):
    def run_case(self, case_id: str) -> CaseResult: ...


def unavailable_case(case_id: str, reason: str) -> CaseResult:
    """Return an unexecuted result; callers with resources must replace cleanup."""
    return {
        'case': case_id, 'commands': [], 'inputs_metadata': {},
        'observations': {}, 'pass': False, 'blocked_reason': reason,
        'provenance': {'kind': 'local', 'scope': 'foundation',
                       'native_required': False, 'native_available': False},
        'cleanup': {'owned_resources': [], 'terminated_processes': [],
                    'removed_paths': [], 'verified_absent': True, 'errors': []},
    }
