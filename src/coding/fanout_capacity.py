"""Invocation-local capacity foundation, not a scheduler or native classifier.

Only a supplied trusted adapter may construct AdmissionReceipt from its own
positively supported admission boundary. Never deserialize model/CLI JSON or a
sidecar into this type merely because it names the schema. No native adapter is
registered here; support=None means unsupported, not inferred capacity.

A launch context belongs at the actual Popen boundary, after owner waits,
stagger and retry backoff. Its callback must do only Popen, not communicate,
postprocessing or backoff. Rejection observation must precede retry decisions
and release of the scheduler's existing owner/pool permits. This module does
not integrate those callers, authorize worktree reuse, or infer provider quota.
"""
from __future__ import annotations

from _thread import LockType
from collections.abc import Callable
from dataclasses import dataclass, field
import threading
from typing import Generic, Literal, TypeVar, final


@dataclass(frozen=True, slots=True)
class AdmissionBinding:
    owner: str
    fanout_id: str
    unit_id: str
    run_ref: str
    attempt: int
    base_sha: str
    worktree: str


@dataclass(frozen=True, slots=True)
class AdmissionSupport:
    """Caller-supplied, source-qualified adapter capability; never CLI input."""
    adapter: str
    protocol: str
    scope: Literal['process_local', 'executor_local']


@dataclass(frozen=True, slots=True)
class AdmissionReceipt:
    """Trusted adapter's typed claim, still checked against observed facts."""
    adapter: str
    protocol: str
    binding: AdmissionBinding
    process_started: bool
    returncode: int | None
    schema_version: str = 'executor_admission/v1'
    source: str = 'trusted_adapter'
    reason: str = 'executor_capacity_rejected'
    implementation_started: bool = False


def validate_admission_receipt(
    receipt: object, *, expected: AdmissionBinding,
    support: AdmissionSupport | None, process_started: bool,
    returncode: int | None, implementation_started: bool,
) -> AdmissionReceipt | None:
    """Accept only closed typed adapter evidence with exact attempt/exit binding.

    Raw dicts/strings are deliberately unsupported, including schema-shaped
    JSON. Caller-observed implementation activity overrides the adapter claim.
    A CLI can have started/exited without implementation having been admitted;
    preserve its actual (even zero or nullable) returncode rather than inventing
    an executor error code. An unstarted process cannot have an exit code.
    """
    if not isinstance(receipt, AdmissionReceipt) or type(receipt) is not AdmissionReceipt:
        return None
    if support is None or not support.adapter or not support.protocol:
        return None
    if support.scope not in ('process_local', 'executor_local'):
        return None
    if (receipt.schema_version != 'executor_admission/v1'
            or receipt.source != 'trusted_adapter'
            or receipt.reason != 'executor_capacity_rejected'
            or receipt.adapter != support.adapter or receipt.protocol != support.protocol):
        return None
    if type(receipt.binding) is not AdmissionBinding or receipt.binding != expected:
        return None
    binding = receipt.binding
    if (type(binding.attempt) is not int or binding.attempt < 1
            or any(type(value) is not str or not value for value in (
                binding.owner, binding.fanout_id, binding.unit_id,
                binding.run_ref, binding.base_sha, binding.worktree))):
        return None
    if receipt.implementation_started is not False or implementation_started is not False:
        return None
    if (type(process_started) is not bool or type(receipt.process_started) is not bool
            or receipt.process_started is not process_started):
        return None
    if (returncode is not None and type(returncode) is not int
            or receipt.returncode is not None and type(receipt.returncode) is not int
            or receipt.returncode != returncode
            or not process_started and returncode is not None):
        return None
    return receipt


@dataclass(frozen=True, slots=True)
class StartReceipt:
    binding: AdmissionBinding
    sequence: int


@dataclass(frozen=True, slots=True)
class CapacityTrip:
    receipt: AdmissionReceipt
    sequence: int


Process = TypeVar('Process')


@dataclass(frozen=True, slots=True)
class LaunchResult(Generic[Process]):
    status: Literal['started', 'executor_capacity_rejected', 'not_started_capacity_blocked']
    process: Process | None = None
    start: StartReceipt | None = None
    trip: CapacityTrip | None = None


@dataclass(slots=True)
class _GateState:
    """Mutable invocation state shared by its immutable launch contexts."""
    lock: LockType = field(default_factory=threading.Lock)
    sequence: int = 0
    trips: dict[str, CapacityTrip] = field(default_factory=dict)

    def trip(self, receipt: AdmissionReceipt) -> CapacityTrip:
        # Caller holds lock; this state never escapes the module's gate/context.
        owner = receipt.binding.owner
        if owner not in self.trips:
            self.sequence += 1
            self.trips[owner] = CapacityTrip(receipt, self.sequence)
        return self.trips[owner]


@dataclass(frozen=True, slots=True)
class LaunchContext:
    _state: _GateState
    binding: AdmissionBinding
    support: AdmissionSupport | None = None

    def start(
        self, spawn: Callable[[], Process], *,
        admission_check: Callable[[], AdmissionReceipt | None] | None = None,
    ) -> LaunchResult[Process]:
        """Atomically check gate, optional trusted preflight, Popen and receipt.

        None from preflight means no rejection observed, not proven admission.
        An invalid supplied preflight raises without spawning or tripping.
        Callback exceptions propagate; failed Popen consumes no start sequence.
        Returned processes remain entirely owned by the existing runner/reaper.
        """
        with self._state.lock:
            trip = self._state.trips.get(self.binding.owner)
            if trip is not None:
                return LaunchResult('not_started_capacity_blocked', trip=trip)
            if admission_check is not None:
                observed = admission_check()
                if observed is not None:
                    receipt = validate_admission_receipt(
                        observed, expected=self.binding, support=self.support,
                        process_started=False, returncode=None, implementation_started=False,
                    )
                    if receipt is None:
                        raise ValueError('invalid_admission_receipt')
                    trip = self._state.trip(receipt)
                    return LaunchResult('executor_capacity_rejected', trip=trip)
            process = spawn()
            self._state.sequence += 1
            start = StartReceipt(self.binding, self._state.sequence)
            return LaunchResult('started', process=process, start=start)

    def reject(
        self, receipt: object, *, process_started: bool,
        returncode: int | None, implementation_started: bool,
    ) -> CapacityTrip | None:
        """Validate and trip under the launch lock; retain the first trigger.

        Call as soon as complete supported rejection evidence is observed,
        before retry/recovery/postprocessing. Invalid/unsupported observations
        do not close the gate or override an earlier validated rejection.
        """
        with self._state.lock:
            validated = validate_admission_receipt(
                receipt, expected=self.binding, support=self.support,
                process_started=process_started, returncode=returncode,
                implementation_started=implementation_started,
            )
            return self._state.trip(validated) if validated is not None else None


@final
class OwnerLaunchGate:
    """One explicit dispatch's owner-keyed monotonic breaker, with no reset.

    One short shared lock orders starts and trips across contexts. Already
    started siblings can finish; unrelated owners can still launch. No process
    handles/output or second spawn ledger are retained here. A new explicit
    invocation constructs a new gate; earlier receipts remain immutable.
    """
    def __init__(self) -> None:
        self._state = _GateState()

    def context(
        self, binding: AdmissionBinding, *, support: AdmissionSupport | None = None,
    ) -> LaunchContext:
        return LaunchContext(self._state, binding, support)

    def rejection(self, owner: str) -> CapacityTrip | None:
        """Cheap supplementary check only; start() is the authoritative check."""
        with self._state.lock:
            return self._state.trips.get(owner)
