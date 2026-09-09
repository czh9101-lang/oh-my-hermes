"""Real local process capacity scenarios; never vendor/native saturation proof."""
from __future__ import annotations

import os
from pathlib import Path
from five_issue_process_fixture import write_fixture_executable
import re
import socket
from contextlib import ExitStack
from uuid import uuid4
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
from unittest.mock import patch
from collections.abc import Callable, Mapping, Sequence
from typing import TypedDict, Unpack

from _local_package import load_local_package
load_local_package()
from omh.coding import fanout_capacity as capacity
from omh.coding.fanout import build_fanout_contract
from omh.coding.fanout_artifacts import write_fanout_contract, fanout_run_journal_path
from omh.coding.fanout_dispatch import build_dispatch_argv, dispatch_fanout, signal_safe_unit_runner
from omh.coding.fanout_journal import read_fanout_run_journal
from omh.coding.fanout_status import project_fanout_status
from omh.system.paths import OmhPaths
from . import CaseResult, JsonValue
from .sessions import decode, record, rows_of, text
from omh.coding.fanout_output import FanoutOutput

Process = subprocess.Popen[bytes] | subprocess.Popen[str]


class RunnerOptions(TypedDict, total=False):
    cwd: str | None
    env: Mapping[str, str] | None
    text: bool | None
    errors: str | None
    capture_output: bool
    timeout: float | None
    on_spawn: Callable[[Process], None] | None
    on_output: Callable[[str], None] | None
    confinement_command: Sequence[str] | None
    output_capture: FanoutOutput | None
    launch: capacity.LaunchCallable | None


def no_stagger(_self: object) -> None:
    return None


def ready(*_args: object) -> dict[str, object]:
    return {'status': 'ready'}

ERROR_LINE = 'Error: turn/start: turn/start failed: in-process app-server request queue is full (code -32001)\n'
SOURCE_REVISION = 'b83105710695b70b6d96a64d1e4612bdf68d5f92'


