"""Director lifecycle: verify -> own -> start runner -> observe; stop keeps jobs.

Order of start (fail-closed):
1. single ownership through aik_supervisor.launch (a live owner -> already_running);
2. director profile verification right before execution (aik_profiles);
3. runner connection/login probe (no model turn);
4. optional task-scoped credential grant (aik_broker), IDs only to the runner;
5. runner.start and actual model/effort observation; a mismatch stops it.
Any failure releases the grant and the supervisor lock and returns concrete
setup issues. stop() rejects new work and never deletes in-flight leases or
inbox items. Wake signals reach the runner only while RUNNING and only when
the inbox has leasable work.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import threading

from aik_broker.errors import BrokerError
from aik_config.errors import InvalidReference
from aik_inbox import InboxStore, WakePolicy
from aik_profiles import ExecutionDecision
from aik_profiles.models import ExecutionChoice
from aik_runtime.errors import StorageError
from aik_supervisor import Attachment, SupervisorError, launch

from .errors import DirectorError, InvalidDirectorConfig
from .models import (
    RUNNER_HEALTH, CheckResult, DirectorBinding, DirectorConfig, DirectorEvent, Observation,
    ProbeResult, RunnerSession, SetupIssue, StartRequest, StartResult, StopResult, classify,
    is_identifier, safe_code,
)


_RUNNER_METHODS = ("probe", "start", "stop", "health", "wake")
_BROKER_ISSUES = {
    "AUTH_REQUIRED": ("credential", "login"),
    "NO_BROKER_SECRET": ("credential", "configure_connection"),
    "ADAPTER_NOT_ALLOWED": ("credential", "configure_connection"),
    "CONNECTION_UNAVAILABLE": ("connection", "fix_connection"),
    "PROVIDER_MISMATCH": ("connection", "configure_connection"),
    "WORKSPACE_MISMATCH": ("connection", "configure_connection"),
}
_FALLBACK_REASONS = {
    "FALLBACK_NOT_APPROVED": "FALLBACK_APPROVAL_REQUIRED",
    "FALLBACK_APPROVAL_PROFILE_MISMATCH": "FALLBACK_APPROVAL_PROFILE_MISMATCH",
    "FALLBACK_APPROVAL_STALE_REVISION": "FALLBACK_APPROVAL_STALE_REVISION",
    "FALLBACK_INDEX_OUT_OF_RANGE": "FALLBACK_INDEX_OUT_OF_RANGE",
    "APPROVED_FALLBACK_REJECTED": "APPROVED_FALLBACK_REJECTED",
}
_PAGE = 1000


class DirectorRunner:
    """Interface for the component that owns the actual model/provider session.

    probe(binding) -> ProbeResult       connection/login check, no model turn
    start(request) -> RunnerSession     start a session, no model turn
    stop(session)                       stop the session; must not cancel VM jobs
    health(session) -> str              one of RUNNER_HEALTH, no model turn
    wake(session, signal)               the only path that may run a model turn
    """

    def probe(self, binding: DirectorBinding) -> ProbeResult:
        raise NotImplementedError

    def start(self, request: StartRequest) -> RunnerSession:
        raise NotImplementedError

    def stop(self, session: RunnerSession) -> None:
        raise NotImplementedError

    def health(self, session: RunnerSession) -> str:
        raise NotImplementedError

    def wake(self, session: RunnerSession, signal) -> None:
        raise NotImplementedError


def _issue(field: str, code: str, action: str, details: tuple = ()) -> tuple:
    return (SetupIssue(field, code, action, details),)


class Director:
    """One director per workspace; second starts report the existing owner."""

    def __init__(self, config: DirectorConfig, runner, profiles, *, inbox: InboxStore | None = None,
                 clock=None, event_sink=None, event_capacity: int = 1024):
        if type(config) is not DirectorConfig:
            raise InvalidDirectorConfig("A DirectorConfig is required.")
        if any(not callable(getattr(runner, name, None)) for name in _RUNNER_METHODS):
            raise InvalidDirectorConfig("The runner does not implement the director runner interface.")
        if not callable(getattr(profiles, "prepare_execution", None)):
            raise InvalidDirectorConfig("A profile gate with prepare_execution is required.")
        if inbox is not None and not isinstance(inbox, InboxStore):
            raise InvalidDirectorConfig("The inbox must be an aik_inbox.InboxStore.")
        if clock is not None and not callable(clock):
            raise InvalidDirectorConfig("The clock must be callable.")
        if event_sink is not None and not callable(event_sink):
            raise InvalidDirectorConfig("The event sink must be callable.")
        if type(event_capacity) is not int or not 1 <= event_capacity <= 65536:
            raise InvalidDirectorConfig("The event capacity must be between 1 and 65536.")
        self.config = config
        self._runner = runner
        self._profiles = profiles
        self._inbox = inbox
        self._clock = clock
        self._sink = event_sink
        self._mutex = threading.RLock()
        self._events = deque(maxlen=event_capacity)
        self._sequence = 0
        self.sink_failures = 0
        self._state = "STOPPED"
        self._supervisor = None
        self._session: RunnerSession | None = None
        self._binding: DirectorBinding | None = None
        self._grant_id: str | None = None
        self._observation: Observation | None = None
        self._issues: tuple = ()
        self._policy = None if inbox is None else WakePolicy(inbox, self._deliver_wake)

    # ------------------------------------------------------------ records
    @property
    def state(self) -> str:
        return self._state

    def events(self) -> tuple:
        with self._mutex:
            return tuple(self._events)

    def _instance_id(self) -> str | None:
        return None if self._supervisor is None else self._supervisor.instance_id

    def _timestamp(self) -> str | None:
        try:
            value = datetime.now(timezone.utc) if self._clock is None else self._clock()
        except Exception:
            return None
        if type(value) is not datetime or value.utcoffset() is None:
            return None
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _record(self, event: str, code: str | None = None, instance: str | None = None, **details) -> None:
        clean = []
        for key in sorted(details):
            value = details[key]
            if value is None or type(value) in (bool, int, str):
                clean.append((key, value))
            elif type(value) is tuple and all(type(item) is str for item in value):
                clean.append((key, value))
        self._sequence += 1
        record = DirectorEvent(self._sequence, self._timestamp(), event, None if code is None else safe_code(code),
                               instance if instance is not None else self._instance_id(), tuple(clean))
        self._events.append(record)
        if self._sink is not None:
            try:
                self._sink(record.to_dict())
            except Exception:
                self.sink_failures += 1

    # ----------------------------------------------------------- preflight
    def _rejection_issues(self, decision: ExecutionDecision) -> tuple:
        try:
            reasons = tuple(safe_code(reason) for reason in decision.reasons)
        except Exception:
            reasons = ()
        primary_details: tuple = ()
        try:
            for attempt in decision.attempts:
                if getattr(attempt, "position", None) == "primary":
                    primary_details = tuple(safe_code(str(reason)) for reason in attempt.reasons)
        except Exception:
            primary_details = ("UNCLASSIFIED",)
        issues = []
        if "REFERENCE_MISSING" in reasons:
            issues.append(SetupIssue("connection", "CONNECTION_NOT_CONFIGURED", "configure_connection"))
        if "CROSS_WORKSPACE_REFERENCE" in reasons:
            issues.append(SetupIssue("connection", "CONNECTION_OTHER_WORKSPACE", "configure_connection"))
        if "PRIMARY_REJECTED" in reasons:
            issues.append(SetupIssue("model", "MODEL_NOT_SUPPORTED", "select_model", primary_details))
        for reason in reasons:
            if reason in _FALLBACK_REASONS:
                issues.append(SetupIssue("fallback", _FALLBACK_REASONS[reason], "approve_fallback"))
        if not issues:
            issues.append(SetupIssue("director_profile", "PROFILE_REJECTED", "configure_profile", reasons))
        return tuple(issues)

    def _verify_profile(self):
        config = self.config
        if config.profile_id is None:
            return None, _issue("director_profile", "PROFILE_NOT_CONFIGURED", "select_model")
        try:
            decision = self._profiles.prepare_execution(config.profile_id, config.inventory,
                                                        approval=config.approval, clock=self._clock)
        except InvalidReference:
            return None, _issue("director_profile", "PROFILE_NOT_FOUND", "select_model")
        except StorageError:
            return None, _issue("director_profile", "PROFILE_STORE_UNAVAILABLE", "retry")
        except Exception:
            return None, _issue("director_profile", "PROFILE_CHECK_FAILED", "retry")
        if type(decision) is not ExecutionDecision:
            return None, _issue("director_profile", "PROFILE_CHECK_FAILED", "retry")
        try:
            role, workspace, profile_id = decision.role, decision.workspace_id, decision.profile_id
        except Exception:
            return None, _issue("director_profile", "PROFILE_CHECK_FAILED", "retry")
        if role != "director":
            return None, _issue("director_profile", "NOT_A_DIRECTOR_PROFILE", "configure_profile")
        if profile_id != config.profile_id or workspace != config.workspace_id:
            return None, _issue("director_profile", "PROFILE_SCOPE_MISMATCH", "configure_profile")
        if decision.status != "bound":
            return None, self._rejection_issues(decision)
        bound = decision.bound
        if type(bound) is not ExecutionChoice or type(decision.profile_revision) is not int \
                or type(decision.bound_position) is not str:
            return None, _issue("director_profile", "PROFILE_CHECK_FAILED", "retry")
        return DirectorBinding(profile_id, decision.profile_revision, bound.connection_id, bound.model,
                               bound.effort, tuple(bound.tools), decision.bound_position), ()

    def _probe(self, binding: DirectorBinding) -> tuple:
        try:
            result = self._runner.probe(binding)
        except Exception:
            return _issue("runner", "PROBE_FAILED", "retry")
        if type(result) is not ProbeResult:
            return _issue("runner", "PROBE_INVALID", "retry")
        if result.status == "auth_required":
            return _issue("login", result.code or "LOGIN_REQUIRED", "login")
        if result.status == "unavailable":
            return _issue("connection", result.code or "CONNECTION_UNAVAILABLE", "fix_connection")
        return ()

    def _issue_grant(self, binding: DirectorBinding):
        scope = self.config.credential_scope
        if scope is None:
            return None, ()
        try:
            grant = scope.broker.issue_grant(
                workspace_id=self.config.workspace_id, task_id=scope.task_id, adapter_id=scope.adapter_id,
                connection_ids=(binding.connection_id,), lease_id=scope.lease_id,
                fencing_token=scope.fencing_token, ttl_seconds=scope.ttl_seconds, max_uses=scope.max_uses,
            )
        except BrokerError as error:
            code = safe_code(getattr(error, "code", None))
            field, action = _BROKER_ISSUES.get(code, ("credential", "retry"))
            return None, _issue(field, code, action)
        except Exception:
            return None, _issue("credential", "CREDENTIAL_SCOPE_FAILED", "retry")
        grant_id = getattr(grant, "grant_id", None)
        if not is_identifier(grant_id):
            return None, _issue("credential", "CREDENTIAL_SCOPE_FAILED", "retry")
        return grant_id, ()

    def _preflight(self):
        binding, issues = self._verify_profile()
        if issues:
            return binding, None, issues
        issues = self._probe(binding)
        if issues:
            return binding, None, issues
        grant_id, issues = self._issue_grant(binding)
        return binding, grant_id, issues

    # ------------------------------------------------------------- release
    def _revoke(self, grant_id: str | None) -> None:
        scope = self.config.credential_scope
        if grant_id is None or scope is None:
            return
        try:
            scope.broker.revoke(grant_id)
        except Exception:
            self._record("grant_revoke_failed", "GRANT_REVOKE_FAILED")

    def _release(self, grant_id: str | None):
        """Revoke the grant and release ownership; return the shutdown report."""
        self._revoke(grant_id)
        supervisor, self._supervisor = self._supervisor, None
        if supervisor is None:
            return None
        try:
            return supervisor.shutdown()
        except Exception:
            self._record("supervisor_shutdown_failed", "SUPERVISOR_SHUTDOWN_FAILED",
                         instance=supervisor.instance_id)
            return None

    def _stop_runner(self, session) -> bool:
        try:
            self._runner.stop(session)
            return True
        except Exception:
            return False

    def _fail_start(self, status: str, issues: tuple, code: str, state: str, grant_id=None,
                    binding=None, observation=None) -> StartResult:
        instance = self._instance_id()
        self._record("start_failed", code, status=status, issue_codes=tuple(issue.code for issue in issues))
        self._release(grant_id)
        self._issues = issues
        self._state = state
        return StartResult(status, instance, binding, observation, issues, safe_code(code))

    # ----------------------------------------------------------------- API
    def check(self) -> CheckResult:
        """Verify profile, login and credential scope without owning or starting."""
        with self._mutex:
            binding, grant_id, issues = self._preflight()
            self._revoke(grant_id)
            status = "ready" if not issues else classify(issues)
            if self._state != "RUNNING":
                self._issues = issues
            self._record("check_passed" if not issues else "check_failed",
                         None if not issues else issues[0].code, status=status,
                         issue_codes=tuple(issue.code for issue in issues))
            return CheckResult(status, binding, issues)

    def start(self) -> StartResult:
        with self._mutex:
            if self._state in ("RUNNING", "STARTING", "STOPPING"):
                self._record("start_rejected_duplicate", "ALREADY_RUNNING")
                return StartResult("already_running", self._instance_id(), self._binding, self._observation,
                                   (), "ALREADY_RUNNING")
            self._state = "STARTING"
            self._issues = ()
            config = self.config
            try:
                owner = launch(config.workspace_id, component="director", state_root=config.state_root,
                               store=config.runtime_store, attach_timeout=config.attach_timeout)
            except (SupervisorError, StorageError, OSError):
                return self._fail_start("unavailable", _issue("supervisor", "SUPERVISOR_UNAVAILABLE", "retry"),
                                        "SUPERVISOR_UNAVAILABLE", "FAILED")
            if type(owner) is Attachment:
                self._state = "STOPPED"
                self._record("start_rejected_duplicate", "ALREADY_RUNNING", instance=owner.record.instance_id,
                             attached=True)
                return StartResult("already_running", owner.record.instance_id, None, None, (), "ALREADY_RUNNING")
            self._supervisor = owner
            grant_id = None
            try:
                binding, grant_id, issues = self._preflight()
                if issues:
                    return self._fail_start(classify(issues), issues, issues[0].code, "STOPPED", grant_id, binding)
                request = StartRequest(config.workspace_id, owner.instance_id, binding, grant_id)
                try:
                    session = self._runner.start(request)
                except Exception:
                    return self._fail_start("failed", _issue("runner", "RUNNER_START_FAILED", "retry"),
                                            "RUNNER_START_FAILED", "FAILED", grant_id, binding)
                if type(session) is not RunnerSession:
                    self._stop_runner(session)
                    return self._fail_start("failed", _issue("runner", "RUNNER_SESSION_INVALID", "retry"),
                                            "RUNNER_SESSION_INVALID", "FAILED", grant_id, binding)
                observation = Observation.of(binding, session)
                self._record("observed", None if observation.matches is not False else "OBSERVED_BINDING_MISMATCH",
                             session_id=session.session_id, requested_model=binding.model,
                             requested_effort=binding.effort, observed_model=observation.observed_model,
                             observed_effort=observation.observed_effort, matches=observation.matches)
                if observation.matches is False:
                    self._stop_runner(session)
                    return self._fail_start("failed",
                                            _issue("model", "OBSERVED_BINDING_MISMATCH", "select_model"),
                                            "OBSERVED_BINDING_MISMATCH", "FAILED", grant_id, binding, observation)
            except BaseException:
                self._release(grant_id)
                self._state = "FAILED"
                raise
            self._binding, self._session, self._grant_id, self._observation = binding, session, grant_id, observation
            self._state = "RUNNING"
            if self._policy is not None:
                self._policy.reset()
            self._record("started", profile_id=binding.profile_id, profile_revision=binding.profile_revision,
                         bound_position=binding.position, connection_id=binding.connection_id,
                         model=binding.model, effort=binding.effort, session_id=session.session_id,
                         grant_issued=grant_id is not None,
                         worker_placements=0)
            return StartResult("started", owner.instance_id, binding, observation, (), None)

    def _inbox_leased(self):
        if self._inbox is None:
            return (), True
        ids = []
        after = 0
        try:
            while True:
                page = self._inbox.list_messages("leased", limit=_PAGE, after_seq=after)
                ids.extend(record.message.message_id for record in page)
                if len(page) < _PAGE:
                    return tuple(ids), True
                after = page[-1].seq
        except Exception:
            return tuple(ids), False

    def stop(self) -> StopResult:
        """Reject new work and release ownership; in-flight jobs stay untouched."""
        with self._mutex:
            if self._state != "RUNNING":
                return StopResult("not_running", (), True, (), True, True)
            instance = self._instance_id()
            self._state = "STOPPING"
            self._record("stop_requested")
            runner_stopped = False
            inbox_ids, inbox_complete = (), False
            try:
                runner_stopped = self._stop_runner(self._session)
                if not runner_stopped:
                    self._record("stop_runner_failed", "RUNNER_STOP_FAILED")
                inbox_ids, inbox_complete = self._inbox_leased()
            finally:
                report = self._release(self._grant_id)
                self._session = self._binding = self._observation = None
                self._grant_id = None
                self._state = "STOPPED"
            lease_ids = () if report is None else tuple(report.in_flight_lease_ids)
            complete = report is not None and report.scan_complete is True
            self._record("stopped", None if runner_stopped else "RUNNER_STOP_FAILED", instance=instance,
                         in_flight_lease_ids=lease_ids, scan_complete=complete,
                         inbox_leased_ids=inbox_ids, inbox_scan_complete=inbox_complete,
                         runner_stopped=runner_stopped)
            return StopResult("stopped", lease_ids, complete, inbox_ids, inbox_complete, runner_stopped)

    def _deliver_wake(self, signal) -> None:
        if self._state != "RUNNING" or self._session is None:
            raise DirectorError("The director is not running.")
        self._runner.wake(self._session, signal)
        self._record("wake_delivered", available=signal.available)

    def poll(self):
        """Wake the runner once per new leasable inbox state; never when idle."""
        with self._mutex:
            if self._state != "RUNNING" or self._policy is None:
                return None
            try:
                return self._policy.poll()
            except Exception:
                self._record("wake_failed", "WAKE_FAILED")
                return None

    def health(self) -> dict:
        """In-process health payload; it never triggers a model turn."""
        with self._mutex:
            runner = "not_running"
            if self._state == "RUNNING":
                try:
                    value = self._runner.health(self._session)
                except Exception:
                    value = "unknown"
                runner = value if type(value) is str and value in RUNNER_HEALTH else "unknown"
            binding, observation = self._binding, self._observation
            return {
                "component": "director",
                "workspace_id": self.config.workspace_id,
                "state": self._state,
                "accepting_work": self._state == "RUNNING",
                "instance_id": self._instance_id(),
                "profile_id": binding.profile_id if binding else self.config.profile_id,
                "profile_revision": binding.profile_revision if binding else None,
                "bound_position": binding.position if binding else None,
                "connection_id": binding.connection_id if binding else None,
                "model": binding.model if binding else None,
                "effort": binding.effort if binding else None,
                "observed_model": observation.observed_model if observation else None,
                "observed_effort": observation.observed_effort if observation else None,
                "observation_match": observation.matches if observation else None,
                "runner": runner,
                "issues": [issue.to_dict() for issue in self._issues],
            }
