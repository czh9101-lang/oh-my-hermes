"""Isolated native Hermes QA, never a replacement executor or a fixture grant.

The normal host hook timeout configuration is deliberately preserved. A broken
prepare/observation bridge blocks the scenario; calling callbacks directly or
turning off host guards to obtain a receipt would invalidate this evidence.

The host is discovered, never hardcoded: `HERMES_HOME` (default `~/.hermes`)
names the Hermes home whose `hermes-agent` checkout is the host, `hermes` is
resolved on `PATH`, and `OMH_QA_HERMES_AGENT` / `OMH_QA_HERMES_PYTHON` /
`OMH_QA_HERMES_CLI` override each part for a non-default layout. Discovery is
presence-only. When any part is missing the cases stay blocked with
`native_normal_loop_and_review_dispatch_not_observed`; an unavailable host is
never a pass.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from importlib import import_module
from importlib.machinery import ModuleSpec
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from tempfile import mkdtemp
from types import ModuleType
from typing import Protocol, runtime_checkable

from . import CaseResult, JsonValue, unavailable_case

_ROOT = Path(__file__).resolve().parents[2]
def _host_root() -> Path:
    """The Hermes Agent checkout under the resolved Hermes home."""
    override = os.environ.get('OMH_QA_HERMES_AGENT')
    home = os.environ.get('HERMES_HOME')
    return Path(override) if override else Path(home or Path.home() / '.hermes') / 'hermes-agent'


def _host_python(host: Path) -> Path:
    """The host checkout's own interpreter, not this suite's."""
    override = os.environ.get('OMH_QA_HERMES_PYTHON')
    if override:
        return Path(override)
    return host / 'venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


# A path that resolves to nothing keeps the blocked branch below: an
# undiscoverable host is reported as unavailable, never as a pass.
_HOST = _host_root()
_PYTHON = _host_python(_HOST)
_CLI = Path(os.environ.get('OMH_QA_HERMES_CLI') or shutil.which('hermes')
            or _HOST.parent / 'bin/hermes')
_PROOF = _ROOT / '.omc/artifacts/five-issues/phases/C/parent-kanban-native'
_CASES = frozenset({'K1', 'K4', 'K6', 'K8'})
_CONFIG = '''plugins:
  enabled: [omh]
toolsets: [kanban, omh]
kanban:
  dispatch_in_gateway: false
  auto_subscribe_on_create: false
  review_dispatch: false
'''


class _Decoder(Protocol):
    def loads(self, s: str) -> JsonValue: ...


_decoder: _Decoder = json


def _object(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ValueError('expected_json_object')
    return value


def _text(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise ValueError('expected_string')
    return value


def native_host_available() -> bool:
    """Presence only: discovery must not read a profile or initialize a DB."""
    try:
        return (all(path.is_file() for path in (
            _PYTHON, _CLI, _HOST / 'hermes', _HOST / 'model_tools.py',
            _HOST / 'hermes_cli/plugins.py', _HOST / 'tools/kanban_tools.py',
            _HOST / 'tools/kanban_tools_schemas.py',
        )) and os.access(_PYTHON, os.X_OK) and os.access(_CLI, os.X_OK))
    except OSError:
        return False


@runtime_checkable
class _ModelTools(Protocol):
    def get_tool_definitions(self, *, enabled_toolsets: list[str], quiet_mode: bool,
                             skip_tool_search_assembly: bool) -> list[dict[str, JsonValue]]: ...
    def handle_function_call(self, function_name: str, function_args: dict[str, JsonValue], *,
                             task_id: str, session_id: str, tool_call_id: str,
                             enabled_toolsets: list[str]) -> str: ...


@runtime_checkable
class _Plugins(Protocol):
    def discover_plugins(self) -> None: ...


class _Blocked(Exception):
    """Closed QA reason, not a raw host error or assertion body."""


class _NativeLoop:
    def __init__(self, observations: dict[str, JsonValue]) -> None:
        model = import_module('model_tools')
        plugins = import_module('hermes_cli.plugins')
        if not isinstance(model, _ModelTools) or not isinstance(plugins, _Plugins):
            raise _Blocked('native_host_api_unavailable')
        plugins.discover_plugins()
        self.model: _ModelTools = model
        self.observations: dict[str, JsonValue] = observations
        self.sequence: int = 0
        self.transcript: list[JsonValue] = []
        self.receipts: list[JsonValue] = []
        self.run_ids: dict[str, int] = {}
        observations.update(tool_calls=self.transcript, receipts=self.receipts,
                            native_calls=0, decisive_assertions=0)

    def require(self, condition: bool, reason: str) -> None:
        count = self.observations['decisive_assertions']
        assert isinstance(count, int)
        self.observations['decisive_assertions'] = count + 1
        if not condition:
            raise _Blocked(reason)

    def names(self, toolsets: list[str]) -> set[str]:
        definitions = self.model.get_tool_definitions(
            enabled_toolsets=toolsets, quiet_mode=True, skip_tool_search_assembly=True)
        return {_text(_object(row['function'])['name']) for row in definitions}

    def call(self, name: str, args: dict[str, JsonValue]) -> dict[str, JsonValue]:
        # handle_function_call alone does not enforce enabled_tools membership.
        # Resolve the real session catalog before every invocation, including list
        # (which recomputes readiness and therefore needs mutation authority).
        self.require(name in self.names(['kanban', 'omh']), 'native_tool_not_exposed:' + name)
        self.sequence += 1
        raw = self.model.handle_function_call(
            name, args, task_id='qa-host-task', session_id='qa-host-session',
            tool_call_id=f'qa-call-{self.sequence}', enabled_toolsets=['kanban', 'omh'])
        response = _object(_decoder.loads(raw))
        self.observations['host_executed'] = True
        if name.startswith('kanban_'):
            count = self.observations['native_calls']
            assert isinstance(count, int)
            self.observations['native_calls'] = count + 1
        self.transcript.append({'tool': name, 'call_id': f'qa-call-{self.sequence}',
                                'error': 'error' in response, 'state': response.get('state'),
                                'missing_capabilities': response.get('missing_capabilities', [])})
        return response

    def prepare(self, operation: str, reference: str, args: dict[str, JsonValue],
                task_id: str | None = None, **extra: JsonValue) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {
            'action': 'prepare', 'request_id': reference, 'coordination': 'durable',
            'operation': operation, 'board': 'qa-board', 'profile': 'qa-profile',
            'arguments': args, **extra,
        }
        if task_id is not None:
            payload['task_id'] = task_id
        return self.call('omh_agent_board', payload)

    def status(self, reference: str) -> dict[str, JsonValue]:
        return self.call('omh_agent_board', {'action': 'status', 'request_id': reference})

    def execute(self, prepared: dict[str, JsonValue], *, failed: bool = False) -> dict[str, JsonValue]:
        action = prepared.get('native_action')
        self.require(isinstance(action, dict), 'typed_native_action_unavailable')
        action = _object(action)
        reply = self.call(_text(action['tool_name']), _object(action['arguments']))
        self.require(('error' in reply) is failed, 'native_result_outcome_mismatch')
        status = self.status(_text(prepared['request_id']))
        receipts = status.get('observed_receipts')
        self.require(isinstance(receipts, list) and len(receipts) == 1, 'correlated_receipt_missing')
        assert isinstance(receipts, list)
        receipt = _object(receipts[0])
        self.require(receipt.get('state') == ('failed' if failed else 'observed'), 'receipt_state_mismatch')
        self.require(receipt.get('operation') == prepared['operation'], 'receipt_operation_mismatch')
        if not failed:
            for key in ('task_id', 'parent_id', 'child_id', 'comment_id', 'run_id'):
                if key in reply:
                    self.require(receipt.get(key) == reply[key], 'receipt_identity_mismatch:' + key)
            self.require(not {'review_approved', 'ci', 'merge', 'dispatch'} & receipt.keys(),
                         'unobserved_evidence_inferred')
        # Persist the bridge's bounded identity facts, not its prose or native bodies.
        self.receipts.append({key: value for key, value in receipt.items() if key in {
            'operation', 'state', 'reason', 'fact', 'task_id', 'parent_id', 'child_id',
            'comment_id', 'run_id', 'landed_status', 'observation_ref', 'attachment_refs',
            'requires_reconciliation', 'count', 'task_ids', 'block_kind',
        }})
        return reply

    def action(self, operation: str, reference: str, args: dict[str, JsonValue],
               task_id: str | None = None, *, failed: bool = False, worker: bool = False) -> dict[str, JsonValue]:
        prepared = self.prepare(operation, reference, args, task_id)
        if not worker:
            return self.execute(prepared, failed=failed)
        with self.worker_scope(_text(task_id)):
            return self.execute(prepared, failed=failed)

    @contextmanager
    def worker_scope(self, task_id: str) -> Iterator[None]:
        # A dispatcher-spawned worker owns its task through HERMES_KANBAN_TASK and
        # HERMES_KANBAN_RUN_ID; the host authorizes claim-clearing mutations by that
        # run ownership and refuses orchestrator-only tools inside it. This mirrors
        # the native worker context exactly; it grants nothing to model JSON.
        run_id = self.run_ids.get(task_id)
        self.require(isinstance(run_id, int), 'worker_scope_without_observed_claim')
        previous = {key: os.environ.get(key) for key in ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_RUN_ID')}
        os.environ['HERMES_KANBAN_TASK'] = task_id
        os.environ['HERMES_KANBAN_RUN_ID'] = str(run_id)
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    _ = os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def show(self, task_id: str, reference: str) -> dict[str, JsonValue]:
        result = self.action('show', reference, {}, task_id)
        task = _object(result['task'])
        self.require(task.get('id') == task_id, 'native_readback_identity_mismatch')
        return task

    def claim(self, task_id: str) -> None:
        command = [str(_CLI), 'kanban', '--board', 'qa-board', 'claim', task_id]
        child = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.transcript.append({'operator': 'claim', 'task_id': task_id, 'exit': child.returncode})
        self.require(child.returncode == 0, 'native_operator_claim_failed')
        task = self.show(task_id, 'claim-readback-' + str(self.sequence))
        run_id = task.get('current_run_id')
        self.require(task.get('status') == 'running' and isinstance(run_id, int),
                     'native_claim_not_observed')
        assert isinstance(run_id, int)
        self.run_ids[task_id] = run_id
        self.observations['claim_observed'] = True
        self.observations['dispatch_observed'] = False


def _scenario(case_id: str, observations: dict[str, JsonValue]) -> None:
    loop = _NativeLoop(observations)
    arguments: dict[str, JsonValue] = {'title': 'qa-task', 'assignee': 'qa-profile',
                                     'completion_contract': 'local-only'}
    prepared = loop.prepare('create', 'qa-create-1', arguments)
    observations['prepare_state'] = prepared.get('state')
    observations['missing_capabilities'] = prepared.get('missing_capabilities', [])
    prepared_receipts = prepared.get('observed_receipts')
    loop.require(isinstance(prepared_receipts, list), 'prepare_receipts_missing')
    assert isinstance(prepared_receipts, list)
    observations['prepare_receipt_count'] = len(prepared_receipts)
    if prepared.get('native_action') is None:
        status = loop.status('qa-create-1')
        observations['status_state'] = status.get('state')
        # Explicitly authorized empty-board readback is a real native tool call,
        # not a fabricated successful create or a fixture-emitted result.
        listing = loop.call('kanban_list', {'board': 'qa-board', 'limit': 10})
        observations['native_task_count'] = listing.get('count')
        loop.require(listing.get('count') == 0 and listing.get('truncated') is False,
                     'unavailable_prepare_mutated_native_board')
        observations['native_boundary'] = [{
            'operation': 'create', 'reason': 'native_prepare_missing_host_identity'
            if prepared.get('missing_capabilities') == ['host_identity'] else 'native_prepare_unavailable',
        }]
        raise _Blocked('native_prepare_missing_host_identity' if prepared.get('missing_capabilities') == ['host_identity']
                       else 'native_prepare_unavailable')
    created = loop.execute(prepared)
    task_id = _text(created['task_id'])
    observations['task_ids'] = [task_id]
    _ = loop.show(task_id, 'created-show')
    if case_id == 'K4':
        repeated = loop.prepare('create', 'qa-create-1', arguments)
        loop.require(repeated.get('native_action') is None, 'duplicate_create_action')
        receipts = repeated.get('observed_receipts')
        loop.require(isinstance(receipts, list) and len(receipts) == 1, 'repeat_receipt_missing')
        assert isinstance(receipts, list)
        loop.require(_object(receipts[0]).get('task_id') == task_id, 'repeat_task_identity_changed')
        listing = loop.action('list', 'dedup-list', {'limit': 10})
        tasks = listing.get('tasks')
        loop.require(isinstance(tasks, list) and len(tasks) == 1, 'native_duplicate_task')
        assert isinstance(tasks, list)
        loop.require(_object(tasks[0]).get('id') == task_id and listing.get('count') == 1
                     and listing.get('truncated') is False, 'native_dedup_readback_mismatch')
        observations['native_task_count'] = 1
        observations['same_task_on_repeat'] = True
        return
    if case_id == 'K8':
        second_id = _text(loop.action('create', 'qa-create-2', arguments)['task_id'])
        observations['task_ids'] = [task_id, second_id]
        second_before = loop.show(second_id, 'unrelated-before')
        changed = loop.prepare('create', 'qa-create-1', {**arguments, 'title': 'changed'})
        loop.require(changed.get('state') == 'denied' and changed.get('native_action') is None,
                     'changed_create_digest_admitted')
        foreign = loop.prepare('heartbeat', 'foreign-input', {'task_id': second_id}, task_id)
        loop.require(foreign.get('state') == 'unavailable' and foreign.get('native_action') is None,
                     'foreign_task_input_admitted')
        loop.require('kanban_create' not in loop.names(['omh']), 'capability_denial_not_enforced')
        observations['restricted_catalog_denied'] = True
        stale = loop.prepare('heartbeat', 'stale', {}, task_id, expected_observation_ref='observation:0')
        loop.require(stale.get('state') == 'denied' and stale.get('native_action') is None, 'stale_call_admitted')
        # A real ready task cannot heartbeat. Do not inject an error result.
        _ = loop.action('heartbeat', 'failed-heartbeat', {}, task_id, failed=True)
        retry = loop.prepare('complete', 'unsafe-retry', {'summary': 'qa'}, task_id)
        loop.require(retry.get('state') == 'denied' and retry.get('native_action') is None,
                     'unreconciled_retry_admitted')
        _ = loop.show(task_id, 'reconcile')
        loop.claim(task_id)
        _ = loop.action('heartbeat', 'retry-heartbeat', {}, task_id, worker=True)
        # A real inherited child-process marker exercises the native mutation
        # guard, not a patched handler or an invented dispatcher-owned run.
        denied = loop.prepare('block', 'child-denied', {'reason': 'qa', 'kind': 'needs_input'}, task_id)
        os.environ['HERMES_DELEGATED_CHILD_CONTEXT'] = '1'
        try:
            _ = loop.execute(denied, failed=True)
        finally:
            del os.environ['HERMES_DELEGATED_CHILD_CONTEXT']
        unchanged = loop.show(task_id, 'denied-readback')
        loop.require(unchanged.get('status') == 'running', 'denied_mutation_changed_task')
        bound = loop.prepare('block', 'binding-revoked', {'reason': 'qa', 'kind': 'needs_input'}, task_id)
        action = _object(bound['native_action'])
        # The host DB override makes the resolved binding unattributable. OMH
        # must veto before native execution; do not create or open this DB.
        foreign_db = Path(os.environ['HOME']) / 'foreign.db'
        os.environ['HERMES_KANBAN_DB'] = str(foreign_db)
        try:
            refused = loop.call(_text(action['tool_name']), _object(action['arguments']))
            loop.require('error' in refused and not foreign_db.exists(), 'revoked_binding_mutated')
        finally:
            del os.environ['HERMES_KANBAN_DB']
        pending = loop.status('binding-revoked')
        loop.require(pending.get('state') == 'prepared' and pending.get('observed_receipts') == [],
                     'blocked_pre_became_observed')
        with loop.worker_scope(task_id):
            _ = loop.execute(bound)
        _ = loop.action('unblock', 'retry-unblock', {}, task_id)
        loop.claim(task_id)
        _ = loop.action('request_review', 'review', {'summary': 'qa'}, task_id, worker=True)
        _ = loop.action('request_changes', 'unclaimed-changes', {'reason': 'qa'}, task_id, failed=True)
        loop.require(loop.show(task_id, 'review-reconcile').get('status') == 'review',
                     'unclaimed_review_failure_changed_state')
        # review -> done is the host's human-approval path: operator scope, not
        # the ended worker run.
        _ = loop.action('complete', 'complete', {'summary': 'qa'}, task_id)
        completed = loop.status('complete')
        _ = loop.action('complete', 'terminal-repeat', {'summary': 'qa'}, task_id, failed=True)
        loop.require(loop.status('complete').get('observed_receipts') == completed.get('observed_receipts'),
                     'terminal_failure_changed_completion_receipt')
        loop.require(loop.show(task_id, 'terminal-readback').get('status') == 'done',
                     'terminal_failure_changed_native_state')
        create_action = _object(prepared['native_action'])
        replay = loop.call(_text(create_action['tool_name']), _object(create_action['arguments']))
        loop.require('error' in replay, 'observed_create_replay_admitted')
        listing = loop.action('list', 'replay-list', {'limit': 10})
        loop.require(listing.get('count') == 2 and listing.get('truncated') is False,
                     'replay_created_duplicate')
        second_after = loop.show(second_id, 'unrelated-after')
        loop.require(second_after == second_before, 'adversarial_path_changed_unrelated_task')
        observations['native_task_count'] = 2
        observations['failed_paths_preserved_separation'] = True
        observations['native_boundary'] = [{'operation': 'request_changes',
            'reason': 'positive review verdict not claimed; only unclaimed-review denial was exercised'}]
        return
    second = loop.action('create', 'qa-create-2', arguments)
    second_id = _text(second['task_id'])
    observations['task_ids'] = [task_id, second_id]
    _ = loop.action('link', 'link', {'parent_id': task_id, 'child_id': second_id})
    _ = loop.action('comment', 'comment', {'body': 'QA-PRIVATE-BODY-SENTINEL'}, task_id)
    attachment = loop.call('kanban_attach', {'board': 'qa-board', 'task_id': task_id,
        'filename': 'qa.txt', 'content_base64': 'cWE=', 'content_type': 'text/plain'})
    loop.require(attachment.get('ok') is True and attachment.get('size') == 2, 'native_attachment_failed')
    references = loop.action('attachments', 'attachment-refs', {}, task_id)
    rows = references.get('attachments')
    loop.require(isinstance(rows, list) and len(rows) == 1, 'native_attachment_reference_missing')
    assert isinstance(rows, list)
    loop.require(_object(rows[0]).get('id') == attachment.get('attachment_id'), 'attachment_identity_mismatch')
    loop.claim(task_id)
    _ = loop.action('heartbeat', 'heartbeat', {}, task_id, worker=True)
    _ = loop.action('block', 'block', {'reason': 'qa', 'kind': 'needs_input'}, task_id, worker=True)
    loop.require(loop.show(task_id, 'blocked-show').get('status') == 'blocked', 'native_block_not_landed')
    _ = loop.action('unblock', 'unblock', {}, task_id)
    loop.claim(task_id)
    _ = loop.action('request_review', 'review', {'summary': 'qa'}, task_id, worker=True)
    loop.require(loop.show(task_id, 'review-show').get('status') == 'review', 'native_review_not_landed')
    _ = loop.action('request_changes', 'unclaimed-changes', {'reason': 'qa'}, task_id, failed=True)
    _ = loop.show(task_id, 'changes-reconcile')
    # review -> done is the host's human-approval path (operator scope).
    _ = loop.action('complete', 'complete', {'summary': 'qa'}, task_id)
    loop.require(loop.show(task_id, 'done-show').get('status') == 'done', 'native_completion_not_landed')
    _ = loop.action('list', 'final-list', {'limit': 10})
    # Every requested operation above was a typed native call with its exact
    # observed identity projected. Two native preconditions are unreachable in
    # a headless isolated host and are recorded as boundaries, not as success:
    # a positive request_changes verdict needs a review-claimed run from the
    # native review dispatcher, and there is no native kanban_dispatch tool
    # (operator claim is the observed native path, not dispatch).
    boundaries: list[JsonValue] = [{'operation': 'request_changes',
        'reason': 'positive verdict needs a review-claimed run; review_dispatch is disabled and CLI claim only claims ready tasks; only unclaimed-review denial was exercised'}]
    if case_id == 'K6':
        dispatch = loop.prepare('dispatch', 'dispatch', {}, task_id)
        loop.require(dispatch.get('state') == 'unavailable', 'unsupported_dispatch_admitted')
        boundaries.append({'operation': 'dispatch', 'reason': 'no native kanban_dispatch tool; operator claim is not dispatch'})
    observations['native_boundary'] = boundaries
    observations['native_task_count'] = 2
    observations['lifecycle_observed'] = ['create', 'show', 'link', 'comment', 'attachments', 'claim', 'heartbeat',
                                          'block', 'unblock', 'request_review', 'complete', 'list']


def _load_source() -> None:
    # Equivalent to _local_package.load_local_package(), without that helper's
    # import-time home override: the parent already owns all four isolated homes.
    package = ModuleType('omh')
    package.__package__ = 'omh'
    package.__dict__['__path__'] = [str(_ROOT / 'src/omh'), str(_ROOT / 'src')]
    package.__spec__ = ModuleSpec('omh', loader=None, is_package=True)
    sys.modules['omh'] = package


def _child_main() -> None:
    case_id = _text(_object(_decoder.loads(sys.stdin.read()))['case'])
    observations: dict[str, JsonValue] = {'host_executed': False, 'task_ids': []}
    blocked: str | None = None
    # Host import chatter is never the wire protocol or persisted task content.
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        try:
            _load_source()
            _scenario(case_id, observations)
        except _Blocked as error:
            blocked = str(error)
        except Exception as error:
            blocked = 'native_scenario_error:' + type(error).__name__
        try:
            private_absent = all(b'QA-PRIVATE-BODY-SENTINEL' not in path.read_bytes()
                                 for path in Path(os.environ['OMH_HOME']).rglob('*.json'))
            observations['private_body_absent'] = private_absent
            if not private_absent:
                blocked = 'private_body_persisted'
        except OSError as error:
            blocked = 'privacy_check_error:' + type(error).__name__
    print(json.dumps({'observations': observations, 'blocked_reason': blocked, 'pass': blocked is None}))


def run_native_case(case_id: str) -> CaseResult:
    if case_id not in _CASES:
        return unavailable_case(case_id, 'unsupported_native_case')
    result = unavailable_case(case_id, 'native_host_unavailable')
    result['provenance'] = {'kind': 'local', 'scope': 'surface', 'native_required': True, 'native_available': False}
    if not native_host_available():
        return result
    root = Path(mkdtemp(prefix='omh-kanban-native-')).resolve()
    result['cleanup']['owned_resources'].append(str(root))
    result['inputs_metadata'] = {'board': 'qa-board', 'request_id': 'qa-create-1',
        'operator_authorized': True, 'live_model': False, 'isolated_host': True}
    receipts: list[dict[str, JsonValue]] = []
    environment = {
        'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(root / 'home'),
        'HERMES_HOME': str(root / 'hermes'), 'HERMES_KANBAN_HOME': str(root / 'kanban'),
        'OMH_HOME': str(root / 'omh'), 'TMPDIR': str(root / 'tmp'),
        'PYTHONDONTWRITEBYTECODE': '1', 'UV_NO_SYNC': '1',
        'PYTHONPATH': os.pathsep.join((str(_HOST), str(_ROOT / 'src'), str(_ROOT / 'tests'))),
    }
    try:
        (root / 'home').mkdir()
        (root / 'tmp').mkdir()
        _ = shutil.copytree(_ROOT / 'src/plugin_bundle/omh', root / 'hermes/plugins/omh',
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        _ = (root / 'hermes/config.yaml').write_text(_CONFIG, encoding='utf-8')
        commands = [[str(_CLI), 'kanban', 'boards', 'create', 'qa-board'],
                    [str(_PYTHON), '-m', 'five_issue_cases.kanban_native']]
        for command in commands:
            result['commands'].append(command)
            with subprocess.Popen(command, cwd=root, env=environment, text=True, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True) as child:
                try:
                    stdout, stderr = child.communicate(json.dumps({'case': case_id}), timeout=90)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    _ = child.communicate()
                    result['cleanup']['terminated_processes'].append(child.pid)
                    result['blocked_reason'] = 'native_child_timeout'
                    receipts.append({'exit': child.returncode, 'timeout': True})
                    break
                receipts.append({'pid': child.pid, 'exit': child.returncode, 'stderr_bytes': len(stderr.encode()),
                                 'stdout_bytes': len(stdout.encode()), 'reaped': child.poll() is not None})
                if child.returncode != 0:
                    result['blocked_reason'] = 'native_child_nonzero'
                    break
                result['provenance'] = {'kind': 'native', 'scope': 'surface', 'native_required': True, 'native_available': True}
                if command == commands[1]:
                    payload = _object(_decoder.loads(stdout))
                    result['observations'] = _object(payload['observations'])
                    result['pass'] = payload['pass'] is True
                    reason = payload['blocked_reason']
                    result['blocked_reason'] = None if reason is None else _text(reason)
    except (OSError, ValueError) as error:
        result['blocked_reason'] = 'native_invocation_error:' + type(error).__name__
        result['pass'] = False
    finally:
        try:
            shutil.rmtree(root)
        except OSError as error:
            result['cleanup']['errors'].append('owned_root_cleanup:' + type(error).__name__)
        absent = not root.exists()
        result['cleanup']['verified_absent'] = absent
        if absent:
            result['cleanup']['removed_paths'].append(str(root))
        else:
            result['cleanup']['errors'].append('owned_native_root_remains')
        if result['cleanup']['errors']:
            result['pass'] = False
        result['observations']['process_receipts'] = list(receipts)
        # The router intentionally replaces blocked producers with a generic
        # failure. Keep the detailed native/cleanup receipt only in our owned
        # proof namespace, never in canonical K*/surface.json.
        _PROOF.mkdir(parents=True, exist_ok=True)
        _ = (_PROOF / (case_id + '.native.json')).write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    return result


if __name__ == '__main__':
    _child_main()
