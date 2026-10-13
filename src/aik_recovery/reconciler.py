"""AIK-039 recovery reconciler: observe first, never relaunch, journal every decision.

Leases (aik_runtime.Lease via aik_lease.LeaseManager):
- provider job recorded: observe it again through observer.poll with the stored
  request/receipt (same connection and account). terminal -> release (or
  resolve_unknown); running -> keep_running; observation failed -> lease
  stays/becomes unknown, no relaunch.
- reserved without dispatch: release(dispatched=False) only when VM reports the
  attempt terminal or absent; otherwise await_vm.
- active/draining/unknown without a provider job ID (dispatch intent only):
  unknown, handed to VM's unknown contract.

Effects (effect_port.inflight()):
- no receipt: effect_port.mark_unknown, never re-executed (non_idempotent halts
  until confirmed).
- receipt, result not returned, VM attempt active: effect_port.resend_result
  (the same result; the effect is not executed again).
- receipt after cancel / finished attempt: recorded only.

Mutating actions write a "planned" row, apply, then write "applied" or
"failed". No-op decisions are skipped when the identical decision is already
the latest journal row of the subject.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timezone
import re

from aik_lease import LeaseError, LeaseManager
from aik_observer import UnknownObservation
from aik_runtime import Lease, Placement, RuntimeStore, StorageError, Versioned

from .journal import RecoveryJournal, decision_fingerprint
from .models import VM_STATES, EffectRef, RecoveryError, RecoveryReport


LEASE_SCAN_STATES = ("reserved", "active", "draining", "unknown")
_LIVE = ("reserved", "active", "draining")
_PAGE = 1000
_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,63}\Z")
_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_IDEMPOTENCY_REASON = {
    "non_idempotent": "NON_IDEMPOTENT_HALT_UNTIL_CONFIRMED",
    "idempotent": "IDEMPOTENT_EFFECT_OUTCOME_UNKNOWN",
    "read_only": "READ_ONLY_EFFECT_OUTCOME_UNKNOWN",
}


def _failure_code(error: BaseException) -> str:
    if isinstance(error, RecoveryError):
        return error.code
    if isinstance(error, LeaseError):
        text = str(error)
        return text if _CODE.fullmatch(text) else "LEASE_TRANSITION_FAILED"
    if isinstance(error, StorageError):
        return "STORAGE_FAILURE"
    return "ACTION_FAILED"


def _safe_value(value: object) -> str:
    text = str(value)
    return text if _VALUE.fullmatch(text) else "UNRECOGNIZED"


class _Run:
    def __init__(self):
        self.decisions = []
        self.unchanged = []
        self.failures = []


class RecoveryReconciler:
    """Call reconcile() once after a restart, before any new dispatch.

    lease_manager must manage the same runtime store. Only one reconciler should
    run at a time; concurrent reconcilers are not coordinated here.
    """

    def __init__(self, runtime_store: RuntimeStore, lease_manager: LeaseManager, observer, *,
                 vm_port, job_port, effect_port, journal: RecoveryJournal, clock=None):
        if not isinstance(runtime_store, RuntimeStore):
            raise RecoveryError("INVALID_RUNTIME_STORE")
        if not isinstance(lease_manager, LeaseManager):
            raise RecoveryError("INVALID_LEASE_MANAGER")
        if not callable(getattr(observer, "poll", None)):
            raise RecoveryError("INVALID_OBSERVER")
        if not callable(getattr(vm_port, "attempt_state", None)):
            raise RecoveryError("INVALID_VM_PORT")
        if not callable(getattr(job_port, "observe", None)):
            raise RecoveryError("INVALID_JOB_PORT")
        for name in ("inflight", "mark_unknown", "resend_result"):
            if not callable(getattr(effect_port, name, None)):
                raise RecoveryError("INVALID_EFFECT_PORT")
        if not isinstance(journal, RecoveryJournal):
            raise RecoveryError("INVALID_JOURNAL")
        if clock is not None and not callable(clock):
            raise RecoveryError("INVALID_CLOCK")
        self._store = runtime_store
        self._leases = lease_manager
        self._observer = observer
        self._vm = vm_port
        self._jobs = job_port
        self._effects = effect_port
        self._journal = journal
        self._clock = clock

    @property
    def journal(self) -> RecoveryJournal:
        return self._journal

    # ------------------------------------------------------------ helpers
    def _now(self) -> str:
        if self._clock is None:
            value = datetime.now(timezone.utc)
        else:
            try:
                value = self._clock()
            except Exception:
                raise RecoveryError("CLOCK_UNAVAILABLE") from None
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise RecoveryError("INVALID_CLOCK")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _vm_state(self, vm_attempt_id: str) -> str:
        try:
            state = self._vm.attempt_state(vm_attempt_id)
        except Exception:
            return "unavailable"
        return state if type(state) is str and state in VM_STATES else "unavailable"

    def _placement(self, placement_id: str):
        try:
            with self._store.transaction() as tx:
                entry = tx.get(Placement, placement_id)
        except StorageError:
            return None
        return None if entry is None else entry.value

    def _scan_leases(self) -> list:
        result = []
        after = None
        while True:
            with self._store.transaction() as tx:
                page = tx.list(Lease, _PAGE, after)
            result.extend(entry for entry in page if entry.value.state in LEASE_SCAN_STATES)
            if len(page) < _PAGE:
                return result
            after = page[-1].value.id

    def _record(self, run, kind, subject, action, outcome, reasons, observed, refs, *, dedupe):
        reasons = tuple(reasons)
        refs = tuple(dict.fromkeys(refs))
        key = f"{kind}:{subject}"
        if dedupe:
            fingerprint = decision_fingerprint(kind, subject, action, outcome, reasons, observed, refs)
            last = self._journal.last(kind, subject)
            if last is not None and last.fingerprint == fingerprint:
                run.unchanged.append(key)
                return None
        decision = self._journal.append(
            subject_kind=kind, subject_id=subject, action=action, outcome=outcome, reasons=reasons,
            observed=observed, evidence_refs=refs, decided_at=self._now(),
        )
        run.decisions.append(decision)
        if outcome == "failed":
            run.failures.append(key)
        return decision

    def _noop(self, run, kind, subject, action, reasons, observed, refs):
        self._record(run, kind, subject, action, "recorded", reasons, observed, refs, dedupe=True)

    def _apply(self, run, kind, subject, action, reasons, observed, refs, operation):
        # Write-ahead: a journal failure here stops before any state change.
        self._record(run, kind, subject, action, "planned", reasons, observed, refs, dedupe=False)
        try:
            result = operation()
        except Exception as error:
            self._record(run, kind, subject, action, "failed", (*reasons, _failure_code(error)),
                         observed, refs, dedupe=False)
            return None
        after = dict(observed)
        if isinstance(result, Versioned):
            after["result_state"] = result.value.state
            after["result_revision"] = result.revision
        self._record(run, kind, subject, action, "applied", reasons, after, refs, dedupe=False)
        return result

    def _mark_lease_unknown(self, entry: Versioned) -> Versioned:
        """Live -> unknown through LeaseManager, only if unchanged since it was read.

        unknown is a holding state: capacity and budget stay held; never a release.
        """
        lease = entry.value
        return self._leases.mark_unknown(lease.id, lease.fencing_token, expected_revision=entry.revision)

    def _observe(self, lease: Lease, placement):
        try:
            view = self._observer.poll(lease.id, self._jobs)
        except UnknownObservation:
            return None, "OBSERVATION_RECORD_MISSING"
        except Exception:
            return None, "PROVIDER_OBSERVATION_FAILED"
        if getattr(view, "provider_job_id", None) != lease.provider_job_id or getattr(view, "lease_id", None) != lease.id:
            return None, "PROVIDER_JOB_IDENTITY_MISMATCH"
        if placement is not None and getattr(view, "vm_attempt_id", None) != placement.vm_attempt_id:
            return None, "VM_ATTEMPT_IDENTITY_MISMATCH"
        if type(getattr(view, "terminal", None)) is not bool:
            return None, "PROVIDER_OBSERVATION_INVALID"
        return view, None

    # --------------------------------------------------------------- leases
    def _reconcile_lease(self, run, entry: Versioned) -> None:
        lease = entry.value
        subject = lease.id
        placement = self._placement(lease.placement_id)
        attempt = None if placement is None else placement.vm_attempt_id
        vm_state = "unavailable" if attempt is None else self._vm_state(attempt)
        refs = [lease.id, lease.placement_id]
        if attempt is not None:
            refs.append(attempt)
        if lease.provider_job_id is not None:
            refs.append(lease.provider_job_id)
        observed = {
            "lease_state": lease.state, "lease_revision": entry.revision,
            "fencing_token": lease.fencing_token, "vm_state": vm_state, "job_state": None,
        }
        base = [] if placement is not None else ["PLACEMENT_UNAVAILABLE"]
        vm_reason = "VM_ATTEMPT_" + vm_state.upper()
        token = lease.fencing_token

        if lease.provider_job_id is not None:
            view, failure = self._observe(lease, placement)
            if view is None:
                reasons = [*base, failure, "NO_RELAUNCH", vm_reason]
                if lease.state == "unknown":
                    self._noop(run, "lease", subject, "observe_failed_keep_unknown", reasons, observed, refs)
                else:
                    self._apply(run, "lease", subject, "observe_failed_keep_unknown", reasons, observed, refs,
                                lambda: self._mark_lease_unknown(entry))
                return
            observed["job_state"] = _safe_value(view.job_state)
            if view.terminal:
                reasons = [*base, "PROVIDER_JOB_TERMINAL_OBSERVED", vm_reason]
                if lease.state == "unknown":
                    operation = lambda: self._leases.resolve_unknown(lease.id, token, provider_terminal=True)
                else:
                    operation = lambda: self._leases.release(lease.id, token, dispatched=True,
                                                             provider_terminal=True)
                self._apply(run, "lease", subject, "release_provider_terminal", reasons, observed, refs, operation)
            else:
                reasons = [*base, "PROVIDER_JOB_RUNNING", vm_reason]
                if vm_state in ("terminal", "absent"):
                    reasons.append("VM_ATTEMPT_NOT_ACTIVE_JOB_RUNNING")
                self._noop(run, "lease", subject, "keep_running", reasons, observed, refs)
            return

        if lease.state == "reserved":
            reasons = [*base, "NO_DISPATCH_RECORDED", vm_reason]
            if vm_state in ("terminal", "absent"):
                self._apply(run, "lease", subject, "release_undispatched", reasons, observed, refs,
                            lambda: self._leases.release(lease.id, token, dispatched=False,
                                                         provider_terminal=False))
            else:
                self._noop(run, "lease", subject, "await_vm", reasons, observed, refs)
            return

        reasons = [*base, "DISPATCH_INTENT_WITHOUT_PROVIDER_JOB_ID", "NO_RELAUNCH",
                   "VM_UNKNOWN_CONTRACT_REQUIRED", vm_reason]
        if lease.state == "unknown":
            self._noop(run, "lease", subject, "handoff_vm_unknown", reasons, observed, refs)
        else:
            self._apply(run, "lease", subject, "handoff_vm_unknown", reasons, observed, refs,
                        lambda: self._mark_lease_unknown(entry))

    # -------------------------------------------------------------- effects
    def _port_action(self, run, ref: EffectRef, action, reasons, observed, refs, call) -> None:
        subject = ref.subject_id
        if self._journal.has_applied("effect", subject, action):
            run.unchanged.append(f"effect:{subject}")
            return
        self._apply(run, "effect", subject, action, reasons, observed, refs, lambda: call(ref))

    def _reconcile_effect(self, run, ref: EffectRef) -> None:
        vm_state = self._vm_state(ref.vm_attempt_id)
        observed = {
            "has_receipt": ref.has_receipt, "model_result_returned": ref.model_result_returned,
            "idempotency": ref.idempotency, "cancelled": ref.cancelled, "vm_state": vm_state,
        }
        refs = [ref.vm_attempt_id, ref.tool_call_id]
        if ref.receipt_ref is not None:
            refs.append(ref.receipt_ref)
        vm_reason = "VM_ATTEMPT_" + vm_state.upper()
        subject = ref.subject_id

        if not ref.has_receipt:
            reasons = ["EFFECT_RECEIPT_MISSING", "NO_AUTOMATIC_REEXECUTION", _IDEMPOTENCY_REASON[ref.idempotency],
                       "VM_UNKNOWN_CONTRACT_REQUIRED", vm_reason]
            if ref.cancelled:
                reasons.append("ATTEMPT_CANCELLED")
            self._port_action(run, ref, "handoff_vm_unknown", reasons, observed, refs, self._effects.mark_unknown)
        elif ref.model_result_returned:
            self._noop(run, "effect", subject, "no_action_result_delivered",
                       ["RECEIPT_RECORDED", "RESULT_ALREADY_RETURNED", vm_reason], observed, refs)
        elif ref.cancelled or vm_state in ("terminal", "absent"):
            reasons = ["RECEIPT_RECORDED", "RESULT_NOT_RETURNED", "NO_AUTOMATIC_REEXECUTION", vm_reason]
            reasons.append("LATE_RECEIPT_AFTER_CANCEL" if ref.cancelled else "VM_ATTEMPT_NOT_ACTIVE")
            self._noop(run, "effect", subject, "record_late_receipt", reasons, observed, refs)
        elif vm_state == "active":
            reasons = ["RECEIPT_RECORDED", "RESULT_NOT_RETURNED", "RESEND_SAME_RESULT", "EFFECT_NOT_REEXECUTED",
                       vm_reason]
            self._port_action(run, ref, "resend_result", reasons, observed, refs, self._effects.resend_result)
        else:
            self._noop(run, "effect", subject, "await_vm",
                       ["RECEIPT_RECORDED", "RESULT_NOT_RETURNED", "NO_AUTOMATIC_REEXECUTION", vm_reason],
                       observed, refs)

    def _reconcile_effects(self, run) -> None:
        try:
            items = tuple(self._effects.inflight())
        except Exception:
            self._record(run, "scan", "effect-inflight", "scan_failed", "failed",
                         ("EFFECT_INFLIGHT_UNAVAILABLE",), {}, (), dedupe=False)
            return
        for item in items:
            try:
                if type(item) is not EffectRef:
                    raise RecoveryError("INVALID_EFFECT_REF")
                # Rebuild so object.__setattr__ forgeries are validated again.
                ref = EffectRef(*(getattr(item, field.name) for field in fields(EffectRef)))
            except (RecoveryError, AttributeError, TypeError):
                self._record(run, "scan", "effect-inflight", "scan_failed", "failed",
                             ("INVALID_EFFECT_REF",), {}, (), dedupe=False)
                continue
            self._reconcile_effect(run, ref)

    # --------------------------------------------------------------- public
    def reconcile(self) -> RecoveryReport:
        """Reconcile every holding lease and in-flight effect once; never relaunch."""
        run = _Run()
        for entry in self._scan_leases():
            self._reconcile_lease(run, entry)
        self._reconcile_effects(run)
        return RecoveryReport(tuple(run.decisions), tuple(run.unchanged), tuple(run.failures))


__all__ = ["RecoveryReconciler", "LEASE_SCAN_STATES"]
