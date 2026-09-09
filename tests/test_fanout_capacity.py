"""Foundation proofs only; supplied adapters are not native capacity evidence."""
from __future__ import annotations

from _thread import LockType
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
import subprocess
import sys
import threading
from typing import Protocol, TypedDict, Unpack, final, runtime_checkable
import unittest

from _local_package import load_local_package
from five_issue_cases import capacity as capacity_cases

load_local_package()

from omh.coding.fanout_admission import AdaptiveFanoutAdmission
from omh.coding.fanout_retry import evaluate_unit_retry


from omh.coding import fanout_capacity


class ValidationOptions(TypedDict):
    expected: fanout_capacity.AdmissionBinding
    support: fanout_capacity.AdmissionSupport | None
    process_started: bool
    returncode: int | None
    implementation_started: bool


class ValidationOverrides(TypedDict, total=False):
    expected: fanout_capacity.AdmissionBinding
    support: fanout_capacity.AdmissionSupport | None
    process_started: bool
    returncode: int | None
    implementation_started: bool


class RetryOptions(TypedDict):
    exit_code: int
    output_tail: str
    stderr_tail: str
    recovery: Mapping[str, str] | None
    max_retries: int
    rng: Callable[[], float]


@runtime_checkable
class GateStateProbe(Protocol):
    """Test-only structural boundary; the lock's concrete type is checked below."""
    lock: object


