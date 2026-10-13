"""AIK-015: Claude Code subscription host bridge for the AIK-011 contract.

Lifecycle:
- ``probe``: host present/absent/unsupported plus auth state
- ``start_login`` / ``begin_sign_in``: launch the official login; never proxy it
- ``connect``: bind the selected host account to an opaque host handle
- ``auth_status``: re-observe the host
- ``prepare`` + ``start_job``: one non-interactive host run
- ``reconnect``: new revision after re-validation
- ``sign_out`` / ``release``: forget the handle; host logout only on request

The bridge stores only the following:
- a ``host-session://`` handle
- the account fingerprint (a digest of a host-provided identifier)
- the observed capability

It never stores login secrets. A login change observed outside the bridge
marks the handle ``reconnect_required``. Only ``reconnect()`` or an official
login that this bridge launched for the binding (with no other account
observed in between) clears it. Fixture results are never live evidence.
Every capability keeps ``qualification_status=NOT_RUN``.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
import json
import math
import os
import re
import secrets
from threading import RLock

from aik_provider.contracts import (
    AccountIsolation, AdapterCapabilities, AdapterFailure, AuthKind, AuthObservation, AuthState,
    ConnectionBinding, FaultKind, JobReceipt, JobRequest, JobState, Outcome, ProviderFault, Support,
)
from aik_security.redaction import OutputRedactor

from .protocol import (
    MIN_HOST_VERSION, STATUS_UNSUPPORTED_EXIT, AuthMethod, HostCommands, HostState, HostStatus,
    account_fingerprint, parse_job, parse_status, parse_version, version_tuple,
)
from .runner import (
    ENV_ALLOWLIST, MAX_TIMEOUT_SECONDS, SOURCES, CommandResult, CommandSpec, RunnerNotStarted,
    RunnerTimeout, build_environment,
)

PROVIDER = "anthropic"
RUNTIME = "claude-code"
HANDLE_NAMESPACE = "claude-code"
ROUTE_QUALIFICATION = "NOT_RUN"
LIVE_EVIDENCE_TASKS = ("AIK-022", "AIK-082", "AIK-086")
VERSION_TIMEOUT = 10.0
STATUS_TIMEOUT = 15.0
LOGIN_TIMEOUT = 900.0
LOGOUT_TIMEOUT = 30.0
DEFAULT_EXEC_TIMEOUT = 600.0
STATUS_OUTPUT_LIMIT = 65536
EXEC_OUTPUT_LIMIT = 4 * 1024 * 1024
MAX_PROMPT_CHARS = 1_000_000
MAX_PREPARED = 1024
# Upper bound for prepared + consumed request IDs (receipts and results are
# subsets of consumed IDs). Entries are never evicted: eviction would drop
# duplicate protection, so new requests are refused instead (fail-closed).
MAX_REQUEST_RECORDS = 65536
_MAX_REVISION = 2**63 - 1
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")


def _failure(kind: FaultKind, outcome: Outcome = Outcome.KNOWN_FAILURE, *,
             native: str | None = None) -> AdapterFailure:
    return AdapterFailure(ProviderFault(kind, outcome, native))


@dataclass(frozen=True, slots=True)
class HostCall:
    """The prompt for one host run; delivered on stdin, never in argv or logs."""

    prompt: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.prompt) is not str or not self.prompt or len(self.prompt) > MAX_PROMPT_CHARS:
            raise ValueError("INVALID_PROMPT")
        try:
            self.prompt.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("INVALID_PROMPT") from None


@dataclass(frozen=True, slots=True)
class HostResult:
    """One observed host run. It is not a VM verdict and not a qualification."""

    request_id: str
    provider_job_id: str
    model: str | None          # host-reported; never substituted by the requested model
    state: JobState
    text: str = field(repr=False)
    source: str = "synthetic_fixture"

    @property
    def measured(self) -> bool:
        """A live runner observation; still not route qualification."""
        return self.source == "live"


@dataclass(frozen=True, slots=True)
class ConnectionCapability:
    """Connection capability: configured catalog kept separate from runtime observation."""

    connection_id: str
    revision: int
    provider: str
    runtime: str
    host_state: HostState
    host_version: str | None
    auth_state: AuthState
    auth_method: AuthMethod
    account_binding: str        # matched | mismatched | not_signed_in | unknown
    isolation: AccountIsolation
    configured_models: tuple[str, ...]
    observed_models: tuple[str, ...]
    observation_source: str
    executable: bool
    reconnect_required: bool
    reasons: tuple[str, ...]
    implementation_state: str = "implemented"
    qualification_status: str = ROUTE_QUALIFICATION
    live_evidence_tasks: tuple[str, ...] = LIVE_EVIDENCE_TASKS

    def __post_init__(self) -> None:
        if self.qualification_status != "NOT_RUN":
            raise ValueError("QUALIFICATION_REQUIRES_EXTERNAL_EVIDENCE")
        if self.account_binding not in ("matched", "mismatched", "not_signed_in", "unknown"):
            raise ValueError("INVALID_ACCOUNT_BINDING")
        if self.observation_source not in SOURCES:
            raise ValueError("INVALID_SOURCE")


@dataclass(slots=True)
class _Handle:
    connection_id: str
    revision: int
    account_fingerprint: str
    reconnect_required: bool = False
    login_pending: bool = False
    selection_verified: bool = False
    in_flight: int = 0
    closing: bool = False


class ClaudeHostBridge:
    """ProviderAdapter over the official Claude Code host. Use through GuardedAdapter.

    ``catalog`` is the configured model list (configuration, not measurement).
    ``isolation`` is a declared, unverified assumption. The default is
    ``single_active``: one host login per OS user, with no concurrent isolated
    subscription accounts.
    """

    def __init__(self, runner, *, catalog=(), commands: HostCommands | None = None,
                 isolation: AccountIsolation = AccountIsolation.SINGLE_ACTIVE,
                 environ=None, env_allowlist=ENV_ALLOWLIST,
                 exec_timeout: float = DEFAULT_EXEC_TIMEOUT, logout_on_release: bool = False):
        if (not callable(getattr(runner, "run", None)) or not callable(getattr(runner, "launch", None))
                or getattr(runner, "source", None) not in SOURCES):
            raise TypeError("A runner with run(), launch() and a declared source is required.")
        if type(catalog) not in (tuple, list):
            raise ValueError("INVALID_CATALOG")
        models: list[str] = []
        for model in catalog:
            if type(model) is not str or _ID.fullmatch(model) is None:
                raise ValueError("INVALID_CATALOG")
            if model not in models:
                models.append(model)
        commands = HostCommands() if commands is None else commands
        if type(commands) is not HostCommands:
            raise TypeError("HostCommands required")
        if type(isolation) is not AccountIsolation:
            raise ValueError("INVALID_ISOLATION")
        if environ is not None and not callable(getattr(environ, "items", None)):
            raise TypeError("An environment mapping is required.")
        allowlist = frozenset(env_allowlist)
        build_environment({}, allowlist)  # validates the allowlist entries
        if (isinstance(exec_timeout, bool) or not isinstance(exec_timeout, (int, float))
                or not math.isfinite(exec_timeout) or not 0 < exec_timeout <= MAX_TIMEOUT_SECONDS):
            raise ValueError("INVALID_TIMEOUT")
        if type(logout_on_release) is not bool:
            raise ValueError("INVALID_RELEASE_MODE")
        self._runner = runner
        self._catalog = tuple(models)
        self._commands = commands
        self._isolation = isolation
        self._environ_source = environ
        self._allowlist = allowlist
        self._exec_timeout = float(exec_timeout)
        self._logout_on_release = logout_on_release
        self._lock = RLock()
        self._handles: dict[str, _Handle] = {}
        self._last_status: HostStatus | None = None
        self._prepared: dict[str, HostCall] = {}
        self._consumed: set[str] = set()
        self._receipts: dict[str, JobReceipt] = {}
        self._results: dict[str, HostResult] = {}
        # Runtime observations are kept per bound account fingerprint.
        self._observed_models: dict[str, list[str]] = {}
        self._audit: deque = deque(maxlen=256)

    def __repr__(self) -> str:
        return f"ClaudeHostBridge(runtime={RUNTIME!r}, source={self._runner.source!r})"

    @property
    def catalog(self) -> tuple[str, ...]:
        return self._catalog

    # -- command plumbing ----------------------------------------------------
    def _spec(self, name: str, timeout: float, limit: int, *, model: str | None = None,
              stdin: bytes | None = None) -> CommandSpec:
        source = os.environ if self._environ_source is None else self._environ_source
        return CommandSpec(self._commands.argv(name, model), timeout, limit,
                           build_environment(source, self._allowlist), stdin)

    def _run(self, spec: CommandSpec) -> tuple[str, CommandResult | None]:
        problem = None
        result = None
        try:
            result = self._runner.run(spec)
        except RunnerNotStarted:
            problem = "absent"
        except RunnerTimeout:
            problem = "timeout"
        except Exception:
            problem = "error"
        # Never chain runner exceptions; their text may echo command data.
        if problem is not None:
            return problem, None
        if type(result) is not CommandResult or result.truncated or len(result.stdout) > spec.max_output_bytes:
            return "malformed", None
        return "ok", result

    def _record(self, operation: str, binding: ConnectionBinding | None, *, request_id: str | None = None,
                fault: ProviderFault | None = None, extra: dict | None = None) -> None:
        event = {
            "operation": operation, "provider": PROVIDER, "runtime": RUNTIME,
            "connection_id": None if binding is None else binding.connection_id,
            "revision": None if binding is None else binding.revision,
            "request_id": request_id, "result": "ok" if fault is None else "fault",
            "source": self._runner.source, "qualification_status": ROUTE_QUALIFICATION,
        }
        if fault is not None:
            event.update({"fault_kind": fault.kind.value, "outcome": fault.outcome.value,
                          "native_code": fault.native_code})
        if extra:
            event.update(extra)
        entry = OutputRedactor().output("log", event)
        with self._lock:
            self._audit.append(entry)

    # -- observation -----------------------------------------------------------
    def _observe(self) -> HostStatus:
        source = self._runner.source
        kind, result = self._run(self._spec("version", VERSION_TIMEOUT, STATUS_OUTPUT_LIMIT))
        if kind == "absent":
            return HostStatus.unobserved(HostState.ABSENT, source, ("HOST_ABSENT",))
        if kind != "ok":
            return HostStatus.unobserved(HostState.UNKNOWN, source, ("HOST_VERSION_UNOBSERVED",))
        version = parse_version(result)
        if version is None:
            return HostStatus.unobserved(HostState.UNSUPPORTED, source, ("HOST_VERSION_UNRECOGNIZED",))
        if version_tuple(version) < MIN_HOST_VERSION:
            return HostStatus.unobserved(HostState.UNSUPPORTED, source, ("HOST_VERSION_UNSUPPORTED",), version)
        kind, result = self._run(self._spec("status", STATUS_TIMEOUT, STATUS_OUTPUT_LIMIT))
        if kind == "absent":
            return HostStatus.unobserved(HostState.ABSENT, source, ("HOST_ABSENT",))
        if kind != "ok":
            return HostStatus.unobserved(HostState.PRESENT, source, ("AUTH_STATUS_UNOBSERVED",), version)
        parsed = parse_status(result.stdout)
        if parsed is None:
            if result.exit_code == STATUS_UNSUPPORTED_EXIT:
                return HostStatus.unobserved(HostState.UNSUPPORTED, source, ("AUTH_STATUS_UNSUPPORTED",), version)
            return HostStatus.unobserved(HostState.PRESENT, source, ("AUTH_STATUS_UNRECOGNIZED",), version)
        state, method, account, reasons = parsed
        if result.exit_code != 0 and state not in (AuthState.UNAUTHENTICATED, AuthState.EXPIRED):
            # A failing status command is never trusted to prove a usable login.
            # Signed-out/expired output remains usable: it can only block execution.
            del account
            return HostStatus.unobserved(HostState.PRESENT, source, ("AUTH_STATUS_UNRECOGNIZED",), version)
        fingerprint = None if account is None else account_fingerprint(account)
        del account
        return HostStatus(HostState.PRESENT, state, method, fingerprint, version, source, reasons)

    def probe(self) -> HostStatus:
        """Observe host presence, version and auth through the official CLI only."""
        status = self._observe()
        with self._lock:
            self._last_status = status
        self._record("probe", None, extra={
            "host_state": status.host_state.value, "auth_state": status.auth_state.value,
            "reasons": list(status.reasons),
        })
        return status

    def last_status(self) -> HostStatus | None:
        with self._lock:
            return self._last_status

    # -- handle checks -------------------------------------------------------
    def _check_binding(self, binding: object) -> _Handle:
        if type(binding) is not ConnectionBinding:
            raise _failure(FaultKind.INVALID_CONTRACT)
        if (binding.provider, binding.runtime, binding.auth_kind) != (
                PROVIDER, RUNTIME, AuthKind.NATIVE_SUBSCRIPTION):
            raise _failure(FaultKind.STALE_CONNECTION)
        with self._lock:
            handle = self._handles.get(binding.credential_ref)
            if handle is None or (handle.connection_id, handle.revision, handle.account_fingerprint) != (
                    binding.connection_id, binding.revision, binding.account_fingerprint):
                raise _failure(FaultKind.STALE_CONNECTION)
            return handle

    def _apply(self, handle: _Handle, status: HostStatus) -> str:
        """Compare the observation with the bound account. A change requires revalidation."""
        with self._lock:
            if status.auth_state is AuthState.AUTHENTICATED:
                if status.account_fingerprint == handle.account_fingerprint:
                    if handle.login_pending:
                        # The official login this bridge launched for this binding
                        # completed with the same host account: revalidated.
                        handle.login_pending = False
                        handle.reconnect_required = False
                    return "matched"
                handle.reconnect_required = True
                # Another account was observed after the launched login: that
                # login can no longer prove a revalidation of this binding.
                handle.login_pending = False
                return "mismatched"
            if status.auth_state in (AuthState.UNAUTHENTICATED, AuthState.EXPIRED):
                handle.reconnect_required = True
                return "not_signed_in"
            return "unknown"

    @staticmethod
    def _require_host(status: HostStatus) -> None:
        if status.host_state is HostState.ABSENT:
            raise _failure(FaultKind.UNSUPPORTED, native="host_absent")
        if status.host_state is HostState.UNSUPPORTED:
            raise _failure(FaultKind.UNSUPPORTED, native="host_unsupported")
        if status.host_state is not HostState.PRESENT:
            raise _failure(FaultKind.PROVIDER_FAILURE, native="host_unknown")

    def _require_authenticated(self, status: HostStatus) -> str:
        self._require_host(status)
        if status.auth_state is AuthState.EXPIRED:
            raise _failure(FaultKind.AUTH_REQUIRED, native="auth_expired")
        if status.auth_state is AuthState.UNAUTHENTICATED:
            native = ("api_key_auth_not_subscription" if status.auth_method is AuthMethod.API_KEY
                      else "auth_required")
            raise _failure(FaultKind.AUTH_REQUIRED, native=native)
        if status.auth_state is not AuthState.AUTHENTICATED or status.account_fingerprint is None:
            raise _failure(FaultKind.AUTH_REQUIRED, native="account_not_observed")
        return status.account_fingerprint

    def _launch_login(self) -> None:
        spec = self._spec("login", LOGIN_TIMEOUT, STATUS_OUTPUT_LIMIT)
        problem = None
        try:
            self._runner.launch(spec)
        except RunnerNotStarted:
            problem = "absent"
        except Exception:
            problem = "error"
        if problem == "absent":
            raise _failure(FaultKind.UNSUPPORTED, native="host_absent")
        if problem is not None:
            raise _failure(FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN, native="login_launch_unknown")

    # -- registration / reconnect (not part of the provider protocol) -----------
    def start_login(self) -> HostStatus:
        """Launch the official interactive login when the host is not signed in."""
        try:
            status = self.probe()
            self._require_host(status)
            if status.auth_state is not AuthState.AUTHENTICATED:
                self._launch_login()
        except AdapterFailure as error:
            self._record("start_login", None, fault=error.fault)
            raise
        self._record("start_login", None, extra={"auth_state": status.auth_state.value})
        return status

    def connect(self, connection_id: str, *, expected_account_fingerprint: str | None = None) -> ConnectionBinding:
        """Bind the host's currently signed-in subscription account to a new handle.

        ``expected_account_fingerprint`` is the selected account binding. The
        host must report exactly that account, or ``ACCOUNT_CHANGED`` is raised.
        Without it, whichever account the host reports is bound and the
        capability carries ``SELECTED_ACCOUNT_UNVERIFIED``.
        """
        if type(connection_id) is not str or _ID.fullmatch(connection_id) is None:
            raise ValueError("INVALID_CONNECTION_ID")
        if expected_account_fingerprint is not None and (
            type(expected_account_fingerprint) is not str
            or _FINGERPRINT.fullmatch(expected_account_fingerprint) is None
        ):
            raise ValueError("INVALID_ACCOUNT_FINGERPRINT")
        verified = expected_account_fingerprint is not None
        try:
            with self._lock:
                if any(handle.connection_id == connection_id for handle in self._handles.values()):
                    raise _failure(FaultKind.INVALID_CONTRACT, native="connection_exists")
            status = self.probe()
            fingerprint = self._require_authenticated(status)
            if verified and fingerprint != expected_account_fingerprint:
                raise _failure(FaultKind.ACCOUNT_CHANGED, native="selected_account_mismatch")
            reference = f"host-session://{HANDLE_NAMESPACE}/{secrets.token_hex(16)}"
            binding = ConnectionBinding(PROVIDER, RUNTIME, connection_id, 1, AuthKind.NATIVE_SUBSCRIPTION,
                                        reference, fingerprint)
            with self._lock:
                if any(handle.connection_id == connection_id for handle in self._handles.values()):
                    raise _failure(FaultKind.INVALID_CONTRACT, native="connection_exists")
                self._handles[reference] = _Handle(connection_id, 1, fingerprint, selection_verified=verified)
        except AdapterFailure as error:
            self._record("connect", None, fault=error.fault, extra={"connection_id": connection_id})
            raise
        self._record("connect", binding, extra={"account_selection": "verified" if verified else "unverified"})
        return binding

    def reconnect(self, binding: ConnectionBinding) -> ConnectionBinding:
        """Re-validate the same host account and issue the next revision."""
        handle = self._check_binding(binding)
        try:
            if binding.revision >= _MAX_REVISION:
                raise _failure(FaultKind.INVALID_CONTRACT)
            status = self.probe()
            relation = self._apply(handle, status)
            self._require_authenticated(status)
            if relation != "matched":
                # A different account needs an explicit new selection (connect), never a silent rebind.
                raise _failure(FaultKind.ACCOUNT_CHANGED, native="account_switched")
            with self._lock:
                if self._handles.get(binding.credential_ref) is not handle or handle.revision != binding.revision:
                    raise _failure(FaultKind.STALE_CONNECTION)
                if handle.closing:
                    raise _failure(FaultKind.STALE_CONNECTION, native="release_in_progress")
                if handle.in_flight:
                    raise _failure(FaultKind.STALE_CONNECTION, native="job_in_flight")
                handle.revision = binding.revision + 1
                handle.reconnect_required = False
                handle.login_pending = False
            rebound = replace(binding, revision=binding.revision + 1)
        except AdapterFailure as error:
            self._record("reconnect", binding, fault=error.fault)
            raise
        self._record("reconnect", rebound)
        return rebound

    def release(self, binding: ConnectionBinding, *, host_logout: bool = False) -> AuthObservation:
        """Forget the AIK handle. Official host logout runs only when requested.

        A handle with a host run in flight cannot be released; a handle whose
        host logout is in progress refuses new runs.
        """
        handle = self._check_binding(binding)
        try:
            if type(host_logout) is not bool:
                raise _failure(FaultKind.INVALID_CONTRACT)
            with self._lock:
                if self._handles.get(binding.credential_ref) is not handle or handle.revision != binding.revision:
                    raise _failure(FaultKind.STALE_CONNECTION)
                if handle.closing:
                    raise _failure(FaultKind.STALE_CONNECTION, native="release_in_progress")
                if handle.in_flight:
                    raise _failure(FaultKind.STALE_CONNECTION, native="job_in_flight")
                if host_logout:
                    handle.closing = True
                else:
                    del self._handles[binding.credential_ref]
            if host_logout:
                try:
                    self._host_logout()
                except BaseException:
                    with self._lock:
                        handle.closing = False
                    raise
                with self._lock:
                    if self._handles.get(binding.credential_ref) is handle:
                        del self._handles[binding.credential_ref]
        except AdapterFailure as error:
            self._record("release", binding, fault=error.fault)
            raise
        self._record("release", binding, extra={"host_logout": host_logout})
        return AuthObservation(binding.connection_id, binding.revision, None, AuthState.UNAUTHENTICATED)

    def _host_logout(self) -> None:
        kind, result = self._run(self._spec("logout", LOGOUT_TIMEOUT, STATUS_OUTPUT_LIMIT))
        if kind == "absent":
            raise _failure(FaultKind.UNSUPPORTED, native="host_absent")
        if kind != "ok":
            raise _failure(FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN, native="logout_unconfirmed")
        if result.exit_code != 0:
            raise _failure(FaultKind.PROVIDER_FAILURE, native="logout_failed")
        status = self.probe()
        if status.auth_state not in (AuthState.UNAUTHENTICATED, AuthState.EXPIRED):
            raise _failure(FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN, native="logout_unconfirmed")

    # -- ProviderAdapter -------------------------------------------------------
    def describe(self, binding: ConnectionBinding) -> AdapterCapabilities:
        self._check_binding(binding)
        with self._lock:
            status = self._last_status
        blocked = status is not None and status.host_state in (HostState.ABSENT, HostState.UNSUPPORTED)
        run = Support.UNSUPPORTED if blocked else Support.SUPPORTED
        # ``isolation`` is reported in its own field; job_start does not fold it in.
        return AdapterCapabilities(
            provider=PROVIDER, runtime=RUNTIME, auth_kind=AuthKind.NATIVE_SUBSCRIPTION,
            isolation=self._isolation, auth_status=Support.SUPPORTED, sign_in=run,
            sign_out=Support.SUPPORTED,
            job_start=Support.UNSUPPORTED if blocked or not self._catalog else Support.SUPPORTED,
            job_observe=Support.SUPPORTED, job_interrupt=Support.UNSUPPORTED,
            implementation_state="implemented", qualification_status=ROUTE_QUALIFICATION,
        )

    def auth_status(self, binding: ConnectionBinding) -> AuthObservation:
        handle = self._check_binding(binding)
        status = self.probe()
        relation = self._apply(handle, status)
        with self._lock:
            stale = relation == "matched" and handle.reconnect_required
        if stale:
            error = _failure(FaultKind.STALE_CONNECTION, native="reconnect_required")
            self._record("auth_status", binding, fault=error.fault)
            raise error
        state = status.auth_state
        self._record("auth_status", binding, extra={
            "auth_state": state.value, "host_state": status.host_state.value, "account_binding": relation,
        })
        return AuthObservation(binding.connection_id, binding.revision,
                               status.account_fingerprint if state is AuthState.AUTHENTICATED else None, state)

    def begin_sign_in(self, binding: ConnectionBinding) -> AuthObservation:
        """Launch the official login for this binding; completion is observed later.

        Observing the bound account does not clear ``reconnect_required``: an
        external login change still needs ``reconnect()``.
        """
        handle = self._check_binding(binding)
        try:
            status = self.probe()
            self._require_host(status)
            relation = self._apply(handle, status)
            if relation == "matched":
                with self._lock:
                    stale = handle.reconnect_required
                if stale:
                    raise _failure(FaultKind.STALE_CONNECTION, native="reconnect_required")
                observation = AuthObservation(binding.connection_id, binding.revision,
                                              handle.account_fingerprint, AuthState.AUTHENTICATED)
            else:
                self._launch_login()
                with self._lock:
                    handle.login_pending = True
                    handle.reconnect_required = True
                observation = AuthObservation(binding.connection_id, binding.revision, None, AuthState.PENDING)
        except AdapterFailure as error:
            self._record("begin_sign_in", binding, fault=error.fault)
            raise
        self._record("begin_sign_in", binding, extra={"auth_state": observation.state.value})
        return observation

    def sign_out(self, binding: ConnectionBinding) -> AuthObservation:
        return self.release(binding, host_logout=self._logout_on_release)

    def prepare(self, request_id: str, call: HostCall) -> None:
        if type(request_id) is not str or _ID.fullmatch(request_id) is None or type(call) is not HostCall:
            raise _failure(FaultKind.INVALID_CONTRACT)
        with self._lock:
            if request_id in self._prepared or request_id in self._receipts or request_id in self._consumed:
                raise _failure(FaultKind.DUPLICATE_REQUEST)
            if len(self._prepared) >= MAX_PREPARED:
                raise _failure(FaultKind.PROVIDER_FAILURE, native="prepared_capacity")
            if len(self._prepared) + len(self._consumed) >= MAX_REQUEST_RECORDS:
                raise _failure(FaultKind.PROVIDER_FAILURE, native="request_record_capacity")
            self._prepared[request_id] = call

    def cancel_prepared(self, request_id: str) -> bool:
        if type(request_id) is not str or _ID.fullmatch(request_id) is None:
            raise _failure(FaultKind.INVALID_CONTRACT)
        with self._lock:
            return self._prepared.pop(request_id, None) is not None

    def _require_bound(self, handle: _Handle, status: HostStatus) -> None:
        relation = self._apply(handle, status)
        self._require_authenticated(status)
        if relation == "mismatched":
            raise _failure(FaultKind.ACCOUNT_CHANGED, native="account_switched")
        with self._lock:
            if handle.reconnect_required:
                raise _failure(FaultKind.STALE_CONNECTION, native="reconnect_required")

    def start_job(self, request: JobRequest) -> JobReceipt:
        if type(request) is not JobRequest:
            raise _failure(FaultKind.INVALID_CONTRACT)
        binding = request.binding
        handle = self._check_binding(binding)
        with self._lock:
            if self._handles.get(binding.credential_ref) is not handle or handle.revision != binding.revision:
                raise _failure(FaultKind.STALE_CONNECTION)
            if handle.closing:
                raise _failure(FaultKind.STALE_CONNECTION, native="release_in_progress")
            if request.request_id in self._receipts or request.request_id in self._consumed:
                raise _failure(FaultKind.DUPLICATE_REQUEST)
            call = self._prepared.pop(request.request_id, None)  # never replayed
            if call is not None:
                self._consumed.add(request.request_id)
            # release()/reconnect() refuse this handle until the run is recorded.
            handle.in_flight += 1
        try:
            try:
                if call is None:
                    raise _failure(FaultKind.INVALID_CONTRACT, native="call_not_prepared")
                if request.model not in self._catalog:
                    raise _failure(FaultKind.INVALID_CONTRACT, native="model_not_configured")
                if self._isolation is AccountIsolation.UNKNOWN:
                    raise _failure(FaultKind.ACCOUNT_CONFLICT, native="isolation_unknown")
                # Re-observe immediately before the effect: external login changes are fenced.
                self._require_bound(handle, self.probe())
                spec = self._spec("execute", self._exec_timeout, EXEC_OUTPUT_LIMIT, model=request.model,
                                  stdin=call.prompt.encode("utf-8"))
                del call
                kind, result = self._run(spec)
                del spec
                if kind == "absent":
                    raise _failure(FaultKind.PROVIDER_FAILURE, native="host_absent")
                if kind == "timeout":
                    raise _failure(FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN, native="timeout")
                if kind != "ok":
                    raise _failure(FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN, native="malformed_output")
                output = parse_job(result)
                if output is None:
                    raise _failure(FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN, native="malformed_output")
            except AdapterFailure as error:
                self._record("start_job", binding, request_id=request.request_id, fault=error.fault)
                raise
            state = JobState.SUCCEEDED if output.succeeded else JobState.FAILED
            if state is not JobState.SUCCEEDED:
                # A failed run may be caused by expiry or an account change; re-observe.
                self._apply(handle, self.probe())
            receipt = JobReceipt(request.request_id, output.session_id, binding.connection_id,
                                 binding.revision, binding.account_fingerprint, state)
            result_record = HostResult(request.request_id, output.session_id, output.model, state,
                                       output.text, self._runner.source)
            with self._lock:
                self._receipts[request.request_id] = receipt
                self._results[request.request_id] = result_record
                if output.model is not None:
                    observed = self._observed_models.setdefault(binding.account_fingerprint, [])
                    if output.model not in observed:
                        observed.append(output.model)
            self._record("start_job", binding, request_id=request.request_id, extra={
                "provider_job_id": output.session_id, "model": output.model, "job_state": state.value,
            })
            return receipt
        finally:
            with self._lock:
                handle.in_flight -= 1

    def observe_job(self, request: JobRequest, receipt: JobReceipt) -> JobReceipt:
        """Host runs are synchronous and terminal at start; return the local record."""
        if type(request) is not JobRequest or type(receipt) is not JobReceipt:
            raise _failure(FaultKind.INVALID_CONTRACT)
        binding = request.binding
        self._check_binding(binding)
        with self._lock:
            stored = self._receipts.get(request.request_id)
        if stored is None or receipt != stored:
            raise _failure(FaultKind.INVALID_RECEIPT, Outcome.UNKNOWN)
        expected = JobReceipt(request.request_id, stored.provider_job_id, binding.connection_id,
                              binding.revision, binding.account_fingerprint, stored.state)
        if expected != stored:
            raise _failure(FaultKind.INVALID_RECEIPT, Outcome.UNKNOWN)
        return stored

    def interrupt_job(self, request: JobRequest, receipt: JobReceipt) -> JobReceipt:
        if type(request) is not JobRequest:
            raise _failure(FaultKind.INVALID_CONTRACT)
        self._check_binding(request.binding)
        raise _failure(FaultKind.UNSUPPORTED)

    # -- capability and local records -------------------------------------------
    def capability(self, binding: ConnectionBinding, *, refresh: bool = True) -> ConnectionCapability:
        handle = self._check_binding(binding)
        with self._lock:
            last = self._last_status
        status = self.probe() if refresh or last is None else last
        relation = self._apply(handle, status)
        reasons = list(status.reasons)
        if status.host_state is HostState.ABSENT:
            reasons.append("HOST_ABSENT")
        elif status.host_state is HostState.UNSUPPORTED:
            reasons.append("HOST_UNSUPPORTED")
        elif status.host_state is HostState.UNKNOWN:
            reasons.append("HOST_UNKNOWN")
        if relation == "mismatched":
            reasons.append("ACCOUNT_CHANGED")
        if relation == "unknown" and status.host_state is HostState.PRESENT:
            reasons.append("ACCOUNT_BINDING_UNKNOWN")
        with self._lock:
            reconnect = handle.reconnect_required
            pending = handle.login_pending
            verified = handle.selection_verified
            observed = tuple(self._observed_models.get(handle.account_fingerprint, ()))
        if not verified:
            reasons.append("SELECTED_ACCOUNT_UNVERIFIED")
        if reconnect:
            reasons.append("RECONNECT_REQUIRED")
        if pending:
            reasons.append("LOGIN_PENDING")
        if self._isolation is AccountIsolation.UNKNOWN:
            reasons.append("ISOLATION_UNKNOWN")
        elif self._isolation is AccountIsolation.SINGLE_ACTIVE:
            reasons.append("ISOLATION_SINGLE_ACTIVE")
        if not self._catalog:
            reasons.append("CATALOG_EMPTY")
        executable = (status.host_state is HostState.PRESENT and relation == "matched" and not reconnect
                      and self._isolation is not AccountIsolation.UNKNOWN and bool(self._catalog))
        return ConnectionCapability(
            connection_id=binding.connection_id, revision=binding.revision, provider=PROVIDER,
            runtime=RUNTIME, host_state=status.host_state, host_version=status.version,
            auth_state=status.auth_state, auth_method=status.auth_method, account_binding=relation,
            isolation=self._isolation, configured_models=self._catalog, observed_models=observed,
            observation_source=self._runner.source, executable=executable, reconnect_required=reconnect,
            reasons=tuple(dict.fromkeys(reasons)),
        )

    def result(self, request_id: str) -> HostResult:
        with self._lock:
            try:
                return self._results[request_id]
            except (KeyError, TypeError):
                raise _failure(FaultKind.INVALID_CONTRACT) from None

    def discard_result(self, request_id: str) -> bool:
        if type(request_id) is not str or _ID.fullmatch(request_id) is None:
            raise _failure(FaultKind.INVALID_CONTRACT)
        with self._lock:
            return self._results.pop(request_id, None) is not None

    def audit_log(self) -> tuple:
        """Redacted, detached audit events (AIK-017 output kind ``log``)."""
        with self._lock:
            return tuple(json.loads(json.dumps(entry)) for entry in self._audit)


__all__ = [
    "ClaudeHostBridge", "ConnectionCapability", "HostCall", "HostResult", "PROVIDER", "RUNTIME",
    "HANDLE_NAMESPACE", "ROUTE_QUALIFICATION", "LIVE_EVIDENCE_TASKS", "DEFAULT_EXEC_TIMEOUT",
]
