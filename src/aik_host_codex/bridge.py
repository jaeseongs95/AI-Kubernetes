"""AIK-014: Codex subscription host-auth bridge implementing ProviderAdapter.

The official Codex host owns the login. This bridge starts the official login
command, observes ``login status``, requests ``logout`` and binds a connection
to a host *handle* (``host-session://codex-cli/<profile>``), never to a token.
Execution reuses an injected existing VM runtime after the non-secret account
binding revision has been compared. Distinct states are reported for:
bridge implementation, host absent/unsupported, auth expired, account binding,
and qualification (always NOT_RUN; live evidence belongs to AIK-022/082/086).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import math
import re
from threading import RLock
import time
from types import MappingProxyType

from aik_provider.contracts import (
    AccountIsolation, AdapterCapabilities, AdapterFailure, AuthKind, AuthObservation,
    AuthState, ConnectionBinding, FaultKind, JobReceipt, JobRequest, JobState, Outcome,
    ProviderFault, Support, TERMINAL, require_auth,
)

from .protocol import CodexCliProtocol, LoginMethod, LoginState, ParsedStatus
from .runner import (
    MAX_OUTPUT_LIMIT, MAX_TIMEOUT, SOURCES, HostNotFound, OutputLimitExceeded,
    RunnerTimeout, RunResult, build_environment,
)

PROVIDER = "openai"
DEFAULT_RUNTIME = "codex-host"
DEFAULT_PROFILE = "default"
IMPLEMENTATION_STATE = "implemented"
NOT_RUN = "NOT_RUN"
MAX_LOGIN_TIMEOUT = 3600.0
_MAX_REVISION = 2**63 - 1
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_ACCOUNT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z")
_TOKEN_LIKE = re.compile(r"[A-Za-z0-9]{30,}|^(?:sk-|sess-|eyJ)")
_FINGERPRINT_DOMAIN = b"AIK-014/codex-host-account/v1\x00"

# GUI command -> bridge method. Every command reaches a real code path that
# calls the injected runner; none of them is an unsupported-only stub.
GUI_COMMANDS = MappingProxyType({
    "host.codex.auth.start": "start_login",
    "host.codex.auth.status": "probe",
    "host.codex.auth.reconnect": "reconnect",
    "host.codex.auth.sign_out": "sign_out",
    "host.codex.auth.cancel": "cancel_login",
})


class HostPresence(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class ProtocolSupport(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class BindingState(StrEnum):
    BOUND = "bound"
    UNBOUND = "unbound"
    CHANGED = "changed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class HostCapability:
    """Connection capability: observations, never qualification."""

    provider: str
    runtime: str
    connection_id: str | None
    presence: HostPresence
    protocol: ProtocolSupport
    login: LoginState
    method: LoginMethod
    binding: BindingState
    execution_ready: bool
    reasons: tuple[str, ...]
    source: str
    implementation_state: str = IMPLEMENTATION_STATE
    auth_qualification: str = NOT_RUN       # AIK-022
    probe_qualification: str = NOT_RUN      # AIK-082
    governed_qualification: str = NOT_RUN   # AIK-086

    def __post_init__(self) -> None:
        for name, kind in (("presence", HostPresence), ("protocol", ProtocolSupport),
                           ("login", LoginState), ("method", LoginMethod), ("binding", BindingState)):
            if type(getattr(self, name)) is not kind:
                raise ValueError("INVALID_" + name.upper())
        if type(self.execution_ready) is not bool:
            raise ValueError("INVALID_EXECUTION_READY")
        if type(self.reasons) is not tuple or any(type(r) is not str for r in self.reasons):
            raise ValueError("INVALID_REASONS")
        if self.source not in SOURCES:
            raise ValueError("INVALID_SOURCE")
        if self.implementation_state not in ("implemented", "not_implemented"):
            raise ValueError("INVALID_IMPLEMENTATION_STATE")
        for name in ("auth_qualification", "probe_qualification", "governed_qualification"):
            if getattr(self, name) != NOT_RUN:
                raise ValueError("QUALIFICATION_REQUIRES_EXTERNAL_EVIDENCE")


@dataclass(frozen=True, slots=True)
class HostHandle:
    """What the existing VM runtime receives: identifiers and a host locator only."""

    provider: str
    runtime: str
    connection_id: str
    revision: int
    account_fingerprint: str
    credential_ref: str
    executable: str
    environment: tuple[tuple[str, str], ...] = field(repr=False)


@dataclass(slots=True)
class _Login:
    handle: object
    deadline: float
    connection_id: str
    binding: ConnectionBinding | None


def _failure(kind: FaultKind, outcome: Outcome = Outcome.KNOWN_FAILURE, *,
             native: str | None = None) -> AdapterFailure:
    return AdapterFailure(ProviderFault(kind, outcome, native))


def _number(value: object, maximum: float, code: str) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or not 0 < value <= maximum):
        raise ValueError(code)
    return float(value)


def _fingerprint(account_id: object) -> str | None:
    """Hash a validated host-provided ID to the contract's 64-hex format.

    This is pseudonymization, not anonymization: without a secret key a
    candidate identifier (for example an e-mail address) can be confirmed.
    """
    if type(account_id) is not str or _ACCOUNT_ID.fullmatch(account_id) is None:
        return None
    if _TOKEN_LIKE.search(account_id) is not None:
        return None
    digest = hashlib.sha256(_FINGERPRINT_DOMAIN)
    digest.update(account_id.encode("utf-8"))
    return digest.hexdigest()


class CodexHostBridge:
    """ProviderAdapter for the Codex host login. Use through GuardedAdapter.

    In-memory state (revisions, stale marks, attempted request IDs, receipts)
    lives only for this process; persistent reconciliation is out of scope.
    """

    def __init__(self, runner, *, vm_runtime=None, protocol=None, executable: str = "codex",
                 runtime: str = DEFAULT_RUNTIME, profile: str = DEFAULT_PROFILE,
                 command_timeout: float = 15.0, login_timeout: float = 600.0,
                 max_output: int = 65536, environment=None, clock=time.monotonic):
        if (not callable(getattr(runner, "run", None)) or not callable(getattr(runner, "spawn", None))
                or getattr(runner, "source", None) not in SOURCES):
            raise TypeError("A host runner with run(), spawn() and a declared source is required.")
        if vm_runtime is not None and not all(
                callable(getattr(vm_runtime, name, None)) for name in ("start", "observe", "interrupt")):
            raise TypeError("A VM runtime requires start(), observe() and interrupt().")
        protocol = CodexCliProtocol() if protocol is None else protocol
        for name in ("version_args", "status_args", "login_args", "logout_args"):
            args = getattr(protocol, name, None)
            if type(args) is not tuple or not args or any(
                    type(item) is not str or not item or "\0" in item for item in args):
                raise TypeError("The host protocol must declare fixed argument tuples.")
        for name in ("version_supported", "parse_status"):
            if not callable(getattr(protocol, name, None)):
                raise TypeError("The host protocol must provide a version check and status parser.")
        if type(executable) is not str or not executable or len(executable) > 260 or "\0" in executable:
            raise ValueError("INVALID_EXECUTABLE")
        if type(profile) is not str or _PROFILE.fullmatch(profile) is None:
            raise ValueError("INVALID_PROFILE")
        self._command_timeout = _number(command_timeout, MAX_TIMEOUT, "INVALID_COMMAND_TIMEOUT")
        self._login_timeout = _number(login_timeout, MAX_LOGIN_TIMEOUT, "INVALID_LOGIN_TIMEOUT")
        if type(max_output) is not int or not 1 <= max_output <= MAX_OUTPUT_LIMIT:
            raise ValueError("INVALID_OUTPUT_LIMIT")
        if environment is not None and not isinstance(environment, Mapping):
            raise TypeError("The environment source must be a mapping.")
        if not callable(clock):
            raise TypeError("A monotonic clock callable is required.")
        job = Support.SUPPORTED if vm_runtime is not None else Support.UNSUPPORTED
        self._caps = AdapterCapabilities(
            PROVIDER, runtime, AuthKind.NATIVE_SUBSCRIPTION, AccountIsolation.SINGLE_ACTIVE,
            Support.SUPPORTED, Support.SUPPORTED, Support.SUPPORTED, job, job, job,
            IMPLEMENTATION_STATE, NOT_RUN,
        )
        self.runtime = runtime
        self.credential_ref = f"host-session://codex-cli/{profile}"
        self._runner = runner
        self._vm_runtime = vm_runtime
        self._protocol = protocol
        self._executable = executable
        self._max_output = max_output
        self._environment = None if environment is None else dict(environment)
        self._clock = clock
        self._lock = RLock()
        self._login: _Login | None = None
        self._bindings: dict[str, tuple[int, str]] = {}
        self._stale: set[str] = set()
        # Bindings presented without being issued/confirmed by this process
        # (e.g. after restart). They stay stale until connect()/reconnect().
        self._adopted: set[str] = set()
        self._attempted: set[str] = set()
        self._jobs: dict[str, JobReceipt] = {}

    def __repr__(self) -> str:
        return f"CodexHostBridge(runtime={self.runtime!r})"

    # -- internal helpers ------------------------------------------------------
    def _env(self) -> dict[str, str]:
        return build_environment(self._environment)

    def _run(self, args: tuple[str, ...]) -> tuple[str | None, RunResult | None]:
        try:
            result = self._runner.run((self._executable, *args), env=self._env(),
                                      timeout=self._command_timeout, max_output=self._max_output)
        except HostNotFound:
            return "host_absent", None
        except RunnerTimeout:
            return "host_timeout", None
        except OutputLimitExceeded:
            return "host_output_limit", None
        except Exception:
            return "host_runner_error", None
        if type(result) is not RunResult:
            return "host_runner_error", None
        if len(result.output) > self._max_output:
            return "host_output_limit", None
        return None, result

    def _version_check(self) -> tuple[str | None, bool]:
        """Run ``--version``; return (runner error code, protocol supported)."""
        error, result = self._run(self._protocol.version_args)
        if error is not None:
            return error, False
        try:
            supported = self._protocol.version_supported(result.exit_code, result.output) is True
        except Exception:
            supported = False
        return None, supported

    def _require_supported(self) -> None:
        """Fail closed on the ProviderAdapter path when the host version is not supported."""
        error, supported = self._version_check()
        if error == "host_absent":
            raise _failure(FaultKind.UNSUPPORTED, native="host_absent")
        if error is not None:
            raise _failure(FaultKind.PROVIDER_FAILURE, native=error)
        if not supported:
            raise _failure(FaultKind.UNSUPPORTED, native="host_unsupported")

    def _parse(self, result: RunResult) -> tuple[LoginState, LoginMethod, str | None]:
        try:
            parsed = self._protocol.parse_status(result.exit_code, result.output)
        except Exception:
            return LoginState.UNKNOWN, LoginMethod.UNKNOWN, None
        if (type(parsed) is not ParsedStatus or type(parsed.login) is not LoginState
                or type(parsed.method) is not LoginMethod or parsed.login is LoginState.PENDING):
            return LoginState.UNKNOWN, LoginMethod.UNKNOWN, None
        if parsed.login is not LoginState.LOGGED_IN:
            return parsed.login, LoginMethod.UNKNOWN, None
        if parsed.method is not LoginMethod.SUBSCRIPTION:
            return parsed.login, parsed.method, None
        return parsed.login, parsed.method, _fingerprint(parsed.account_id)

    def _status(self, revalidate: ConnectionBinding | None):
        """Run ``login status``; mark every bound connection the host no longer confirms."""
        error, result = self._run(self._protocol.status_args)
        if error is not None:
            return error, LoginState.UNKNOWN, LoginMethod.UNKNOWN, None
        login, method, fingerprint = self._parse(result)
        del result
        confirmed = login is LoginState.LOGGED_IN and method is LoginMethod.SUBSCRIPTION
        if login is not LoginState.UNKNOWN:
            for connection_id, (_, known) in self._bindings.items():
                if not (confirmed and fingerprint == known):
                    self._stale.add(connection_id)
        # A login started by this bridge for exactly this binding, observed to
        # *succeed* with the same account, re-validates it without a new
        # revision. Adopted (not issued here) bindings never qualify.
        if (revalidate is not None and confirmed and fingerprint == revalidate.account_fingerprint
                and revalidate.connection_id not in self._adopted
                and self._bindings.get(revalidate.connection_id) == (
                    revalidate.revision, revalidate.account_fingerprint)):
            self._stale.discard(revalidate.connection_id)
        return None, login, method, fingerprint

    def _poll_login(self) -> tuple[str, _Login | None, bool]:
        """Return (state, login, succeeded). Only an exit code 0 counts as success.

        The deadline is enforced lazily: only when a bridge call polls.
        """
        login = self._login
        if login is None:
            return "none", None, False
        try:
            code = login.handle.poll()
        except Exception:
            code = -1
        if code is None and self._clock() < login.deadline:
            return "running", login, False
        self._login = None
        if code is None:
            try:
                login.handle.terminate()
            except Exception:
                pass
            return "finished", login, False
        return "finished", login, type(code) is int and code == 0

    @staticmethod
    def _revalidation(login: _Login | None, succeeded: bool) -> ConnectionBinding | None:
        return login.binding if login is not None and succeeded else None

    def _check_binding(self, binding: object) -> None:
        if type(binding) is not ConnectionBinding:
            raise _failure(FaultKind.INVALID_CONTRACT)
        if (binding.provider, binding.runtime, binding.auth_kind, binding.credential_ref) != (
                PROVIDER, self.runtime, AuthKind.NATIVE_SUBSCRIPTION, self.credential_ref):
            raise _failure(FaultKind.STALE_CONNECTION)

    def _check_current(self, binding: object) -> None:
        self._check_binding(binding)
        with self._lock:
            known = self._bindings.get(binding.connection_id)
            if known is None:
                # A persisted binding (e.g. after restart) may already have been
                # fenced by a newer revision. Adopt it only as stale: execution
                # requires reconnect(), which always issues a newer revision.
                self._bindings[binding.connection_id] = (binding.revision, binding.account_fingerprint)
                self._stale.add(binding.connection_id)
                self._adopted.add(binding.connection_id)
            elif known != (binding.revision, binding.account_fingerprint):
                raise _failure(FaultKind.STALE_CONNECTION, native="reconnect_required")

    def _binding(self, connection_id: str, revision: int, fingerprint: str) -> ConnectionBinding:
        return ConnectionBinding(PROVIDER, self.runtime, connection_id, revision,
                                 AuthKind.NATIVE_SUBSCRIPTION, self.credential_ref, fingerprint)

    @staticmethod
    def _observation(binding: ConnectionBinding, state: AuthState,
                     fingerprint: str | None = None) -> AuthObservation:
        return AuthObservation(binding.connection_id, binding.revision, fingerprint, state)

    @staticmethod
    def _raise_unusable(error, login, method, fingerprint) -> None:
        if error == "host_absent":
            raise _failure(FaultKind.UNSUPPORTED, native="host_absent")
        if error is not None:
            raise _failure(FaultKind.PROVIDER_FAILURE, native=error)
        if login is LoginState.LOGGED_OUT:
            raise _failure(FaultKind.AUTH_REQUIRED, native="auth_required")
        if login is LoginState.EXPIRED:
            raise _failure(FaultKind.AUTH_REQUIRED, native="auth_expired")
        if login is not LoginState.LOGGED_IN:
            raise _failure(FaultKind.PROVIDER_FAILURE, native="host_state_unknown")
        if method is not LoginMethod.SUBSCRIPTION:
            raise _failure(FaultKind.UNSUPPORTED, native="non_subscription_login")
        if fingerprint is None:
            raise _failure(FaultKind.AUTH_REQUIRED, native="account_unknown")

    def _start_login(self, connection_id: str, binding: ConnectionBinding | None) -> None:
        state, login, _ = self._poll_login()
        if state == "running":
            if login.connection_id == connection_id and login.binding == binding:
                return
            raise _failure(FaultKind.ACCOUNT_CONFLICT, native="auth_pending")
        self._require_supported()
        problem = None
        handle = None
        try:
            handle = self._runner.spawn((self._executable, *self._protocol.login_args), env=self._env())
        except HostNotFound:
            problem = ("absent",)
        except Exception:
            problem = ("error",)
        if problem == ("absent",):
            raise _failure(FaultKind.UNSUPPORTED, native="host_absent")
        if problem is not None:
            raise _failure(FaultKind.PROVIDER_FAILURE, native="host_runner_error")
        if not callable(getattr(handle, "poll", None)) or not callable(getattr(handle, "terminate", None)):
            raise _failure(FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN, native="host_runner_error")
        self._login = _Login(handle, self._clock() + self._login_timeout, connection_id, binding)

    def _handle(self, binding: ConnectionBinding) -> HostHandle:
        return HostHandle(PROVIDER, self.runtime, binding.connection_id, binding.revision,
                          binding.account_fingerprint, self.credential_ref, self._executable,
                          tuple(sorted(self._env().items())))

    # -- GUI-facing commands -----------------------------------------------------
    def probe(self, binding: ConnectionBinding | None = None) -> HostCapability:
        """Report presence, protocol, login, method and binding as separate fields."""
        if binding is not None:
            self._check_binding(binding)
        with self._lock:
            reasons: list[str] = []
            login, method = LoginState.UNKNOWN, LoginMethod.UNKNOWN
            binding_state = BindingState.UNBOUND if binding is None else BindingState.UNKNOWN
            error, supported = self._version_check()
            if error == "host_absent":
                presence, protocol = HostPresence.ABSENT, ProtocolSupport.UNKNOWN
                reasons.append("host_absent")
            elif error is not None:
                presence, protocol = HostPresence.UNKNOWN, ProtocolSupport.UNKNOWN
                reasons.append("host_state_unknown")
            else:
                presence = HostPresence.PRESENT
                protocol = ProtocolSupport.SUPPORTED if supported else ProtocolSupport.UNSUPPORTED
                if not supported:
                    reasons.append("host_unsupported")
            # Enforce the login deadline on every probe, even when the version
            # check fails; a finished login is then not re-validated.
            state, pending, succeeded = self._poll_login()
            if protocol is ProtocolSupport.SUPPORTED:
                if state == "running":
                    login = LoginState.PENDING
                    reasons.append("auth_pending")
                else:
                    error, login, method, fingerprint = self._status(self._revalidation(pending, succeeded))
                    if error == "host_absent":
                        presence = HostPresence.ABSENT
                        reasons.append("host_absent")
                    elif error is not None or login is LoginState.UNKNOWN:
                        reasons.append("host_state_unknown")
                    elif login is LoginState.LOGGED_OUT:
                        reasons.append("auth_required")
                    elif login is LoginState.EXPIRED:
                        reasons.append("auth_expired")
                    elif method is not LoginMethod.SUBSCRIPTION:
                        reasons.append("non_subscription_login")
                    elif fingerprint is None:
                        reasons.append("account_unknown")
                    elif binding is not None:
                        known = self._bindings.get(binding.connection_id)
                        if fingerprint != binding.account_fingerprint:
                            binding_state = BindingState.CHANGED
                            reasons.append("account_changed")
                        elif binding.connection_id in self._stale or known is None or (
                                known != (binding.revision, binding.account_fingerprint)):
                            # An unseen binding needs reconnect() before execution.
                            binding_state = BindingState.CHANGED
                            reasons.append("connection_stale")
                        else:
                            binding_state = BindingState.BOUND
            if binding is None:
                reasons.append("connection_unbound")
            if self._vm_runtime is None:
                reasons.append("vm_runtime_unavailable")
            return HostCapability(
                PROVIDER, self.runtime, None if binding is None else binding.connection_id,
                presence, protocol, login, method, binding_state,
                not reasons and binding_state is BindingState.BOUND, tuple(reasons),
                self._runner.source,
            )

    def start_login(self, connection_id: str) -> HostCapability:
        """Start the official host login (first connection); returns the capability."""
        if type(connection_id) is not str or _ID.fullmatch(connection_id) is None:
            raise _failure(FaultKind.INVALID_CONTRACT)
        with self._lock:
            self._start_login(connection_id, None)
            return self.probe()

    def cancel_login(self) -> bool:
        with self._lock:
            login, self._login = self._login, None
            if login is None:
                return False
            try:
                login.handle.terminate()
            except Exception:
                pass
            return True

    def connect(self, connection_id: str, *, previous: ConnectionBinding | None = None) -> ConnectionBinding:
        """Bind a connection to the observed host account (host handle, no token)."""
        if type(connection_id) is not str or _ID.fullmatch(connection_id) is None:
            raise _failure(FaultKind.INVALID_CONTRACT)
        if previous is not None:
            self._check_binding(previous)
            if previous.connection_id != connection_id:
                raise _failure(FaultKind.INVALID_CONTRACT)
        with self._lock:
            state, login, succeeded = self._poll_login()
            if state == "running":
                raise _failure(FaultKind.AUTH_REQUIRED, native="auth_pending")
            self._require_supported()
            error, status, method, fingerprint = self._status(self._revalidation(login, succeeded))
            self._raise_unusable(error, status, method, fingerprint)
            known = self._bindings.get(connection_id)
            if known is not None and known[1] == fingerprint and connection_id not in self._stale:
                return self._binding(connection_id, known[0], fingerprint)
            # Always issue a revision newer than every one presented or known,
            # so a restored older binding cannot outrank a newer one.
            base = max(known[0] if known is not None else 0, previous.revision if previous is not None else 0)
            if base >= _MAX_REVISION:
                raise _failure(FaultKind.INVALID_CONTRACT)
            binding = self._binding(connection_id, base + 1, fingerprint)
            self._bindings[connection_id] = (binding.revision, fingerprint)
            self._stale.discard(connection_id)
            self._adopted.discard(connection_id)
            return binding

    def reconnect(self, binding: ConnectionBinding) -> ConnectionBinding:
        """Re-validate after an external login change; issues a new revision if needed."""
        self._check_binding(binding)
        return self.connect(binding.connection_id, previous=binding)

    # -- ProviderAdapter -------------------------------------------------------
    def describe(self, binding: ConnectionBinding) -> AdapterCapabilities:
        self._check_binding(binding)
        return self._caps

    def auth_status(self, binding: ConnectionBinding) -> AuthObservation:
        self._check_current(binding)
        with self._lock:
            state, login, succeeded = self._poll_login()
            if state == "running":
                if login.connection_id == binding.connection_id:
                    return self._observation(binding, AuthState.PENDING)
                raise _failure(FaultKind.ACCOUNT_CONFLICT, native="auth_pending")
            self._require_supported()
            error, status, method, fingerprint = self._status(self._revalidation(login, succeeded))
            if error == "host_absent":
                raise _failure(FaultKind.UNSUPPORTED, native="host_absent")
            if error is not None:
                raise _failure(FaultKind.PROVIDER_FAILURE, native=error)
            if status is LoginState.LOGGED_OUT:
                return self._observation(binding, AuthState.UNAUTHENTICATED)
            if status is LoginState.EXPIRED:
                return self._observation(binding, AuthState.EXPIRED)
            if status is not LoginState.LOGGED_IN:
                return self._observation(binding, AuthState.UNKNOWN)
            if method is not LoginMethod.SUBSCRIPTION:
                raise _failure(FaultKind.UNSUPPORTED, native="non_subscription_login")
            if fingerprint is None:
                return self._observation(binding, AuthState.UNKNOWN)
            if fingerprint != binding.account_fingerprint:
                return self._observation(binding, AuthState.AUTHENTICATED, fingerprint)
            if binding.connection_id in self._stale:
                raise _failure(FaultKind.STALE_CONNECTION, native="reconnect_required")
            return self._observation(binding, AuthState.AUTHENTICATED, fingerprint)

    def begin_sign_in(self, binding: ConnectionBinding) -> AuthObservation:
        self._check_current(binding)
        with self._lock:
            self._start_login(binding.connection_id, binding)
        return self._observation(binding, AuthState.PENDING)

    def sign_out(self, binding: ConnectionBinding) -> AuthObservation:
        """Ask the official host to log out, then confirm by observing status.

        The host is single-active: logout is only requested when the host does
        not currently show a different (or unidentifiable) logged-in account.
        """
        self._check_current(binding)
        with self._lock:
            state, _, _ = self._poll_login()
            if state == "running":
                raise _failure(FaultKind.ACCOUNT_CONFLICT, native="auth_pending")
            self._require_supported()
            error, status, method, fingerprint = self._status(None)
            if error == "host_absent":
                raise _failure(FaultKind.UNSUPPORTED, native="host_absent")
            if error is not None:
                raise _failure(FaultKind.PROVIDER_FAILURE, native=error)
            if status is LoginState.LOGGED_IN:
                if method is not LoginMethod.SUBSCRIPTION:
                    raise _failure(FaultKind.UNSUPPORTED, native="non_subscription_login")
                if fingerprint is None:
                    raise _failure(FaultKind.PROVIDER_FAILURE, native="account_unknown")
                if fingerprint != binding.account_fingerprint:
                    raise _failure(FaultKind.ACCOUNT_CHANGED, native="account_changed")
            error, result = self._run(self._protocol.logout_args)
            if error == "host_absent":
                raise _failure(FaultKind.UNSUPPORTED, native="host_absent")
            if error is not None:
                raise _failure(FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN, native=error)
            exit_code = result.exit_code
            del result
            error, status, method, fingerprint = self._status(None)
            if error is not None:
                raise _failure(FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN, native=error)
            if status is LoginState.LOGGED_OUT:
                self._stale.add(binding.connection_id)
                return self._observation(binding, AuthState.UNAUTHENTICATED)
            if (exit_code != 0 and status is LoginState.LOGGED_IN and method is LoginMethod.SUBSCRIPTION
                    and fingerprint == binding.account_fingerprint):
                raise _failure(FaultKind.PROVIDER_FAILURE, native="logout_failed")
            return self._observation(binding, AuthState.UNKNOWN)

    def start_job(self, request: JobRequest) -> JobReceipt:
        """Dispatch through the existing VM runtime after account revalidation."""
        if type(request) is not JobRequest:
            raise _failure(FaultKind.INVALID_CONTRACT)
        binding = request.binding
        self._check_current(binding)
        if self._vm_runtime is None:
            raise _failure(FaultKind.UNSUPPORTED, native="vm_runtime_unavailable")
        with self._lock:
            if request.request_id in self._attempted:
                raise _failure(FaultKind.DUPLICATE_REQUEST)
            # auth_status also verifies the host version (fail-closed).
            require_auth(self.auth_status(binding), binding)
            handle = self._handle(binding)
            self._attempted.add(request.request_id)
            failure = None
            outcome = None
            try:
                outcome = self._vm_runtime.start(handle, request)
            except AdapterFailure as error:
                failure = AdapterFailure(error.fault)
            except Exception:
                failure = _failure(FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN, native="vm_runtime_error")
            if failure is not None:
                raise failure
            if (type(outcome) is not tuple or len(outcome) != 2 or type(outcome[0]) is not str
                    or _ID.fullmatch(outcome[0]) is None or type(outcome[1]) is not JobState):
                raise _failure(FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN, native="malformed_vm_receipt")
            receipt = JobReceipt(request.request_id, outcome[0], binding.connection_id, binding.revision,
                                 binding.account_fingerprint, outcome[1])
            self._jobs[request.request_id] = receipt
            return receipt

    def _stored(self, request: object, receipt: object) -> JobReceipt:
        if type(request) is not JobRequest or type(receipt) is not JobReceipt:
            raise _failure(FaultKind.INVALID_CONTRACT)
        self._check_binding(request.binding)
        with self._lock:
            stored = self._jobs.get(request.request_id)
        binding = request.binding
        if stored is None or receipt != stored or (
                stored.connection_id, stored.revision, stored.account_fingerprint) != (
                binding.connection_id, binding.revision, binding.account_fingerprint):
            raise _failure(FaultKind.INVALID_RECEIPT, Outcome.UNKNOWN)
        return stored

    def _advance(self, request: JobRequest, stored: JobReceipt, method: str, effectful: bool) -> JobReceipt:
        failure = None
        state = None
        try:
            state = getattr(self._vm_runtime, method)(self._handle(request.binding), request, stored.provider_job_id)
        except AdapterFailure as error:
            failure = AdapterFailure(error.fault)
        except Exception:
            failure = (_failure(FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN, native="vm_runtime_error")
                       if effectful else _failure(FaultKind.PROVIDER_FAILURE, native="vm_runtime_error"))
        if failure is not None:
            raise failure
        if type(state) is not JobState:
            raise _failure(FaultKind.INVALID_RECEIPT, Outcome.UNKNOWN)
        updated = JobReceipt(stored.request_id, stored.provider_job_id, stored.connection_id,
                             stored.revision, stored.account_fingerprint, state)
        with self._lock:
            # Compare-and-set: a concurrent observe/interrupt may already have
            # stored a newer (possibly terminal) receipt; never overwrite it.
            current = self._jobs.get(request.request_id)
            if current is not None and current is not stored:
                return current
            self._jobs[request.request_id] = updated
        return updated

    def observe_job(self, request: JobRequest, receipt: JobReceipt) -> JobReceipt:
        stored = self._stored(request, receipt)
        if stored.state in TERMINAL:
            return stored
        if self._vm_runtime is None:
            raise _failure(FaultKind.UNSUPPORTED, native="vm_runtime_unavailable")
        return self._advance(request, stored, "observe", False)

    def interrupt_job(self, request: JobRequest, receipt: JobReceipt) -> JobReceipt:
        stored = self._stored(request, receipt)
        if stored.state in TERMINAL:
            return stored
        if self._vm_runtime is None:
            raise _failure(FaultKind.UNSUPPORTED, native="vm_runtime_unavailable")
        self._check_current(request.binding)
        with self._lock:
            require_auth(self.auth_status(request.binding), request.binding)
            return self._advance(request, stored, "interrupt", True)


__all__ = [
    "CodexHostBridge", "HostCapability", "HostHandle", "HostPresence", "ProtocolSupport",
    "BindingState", "GUI_COMMANDS", "PROVIDER", "DEFAULT_RUNTIME", "DEFAULT_PROFILE",
    "IMPLEMENTATION_STATE", "NOT_RUN", "MAX_LOGIN_TIMEOUT",
]
