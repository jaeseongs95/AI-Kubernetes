"""Director bootstrap records: IDs, fixed codes and model names only, no secrets."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re

from aik_profiles import FallbackApproval
from aik_runtime import RuntimeStore
from aik_runtime.errors import InvalidRecord
from aik_runtime.models import MAX_INTEGER, identifier
from aik_supervisor import InvalidWorkspace, validate_workspace_id

from .errors import InvalidDirectorConfig, InvalidRunnerResult


LIFECYCLE_STATES = ("STOPPED", "STARTING", "RUNNING", "STOPPING", "FAILED")
CHECK_STATUSES = ("ready", "not_configured", "auth_required", "unavailable")
START_STATUSES = ("started", "already_running", "not_configured", "auth_required", "unavailable", "failed")
PROBE_STATUSES = ("ok", "auth_required", "unavailable")
RUNNER_HEALTH = ("alive", "degraded", "dead", "unknown")
SETUP_ACTIONS = (
    "select_model", "configure_profile", "configure_connection", "approve_fallback",
    "login", "fix_connection", "retry",
)
_CONFIG_ACTIONS = frozenset(("select_model", "configure_profile", "configure_connection", "approve_fallback"))
_CODE = re.compile(r"[A-Z][A-Z0-9_:]{0,95}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}\Z")
MAX_TTL_SECONDS = 3600.0


def safe_code(value: object) -> str:
    """Return a fixed upper-case code, or UNCLASSIFIED for anything else."""
    return value if type(value) is str and _CODE.fullmatch(value) is not None else "UNCLASSIFIED"


def is_identifier(value: object) -> bool:
    try:
        identifier(value)
    except InvalidRecord:
        return False
    return True


def _finite_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


@dataclass(frozen=True, slots=True)
class SetupIssue:
    """A concrete item the user must configure, log into, or retry."""

    field: str
    code: str
    action: str
    details: tuple = ()

    def to_dict(self) -> dict:
        return {"field": self.field, "code": self.code, "action": self.action, "details": list(self.details)}


def classify(issues) -> str:
    """not_configured > auth_required > unavailable, by the required actions."""
    actions = {issue.action for issue in issues}
    if actions & _CONFIG_ACTIONS:
        return "not_configured"
    if "login" in actions:
        return "auth_required"
    return "unavailable"


@dataclass(frozen=True, slots=True)
class CredentialScope:
    """Broker grant parameters; the director passes only IDs to its runner."""

    broker: object
    adapter_id: str
    lease_id: str
    fencing_token: int
    task_id: str = "director-session"
    ttl_seconds: float = 600.0
    max_uses: int = 1

    def __post_init__(self) -> None:
        if not callable(getattr(self.broker, "issue_grant", None)) or not callable(getattr(self.broker, "revoke", None)):
            raise InvalidDirectorConfig("The credential scope requires a broker with issue_grant and revoke.")
        for value in (self.adapter_id, self.lease_id, self.task_id):
            if not is_identifier(value):
                raise InvalidDirectorConfig("Invalid credential scope identifier.")
        if type(self.fencing_token) is not int or not 1 <= self.fencing_token <= MAX_INTEGER:
            raise InvalidDirectorConfig("Invalid credential scope fencing token.")
        if not _finite_number(self.ttl_seconds) or not 0 < self.ttl_seconds <= MAX_TTL_SECONDS:
            raise InvalidDirectorConfig("Invalid credential scope lifetime.")
        if type(self.max_uses) is not int or not 1 <= self.max_uses <= 16:
            raise InvalidDirectorConfig("Invalid credential scope use limit.")


@dataclass(frozen=True)
class DirectorConfig:
    """User-selected director resources. profile_id=None means not configured."""

    workspace_id: str
    profile_id: str | None = None
    inventory: object = None
    approval: FallbackApproval | None = None
    credential_scope: CredentialScope | None = None
    state_root: object = None
    runtime_store: RuntimeStore | None = None
    attach_timeout: float = 5.0

    def __post_init__(self) -> None:
        try:
            validate_workspace_id(self.workspace_id)
        except InvalidWorkspace:
            raise InvalidDirectorConfig("Invalid workspace identifier.") from None
        if self.profile_id is not None and not is_identifier(self.profile_id):
            raise InvalidDirectorConfig("Invalid director profile identifier.")
        if self.approval is not None and type(self.approval) is not FallbackApproval:
            raise InvalidDirectorConfig("A fallback approval must be a FallbackApproval.")
        if self.credential_scope is not None and type(self.credential_scope) is not CredentialScope:
            raise InvalidDirectorConfig("The credential scope must be a CredentialScope.")
        if self.runtime_store is not None and type(self.runtime_store) is not RuntimeStore:
            raise InvalidDirectorConfig("The runtime store must be an aik_runtime.RuntimeStore.")
        if not _finite_number(self.attach_timeout) or self.attach_timeout < 0:
            raise InvalidDirectorConfig("Attach timeout must be a finite nonnegative number.")


@dataclass(frozen=True, slots=True)
class DirectorBinding:
    """The verified (connection, model, effort, tools) the runner must use."""

    profile_id: str
    profile_revision: int
    connection_id: str
    model: str
    effort: str | None
    tools: tuple
    position: str


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Connection/login reachability; a probe must not run a model turn."""

    status: str
    code: str | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not str or self.status not in PROBE_STATUSES:
            raise InvalidRunnerResult("Unknown probe status.")
        if self.code is not None and safe_code(self.code) != self.code:
            raise InvalidRunnerResult("A probe code must be a fixed upper-case code.")