def negative_admission_cases() -> int:
    """Exact framing contrast, through real binary pipes and the shared decoder."""
    from omh.coding.fanout_output import FanoutOutput
    from omh.coding.fanout_executor_sessions import BinaryIdentity, SessionCapability
    source = capacity.CodexAdmissionSource('/fixture', 'a' * 64, '0.0.0', SOURCE_REVISION, 'fixture')
    capability = SessionCapability('codex', 'codex_exec_json', BinaryIdentity('/fixture', 'a' * 64), '0.0.0')
    assert capacity.codex_admission_source(capability, (source,)) == source
    assert capacity.codex_admission_source(capability, ()) is None
    from dataclasses import replace
    for change in (replace(source, version='unknown'), replace(source, sha256='b' * 64),
                   replace(source, source_revision='unknown'), replace(source, resolved_path='/foreign')):
        assert capacity.codex_admission_source(capability, (change,)) is None
    stdout = b'{"type":"thread.started","thread_id":"11111111-1111-4111-8111-111111111111"}\n'
    stderr = ERROR_LINE.encode()
    variants = [(stdout, value.encode()) for value in (
        'HTTP 429\n', 'HTTP 529\n', 'usage limit reached\n', 'Authentication failed\n',
        ERROR_LINE.replace('app-server request', 'server request'),
        ERROR_LINE.replace('app-server request', 'app-server client'),
        ERROR_LINE.replace('turn/start', 'thread/start'), ERROR_LINE.rstrip('\n'),
        ERROR_LINE + 'backtrace\n', 'nested agent thread limit reached\n', '')]
    variants += [(value, stderr) for value in (
        b'', b'{bad}\n', stdout.rstrip(b'\n'), stdout + b'{"type":"turn.started"}\n',
        stdout + b'{"type":"item.started"}\n', stdout + stdout,
        b'{"tool_result":{"type":"thread.started"}}\n',
        stdout.replace(b'"thread_id":', b'"extra":true,"thread_id":'),
        stdout.replace(b'"thread_id":', b'"type":"thread.started","thread_id":'),
        stdout + b'forged extra output\n', b'\xff\n')]
    for out, err in [(stdout, stderr), *variants]:
        observer = capacity.CodexAdmissionObserver()
        capture = FanoutOutput(protocol='codex', observer=observer.observe)
        result = signal_safe_unit_runner([sys.executable, '-c',
            'import sys;sys.stdout.buffer.write(bytes.fromhex(sys.argv[1]));sys.stderr.buffer.write(bytes.fromhex(sys.argv[2]));sys.exit(1)',
            out.hex(), err.hex()], capture_output=True, timeout=5, output_capture=capture)
        binding = capacity.AdmissionBinding('codex', 'fanout-1', 'a', 'run-a', 1, 'a' * 40,
                                             '/owned', 'invocation-1', 'attempt-1')
        receipt = observer.receipt(capture, binding=binding, source=source, returncode=result.returncode,
                                   process_started=True, fresh_exec=True, artifact_observed=False)
        assert (receipt is not None) == ((out, err) == (stdout, stderr)), 'admission framing misclassified'
        if receipt is not None:
            cases: list[tuple[capacity.CodexAdmissionSource | None, int | None, bool, bool, bool]] = [
                (None, 1, True, True, False), (source, 0, True, True, False),
                (source, None, True, True, False), (source, 1, True, False, False),
                (source, 1, True, True, True), (source, 1, False, True, False)]
            for qualified, code, spawned, fresh, artifact in cases:
                assert observer.receipt(capture, binding=binding, source=qualified, returncode=code,
                    process_started=spawned, fresh_exec=fresh, artifact_observed=artifact) is None
            gate = capacity.OwnerLaunchGate()
            support = capacity.CODEX_ADMISSION_SUPPORT
            for foreign in (replace(binding, invocation_id='other'), replace(binding, attempt_id='other')):
                assert gate.context(foreign, support=support).reject(receipt, process_started=True,
                    returncode=1, implementation_started=False) is None
            for stream in ('stdout', 'stderr'):
                incomplete_observer = capacity.CodexAdmissionObserver()
                incomplete = FanoutOutput(protocol='codex', observer=incomplete_observer.observe)
                incomplete.feed('stdout', stdout)
                incomplete.feed('stderr', stderr)
                incomplete.finish('stdout', complete=stream != 'stdout')
                incomplete.finish('stderr', complete=stream != 'stderr')
                assert incomplete_observer.receipt(incomplete, binding=binding, source=source, returncode=1,
                    process_started=True, fresh_exec=True, artifact_observed=False) is None
    return len(variants)


