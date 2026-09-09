"""Process-attributed native session references, never continuation authority.

The shared bounded binary intake calls observe() with individual decoded frames.
The dispatcher supplies capability probes bound to the resolved binary, immutable
attempt lineage, and freshly observed workspace/recovery state. This module does
no I/O, capability probing, stream parsing, session-store lookup or execution.
An observed ID proves neither successful work nor durable native session storage.
"""
from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
import os
from pathlib import PurePosixPath, PureWindowsPath
import re
import shlex
from typing import Literal, TypeGuard, TypedDict

SCHEMA_VERSION = 'fanout_executor_session/v1'
MAX_EVENTS = 65536
_PROTOCOLS = {'codex': 'codex_exec_json', 'claude-code': 'claude_stream_json'}
_UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')
_SHA = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})')
_DIGEST = re.compile(r'[0-9a-f]{64}')
_TOKEN = re.compile(r'[A-Za-z0-9_.:/-]{1,256}')
State = Literal['observed', 'not_observed', 'not_available']


@dataclass(frozen=True, slots=True)
class BinaryIdentity:
    resolved_path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class SessionCapability:
    """Supplied supported-help/version observation, not a version-floor guess."""
    executor: str
    protocol: str | None
    binary_identity: BinaryIdentity
    version: str | None


@dataclass(frozen=True, slots=True)
class WorktreeIncarnation:
    incarnation_id: str
    common_dir: str
    branch: str
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class SessionBinding:
    fanout_id: str
    unit_id: str
    run_ref: str
    attempt_id: str
    contract_digest: str
    worktree_path: str
    worktree_incarnation: WorktreeIncarnation
    base_sha: str
    launch_head: str
    predecessor_attempt_id: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    """Fresh dispatcher observation; recovery digest includes dirty/untracked state."""
    path: str
    incarnation: WorktreeIncarnation
    head: str
    dirty: bool
    recovery_snapshot: str | None


@dataclass(frozen=True, slots=True)
class SessionReceipt:
    state: State
    reason: str
    capability: SessionCapability
    binding: SessionBinding
    reference: str | None
    event_ref: str | None
    end_head: str | None

    def to_dict(self) -> dict[str, object]:
        binding, capability = self.binding, self.capability
        incarnation = binding.worktree_incarnation
        return {
            'schema_version': SCHEMA_VERSION, 'state': self.state, 'reason': self.reason,
            'executor': capability.executor, 'protocol': capability.protocol,
            'reference_kind': _reference_kind(capability) if self.state == 'observed' else None,
            'reference': self.reference, 'event_ref': self.event_ref,
            'fanout_id': binding.fanout_id, 'unit_id': binding.unit_id, 'run_ref': binding.run_ref,
            'attempt_id': binding.attempt_id, 'contract_digest': binding.contract_digest,
            'worktree_path': binding.worktree_path,
            'worktree_incarnation': {
                'incarnation_id': incarnation.incarnation_id, 'common_dir': incarnation.common_dir,
                'branch': incarnation.branch, 'device': incarnation.device, 'inode': incarnation.inode},
            'base_sha': binding.base_sha, 'launch_head': binding.launch_head, 'end_head': self.end_head,
            'binary_identity': {'resolved_path': capability.binary_identity.resolved_path,
                                'sha256': capability.binary_identity.sha256},
            'version': capability.version, 'predecessor_attempt_id': binding.predecessor_attempt_id,
        }


@dataclass(frozen=True, slots=True)
class SessionRead:
    receipt: SessionReceipt | None
    reason: str


class ResumeProjection(TypedDict):
    available: bool
    reason: str
    argv: list[str]
    cwd: str | None
    shell_command: str | None
    execution_policy: Literal['copy_only']
    required_input: Literal['follow_up_prompt_on_stdin'] | None
    native_resume_state: Literal['not_tested']


def _supported(capability: SessionCapability) -> bool:
    return (capability.executor in _PROTOCOLS and
            capability.protocol == _PROTOCOLS[capability.executor] and capability.version is not None)


def _reference_kind(capability: SessionCapability) -> str:
    return 'thread_id' if capability.executor == 'codex' else 'session_id'


