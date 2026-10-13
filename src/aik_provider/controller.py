"""AIK-011 guarded adapter boundary with connection/account pinning.

In-memory request fencing is intentionally not durable. AIK-033/035 must
persist the VM Attempt/route/lease identity and reconcile all in-flight work
after restart before permitting dispatch. No provider is invoked by this file.
"""
from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from .contracts import (
    AccountIsolation, AdapterCapabilities, AdapterFailure, AuthKind,
    AuthObservation, AuthState, ConnectionBinding, FaultKind, JobReceipt,
    JobRequest, JobState, Outcome, ProviderAdapter, ProviderFault, Support,
    TERMINAL, require_auth, require_caps, require_job,
)


def _fault(kind: FaultKind, outcome: Outcome = Outcome.KNOWN_FAILURE) -> AdapterFailure:
    return AdapterFailure(ProviderFault(kind, outcome))


@dataclass(frozen=True, slots=True)
class BoundJob:
    """An immutable snapshot; provider terminal never implies VM completion."""

    request: JobRequest
    receipt: JobReceipt


class GuardedAdapter:
    """Validate typed provider operations without pretending to grant execution rights.

    An authoritative VM/AGS authorization and AIK lease must be checked by
    the caller. A guarded adapter is only a non-secret local protocol boundary.
    """

    def __init__(self, provider: ProviderAdapter, *, authorize_dispatch=None):
        if authorize_dispatch is not None and not callable(authorize_dispatch):
            raise TypeError("An authoritative authorization callback is required")
        self._authorize_dispatch = authorize_dispatch
        for name in ("describe", "auth_status", "begin_sign_in", "sign_out",
                     "start_job", "observe_job", "interrupt_job"):
            if not callable(getattr(provider, name, None)):
                raise TypeError("ProviderAdapter methods required")
        self._provider = provider
        self._lock = RLock()
        self._jobs: dict[str, BoundJob] = {}
        self._attempted: set[str] = set()
        self._uncertain_hosts: set[tuple[str, str]] = set()

    @staticmethod
    def _host(binding: ConnectionBinding) -> tuple[str, str]:
        return binding.provider, binding.runtime

    def _capabilities(self, binding: ConnectionBinding) -> AdapterCapabilities:
        try:
            return require_caps(self._provider.describe(binding), binding)
        except AdapterFailure:
            raise
        except Exception as exc:
            raise _fault(FaultKind.PROVIDER_FAILURE) from None

    def capabilities(self, binding: ConnectionBinding) -> AdapterCapabilities:
        if type(binding) is not ConnectionBinding:
            raise _fault(FaultKind.INVALID_CONTRACT)
        with self._lock:
            return self._capabilities(binding)

    def _auth_observation(self, binding: ConnectionBinding) -> AuthObservation:
        try:
            observed = self._provider.auth_status(binding)
            if type(observed) is not AuthObservation:
                raise _fault(FaultKind.INVALID_CONTRACT)
            if observed.connection_id != binding.connection_id or observed.revision != binding.revision:
                raise _fault(FaultKind.STALE_CONNECTION)
            return observed
        except AdapterFailure:
            raise
        except Exception:
            raise _fault(FaultKind.PROVIDER_FAILURE) from None

    def auth_status(self, binding: ConnectionBinding) -> AuthObservation:
        with self._lock:
            caps = self._capabilities(binding)
            if caps.auth_status is not Support.SUPPORTED:
                raise _fault(FaultKind.UNSUPPORTED)
            return self._auth_observation(binding)

    def _checked_auth(self, binding: ConnectionBinding) -> None:
        if self._capabilities(binding).auth_status is not Support.SUPPORTED:
            raise _fault(FaultKind.UNSUPPORTED)
        require_auth(self._auth_observation(binding), binding)

    def _active_on_host(self, binding: ConnectionBinding) -> tuple[BoundJob, ...]:
        host = self._host(binding)
        return tuple(job for job in self._jobs.values()
                     if self._host(job.request.binding) == host and
                     job.receipt.state not in TERMINAL)

    def _check_account_conflict(self, binding: ConnectionBinding,
                                isolation: AccountIsolation) -> None:
        if self._host(binding) in self._uncertain_hosts:
            raise _fault(FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN)
        if binding.auth_kind is not AuthKind.NATIVE_SUBSCRIPTION:
            return  # separate API credentials are not native host accounts
        if isolation is AccountIsolation.UNKNOWN:
            raise _fault(FaultKind.ACCOUNT_CONFLICT)
        if isolation is AccountIsolation.SINGLE_ACTIVE:
            for job in self._active_on_host(binding):
                existing = job.request.binding
                if (existing.connection_id, existing.revision, existing.account_fingerprint) != (
                    binding.connection_id, binding.revision, binding.account_fingerprint
                ):
                    raise _fault(FaultKind.ACCOUNT_CONFLICT)

    def begin_sign_in(self, binding: ConnectionBinding) -> AuthObservation:
        with self._lock:
            caps = self._capabilities(binding)
            if binding.auth_kind is not AuthKind.NATIVE_SUBSCRIPTION or caps.sign_in is not Support.SUPPORTED:
                raise _fault(FaultKind.UNSUPPORTED)
            self._check_account_conflict(binding, caps.isolation)
            if (caps.isolation is not AccountIsolation.ISOLATED and
                    self._active_on_host(binding)):
                raise _fault(FaultKind.ACCOUNT_CONFLICT)
            try:
                observed = self._provider.begin_sign_in(binding)
                if type(observed) is not AuthObservation:
                    raise _fault(FaultKind.INVALID_CONTRACT)
                if observed.connection_id != binding.connection_id or observed.revision != binding.revision:
                    raise _fault(FaultKind.STALE_CONNECTION)
                if observed.state is AuthState.AUTHENTICATED:
                    require_auth(observed, binding)
                return observed
            except AdapterFailure as error:
                if error.fault.kind in (FaultKind.INVALID_CONTRACT, FaultKind.STALE_CONNECTION,
                                        FaultKind.ACCOUNT_CHANGED):
                    self._uncertain_hosts.add(self._host(binding))
                raise
            except Exception:
                # Login could have changed the host account; no silent fallback.
                self._uncertain_hosts.add(self._host(binding))
                raise _fault(FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN) from None

    def sign_out(self, binding: ConnectionBinding) -> AuthObservation:
        with self._lock:
            caps = self._capabilities(binding)
            if caps.sign_out is not Support.SUPPORTED:
                raise _fault(FaultKind.UNSUPPORTED)
            self._check_account_conflict(binding, caps.isolation)
            if any(job.request.binding == binding for job in self._active_on_host(binding)):
                raise _fault(FaultKind.ACCOUNT_CONFLICT)
            self._checked_auth(binding)
            try:
                observed = self._provider.sign_out(binding)
                if type(observed) is not AuthObservation:
                    raise _fault(FaultKind.INVALID_CONTRACT)
                if observed.connection_id != binding.connection_id or observed.revision != binding.revision:
                    raise _fault(FaultKind.STALE_CONNECTION)
                if observed.state is AuthState.AUTHENTICATED:
                    raise _fault(FaultKind.INVALID_CONTRACT)
                return observed
            except AdapterFailure as error:
                if error.fault.kind in (FaultKind.INVALID_CONTRACT, FaultKind.STALE_CONNECTION):
                    self._uncertain_hosts.add(self._host(binding))
                raise
            except Exception:
                self._uncertain_hosts.add(self._host(binding))
                raise _fault(FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN) from None

    def start_job(self, request: JobRequest) -> BoundJob:
        if type(request) is not JobRequest:
            raise _fault(FaultKind.INVALID_CONTRACT)
        binding = request.binding
        with self._lock:
            if request.request_id in self._attempted:
                raise _fault(FaultKind.DUPLICATE_REQUEST)
            caps = self._capabilities(binding)
            if caps.job_start is not Support.SUPPORTED:
                raise _fault(FaultKind.UNSUPPORTED)
            self._check_account_conflict(binding, caps.isolation)
            self._checked_auth(binding)
            # The VM/AGS/AIK authorization gate is external and must be wired
            # explicitly; metadata and account auth never confer run authority.
            if self._authorize_dispatch is None:
                raise _fault(FaultKind.ACCESS_DENIED)
            try:
                authorized = self._authorize_dispatch(request)
            except Exception:
                raise _fault(FaultKind.ACCESS_DENIED) from None
            if authorized is not True:
                raise _fault(FaultKind.ACCESS_DENIED)
            # The request ID is consumed before dispatch and never retried
            # automatically, even if the transport times out.
            self._attempted.add(request.request_id)
            try:
                receipt = require_job(self._provider.start_job(request), request)
                existing = {
                    (job.request.binding.provider, job.request.binding.runtime,
                     job.request.binding.connection_id, job.request.binding.revision,
                     job.receipt.provider_job_id)
                    for job in self._jobs.values()
                }
                current = (binding.provider, binding.runtime, binding.connection_id,
                           binding.revision, receipt.provider_job_id)
                if current in existing:
                    raise _fault(FaultKind.INVALID_RECEIPT, Outcome.UNKNOWN)
            except AdapterFailure as exc:
                if exc.fault.outcome is Outcome.UNKNOWN:
                    self._uncertain_hosts.add(self._host(binding))
                raise
            except Exception:
                self._uncertain_hosts.add(self._host(binding))
                raise _fault(FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN) from None
            job = BoundJob(request, receipt)
            self._jobs[request.request_id] = job
            return job

    def get(self, request_id: str) -> BoundJob:
        with self._lock:
            try:
                return self._jobs[request_id]
            except (KeyError, TypeError):
                raise _fault(FaultKind.INVALID_CONTRACT) from None

    def _advance(self, request_id: str, method: str) -> BoundJob:
        with self._lock:
            job = self.get(request_id)
            if job.receipt.state in TERMINAL:
                return job
            binding = job.request.binding
            caps = self._capabilities(binding)
            feature = caps.job_observe if method == "observe_job" else caps.job_interrupt
            if feature is not Support.SUPPORTED:
                raise _fault(FaultKind.UNSUPPORTED)
            self._checked_auth(binding)
            try:
                next_receipt = require_job(
                    getattr(self._provider, method)(job.request, job.receipt), job.request
                )
                if next_receipt.provider_job_id != job.receipt.provider_job_id:
                    raise _fault(FaultKind.INVALID_RECEIPT, Outcome.UNKNOWN)
            except AdapterFailure as exc:
                if exc.fault.outcome is Outcome.UNKNOWN and method == "interrupt_job":
                    self._uncertain_hosts.add(self._host(binding))
                raise
            except Exception:
                if method == "interrupt_job":
                    self._uncertain_hosts.add(self._host(binding))
                raise _fault(FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN) from None
            # Merely accepting cancel/interrupt is not a terminal observation.
            updated = BoundJob(job.request, next_receipt)
            self._jobs[request_id] = updated
            return updated

    def observe_job(self, request_id: str) -> BoundJob:
        return self._advance(request_id, "observe_job")

    def interrupt_job(self, request_id: str) -> BoundJob:
        return self._advance(request_id, "interrupt_job")

    def unresolved(self) -> tuple[BoundJob, ...]:
        with self._lock:
            return tuple(job for job in self._jobs.values() if job.receipt.state not in TERMINAL)

    @property
    def uncertain_hosts(self) -> frozenset[tuple[str, str]]:
        with self._lock:
            return frozenset(self._uncertain_hosts)


__all__ = ["BoundJob", "GuardedAdapter"]