def run_case(case_id: str) -> CaseResult:
    if case_id not in {'C1', 'C2', 'C3', 'C4', 'C5', 'C6', 'C7'}:
        raise ValueError('unknown_capacity_case')
    negative_count = negative_admission_cases() if case_id == 'C4' else 0
    commands: list[list[str]] = []
    observations: dict[str, JsonValue] = {}
    with TemporaryDirectory(prefix='five-capacity-') as directory, ExitStack() as resources:
        root = Path(directory).resolve()
        repo = root / 'repo'
        repo.mkdir()
        def git(*args: str) -> str:
            command = ['git', *args]
            commands.append(command)
            return subprocess.run(command, cwd=repo, capture_output=True, text=True,
                                  check=True).stdout.strip()
        _ = git('init', '-q')
        _ = (repo / 'seed').write_text('seed\n')
        _ = git('add', 'seed')
        _ = git('-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
        base = git('rev-parse', 'HEAD')
        paths = OmhPaths(omh_home=root / 'omh', hermes_home=root / 'hermes')
        env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(root / 'home'),
               'CODEX_HOME': str(root / 'codex'), 'CLAUDE_CONFIG_DIR': str(root / 'claude')}
        for key in ('HOME', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR'):
            Path(env[key]).mkdir()
        listener = resources.enter_context(socket.socket())
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(10)
        address: Callable[[], tuple[str, int]] = listener.getsockname
        port = address()[1]
        control_token = uuid4().hex
        child_control: socket.socket | None = None
        executor_argv = write_fixture_executable(root / 'executor',
            'import sys,json,re,socket\nfrom pathlib import Path\n' +
            "if '--version' in sys.argv: print('codex 0.0.0'); raise SystemExit(0)\n" +
            "if '--help' in sys.argv: print('--json resume --output-format stream-json --verbose --resume'); raise SystemExit(0)\n" +
            "if '--held' in sys.argv:\n" +
            " i=sys.argv.index('--held')\n" +
            " with socket.create_connection(('127.0.0.1',int(sys.argv[i+1])),timeout=10) as control:\n" +
            "  control.sendall((sys.argv[i+2]+'\\n').encode())\n" +
            "  with control.makefile('rb') as incoming: assert incoming.readline(64)==b'finish\\n'\n" +
            "if '--reject' in sys.argv:\n" +
            " print(json.dumps({'type':'thread.started','thread_id':'11111111-1111-4111-8111-111111111111'}))\n" +
            ' sys.stderr.write(' + repr(ERROR_LINE) + "); raise SystemExit(1)\n" +
            "if '--ordinary' in sys.argv: sys.stderr.write('compiler failed\\n'); raise SystemExit(3)\n" +
            "if '--rate' in sys.argv: sys.stderr.write('HTTP 429\\n'); raise SystemExit(1)\n" +
            "if '--transport' in sys.argv: sys.stderr.write('HTTP 503\\n'); raise SystemExit(1)\n" +
            'sys.path.insert(0, ' + repr(str(Path(__file__).resolve().parents[1])) + ')\n' +
            'from five_issue_process_fixture import executor_main\n' +
            "raise SystemExit(executor_main('codex', sys.argv[1:]))\n")
        rejecting = True
        def argv_for(owner: str, prompt: str, route: Mapping[str, object] | None = None) -> list[str]:
            argv = build_dispatch_argv(owner, prompt, route)
            assert argv is not None
            argv[0:1] = executor_argv
            match = re.search(r'schema_version=fanout_unit_result/v1, unit_id=([a-z0-9-]+),', prompt)
            assert match is not None
            if match[1] == 'a' and case_id != 'C6':
                argv[2:2] = ['--held', str(port), control_token]
            if match[1] == 'b' and rejecting:
                argv.append('--reject')
            if match[1] == 'f':
                argv.append('--rate' if case_id == 'C7' else '--ordinary')
            if case_id == 'C6' and match[1] == 'a':
                argv.append('--transport')
            return argv
        started = threading.Event()
        tripped = threading.Event()
        release = threading.Event()
        prepared = threading.Event()
        backoff = threading.Event()
        def retry_wait(_delay: float) -> None:
            backoff.set()
            assert tripped.wait(10), 'retry did not observe the owner trip'
        process_exits: list[int] = []
        starts: list[str] = []
        actual_reject = capacity.LaunchContext.reject
        def reject(context: capacity.LaunchContext, receipt: object, *, process_started: bool,
                   returncode: int | None, implementation_started: bool) -> capacity.CapacityTrip | None:
            outcome = actual_reject(context, receipt, process_started=process_started,
                returncode=returncode, implementation_started=implementation_started)
            if outcome is not None:
                tripped.set()
                if child_control is not None:
                    child_control.sendall(b'finish\n')
                release.set()
            return outcome
        def runner(argv: Sequence[str], **kwargs: Unpack[RunnerOptions]) -> subprocess.CompletedProcess[bytes] | subprocess.CompletedProcess[str]:
            if list(argv[:len(executor_argv)]) != executor_argv:
                return signal_safe_unit_runner(argv, **kwargs)
            prompt = next((part for part in argv if 'schema_version=fanout_unit_result/v1, unit_id=' in part), '')
            match = re.search(r'schema_version=fanout_unit_result/v1, unit_id=([a-z0-9-]+),', prompt)
            assert match is not None
            unit = match[1]
            old_spawn = kwargs.get('on_spawn')
            def on_spawn(process: Process) -> None:
                nonlocal child_control
                starts.append(unit)
                commands.append(['<fixture prompt>' if part == prompt else
                                 '<fixture control token>' if part == control_token else part for part in argv])
                if old_spawn is not None:
                    old_spawn(process)
                if unit == 'a':
                    if case_id != 'C6':
                        child_control = resources.enter_context(listener.accept()[0])
                        child_control.settimeout(10)
                        with child_control.makefile('rb') as incoming:
                            assert incoming.readline(64) == (control_token + '\n').encode()
                        assert process.poll() is None, 'held child exited before admission rejection'
                    started.set()
                    # The child itself blocks on an authenticated release, not a timing delay.
                    if case_id != 'C6' and not release.wait(10):
                        raise TimeoutError('capacity_trip_release')
            kwargs['on_spawn'] = on_spawn
            if unit == 'c' and case_id == 'C1':
                prepared.set()
                assert tripped.wait(10), 'prepared waiter did not observe trip'
            if unit == 'b' and rejecting:
                assert started.wait(10), 'A did not cross Popen'
                if case_id == 'C1':
                    assert prepared.wait(10), 'C never reached the prepared boundary'
                if case_id == 'C6':
                    assert backoff.wait(10), 'A did not enter retry backoff'
            result = signal_safe_unit_runner(argv, **kwargs)
            process_exits.append(result.returncode)
            if unit == 'a':
                captured = kwargs.get('output_capture')
                assert captured is not None
                assert result.returncode == (1 if case_id == 'C6' else 0), captured.error_window('stderr')
            return result
        for marker in ('accepts_on_spawn', 'accepts_output_capture', 'accepts_launch'):
            setattr(runner, marker, True)
        units = [
            {'unit_id': 'a', 'title': 'A', 'owner': 'codex', 'file_scope': ['a/']},
            {'unit_id': 'b', 'title': 'B', 'owner': 'codex', 'file_scope': ['b/']},
            {'unit_id': 'c', 'title': 'C', 'owner': 'codex', 'file_scope': ['c/']},
            {'unit_id': 'd', 'title': 'D', 'owner': 'codex', 'file_scope': ['d/']},
            {'unit_id': 'e', 'title': 'E', 'owner': 'codex', 'file_scope': ['e/'], 'depends_on': ['b', 'c']},
        ]
        if case_id == 'C3':
            units.extend([
                {'unit_id': 'f', 'title': 'F', 'owner': 'claude-code', 'file_scope': ['f/']},
                {'unit_id': 'm', 'title': 'M', 'owner': 'codex', 'file_scope': ['m/'], 'depends_on': ['b', 'f']},
            ])
        if case_id == 'C7':
            units.append({'unit_id': 'f', 'title': 'F', 'owner': 'claude-code', 'file_scope': ['f/']})
        goal = 'capacity fixture PROMPT_PRIVATE_SENTINEL'
        contract = write_fanout_contract(paths, build_fanout_contract(goal, units, spawn_plan={
            'why_parallel': 'Exercise independently launched local fixtures.',
            'why_not_single_unit': 'A launch race requires sibling processes.',
            'independence': 'Each fixture has its own isolated file scope.',
            'expected_evidence_shape': 'Observed process exits and bound capacity receipts.'}))
        from hashlib import sha256
        executable = Path(executor_argv[-1])
        sources = (capacity.CodexAdmissionSource(str(executable.resolve()), sha256(executable.read_bytes()).hexdigest(),
                                                 '0.0.0', SOURCE_REVISION, 'fixture'),)
        try:
            with (patch('omh.coding.fanout_dispatch.build_dispatch_argv', argv_for),
                  patch('omh.coding.fanout_dispatch._SpawnStagger.reserve', no_stagger),
                  patch.object(capacity.LaunchContext, 'reject', reject)):
                summary = dispatch_fanout(paths, contract, goal_text=goal, repo_root=repo, base_sha=base,
                    concurrency=3 if case_id == 'C1' else 2, runner=runner,
                    readiness=ready, max_retries=2 if case_id == 'C6' else 0,
                    sleep=retry_wait, env=env, capacity_sources=sources,
                    concurrency_policy={'requested': 8, 'applied': 2} if case_id == 'C5' else None,
                    on_failure='retarget' if case_id == 'C7' else 'report', retarget_owner='codex')
        finally:
            release.set()
        rows = {text(row['unit_id']): row for row in rows_of(summary)}
        assert rows['b']['status'] == 'executor_capacity_rejected', 'positive rejection was not classified'
        assert tripped.is_set()
        if case_id == 'C5':
            admission = record(rows['b']['capacity'])
            assert admission.get('definition_revision') == SOURCE_REVISION
            assert admission.get('evidence_kind') == 'fixture'
            assert admission.get('adapter') == 'codex_fresh_initial_request_queue'
            assert admission.get('protocol') == 'codex_exec_json'
        if case_id == 'C6':
            assert rows['a']['status'] == 'not_started_capacity_blocked'
            prior = rows['a']['prior_attempts']
            assert isinstance(prior, list) and record(prior[0])['failure_class'] == 'transient_transport'
            assert starts.count('a') == 1 and backoff.is_set()
        else:
            assert rows['a']['process_succeeded'] and rows['a']['exit_code'] == 0, rows['a']
        assert rows['c']['status'] == rows['d']['status'] == 'not_started_capacity_blocked'
        assert rows['e']['status'] == 'blocked_by_capacity_dependency'
        assert starts == ['a', 'b'] or (case_id in ('C3', 'C7') and set(starts) == {'a', 'b', 'f'}), starts
        expected_affected = (['a'] if case_id == 'C6' else []) + ['b', 'c', 'd', 'e'] + (['m'] if case_id == 'C3' else [])
        assert record(record(summary)['capacity'])['affected_units'] == expected_affected
        if case_id == 'C1':
            assert rows['c']['worktree_created'] and rows['c']['worktree_path']
            assert not rows['d']['worktree_created'] and 'worktree_path' not in rows['d']
        journal = read_fanout_run_journal(fanout_run_journal_path(paths, str(contract['fanout_id'])))
        roster = project_fanout_status(paths, str(contract['fanout_id']))
        status_rows = {text(row['unit_id']): row for row in rows_of(roster)}
        assert record(status_rows['c']['capacity'])['status'] == 'not_started_capacity_blocked'
        assert status_rows['c']['lifecycle_state'] == 'not_dispatched'
        if case_id == 'C5':
            from _cli_harness import run_cli
            widths = record(record(summary)['capacity'])
            assert (widths['requested_concurrency'], widths['effective_concurrency']) == (8, 2)
            for tail in (['status', '--fanout-id', str(contract['fanout_id']), '--unit', 'c', '--json'],
                         ['brief', str(contract['fanout_id']), '--json']):
                command = ['--omh-home', str(paths.omh_home), '--hermes-home', str(paths.hermes_home),
                           'coding', 'fanout', *tail]
                commands.append(['omh', *command])
                code, output, error = run_cli(command)
                assert code == 0, error
                view = record(decode(output))
                public_c = next(row for row in rows_of(view) if row['unit_id'] == 'c')
                assert public_c['capacity'] == rows['c']['capacity']
        if case_id == 'C7':
            from omh.coding.fanout_admission import AdaptiveFanoutAdmission
            admission = AdaptiveFanoutAdmission(ceiling=3)
            admission.observe('a', rows['a'])
            assert admission.window == 3
            admission.observe('pressure', {'exit_code': 1, 'failure_kind': 'limit_shaped'})
            assert admission.window == 1
            assert not record(record(summary)['capacity'])['quota_observed']
            recovery = record(record(summary)['failure_recovery'])
            decisions = recovery['decisions']
            assert isinstance(decisions, list) and len(decisions) == 1
            attempt = record(record(decisions[0])['attempt'])
            assert attempt['status'] == 'not_started_capacity_blocked'
            assert attempt['unit_id'] == 'f-retarget-codex'
            assert not (root / 'repo-fanout-f-retarget-codex').exists()
        if case_id == 'C3':
            assert rows['f']['status'] == 'failed' and rows['f']['exit_code'] == 3
            assert rows['m']['blocked_reasons'] == {'b': 'capacity', 'f': 'failure'}
        if case_id == 'C2':
            for row in rows_of(journal):
                if row['unit_id'] in ('b', 'c', 'd', 'e'):
                    assert 'capacity_lineage' in row, 'planned capacity lineage missing'
                    assert record(row['capacity_lineage'])['base_sha'] == base
                    assert record(row['capacity_lineage'])['contract_digest'] == record(summary)['contract_digest']
            prior_starts = list(starts)
            with (patch('omh.coding.fanout_dispatch.build_dispatch_argv', argv_for),
                  patch('omh.coding.fanout_dispatch._SpawnStagger.reserve', no_stagger)):
                _ = dispatch_fanout(paths, contract, goal_text=goal, repo_root=repo, base_sha=base,
                    concurrency=1, runner=runner, readiness=ready, max_retries=0,
                    env=env, resume_journal=journal, capacity_sources=sources)
            assert starts == prior_starts, 'capacity reselection requires an explicit finite unit selection'
            from omh.coding.worktree_creator import ensure_fanout_unit_worktree
            from omh.coding.fanout_artifacts import fanout_contract_digest, unit_result_path
            prior_b = next(row for row in rows_of(journal) if row['unit_id'] == 'b')
            worktree = Path(text(rows['b']['worktree_path']))
            context = {'fanout_id': str(contract['fanout_id']), 'unit_id': 'b',
                       'run_ref': text(rows['b']['run_ref']), 'owner': 'codex', 'attempt_id': 'reuse-probe',
                       'worktree_ref': str(worktree), 'base_sha': base, 'observed_revision': None}
            def reuse_probe(prior: Mapping[str, object]) -> dict[str, object]:
                return ensure_fanout_unit_worktree(paths, repo_root=repo, unit_id='b', branch='agent/b',
                    base_sha=base, run_ref=text(rows['b']['run_ref']), failure_diagnostic_context=context,
                    capacity_resume=prior, contract_digest=fanout_contract_digest(contract))
            _ = (worktree / 'seed').write_text('startup side effect\n')
            assert reuse_probe(prior_b)['refusal'] == 'capacity_reuse_unsafe'
            assert (worktree / 'seed').read_text() == 'startup side effect\n'
            _ = (worktree / 'seed').write_text('seed\n')
            foreign = {**prior_b, 'capacity_lineage': {**record(prior_b['capacity_lineage']), 'incarnation_id': 'foreign'}}
            assert reuse_probe(foreign)['refusal'] == 'capacity_reuse_unsafe'
            from omh.coding.inflight import write_inflight_marker, clear_inflight_marker
            _ = write_inflight_marker(paths, str(contract['fanout_id']), 'b', {'owner': 'codex', 'worktree': str(worktree)})
            assert reuse_probe(prior_b)['refusal'] == 'capacity_reuse_unsafe'
            assert clear_inflight_marker(paths, str(contract['fanout_id']), 'b')
            artifact = unit_result_path(paths, str(contract['fanout_id']), 'b')
            artifact.parent.mkdir(parents=True, exist_ok=True)
            _ = artifact.write_text('{}')
            assert reuse_probe(prior_b)['refusal'] == 'capacity_reuse_unsafe'
            artifact.unlink()
            assert starts == prior_starts
            rejecting = False
            with (patch('omh.coding.fanout_dispatch.build_dispatch_argv', argv_for),
                  patch('omh.coding.fanout_dispatch._SpawnStagger.reserve', no_stagger)):
                fresh = dispatch_fanout(paths, contract, goal_text=goal, repo_root=repo, base_sha=base,
                    concurrency=1, runner=runner, readiness=ready, max_retries=0,
                    env=env, only_units=['b', 'c', 'd', 'e'], resume_journal=journal, capacity_sources=sources)
            fresh_rows = {text(row['unit_id']): row for row in rows_of(fresh)}
            assert all(fresh_rows[unit]['process_succeeded'] for unit in ('a', 'b', 'c', 'd', 'e'))
            assert fresh_rows['b']['attempt_id'] != rows['b']['attempt_id']
            assert fresh_rows['b']['invocation_id'] != rows['b']['invocation_id']
            assert fresh_rows['b']['worktree_path'] == rows['b']['worktree_path']
            assert fresh_rows['b']['worktree_reused'] is True
        persisted = b'\n'.join(path.read_bytes() for path in paths.omh_home.rglob('*') if path.is_file())
        assert b'PROMPT_PRIVATE_SENTINEL' not in persisted
        observations = record({'summary': summary, 'process_exits': process_exits, 'starts': starts,
                        'negative_case_count': negative_count,
                        'native_positive_admission_executed': False})
    return {'case': case_id, 'commands': commands, 'inputs_metadata': {'scope': 'source_qualified_fixture'},
            'observations': observations, 'pass': True, 'blocked_reason': None,
            'provenance': {'kind': 'fixture', 'scope': 'surface', 'native_required': False, 'native_available': False},
            'cleanup': {'owned_resources': [directory], 'terminated_processes': [], 'removed_paths': [directory],
                        'verified_absent': not root.exists(), 'errors': []}}