class SessionDecoder:
    """One decoder per fresh attempt; retains only a UUID and first event reference.

    Malformed candidate IDs, conflicts or excess events poison the whole attempt.
    Unrelated/nested/subagent frames never contribute identity. Wire/frame/depth
    budgets belong to the shared ingestion pipeline, not another parser here.
    """
    __slots__: tuple[str, ...] = ('capability', 'event_count', '_reference', '_event_ref', '_reason')

    def __init__(self, capability: SessionCapability) -> None:
        self.capability: SessionCapability = capability
        self.event_count: int = 0
        self._reference: str | None = None
        self._event_ref: str | None = None
        self._reason: str = 'event_missing' if _supported(capability) else 'unsupported_protocol'

    def observe(self, event: Mapping[str, object], *, event_ref: str) -> None:
        if self.event_count == MAX_EVENTS:
            if _supported(self.capability):
                self._invalidate('event_limit')
            return
        self.event_count += 1
        if self._reason not in ('event_missing', 'native_event_observed'):
            return
        if event.get('parent_tool_use_id') is not None:
            return
        if self.capability.executor == 'codex':
            if event.get('type') != 'thread.started':
                return
            reference = event.get('thread_id')
        else:
            if not (event.get('type') == 'result' or
                    (event.get('type') == 'system' and event.get('subtype') == 'init')):
                return
            reference = event.get('session_id')
        if not isinstance(reference, str) or not _UUID.fullmatch(reference):
            self._invalidate('invalid_reference')
        elif not _TOKEN.fullmatch(event_ref):
            self._invalidate('invalid_event_ref')
        elif self._reference is not None and reference != self._reference:
            self._invalidate('conflicting_reference')
        elif self._reference is None:
            self._reference, self._event_ref = reference, event_ref
            self._reason = 'native_event_observed'

    def _invalidate(self, reason: str) -> None:
        self._reference, self._event_ref, self._reason = None, None, reason

    def receipt(self, binding: SessionBinding, *, end_head: str | None = None) -> SessionReceipt:
        state: State = ('not_available' if not _supported(self.capability) else
                        'observed' if self._reference is not None else 'not_observed')
        receipt = SessionReceipt(state, self._reason, self.capability, binding,
                                 self._reference, self._event_ref, end_head)
        if read_session_receipt(receipt.to_dict()).receipt is None:
            raise ValueError('invalid_dispatcher_session_binding')
        return receipt


def _text(value: object, *, limit: int = 256) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= limit or not value.isprintable():
        raise ValueError('invalid_text')
    return value


def _match(value: object, pattern: re.Pattern[str]) -> str:
    text = _text(value)
    if not pattern.fullmatch(text):
        raise ValueError('invalid_shape')
    return text


def _optional_match(value: object, pattern: re.Pattern[str]) -> str | None:
    return None if value is None else _match(value, pattern)


def _path(value: object) -> str:
    text = _text(value, limit=4096)
    if not (PurePosixPath(text).is_absolute() or PureWindowsPath(text).is_absolute()):
        raise ValueError('path_not_absolute')
    return text


def _integer(value: object) -> int:
    if type(value) is not int or value < 0 or value > 2**64 - 1:
        raise ValueError('invalid_filesystem_identity')
    return value


