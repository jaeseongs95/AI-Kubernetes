"""AIK-011 guarded adapter boundary with connection/account pinning.

Fences are in-memory, scoped to one GuardedAdapter. They do not replace
VM/AGS authorization, persistent AIK leases or original-account recovery.
"""
from __future__ import annotations

from contextlib import contextmanager
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
    """A provider observation, never a VM completion verdict."""
    request: JobRequest
    receipt: JobReceipt


@dataclass(frozen=True, slots=True)
class _AuthChange:
    operation: str
    isolation: AccountIsolation


class GuardedAdapter:
    """Guard provider effects; caller must supply authoritative run approval.

    Pending authentication owns its original context until that same binding
    observes the requested terminal state. An unknown effect cannot be replayed
    merely because a status query or a different connection looks healthy.
    """

    def __init__(self, provider: ProviderAdapter, *, authorize_dispatch=None):
        if authorize_dispatch is not None and not callable(authorize_dispatch):
            raise TypeError("An authoritative authorization callback is required")
        for name in ("describe", "auth_status", "begin_sign_in", "sign_out",
                     "start_job", "observe_job", "interrupt_job"):
            if not callable(getattr(provider, name, None)):
                raise TypeError("ProviderAdapter methods required")
        self._authorize_dispatch = authorize_dispatch
        self._provider = provider
        self._lock = RLock()
        self._in_operation = False
        self._jobs: dict[str, BoundJob] = {}
        self._attempted: set[str] = set()
        self._uncertain_hosts: set[tuple[str, str]] = set()
        self._pending_auth: dict[ConnectionBinding, _AuthChange] = {}

    @contextmanager
    def _operation(self):
        # RLock alone permits callbacks to enter start/login/cancel again before
        # the outer operation has recorded its effect. Read-only local getters
        # remain usable; provider-facing operations must not nest.
        with self._lock:
            if self._in_operation:
                raise _fault(FaultKind.ACCOUNT_CONFLICT)
            self._in_operation = True
            try:
                yield
            finally:
                self._in_operation = False

    @staticmethod
    def _host(binding: ConnectionBinding) -> tuple[str, str]:
        return binding.provider, binding.runtime

    def _capabilities(self, binding: ConnectionBinding) -> AdapterCapabilities:
        if type(binding) is not ConnectionBinding:
            raise _fault(FaultKind.INVALID_CONTRACT)
        try:
            return require_caps(self._provider.describe(binding), binding)
        except AdapterFailure:
            raise
        except Exception:
            raise _fault(FaultKind.PROVIDER_FAILURE) from None

    def capabilities(self, binding: ConnectionBinding) -> AdapterCapabilities:
        with self._operation():
            return self._capabilities(binding)

    @staticmethod
    def _validated_auth(observed: object, binding: ConnectionBinding) -> AuthObservation:
        if type(observed) is not AuthObservation:
            raise _fault(FaultKind.INVALID_CONTRACT)
        if observed.connection_id != binding.connection_id or observed.revision != binding.revision:
            raise _fault(FaultKind.STALE_CONNECTION)
        if observed.state is AuthState.AUTHENTICATED:
            require_auth(observed, binding)
        return observed

    def _settle_auth(self, binding: ConnectionBinding, observed: AuthObservation) -> None:
        change = self._pending_auth.get(binding)
        if change is None:
            return
        desired = (AuthState.AUTHENTICATED if change.operation == "begin_sign_in"
                   else AuthState.UNAUTHENTICATED)
        if observed.state is desired:
            # A matching observation resolves only this pending auth operation.
            # It never clears a host fence for an unknown external effect.
            del self._pending_auth[binding]

    def _auth_observation(self, binding: ConnectionBinding) -> AuthObservation:
        try:
            observed = self._validated_auth(self._provider.auth_status(binding), binding)
            self._settle_auth(binding, observed)
            return observed
        except AdapterFailure:
            raise
        except Exception:
            raise _fault(FaultKind.PROVIDER_FAILURE) from None

    def auth_status(self, binding: ConnectionBinding) -> AuthObservation:
        with self._operation():
            if self._capabilities(binding).auth_status is not Support.SUPPORTED:
                raise _fault(FaultKind.UNSUPPORTED)
            return self._auth_observation(binding)

    def _checked_auth(self, binding: ConnectionBinding) -> None:
        if self._capabilities(binding).auth_status is not Support.SUPPORTED:
            raise _fault(FaultKind.UNSUPPORTED)
        require_auth(self._auth_observation(binding), binding)

    def _active_on_host(self, binding: ConnectionBinding) -> tuple[BoundJob, ...]:
        return tuple(job for job in self._jobs.values()
                     if self._host(job.request.binding) == self._host(binding)
                     and job.receipt.state not in TERMINAL)

    def _auth_context_conflict(self, binding: ConnectionBinding, isolation: AccountIsolation) -> bool:
        for existing, change in self._pending_auth.items():
            if self._host(existing) != self._host(binding):
                continue
            # Keep the isolation disclosed at reservation time. A new capability
            # claim cannot silently widen an already pending single-user context.
            shared = (existing.auth_kind is AuthKind.NATIVE_SUBSCRIPTION or
                      binding.auth_kind is AuthKind.NATIVE_SUBSCRIPTION) and (
                          isolation is not AccountIsolation.ISOLATED or
                          change.isolation is not AccountIsolation.ISOLATED)
            if shared or (existing.auth_kind, existing.connection_id) == (
                    binding.auth_kind, binding.connection_id):
                return True
        return False

    def _check_account_conflict(self, binding: ConnectionBinding, isolation: AccountIsolation) -> None:
        if self._host(binding) in self._uncertain_hosts:
            raise _fault(FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN)
        if self._auth_context_conflict(binding, isolation):
            raise _fault(FaultKind.ACCOUNT_CONFLICT)
        if binding.auth_kind is not AuthKind.NATIVE_SUBSCRIPTION:
            return
        if isolation is AccountIsolation.UNKNOWN:
            raise _fault(FaultKind.ACCOUNT_CONFLICT)
        if isolation is AccountIsolation.SINGLE_ACTIVE:
            for job in self._active_on_host(binding):
                previous = job.request.binding
                if (previous.connection_id, previous.revision, previous.account_fingerprint) != (
                        binding.connection_id, binding.revision, binding.account_fingerprint):
                    raise _fault(FaultKind.ACCOUNT_CONFLICT)

    def _change_auth(self, binding: ConnectionBinding, operation: str) -> AuthObservation:
        with self._operation():
            caps = self._capabilities(binding)
            support = caps.sign_in if operation == "begin_sign_in" else caps.sign_out
            if support is not Support.SUPPORTED or (operation == "begin_sign_in" and
                    binding.auth_kind is not AuthKind.NATIVE_SUBSCRIPTION):
                raise _fault(FaultKind.UNSUPPORTED)
            self._check_account_conflict(binding, caps.isolation)
            for job in self._active_on_host(binding):
                same_context = (job.request.binding.auth_kind, job.request.binding.connection_id) == (
                    binding.auth_kind, binding.connection_id)
                if same_context or (binding.auth_kind is AuthKind.NATIVE_SUBSCRIPTION and
                                    caps.isolation is not AccountIsolation.ISOLATED):
                    raise _fault(FaultKind.ACCOUNT_CONFLICT)
            if operation == "sign_out":
                self._checked_auth(binding)
            # Reserve before crossing the provider boundary, not after it returns.
            self._pending_auth[binding] = _AuthChange(operation, caps.isolation)
            try:
                observed = self._validated_auth(getattr(self._provider, operation)(binding), binding)
                if operation == "sign_out" and observed.state is AuthState.AUTHENTICATED:
                    raise _fault(FaultKind.INVALID_CONTRACT)
                self._settle_auth(binding, observed)
                if observed.state is AuthState.UNKNOWN:
                    self._uncertain_hosts.add(self._host(binding))
                return observed
            except AdapterFailure as error:
                unknown = error.fault.outcome is Outcome.UNKNOWN or error.fault.kind in (
                    FaultKind.INVALID_CONTRACT, FaultKind.STALE_CONNECTION,
                    FaultKind.ACCOUNT_CHANGED, FaultKind.INVALID_RECEIPT)
                if unknown:
                    self._uncertain_hosts.add(self._host(binding))
                else:
                    self._pending_auth.pop(binding, None)  # explicit known failure; no effect outstanding
                raise
            except Exception:
                self._uncertain_hosts.add(self._host(binding))
                raise _fault(FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN) from None

    def begin_sign_in(self, binding: ConnectionBinding) -> AuthObservation:
        return self._change_auth(binding, "begin_sign_in")

    def sign_out(self, binding: ConnectionBinding) -> AuthObservation:
        return self._change_auth(binding, "sign_out")

    def start_job(self, request: JobRequest) -> BoundJob:
        if type(request) is not JobRequest:
            raise _fault(FaultKind.INVALID_CONTRACT)
        binding = request.binding
        with self._operation():
            if request.request_id in self._attempted:
                raise _fault(FaultKind.DUPLICATE_REQUEST)
            caps = self._capabilities(binding)
            if caps.job_start is not Support.SUPPORTED:
                raise _fault(FaultKind.UNSUPPORTED)
            if binding in self._pending_auth and self._host(binding) not in self._uncertain_hosts:
                # A read for the original binding may confirm completed login.
                # Reading a different account/revision never releases its slot.
                if caps.auth_status is not Support.SUPPORTED:
                    raise _fault(FaultKind.UNSUPPORTED)
                self._auth_observation(binding)
            self._check_account_conflict(binding, caps.isolation)
            self._checked_auth(binding)
            if self._authorize_dispatch is None:
                raise _fault(FaultKind.ACCESS_DENIED)
            try:
                authorized = self._authorize_dispatch(request)
            except Exception:
                raise _fault(FaultKind.ACCESS_DENIED) from None
            if authorized is not True:
                raise _fault(FaultKind.ACCESS_DENIED)
            # Authorization is an external callback; recheck the account and
            # capability after it, rather than dispatching with stale observations.
            caps = self._capabilities(binding)
            if caps.job_start is not Support.SUPPORTED:
                raise _fault(FaultKind.UNSUPPORTED)
            self._check_account_conflict(binding, caps.isolation)
            self._checked_auth(binding)
            self._attempted.add(request.request_id)
            try:
                receipt = require_job(self._provider.start_job(request), request)
                for previous in self._jobs.values():
                    old = previous.request.binding
                    if (old.provider, old.runtime, old.connection_id, old.revision,
                        previous.receipt.provider_job_id) == (
                            binding.provider, binding.runtime, binding.connection_id,
                            binding.revision, receipt.provider_job_id):
                        raise _fault(FaultKind.INVALID_RECEIPT, Outcome.UNKNOWN)
            except AdapterFailure as error:
                if error.fault.outcome is Outcome.UNKNOWN:
                    self._uncertain_hosts.add(self._host(binding))
                raise
            except Exception:
                self._uncertain_hosts.add(self._host(binding))
                raise _fault(FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN) from None
            if receipt.state is JobState.UNKNOWN:
                self._uncertain_hosts.add(self._host(binding))
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
        with self._operation():
            job = self.get(request_id)
            if job.receipt.state in TERMINAL:
                return job
            binding = job.request.binding
            caps = self._capabilities(binding)
            feature = caps.job_observe if method == "observe_job" else caps.job_interrupt
            if feature is not Support.SUPPORTED:
                raise _fault(FaultKind.UNSUPPORTED)
            if method == "interrupt_job":
                self._check_account_conflict(binding, caps.isolation)
            self._checked_auth(binding)
            try:
                receipt = require_job(getattr(self._provider, method)(job.request, job.receipt), job.request)
                if receipt.provider_job_id != job.receipt.provider_job_id:
                    raise _fault(FaultKind.INVALID_RECEIPT, Outcome.UNKNOWN)
            except AdapterFailure as error:
                if error.fault.outcome is Outcome.UNKNOWN and method == "interrupt_job":
                    self._uncertain_hosts.add(self._host(binding))
                raise
            except Exception:
                if method == "interrupt_job":
                    self._uncertain_hosts.add(self._host(binding))
                raise _fault(FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN) from None
            if method == "interrupt_job" and receipt.state is JobState.UNKNOWN:
                self._uncertain_hosts.add(self._host(binding))
            updated = BoundJob(job.request, receipt)
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