@dataclass(frozen=True, slots=True)
class StartRequest:
    """IDs only: the runner obtains secrets through its broker adapter, not here."""

    workspace_id: str
    instance_id: str
    binding: DirectorBinding
    grant_id: str | None


@dataclass(frozen=True, slots=True)
class RunnerSession:
    """A started session with the model/effort the runner actually observed."""

    session_id: str
    observed_model: str | None = None
    observed_effort: str | None = None

    def __post_init__(self) -> None:
        if not is_identifier(self.session_id):
            raise InvalidRunnerResult("Invalid runner session identifier.")
        if self.observed_model is not None and (
            type(self.observed_model) is not str or _MODEL.fullmatch(self.observed_model) is None
        ):
            raise InvalidRunnerResult("An observed model must be a bounded model identifier.")
        if self.observed_effort is not None and not is_identifier(self.observed_effort):
            raise InvalidRunnerResult("An observed effort must be an identifier.")


@dataclass(frozen=True, slots=True)
class Observation:
    """matches: True observed equal, False observed different, None not fully observed."""

    requested_model: str
    requested_effort: str | None
    observed_model: str | None
    observed_effort: str | None
    matches: bool | None

    @classmethod
    def of(cls, binding: DirectorBinding, session: RunnerSession) -> "Observation":
        mismatch = (
            (session.observed_model is not None and session.observed_model != binding.model)
            or (session.observed_effort is not None and session.observed_effort != binding.effort)
        )
        if mismatch:
            matches = False
        elif session.observed_model is not None and (binding.effort is None or session.observed_effort is not None):
            matches = True
        else:
            matches = None
        return cls(binding.model, binding.effort, session.observed_model, session.observed_effort, matches)


@dataclass(frozen=True, slots=True)
class CheckResult:
    status: str
    binding: DirectorBinding | None
    issues: tuple

    @property
    def ready(self) -> bool:
        return self.status == "ready"


@dataclass(frozen=True, slots=True)
class StartResult:
    status: str
    instance_id: str | None
    binding: DirectorBinding | None
    observation: Observation | None
    issues: tuple
    code: str | None = None


@dataclass(frozen=True, slots=True)
class StopResult:
    """scan_complete=False means in-flight jobs are unknown, not absent."""

    status: str
    in_flight_lease_ids: tuple
    scan_complete: bool
    inbox_leased_ids: tuple
    inbox_scan_complete: bool
    runner_stopped: bool


@dataclass(frozen=True, slots=True)
class DirectorEvent:
    sequence: int
    at: str | None
    event: str
    code: str | None
    instance_id: str | None
    details: tuple

    def to_dict(self) -> dict:
        return {
            "sequence": self.sequence, "at": self.at, "event": self.event, "code": self.code,
            "instance_id": self.instance_id,
            "details": {key: list(value) if type(value) is tuple else value for key, value in self.details},
        }