def _is_mapping(value: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(value, Mapping)


def _object(value: object, keys: Collection[str]) -> Mapping[str, object]:
    if not _is_mapping(value) or len(value) != len(keys) or not all(key in value for key in keys):
        raise ValueError('invalid_fields')
    return {key: value[key] for key in keys}


_FIELDS = (
    'schema_version', 'state', 'reason', 'executor', 'protocol', 'reference_kind', 'reference',
    'event_ref', 'fanout_id', 'unit_id', 'run_ref', 'attempt_id', 'contract_digest',
    'worktree_path', 'worktree_incarnation', 'base_sha', 'launch_head', 'end_head',
    'binary_identity', 'version', 'predecessor_attempt_id',
)


def read_session_receipt(value: object) -> SessionRead:
    """Validate one optional decoded record without altering old result evidence.

    Closed fixed-depth fields bound retained data; the caller bounds JSON reads.
    Missing legacy or unknown-version records never synthesize an observation.
    """
    if value is None:
        return SessionRead(None, 'legacy_missing')
    if not _is_mapping(value):
        return SessionRead(None, 'invalid_receipt')
    if value.get('schema_version') != SCHEMA_VERSION:
        return SessionRead(None, 'unknown_schema')
    try:
        record = _object(value, _FIELDS)
        binary = _object(record['binary_identity'], ('resolved_path', 'sha256'))
        capability = SessionCapability(
            _match(record['executor'], _TOKEN),
            None if record['protocol'] is None else _match(record['protocol'], _TOKEN),
            BinaryIdentity(_path(binary['resolved_path']), _match(binary['sha256'], _DIGEST)),
            None if record['version'] is None else _text(record['version']))
        incarnation = _object(record['worktree_incarnation'],
                              ('incarnation_id', 'common_dir', 'branch', 'device', 'inode'))
        binding = SessionBinding(
            _match(record['fanout_id'], _TOKEN), _match(record['unit_id'], _TOKEN),
            _match(record['run_ref'], _TOKEN), _match(record['attempt_id'], _UUID),
            _match(record['contract_digest'], _DIGEST), _path(record['worktree_path']),
            WorktreeIncarnation(_match(incarnation['incarnation_id'], _UUID),
                                _path(incarnation['common_dir']), _text(incarnation['branch']),
                                _integer(incarnation['device']), _integer(incarnation['inode'])),
            _match(record['base_sha'], _SHA), _match(record['launch_head'], _SHA),
            _optional_match(record['predecessor_attempt_id'], _UUID))
        if binding.predecessor_attempt_id == binding.attempt_id:
            raise ValueError('self_predecessor')
        end_head = _optional_match(record['end_head'], _SHA)
        reference = _optional_match(record['reference'], _UUID)
        event_ref = _optional_match(record['event_ref'], _TOKEN)
        reason = _text(record['reason'])
        state: State
        if record['state'] == 'observed':
            state = 'observed'
            if (not _supported(capability) or reference is None or event_ref is None or
                    capability.version is None or reason != 'native_event_observed' or
                    record['reference_kind'] != _reference_kind(capability)):
                raise ValueError('invalid_observation')
        else:
            if reference is not None or event_ref is not None or record['reference_kind'] is not None:
                raise ValueError('unobserved_reference')
            if record['state'] == 'not_available':
                state = 'not_available'
                if _supported(capability) or reason != 'unsupported_protocol':
                    raise ValueError('invalid_unavailable')
            elif record['state'] == 'not_observed':
                state = 'not_observed'
                if not _supported(capability) or reason not in (
                    'event_missing', 'invalid_reference', 'conflicting_reference',
                    'event_limit', 'invalid_event_ref',
                ):
                    raise ValueError('invalid_not_observed')
            else:
                raise ValueError('invalid_state')
        return SessionRead(SessionReceipt(state, reason, capability, binding,
                                          reference, event_ref, end_head), reason)
    except ValueError:
        # Malformed optional metadata disables this capability, not the unit result.
        return SessionRead(None, 'invalid_receipt')


def duplicate_session_references(values: Iterable[object]) -> frozenset[str]:
    """Find IDs shared by distinct attempt bindings; exact receipt replay is safe.

    The dispatcher/status reader must supply all fresh and held attempt receipts,
    then pass this set to each projection so BOTH ambiguous actions are disabled.
    """
    owners: dict[str, SessionBinding] = {}
    duplicates: set[str] = set()
    for value in values:
        receipt = read_session_receipt(value).receipt
        if receipt is None or receipt.reference is None:
            continue
        previous = owners.setdefault(receipt.reference, receipt.binding)
        if previous != receipt.binding:
            duplicates.add(receipt.reference)
    return frozenset(duplicates)


def project_session_resume(
    value: object, *, binding: SessionBinding, workspace: WorkspaceSnapshot | None,
    recovery_snapshot: str | None = None, duplicate_references: Collection[str] = (),
    platform: str = os.name,
) -> ResumeProjection:
    """Copy-only exact-ID action after selected lineage and fresh workspace checks.

    recovery_snapshot is the dispatcher's recorded recovery-state digest, NOT a
    value learned from the executor or copied from the current snapshot. Dirty
    failed work is actionable only when those independently observed digests match.
    No native-store existence or future resume success is implied by availability.
    """
    read = read_session_receipt(value)
    result: ResumeProjection = {
        'available': False, 'reason': read.reason, 'argv': [], 'cwd': None,
        'shell_command': None, 'execution_policy': 'copy_only', 'required_input': None,
        'native_resume_state': 'not_tested',
    }
    receipt = read.receipt
    if receipt is None or receipt.state != 'observed':
        return result
    if receipt.binding != binding:
        result['reason'] = 'binding_mismatch'
    elif receipt.reference in duplicate_references:
        result['reason'] = 'duplicate_reference'
    elif receipt.end_head is None:
        result['reason'] = 'end_revision_not_observed'
    elif (workspace is None or workspace.path != binding.worktree_path or
          workspace.incarnation != binding.worktree_incarnation or workspace.head != receipt.end_head):
        result['reason'] = 'stale_workspace'
    elif (workspace.dirty or recovery_snapshot is not None) and (
        recovery_snapshot is None or not _DIGEST.fullmatch(recovery_snapshot) or
        workspace.recovery_snapshot != recovery_snapshot
    ):
        result['reason'] = 'recovery_snapshot_mismatch'
    else:
        # read_session_receipt established the canonical UUID and supported pair.
        reference = _match(receipt.reference, _UUID)
        binary = receipt.capability.binary_identity.resolved_path
        argv = ([binary, 'exec', 'resume', reference, '-'] if receipt.capability.executor == 'codex'
                else [binary, '--resume=' + reference])
        result.update(available=True, reason='copy_only', argv=argv, cwd=binding.worktree_path)
        if receipt.capability.executor == 'codex':
            result['required_input'] = 'follow_up_prompt_on_stdin'
        if platform == 'posix':
            result['shell_command'] = 'cd -- ' + shlex.quote(binding.worktree_path) + ' && ' + shlex.join(argv)
    return result
