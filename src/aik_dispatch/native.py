"""Native dispatch: lease dispatch intent, one provider start, acknowledgement and observed start.

This bridge does not replace VM's runtime or aik_vm_bridge's hosted runtime.
It records which already reserved Attempt was sent through which connection.
A ``known_failure`` from the provider guard means nothing was started: it is
recorded as ``rejected`` and the lease is returned without a dispatch. Any
other exception or a wrong-connection answer from ``start_job`` is an unknown
effect: it is recorded as ``send_unknown`` and never resent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from aik_provider.contracts import TERMINAL, AdapterFailure, JobReceipt, JobRequest, JobState, Outcome
from aik_runtime.errors import InvalidRecord
from aik_runtime.models import identifier

from .errors import DispatchError, DispatchOutcomeUnknown, DispatchRejected
from .identity import DispatchIdentity
from .journal import DispatchJournal, DispatchRecord


STARTED_JOB_STATES = frozenset(TERMINAL) | frozenset(state for state in JobState if state.name == "RUNNING")


def _value(state) -> str:
    return str(state.value) if isinstance(state, Enum) else str(state)


def _receipt_matches(receipt: object, identity: DispatchIdentity) -> bool:
    if not isinstance(receipt, JobReceipt):
        return False
    binding = identity.binding
    if (receipt.request_id != identity.request_id or receipt.connection_id != binding.connection_id
            or receipt.revision != binding.revision or receipt.account_fingerprint != binding.account_fingerprint):
        return False
    try:
        identifier(receipt.provider_job_id, "provider_job_id")
    except InvalidRecord:
        return False
    return True


class NativeDispatcher:
    """Dispatch a VM-reserved Attempt through one exact provider connection.

    ``adapter`` is an ``aik_provider.GuardedAdapter``; it must be the same
    instance (same connection memory) for ``dispatch`` and ``confirm_started``.
    """

    def __init__(self, journal: DispatchJournal, lease_manager, adapter, observer, *, clock=None):
        if not isinstance(journal, DispatchJournal):
            raise TypeError("A DispatchJournal is required.")
        self._journal = journal
        self._leases = lease_manager
        self._adapter = adapter
        self._observer = observer
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _stamp(self) -> str:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise DispatchError("INVALID_STATE", subject="clock")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _check_request(identity: object, job_request: object) -> DispatchIdentity:
        if type(identity) is not DispatchIdentity or identity.path != "native":
            raise DispatchError("INVALID_STATE", subject="path")
        if not isinstance(job_request, JobRequest) or (
            job_request.request_id != identity.request_id
            or job_request.vm_attempt_id != identity.vm_attempt_id
            or job_request.route_id != identity.route_id
            or job_request.binding != identity.binding
        ):
            raise DispatchError("IDENTITY_MISMATCH", subject="job_request")
        return identity

    def dispatch(self, identity: DispatchIdentity, job_request: JobRequest, *, requested_model: str,
                 requested_effort: str | None = None) -> DispatchRecord:
        identity = self._check_request(identity, job_request)
        request_id = identity.request_id
        existing = self._journal.find(request_id)
        if existing is not None:
            if existing.identity.identity_digest != identity.identity_digest:
                raise DispatchError("CONFLICT", subject="request_id")
            # Duplicate dispatch: never call the provider again, including after send_unknown.
            return existing

        # Dispatch intent on the lease and in the journal, both before the provider call.
        self._leases.activate(identity.lease_id, identity.fencing_token)
        self._journal.reserve(identity, {
            "lease_id": identity.lease_id, "fencing_token": identity.fencing_token,
            "contract_digest": identity.contract_digest, "connection_id": identity.binding.connection_id,
        })

        try:
            bound = self._adapter.start_job(job_request)
        except AdapterFailure as error:
            if error.fault.outcome is not Outcome.KNOWN_FAILURE:
                self._journal.append_dispatch_event(request_id, "send_unknown",
                                                    {"error_class": type(error).__name__})
                raise DispatchOutcomeUnknown(subject="start_job") from None
            # A declared known failure: the provider contract says no job was started.
            self._journal.append_dispatch_event(request_id, "rejected", {"fault_kind": error.fault.kind.value})
            self._leases.release(identity.lease_id, identity.fencing_token, dispatched=False,
                                 provider_terminal=False)
            raise DispatchRejected(subject="start_job") from None
        except Exception as error:
            self._journal.append_dispatch_event(request_id, "send_unknown", {"error_class": type(error).__name__})
            raise DispatchOutcomeUnknown(subject="start_job") from None

        receipt = getattr(bound, "receipt", None)
        if not _receipt_matches(receipt, identity):
            self._journal.append_dispatch_event(request_id, "send_unknown", {"reason": "receipt_binding_mismatch"})
            raise DispatchOutcomeUnknown(subject="receipt")

        job_id = receipt.provider_job_id
        failure = None
        try:
            self._leases.activate(identity.lease_id, identity.fencing_token, provider_job_id=job_id)
            self._observer.start(
                identity.lease_id, vm_attempt_id=identity.vm_attempt_id, provider_job_id=job_id,
                requested_model=requested_model, requested_effort=requested_effort,
                request=job_request, receipt=receipt,
            )
        except Exception as error:  # the provider already acknowledged; record that before failing
            failure = error
        self._journal.append_dispatch_event(request_id, "sent_ack", {
            "provider_job_id": job_id, "receipt_state": _value(receipt.state),
            "acknowledged_at": self._stamp(),
            "bookkeeping": "ok" if failure is None else "failed:" + type(failure).__name__,
        })
        if failure is not None:
            raise failure
        return self._journal.record(request_id)

    def confirm_started(self, request_id: str) -> DispatchRecord:
        record = self._journal.record(request_id)
        if record.state != "sent_ack":
            return record  # reserved/send_unknown/started: the provider is not asked
        bound = self._adapter.observe_job(request_id)
        receipt = getattr(bound, "receipt", None)
        if not _receipt_matches(receipt, record.identity) or receipt.provider_job_id != record.provider_job_id:
            raise DispatchError("IDENTITY_MISMATCH", subject="observed_receipt")
        if receipt.state in STARTED_JOB_STATES:
            return self._journal.append_dispatch_event(request_id, "started", {"observed_state": _value(receipt.state)})
        return record

    def recover(self) -> tuple:
        """After a restart: a ``reserved`` record may have been sent; mark it ``send_unknown``.

        Returns the records changed by this call. Nothing is resent.
        """
        changed = []
        for request_id in self._journal.request_ids_in_state("reserved"):
            try:
                changed.append(self._journal.append_dispatch_event(
                    request_id, "send_unknown", {"reason": "recovered_after_restart"},
                ))
            except DispatchError as error:
                if error.code != "INVALID_STATE":
                    raise
        return tuple(changed)

    def record(self, request_id: str) -> DispatchRecord:
        return self._journal.record(request_id)