@final
class CapacityFoundationTests(unittest.TestCase):
    def __init__(self, methodName: str = 'runTest') -> None:
        super().__init__(methodName)
        self.api = fanout_capacity
        self.binding = self.api.AdmissionBinding(
            owner='supplied-owner', fanout_id='fanout-1', unit_id='unit-a',
            run_ref='run-a', attempt=1, base_sha='a' * 40, worktree='/owned/unit-a',
        )
        self.support = self.api.AdmissionSupport('supplied-test-adapter', 'fixture/v1', 'process_local')
        self.receipt = self.api.AdmissionReceipt(
            adapter=self.support.adapter, protocol=self.support.protocol,
            binding=self.binding, process_started=True, returncode=7,
        )
        self.gate = self.api.OwnerLaunchGate()

    def gate_lock(self) -> LockType:
        """Inspect the actual gate lock without publishing mutable product state.

        Keep this precise internal probe: event ordering alone cannot reliably
        detect an unlocked start before the rejection thread gets scheduled.
        """
        fields = dict[str, object](vars(self.gate))
        state = fields['_state']
        assert isinstance(state, GateStateProbe)
        lock = state.lock
        assert isinstance(lock, LockType)
        return lock

    def context(self, **changes: str | int) -> fanout_capacity.LaunchContext:
        return self.gate.context(replace(self.binding, **changes), support=self.support)

    def reject(self, context: fanout_capacity.LaunchContext | None = None,
               receipt: fanout_capacity.AdmissionReceipt | None = None) -> fanout_capacity.CapacityTrip:
        trip = (context or self.context()).reject(
            receipt or self.receipt, process_started=True, returncode=7,
            implementation_started=False,
        )
        assert trip is not None
        return trip

    def validate(self, receipt: object, **changes: Unpack[ValidationOverrides]) -> fanout_capacity.AdmissionReceipt | None:
        options: ValidationOptions = {'expected': self.binding, 'support': self.support,
                                      'process_started': True, 'returncode': 7, 'implementation_started': False}
        options.update(changes)
        return self.api.validate_admission_receipt(receipt, **options)

    def test_c1_supplied_rejection_closes_owner_before_next_spawn(self):
        calls: list[str] = []
        started = self.context().start(lambda: calls.append('a') or 'process-a')
        trip = self.reject()
        blocked = self.context(unit_id='unit-b').start(lambda: calls.append('b'))
        self.assertEqual(started.status, 'started')
        self.assertEqual(started.process, 'process-a')
        assert started.start is not None
        self.assertLess(started.start.sequence, trip.sequence)
        self.assertEqual(blocked.status, 'not_started_capacity_blocked')
        self.assertIsNone(blocked.process)
        self.assertIs(blocked.trip, trip)
        self.assertEqual(calls, ['a'])

    def test_c1_prelaunch_admission_and_spawn_share_gate(self):
        admission = replace(self.receipt, process_started=False, returncode=None)
        calls: list[str] = []
        rejected = self.context().start(
            lambda: calls.append('spawn'), admission_check=lambda: admission,
        )
        self.assertEqual(rejected.status, 'executor_capacity_rejected')
        self.assertIsNone(rejected.start)
        self.assertIsNone(rejected.process)
        assert rejected.trip is not None
        self.assertEqual(rejected.trip.receipt, admission)
        blocked = self.context(unit_id='next').start(lambda: calls.append('next'))
        self.assertEqual(blocked.status, 'not_started_capacity_blocked')
        self.assertEqual(calls, [])

    def test_c2_binding_and_first_rejection_survive_new_invocation(self):
        trip = self.reject()
        again = self.reject(receipt=replace(self.receipt, binding=replace(self.binding, attempt=2)),
                            context=self.context(attempt=2))
        self.assertIs(again, trip)
        self.assertEqual(asdict(trip.receipt.binding), asdict(self.binding))
        fresh = self.api.OwnerLaunchGate().context(self.binding, support=self.support)
        self.assertEqual(fresh.start(lambda: 'explicit-new-invocation').status, 'started')
        self.assertEqual(self.context(attempt=3).start(lambda: 'forbidden').status,
                         'not_started_capacity_blocked')
        self.assertEqual(trip.receipt.binding.attempt, 1)

    def test_c4_positive_receipt_preserves_actual_exit_and_process_state(self):
        self.assertIs(self.validate(self.receipt), self.receipt)
        for code in (None, 0, 1, -9, 124, 127):
            with self.subTest(returncode=code):
                receipt = replace(self.receipt, returncode=code)
                self.assertIs(self.validate(receipt, returncode=code), receipt)
        unspawned = replace(self.receipt, process_started=False, returncode=None)
        self.assertIs(self.validate(unspawned, process_started=False, returncode=None), unspawned)
        self.assertIsNone(self.validate(replace(unspawned, returncode=7), process_started=False))

    def test_c4_rejects_every_mismatched_binding_field(self):
        for field, changed in (('owner', 'foreign'), ('fanout_id', 'other-fanout'),
                               ('unit_id', 'other-unit'), ('run_ref', 'other-run'),
                               ('attempt', 2), ('base_sha', 'b' * 40), ('worktree', '/foreign')):
            with self.subTest(field=field):
                receipt = replace(self.receipt, binding=replace(self.binding, **{field: changed}))
                self.assertIsNone(self.validate(receipt))
        for binding in (replace(self.binding, attempt=True), replace(self.binding, attempt=0),
                        replace(self.binding, unit_id='')):
            with self.subTest(binding=binding):
                self.assertIsNone(self.validate(replace(self.receipt, binding=binding), expected=binding))

    def test_c4_closed_receipt_rejects_malformed_and_conflicting_evidence(self):
        variants = dict(schema_version='executor_admission/v2', adapter='foreign', protocol='unknown',
                        source='model', reason='rate_limit', implementation_started=True,
                        process_started=False, returncode=8)
        for field, value in variants.items():
            with self.subTest(field=field):
                self.assertIsNone(self.validate(replace(self.receipt, **{field: value})))
        for field in ('implementation_started', 'process_started', 'returncode'):
            with self.subTest(numeric_boolean=field):
                self.assertIsNone(self.validate(replace(self.receipt, **{field: 0.0})))
        self.assertIsNone(self.validate(self.receipt, implementation_started=True))
        self.assertIsNone(self.validate(self.receipt, returncode=True))
        self.assertIsNone(self.validate(self.receipt, support=None))

    def test_c4_raw_native_model_sidecar_and_nested_errors_never_trip(self):
        payloads = [None, '', 'capacity reached', 'HTTP 429', 'overloaded_error',
                    {'type': 'turn.failed', 'error': {'message': 'agent thread limit reached'}},
                    {'type': 'result', 'is_error': True, 'num_turns': 0, 'api_error_status': 429},
                    {'status': 'at_capacity', 'results': ['synchronous fallback']},
                    asdict(self.receipt), {'tool_result': asdict(self.receipt)},
                    {'sidecar': asdict(self.receipt)}]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertIsNone(self.validate(payload))
                self.assertIsNone(self.context().reject(
                    payload, process_started=True, returncode=7, implementation_started=False))
        self.assertIsNone(self.gate.rejection(self.binding.owner))
        self.assertEqual(self.context().start(lambda: 'still-open').status, 'started')

    def test_c4_unregistered_native_receipts_remain_unsupported(self):
        for owner in ('codex', 'claude-code', 'hermes', 'generic'):
            binding = replace(self.binding, owner=owner)
            receipt = replace(self.receipt, adapter=owner, protocol='native/unknown', binding=binding)
            context = self.gate.context(binding)
            self.assertIsNone(context.reject(receipt, process_started=True, returncode=7,
                                             implementation_started=False))
            self.assertEqual(context.start(lambda: owner).process, owner)

    def test_c4_invalid_preflight_is_not_a_capacity_trip_or_a_launch(self):
        calls: list[str] = []
        with self.assertRaisesRegex(ValueError, '^invalid_admission_receipt$'):
            _ = self.context().start(lambda: calls.append('spawn'),
                                 admission_check=lambda: replace(self.receipt, source='model'))
        self.assertEqual(calls, [])
        self.assertIsNone(self.gate.rejection(self.binding.owner))
        self.assertEqual(self.context().start(lambda: 'valid').status, 'started')

    def test_c5_start_and_trip_receipts_are_immutable_exact_metadata(self):
        from dataclasses import FrozenInstanceError
        start = self.context().start(lambda: 'not-retained-by-gate')
        trip = self.reject()
        assert start.start is not None
        self.assertEqual(start.start.binding, self.binding)
        self.assertEqual(trip.receipt.adapter, self.support.adapter)
        self.assertEqual(self.support.scope, 'process_local')
        self.assertIs(self.gate.rejection(self.binding.owner), trip)
        with self.assertRaises(FrozenInstanceError):
            setattr(trip, 'sequence', 100)
        with self.assertRaises(FrozenInstanceError):
            setattr(trip.receipt.binding, 'attempt', 4)

    def test_c6_dequeued_prepared_retry_and_retarget_waiters_recheck_gate(self):
        for label in ('dequeued', 'worktree_prepared', 'owner_lane_wait', 'retry_backoff', 'retarget'):
            with self.subTest(waiter=label):
                gate = self.api.OwnerLaunchGate()
                context = gate.context(replace(self.binding, attempt=2), support=self.support)
                waiting, release = threading.Event(), threading.Event()
                calls: list[str] = []
                def worker():
                    waiting.set()
                    if not release.wait(5):
                        raise TimeoutError('waiter_release')
                    return context.start(lambda: calls.append(label))
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(worker)
                    try:
                        self.assertTrue(waiting.wait(5))
                        self.assertFalse(future.cancel())
                        trip = gate.context(self.binding, support=self.support).reject(
                            self.receipt, process_started=True, returncode=7, implementation_started=False)
                    finally:
                        release.set()
                    result = future.result(timeout=5)
                self.assertEqual(result.status, 'not_started_capacity_blocked')
                self.assertIs(result.trip, trip)
                self.assertEqual(calls, [])

    def test_c6_spawn_wins_lock_order_and_trip_waits_for_start_receipt(self):
        entered, release, rejecting = threading.Event(), threading.Event(), threading.Event()
        def spawn():
            try:
                self.assertTrue(self.gate_lock().locked())
            finally:
                entered.set()
            if not release.wait(5):
                raise TimeoutError('spawn_release')
            return 'already-started'
        def trip_worker():
            rejecting.set()
            return self.reject()
        with ThreadPoolExecutor(max_workers=2) as pool:
            start = pool.submit(self.context().start, spawn)
            try:
                self.assertTrue(entered.wait(5))
                rejection = pool.submit(trip_worker)
                self.assertTrue(rejecting.wait(5))
            finally:
                release.set()
            result, trip = start.result(timeout=5), rejection.result(timeout=5)
        assert result.start is not None
        self.assertLess(result.start.sequence, trip.sequence)
        self.assertEqual(result.process, 'already-started')
        self.assertEqual(self.context().start(lambda: 'forbidden').status, 'not_started_capacity_blocked')

    def test_c6_admission_check_holds_same_lock_as_spawn_and_trip(self):
        checking, release, rejecting = threading.Event(), threading.Event(), threading.Event()
        def admission_check():
            try:
                self.assertTrue(self.gate_lock().locked())
            finally:
                checking.set()
            if not release.wait(5):
                raise TimeoutError('admission_release')
            return None
        def trip_worker():
            rejecting.set()
            return self.reject()
        with ThreadPoolExecutor(max_workers=2) as pool:
            start = pool.submit(self.context().start, lambda: 'started', admission_check=admission_check)
            try:
                self.assertTrue(checking.wait(5))
                rejection = pool.submit(trip_worker)
                self.assertTrue(rejecting.wait(5))
            finally:
                release.set()
            result, trip = start.result(timeout=5), rejection.result(timeout=5)
        assert result.start is not None
        self.assertLess(result.start.sequence, trip.sequence)

    def test_c6_failed_spawn_and_callback_release_lock_without_start_receipt(self):
        def missing():
            raise FileNotFoundError('fixture-missing')
        with self.assertRaises(FileNotFoundError):
            _ = self.context().start(missing)
        with self.assertRaises(FileNotFoundError):
            _ = self.context().start(lambda: 'unused', admission_check=missing)
        result = self.context(owner='unrelated').start(lambda: 'first-actual-start')
        assert result.start is not None
        self.assertEqual(result.start.sequence, 1)
        self.assertIsNone(self.gate.rejection(self.binding.owner))

    def test_c6_real_popen_lifetime_does_not_hold_gate_and_children_are_reaped(self):
        fixture = Path(__file__).with_name('five_issue_process_fixture.py')
        processes: list[subprocess.Popen[bytes]] = []
        def spawn_held():
            process = subprocess.Popen(
                [sys.executable, '-c', 'import sys; print("ready", flush=True); sys.stdin.buffer.read()'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            processes.append(process)
            return process
        try:
            result = self.context().start(spawn_held)
            assert result.process is not None
            self.assertIs(result.process, processes[0])
            # The live child blocks on stdin; no timing assertion establishes liveness.
            trip = self.reject()
            def spawn_other():
                process = subprocess.Popen([sys.executable, str(fixture), '--exit-code', '3'],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                processes.append(process)
                return process
            other = self.context(owner='unrelated', unit_id='unit-other').start(spawn_other)
            assert other.start is not None and other.process is not None
            self.assertGreater(other.start.sequence, trip.sequence)
            out, err = other.process.communicate(timeout=5)
            self.assertEqual((other.process.returncode, out, err), (3, b'', b''))
            out, err = result.process.communicate(timeout=5)
            # The child writes its frame in text mode; Windows emits CRLF.
            self.assertEqual((result.process.returncode, out.splitlines(), err), (0, [b'ready'], b''))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                _ = process.communicate(timeout=5)
        self.assertEqual(len(processes), 2)
        self.assertTrue(all(process.returncode is not None for process in processes))


class CapacityQAContractTests(unittest.TestCase):
    def test_c5_qa_component_never_claims_native_acceptance(self):
        qa = Path(__file__).resolve().parent / 'five_issue_cases/capacity.py'
        self.assertTrue(qa.is_file(), 'missing capacity QA foundation boundary')
        self.assertEqual(Path(capacity_cases.__file__).resolve(), qa)
        for case_id in ('C1', 'C2', 'C3', 'C4', 'C5', 'C6', 'C7'):
            with self.subTest(case=case_id):
                result = capacity_cases.run_case(case_id)
                self.assertEqual(result['case'], case_id)
                self.assertTrue(result['pass'])
                self.assertEqual(result['provenance']['scope'], 'surface')
                self.assertEqual(result['provenance']['kind'], 'fixture')
                self.assertFalse(result['provenance']['native_available'])
                self.assertFalse(result['observations']['native_positive_admission_executed'])
                self.assertTrue(result['cleanup']['verified_absent'])
                self.assertTrue(result['cleanup']['owned_resources'])
                self.assertIsNone(result['blocked_reason'])


class CapacityCompatibilityTests(unittest.TestCase):
    def test_c7_existing_adaptive_pressure_and_dry_run_are_not_owner_breakers(self):
        admission = AdaptiveFanoutAdmission(ceiling=4)
        clean = {'status': 'completed', 'process_succeeded': True, 'exit_code': 0}
        self.assertEqual(admission.available_slots(1), 1)
        admission.observe('a', clean)
        self.assertEqual(admission.window, 3)
        admission.observe('b', dict(clean, retry={'decisions': [{'failure_class': 'transient_provider_limit'}]}))
        self.assertEqual(admission.window, 1)
        admission.observe('c', clean)
        self.assertEqual(admission.window, 2)
        self.assertEqual(admission.receipt()['observed_provider_pressure_count'], 1)
        for n in range(40):
            admission.observe(str(n), clean)
        self.assertEqual(admission.window, 4)
        self.assertEqual(admission.receipt()['adjustments'][32:], [])
        self.assertEqual(admission.receipt()['adjustments'][-1]['unit_id'], '28')
        self.assertEqual(admission.receipt()['adjustments_omitted'], 11)
        dry = AdaptiveFanoutAdmission(ceiling=4, dry_run=True)
        dry.observe('a', clean)
        self.assertEqual(dry.receipt()['observed_completion_count'], 0)

    def test_c7_existing_retry_budget_and_replay_safety_remain_bounded(self):
        options: RetryOptions = {'exit_code': 1, 'output_tail': '', 'stderr_tail': 'HTTP 503',
                                 'recovery': {'outcome': 'no_changes'}, 'max_retries': 2, 'rng': lambda: 0.0}
        decisions = [evaluate_unit_retry(attempt=n, **options) for n in (1, 2, 3)]
        self.assertEqual([d['retry'] for d in decisions], [True, True, False])
        self.assertEqual([d.get('delay_seconds') for d in decisions], [1.5, 3.0, None])
        self.assertEqual(decisions[-1]['decision'], 'retries_exhausted')
        for code in (0, 124, 127):
            changed = options.copy()
            changed['exit_code'] = code
            self.assertFalse(evaluate_unit_retry(attempt=1, **changed)['retry'])
        for recovery in (None, {'outcome': 'capture_failed'}, {'outcome': 'recovery_available'}):
            changed = options.copy()
            changed['recovery'] = recovery
            self.assertFalse(evaluate_unit_retry(attempt=1, **changed)['retry'])
        self.assertFalse(evaluate_unit_retry(attempt=1, artifact_observed=True, **options)['retry'])


class CapacityPublicIntegrationTests(unittest.TestCase):
    def test_c1_real_process_trip_preserves_inflight_and_blocks_queue(self):
        self.assertTrue(capacity_cases.run_case('C1')['pass'])

    def test_c2_explicit_bounded_clean_owned_reselection(self):
        self.assertTrue(capacity_cases.run_case('C2')['pass'])

    def test_c3_mixed_dependency_reasons_and_failure_evidence(self):
        self.assertTrue(capacity_cases.run_case('C3')['pass'])

    def test_c4_real_process_negative_admission_matrix(self):
        result = capacity_cases.run_case('C4')
        self.assertTrue(result['pass'])
        count = result['observations']['negative_case_count']
        assert isinstance(count, int)
        self.assertGreater(count, 0)

    def test_c4_optional_capacity_reader_is_total_and_bound(self):
        binding = fanout_capacity.AdmissionBinding('codex', 'fanout-a', 'a', 'run-a', 1,
                                                   'a' * 40, '/owned', 'invocation', 'attempt')
        receipt = fanout_capacity.AdmissionReceipt('fixture', 'fixture/v1', binding, True, 1)
        value = fanout_capacity.capacity_fields(binding, fanout_capacity.CapacityTrip(receipt, 2),
                                               status='executor_capacity_rejected', process_started=True)
        self.assertEqual(fanout_capacity.read_capacity_fields({'capacity': value}), {'capacity': value})
        malformed: tuple[object, ...] = ([], {}, None, '', True)
        for key in value:
            for bad in malformed:
                if bad == value[key] and type(bad) is type(value[key]):
                    continue
                with self.subTest(key=key, value=bad):
                    try:
                        result = fanout_capacity.read_capacity_fields({'capacity': {**value, key: bad}})
                    except (TypeError, ValueError) as exc:
                        self.fail(f'optional capacity reader raised {type(exc).__name__}')
                    self.assertEqual(result, {})
        self.assertEqual(fanout_capacity.read_capacity_fields({'capacity': value, 'attempt_id': 'foreign'}), {})
        self.assertEqual(fanout_capacity.read_capacity_fields({'capacity': value, 'invocation_id': 'foreign'}), {})

    def test_c6_retry_backoff_rechecks_actual_launch_gate(self):
        self.assertTrue(capacity_cases.run_case('C6')['pass'])

    def test_c7_retarget_and_adaptive_compatibility(self):
        self.assertTrue(capacity_cases.run_case('C7')['pass'])

    def test_c5_public_capacity_status_and_privacy(self):
        result = capacity_cases.run_case('C5')
        self.assertTrue(result['pass'])
        self.assertFalse(result['provenance']['native_available'])
        self.assertTrue(result['cleanup']['verified_absent'])


class CapacityLaunchIntegrationTests(unittest.TestCase):
    def test_c6_actual_runner_cannot_cross_closed_gate(self):
        from omh.coding.fanout_dispatch import signal_safe_unit_runner
        binding = fanout_capacity.AdmissionBinding('codex', 'fanout-a', 'a', 'run-a', 1, 'a' * 40, '/owned')
        support = fanout_capacity.AdmissionSupport('test', 'fixture/v1', 'process_local')
        context = fanout_capacity.OwnerLaunchGate().context(binding, support=support)
        receipt = fanout_capacity.AdmissionReceipt('test', 'fixture/v1', binding, True, 1)
        self.assertIsNotNone(context.reject(receipt, process_started=True, returncode=1, implementation_started=False))
        calls: list[str] = []
        with self.assertRaises(fanout_capacity.CapacityBlocked):
            _ = signal_safe_unit_runner([sys.executable, '-c', 'pass'], capture_output=True,
                on_spawn=lambda _process: calls.append('observed'), launch=context.launch)
        self.assertEqual(calls, [], 'closed gate still launched a real process')


if __name__ == '__main__':
    _ = unittest.main()
