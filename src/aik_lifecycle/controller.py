"""AIK-038 cancel / drain / teardown controller.

Every decision is an append-only lifecycle event. Codes, IDs and provider job
states are recorded; no provider text, payload or secret.

- cancel(): records cancel_requested first. With disclosed interrupt support
  the job port's interrupt() is called; the answer is recorded as
  cancel_interrupt_sent and is NOT termination. Without support the job is
  left alone (cancel_unsupported). The lease only drains.
- settle_cancel(): polls through the observer. A terminal CANCELLED is
  cancel_confirmed, SUCCEEDED is late_success, FAILED is terminal_failed.
  Usage settlement stays with the observer. Only then is the lease released.
- teardown(): waits for tool effects, evidence and provider termination before
  returning the lease, terminating an owned worker and cleaning temporary data.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from types import MappingProxyType

from aik_lease import HOLDING_STATES, LeaseManager, StaleLease, UnknownLease
from aik_lease import LeaseError
from aik_observer import UnknownObservation
from aik_provider.contracts import (
    AdapterCapabilities, JobReceipt, JobRequest, JobState, Support, require_job,
)
from aik_runtime import Lease, Placement, RuntimeStore
from aik_runtime.errors import InvalidDatabase, InvalidRecord
from aik_runtime.models import MAX_INTEGER, identifier

from .database import timestamp
from .errors import LifecycleError
from .gate import GatedLeaseManager, LifecycleGate
from .workers import WorkerRegistry


EVENT_KINDS = (
    "cancel_requested", "cancel_before_dispatch", "cancel_dispatch_unconfirmed", "cancel_unsupported",
    "cancel_interrupt_unavailable", "cancel_interrupt_sent", "cancel_interrupt_unknown",
    "lease_draining", "lease_unknown_held", "observation_unavailable",
    "cancel_confirmed", "late_success", "terminal_failed", "lease_released",
    "awaiting_effect_resolution", "awaiting_evidence_handoff", "awaiting_provider_terminal",
    "worker_unregistered", "worker_in_use", "worker_terminated", "worker_terminate_failed",
    "worker_returned", "worker_cleaned", "worker_cleanup_failed", "teardown_complete",
)
OUTCOME_KINDS = ("cancel_confirmed", "late_success", "terminal_failed")
_LIVE = ("reserved", "active", "draining")
_PAGE = 1000
_MAX_ARTIFACTS = 1024


def interrupt_supported(capabilities: object) -> bool:
    """True only for an implemented adapter that discloses job_interrupt as SUPPORTED."""
    if type(capabilities) is not AdapterCapabilities:
        raise LifecycleError("INVALID_CAPABILITIES")
    member = Support.__members__.get("SUPPORTED")
    return (member is not None and capabilities.job_interrupt == member
            and capabilities.implementation_state == "implemented")


def _outcome_kind(state) -> str:
    if state == JobState.CANCELLED:
        return "cancel_confirmed"
    if state == JobState.SUCCEEDED:
        return "late_success"
    return "terminal_failed"


def _freeze(value):
    return tuple(value) if isinstance(value, list) else value


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    seq: int
    lease_id: str
    kind: str
    detail: MappingProxyType
    recorded_at: str


@dataclass(frozen=True, slots=True)
class LifecycleView:
    """Current stage (kind of the latest event) and the full event history of one lease."""

    lease_id: str
    stage: str | None
    events: tuple[LifecycleEvent, ...]

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(event.kind for event in self.events)

    @property
    def cancel_requested(self) -> bool:
        return "cancel_requested" in self.kinds

    @property
    def cancel_outcome(self) -> str | None:
        return next((kind for kind in self.kinds if kind in OUTCOME_KINDS), None)

    @property
    def teardown_complete(self) -> bool:
        return "teardown_complete" in self.kinds


class LifecycleController:
    """Cancel, settle and tear down one lease through injected ports.

    Ports (duck-typed):
    - job_port: interrupt(request, receipt) -> JobReceipt and
      observe(request, receipt) -> JobReceipt (used by the observer's poll).
    - worker_port: terminate(worker_id), cleanup(worker_id).
    - effect_port: unresolved(vm_attempt_id) -> sequence of open tool effects.
    - evidence_port: verify(artifact_id) -> object with artifact_id/status.
    """

    def __init__(self, gate: LifecycleGate, lease_manager, observer, workers: WorkerRegistry, *,
                 store: RuntimeStore, job_port, worker_port, effect_port, evidence_port, clock=None):
        if not isinstance(gate, LifecycleGate):
            raise LifecycleError("INVALID_GATE")
        if isinstance(lease_manager, GatedLeaseManager):
            lease_manager = lease_manager.lease_manager
        if not isinstance(lease_manager, LeaseManager):
            raise LifecycleError("INVALID_LEASE_MANAGER")
        if not (callable(getattr(observer, "poll", None)) and callable(getattr(observer, "snapshot", None))):
            raise LifecycleError("INVALID_OBSERVER")
        if not isinstance(workers, WorkerRegistry):
            raise LifecycleError("INVALID_WORKER_REGISTRY")
        if not isinstance(store, RuntimeStore):
            raise LifecycleError("INVALID_RUNTIME_STORE")
        for port, names, code in (
            (job_port, ("interrupt", "observe"), "INVALID_JOB_PORT"),
            (worker_port, ("terminate", "cleanup"), "INVALID_WORKER_PORT"),
            (effect_port, ("unresolved",), "INVALID_EFFECT_PORT"),
            (evidence_port, ("verify",), "INVALID_EVIDENCE_PORT"),
        ):
            if not all(callable(getattr(port, name, None)) for name in names):
                raise LifecycleError(code)
        if clock is not None and not callable(clock):
            raise LifecycleError("INVALID_CLOCK")
        self._gate = gate
        self._db = gate._db
        self._leases = lease_manager
        self._observer = observer
        self._workers = workers
        self._store = store
        self._job = job_port
        self._worker = worker_port
        self._effects = effect_port
        self._evidence = evidence_port
        self._clock = clock

    # ---------------------------------------------------------------- events
    def _record(self, lease_id: str, kind: str, **detail) -> None:
        if kind not in EVENT_KINDS:
            raise LifecycleError("INVALID_EVENT_KIND")
        clean = {}
        for key, value in detail.items():
            if isinstance(value, tuple):
                if not all(type(item) is str for item in value):
                    raise LifecycleError("INVALID_EVENT_DETAIL")
                value = list(value)
            elif value is not None and type(value) not in (str, int, bool):
                raise LifecycleError("INVALID_EVENT_DETAIL")
            clean[key] = value
        text = json.dumps(clean, sort_keys=True, separators=(",", ":"))
        recorded_at = timestamp(self._clock)
        with self._db.transaction() as connection:
            connection.execute(
                'INSERT INTO "lifecycle_events" ("lease_id", "kind", "detail", "recorded_at") VALUES (?, ?, ?, ?)',
                (lease_id, kind, text, recorded_at),
            )

    def view(self, lease_id: str) -> LifecycleView:
        try:
            identifier(lease_id, "lease_id")
        except InvalidRecord:
            raise LeaseError("INVALID_LEASE_ID") from None
        with self._db.transaction() as connection:
            rows = connection.execute(
                'SELECT "seq", "lease_id", "kind", "detail", "recorded_at" FROM "lifecycle_events" '
                'WHERE "lease_id" = ? ORDER BY "seq"', (lease_id,),
            ).fetchall()
        events = []
        for seq, owner, kind, detail, recorded_at in rows:
            try:
                decoded = json.loads(detail)
                if type(decoded) is not dict:
                    raise ValueError
            except ValueError:
                raise InvalidDatabase("A stored lifecycle event is damaged.") from None
            events.append(LifecycleEvent(seq, owner, kind,
                                         MappingProxyType({k: _freeze(v) for k, v in decoded.items()}),
                                         recorded_at))
        return LifecycleView(lease_id, events[-1].kind if events else None, tuple(events))

    # --------------------------------------------------------------- helpers
    def _lease(self, lease_id: object, fencing_token: object) -> Lease:
        try:
            identifier(lease_id, "lease_id")
        except InvalidRecord:
            raise LeaseError("INVALID_LEASE_ID") from None
        if type(fencing_token) is not int or not 1 <= fencing_token <= MAX_INTEGER:
            raise LeaseError("INVALID_FENCING_TOKEN")
        entry = self._store.get(Lease, lease_id)
        if entry is None:
            raise UnknownLease("UNKNOWN_LEASE")
        if entry.value.fencing_token != fencing_token:
            raise StaleLease("STALE_FENCING_TOKEN")
        return entry.value

    def _observation(self, lease: Lease):
        try:
            observation = self._observer.snapshot(lease.id)
        except UnknownObservation:
            return None
        except Exception:
            self._record(lease.id, "observation_unavailable", code="OBSERVATION_UNAVAILABLE")
            raise LifecycleError("OBSERVATION_UNAVAILABLE") from None
        if observation.provider_job_id != lease.provider_job_id:
            self._record(lease.id, "observation_unavailable", code="OBSERVATION_MISMATCH")
            raise LifecycleError("OBSERVATION_MISMATCH")
        return observation

    def _record_outcome(self, lease_id: str, state) -> None:
        self._record(lease_id, _outcome_kind(state), job_state=str(state))

    def _return_terminal(self, lease_id: str, fencing_token: int) -> None:
        """Release after an observed terminal provider state; never before."""
        lease = self._lease(lease_id, fencing_token)
        if lease.state in _LIVE:
            self._leases.release(lease_id, fencing_token, dispatched=True, provider_terminal=True)
        elif lease.state == "unknown":
            self._leases.resolve_unknown(lease_id, fencing_token, provider_terminal=True)
        else:
            return
        self._record(lease_id, "lease_released", lease_state=lease.state)

    def _drain(self, lease_id: str, fencing_token: int) -> None:
        lease = self._lease(lease_id, fencing_token)
        if lease.state in _LIVE:
            if lease.state != "draining":
                self._leases.release(lease_id, fencing_token, dispatched=True, provider_terminal=False)
            self._record(lease_id, "lease_draining")
        elif lease.state == "unknown":
            self._record(lease_id, "lease_unknown_held")

    def _interrupt(self, lease: Lease, request: JobRequest, receipt: JobReceipt) -> None:
        try:
            answer = require_job(self._job.interrupt(request, receipt), request)
            if type(answer) is not JobReceipt or answer.provider_job_id != lease.provider_job_id:
                raise LifecycleError("INTERRUPT_RECEIPT_MISMATCH")
        except Exception:
            # The interrupt may or may not have reached the provider.
            self._record(lease.id, "cancel_interrupt_unknown", provider_job_id=lease.provider_job_id)
            return
        # An interrupt answer is an acknowledgement, not an observed termination.
        self._record(lease.id, "cancel_interrupt_sent", provider_job_id=lease.provider_job_id,
                     reported_state=str(answer.state))

    def _holding_others(self, lease: Lease) -> int:
        count = 0
        after = None
        while True:
            page = self._store.list(Lease, _PAGE, after)
            for entry in page:
                other = entry.value
                if other.id != lease.id and other.worker_id == lease.worker_id and other.state in HOLDING_STATES:
                    count += 1
            if len(page) < _PAGE:
                return count
            after = page[-1].value.id

    # ---------------------------------------------------------------- cancel
    def cancel(self, lease_id: str, fencing_token: int, *, capabilities: AdapterCapabilities,
               request: JobRequest | None = None, receipt: JobReceipt | None = None) -> LifecycleView:
        """Request cancellation. The lease drains; it is released only after observed termination."""
        lease = self._lease(lease_id, fencing_token)
        supported = interrupt_supported(capabilities)
        if request is not None and type(request) is not JobRequest:
            raise LifecycleError("INVALID_JOB_REQUEST")
        if receipt is not None and type(receipt) is not JobReceipt:
            raise LifecycleError("INVALID_JOB_RECEIPT")
        if lease.state not in HOLDING_STATES:
            raise LifecycleError("LEASE_NOT_ACTIVE")
        if self.view(lease_id).cancel_outcome is not None:
            return self.view(lease_id)
        dispatched = lease.provider_job_id is not None or lease.state != "reserved"
        self._record(lease_id, "cancel_requested", lease_state=lease.state, dispatched=dispatched,
                     interrupt="supported" if supported else "unsupported")
        if not dispatched:
            self._leases.release(lease_id, fencing_token, dispatched=False, provider_terminal=False)
            self._record(lease_id, "cancel_before_dispatch")
            self._record(lease_id, "lease_released", lease_state=lease.state)
            return self.view(lease_id)
        if lease.provider_job_id is not None:
            observation = self._observation(lease)
            if observation is not None and observation.terminal:
                # The result arrived just before the cancel request.
                self._record_outcome(lease_id, observation.job_state)
                self._return_terminal(lease_id, fencing_token)
                return self.view(lease_id)
        if lease.provider_job_id is None:
            self._record(lease_id, "cancel_dispatch_unconfirmed")
        elif not supported:
            self._record(lease_id, "cancel_unsupported", provider_job_id=lease.provider_job_id)
        elif request is None or receipt is None or receipt.provider_job_id != lease.provider_job_id:
            self._record(lease_id, "cancel_interrupt_unavailable", code="JOB_HANDLE_UNAVAILABLE")
        else:
            self._interrupt(lease, request, receipt)
        self._drain(lease_id, fencing_token)
        return self.view(lease_id)

    def settle_cancel(self, lease_id: str, fencing_token: int) -> LifecycleView:
        """Observe the provider; record confirmation vs late success and release on a terminal state."""
        lease = self._lease(lease_id, fencing_token)
        current = self.view(lease_id)
        if not current.cancel_requested:
            raise LifecycleError("CANCEL_NOT_REQUESTED")
        if current.cancel_outcome is not None or lease.provider_job_id is None:
            return current
        if self._observation(lease) is None:
            return current
        try:
            observation = self._observer.poll(lease_id, self._job)
        except Exception:
            self._record(lease_id, "observation_unavailable", code="POLL_FAILED")
            raise LifecycleError("OBSERVATION_UNAVAILABLE") from None
        if observation.provider_job_id != lease.provider_job_id:
            self._record(lease_id, "observation_unavailable", code="OBSERVATION_MISMATCH")
            raise LifecycleError("OBSERVATION_MISMATCH")
        if not observation.terminal:
            return self.view(lease_id)
        self._record_outcome(lease_id, observation.job_state)
        self._return_terminal(lease_id, fencing_token)
        return self.view(lease_id)

    # -------------------------------------------------------------- teardown
    def teardown(self, lease_id: str, fencing_token: int, *, required_artifacts: tuple[str, ...]) -> LifecycleView:
        """Return the lease and worker resources only when effects, evidence and termination are settled."""
        lease = self._lease(lease_id, fencing_token)
        if type(required_artifacts) is not tuple or len(required_artifacts) > _MAX_ARTIFACTS:
            raise LifecycleError("INVALID_REQUIRED_ARTIFACTS")
        for artifact_id in required_artifacts:
            try:
                identifier(artifact_id, "artifact_id")
            except InvalidRecord:
                raise LifecycleError("INVALID_REQUIRED_ARTIFACTS") from None
        if len(set(required_artifacts)) != len(required_artifacts):
            raise LifecycleError("INVALID_REQUIRED_ARTIFACTS")
        history = self.view(lease_id)
        if history.teardown_complete:
            return history
        worker = self._workers.get(lease.worker_id)
        if worker is None or worker.pool_id != lease.pool_id:
            self._record(lease_id, "worker_unregistered",
                         code="WORKER_UNREGISTERED" if worker is None else "WORKER_POOL_MISMATCH")
            return self.view(lease_id)
        placement = self._store.get(Placement, lease.placement_id)
        if placement is None:
            raise LifecycleError("PLACEMENT_UNKNOWN")
        attempt = placement.value.vm_attempt_id

        # (1) Tool effects in flight, unknown or waiting for a late receipt.
        try:
            open_effects = len(tuple(self._effects.unresolved(attempt)))
        except Exception:
            self._record(lease_id, "awaiting_effect_resolution", code="EFFECT_STATUS_UNAVAILABLE")
            return self.view(lease_id)
        if open_effects:
            self._record(lease_id, "awaiting_effect_resolution", code="EFFECTS_UNRESOLVED", count=open_effects)
            return self.view(lease_id)

        # (2) Required evidence must be durably stored and intact.
        missing = []
        for artifact_id in required_artifacts:
            try:
                result = self._evidence.verify(artifact_id)
                intact = (getattr(result, "artifact_id", None) == artifact_id
                          and getattr(result, "status", None) == "intact")
            except Exception:
                intact = False
            if not intact:
                missing.append(artifact_id)
        if missing:
            self._record(lease_id, "awaiting_evidence_handoff", artifacts=tuple(missing))
            return self.view(lease_id)

        # (3) A dispatched job must be observed terminal.
        if lease.provider_job_id is not None:
            observation = self._observation(lease)
            if observation is None or not observation.terminal:
                self._record(lease_id, "awaiting_provider_terminal", code="PROVIDER_NOT_TERMINAL")
                return self.view(lease_id)
        elif lease.state in ("active", "draining", "unknown"):
            # Dispatch intent without a receipt: termination cannot be confirmed.
            self._record(lease_id, "awaiting_provider_terminal", code="DISPATCH_UNCONFIRMED")
            return self.view(lease_id)

        # (4) Return the lease, then worker resources.
        if lease.state == "reserved" and lease.provider_job_id is None:
            self._leases.release(lease_id, fencing_token, dispatched=False, provider_terminal=False)
            self._record(lease_id, "lease_released", lease_state=lease.state)
        else:
            self._return_terminal(lease_id, fencing_token)
        lease = self._lease(lease_id, fencing_token)
        others = self._holding_others(lease)
        if others:
            self._record(lease_id, "worker_in_use", count=others)
            return self.view(lease_id)
        kinds = self.view(lease_id).kinds
        if worker.ownership == "owned":
            if "worker_terminated" not in kinds:
                try:
                    self._worker.terminate(worker.worker_id)
                except Exception:
                    self._record(lease_id, "worker_terminate_failed", worker_id=worker.worker_id)
                    return self.view(lease_id)
                self._record(lease_id, "worker_terminated", worker_id=worker.worker_id)
        elif "worker_returned" not in kinds:
            # A borrowed host is never terminated; its use right ends with the lease.
            self._record(lease_id, "worker_returned", worker_id=worker.worker_id)
        if "worker_cleaned" not in kinds:
            try:
                self._worker.cleanup(worker.worker_id)
            except Exception:
                self._record(lease_id, "worker_cleanup_failed", worker_id=worker.worker_id)
                return self.view(lease_id)
            self._record(lease_id, "worker_cleaned", worker_id=worker.worker_id)
        self._record(lease_id, "teardown_complete", ownership=worker.ownership)
        return self.view(lease_id)
