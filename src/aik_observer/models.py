"""AIK-036 observer records. Fields hold IDs, digests and counts, never raw output.

An event payload is plain JSON. The observer stores only its digest, its size
and the structured values it extracts (model/effort/usage). Output text and
tool arguments are never persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Protocol, runtime_checkable

from aik_budget.models import UNKNOWN, BudgetError, Usage, confirmed
from aik_provider.contracts import JobReceipt, JobRequest, JobState
from aik_runtime.errors import InvalidRecord
from aik_runtime.models import MAX_INTEGER, identifier


EVENT_KINDS = ("output_delta", "model_reported", "usage", "state", "tool_call")
EVENT_PROVENANCES = ("provider_observed", "model_reported")
REQUESTED_PROVENANCE = "client_requested"
NOT_PROVIDED = "NOT_PROVIDED"
MAX_PAYLOAD_BYTES = 65536
# The last three are the AIK-093 token components (cache read, cache write and
# reasoning, which is already inside output_tokens); same names as aik_budget.Usage.
USAGE_FIELDS = ("api_cost_cents", "input_tokens", "output_tokens", "quota_used",
                "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}\Z")


class ObserverError(Exception):
    """Fixed code-only failure; the message never contains supplied values."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class InvalidEvent(ObserverError, ValueError):
    """An event or batch does not satisfy the event contract."""


class EventConflict(ObserverError):
    """The same sequence was delivered with different content (fail-closed)."""


class UnknownObservation(ObserverError):
    """The lease has no observation record."""


def check_id(value: object, field: str) -> None:
    try:
        identifier(value, field)
    except InvalidRecord:
        raise ObserverError("INVALID_" + field.upper()) from None


def check_model(value: object, field: str = "model") -> None:
    if type(value) is not str or _MODEL.fullmatch(value) is None:
        raise ObserverError("INVALID_" + field.upper())


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def parse_usage(payload: dict) -> Usage:
    """Convert only supplied usage fields. Missing or null stays UNKNOWN; 0 is confirmed(0)."""
    values = {}
    for name in USAGE_FIELDS:
        raw = payload.get(name)
        if raw is None:
            values[name] = UNKNOWN
        elif type(raw) is int and 0 <= raw <= MAX_INTEGER:
            values[name] = confirmed(raw)
        else:
            raise InvalidEvent("INVALID_USAGE_VALUE")
    try:
        return Usage(**values, quota_unit=payload.get("quota_unit"))
    except BudgetError:
        raise InvalidEvent("INVALID_USAGE") from None


def merge_usage(previous: Usage, report: Usage) -> Usage:
    """A later report restates the call total per field; unknown fields keep earlier values."""
    values = {}
    for name in USAGE_FIELDS:
        newer = getattr(report, name)
        values[name] = newer if newer.known else getattr(previous, name)
    if report.quota_used.known:
        unit = report.quota_unit
    elif previous.quota_used.known:
        unit = previous.quota_unit
    else:
        unit = report.quota_unit or previous.quota_unit
    return Usage(**values, quota_unit=unit)


def event_settings(payload: dict) -> tuple[str | None, str | None]:
    model = payload.get("model")
    effort = payload.get("effort")
    if model is not None:
        try:
            check_model(model)
        except ObserverError:
            raise InvalidEvent("INVALID_EVENT_MODEL") from None
    if effort is not None:
        try:
            check_id(effort, "effort")
        except ObserverError:
            raise InvalidEvent("INVALID_EVENT_EFFORT") from None
    return model, effort


@dataclass(frozen=True, slots=True)
class ProviderEvent:
    """One provider stream event. The payload is detached into plain JSON."""

    sequence: int
    event_id: str
    kind: str
    payload: dict
    provenance: str = "provider_observed"

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or not 1 <= self.sequence <= MAX_INTEGER:
            raise InvalidEvent("INVALID_EVENT_SEQUENCE")
        try:
            identifier(self.event_id, "event_id")
        except InvalidRecord:
            raise InvalidEvent("INVALID_EVENT_ID") from None
        if type(self.kind) is not str or self.kind not in EVENT_KINDS:
            raise InvalidEvent("INVALID_EVENT_KIND")
        if type(self.provenance) is not str or self.provenance not in EVENT_PROVENANCES:
            raise InvalidEvent("INVALID_EVENT_PROVENANCE")
        if type(self.payload) is not dict:
            raise InvalidEvent("INVALID_EVENT_PAYLOAD")
        try:
            text = canonical_json(self.payload)
            copy = json.loads(text)
        except (TypeError, ValueError, RecursionError):
            raise InvalidEvent("INVALID_EVENT_PAYLOAD") from None
        # A round trip rejects tuples, non-string keys and other non-JSON values.
        if copy != self.payload or len(text.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise InvalidEvent("INVALID_EVENT_PAYLOAD")
        object.__setattr__(self, "payload", copy)
        if self.kind in ("state", "model_reported"):
            event_settings(copy)
        elif self.kind == "usage":
            parse_usage(copy)

    @property
    def payload_bytes(self) -> int:
        return len(canonical_json(self.payload).encode("utf-8"))

    @property
    def digest(self) -> str:
        document = {"event_id": self.event_id, "kind": self.kind,
                    "payload": self.payload, "provenance": self.provenance}
        return "sha256:" + hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


@runtime_checkable
class JobPort(Protocol):
    def observe(self, request: JobRequest, receipt: JobReceipt) -> JobReceipt: ...


@runtime_checkable
class EventSource(Protocol):
    def read(self, after_sequence: int | None, limit: int) -> tuple[ProviderEvent, ...]: ...


@dataclass(frozen=True, slots=True)
class RequestedSettings:
    model: str
    effort: str | None
    provenance: str = REQUESTED_PROVENANCE


@dataclass(frozen=True, slots=True)
class ObservedSetting:
    """value None means not provided; reason then explains why (NOT_PROVIDED)."""

    value: str | None
    provenance: str
    source_sequence: int | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class ObservedSettings:
    model: ObservedSetting
    effort: ObservedSetting


@dataclass(frozen=True, slots=True)
class GapRecord:
    start_sequence: int
    end_sequence: int
    resolved: bool


@dataclass(frozen=True, slots=True)
class SettlementSubmission:
    kind: str  # settlement, late, release or unreported (as returned by the budget store)
    usage: Usage
    submitted_at: str


@dataclass(frozen=True, slots=True)
class IngestResult:
    applied: int
    duplicates: int
    gaps: int
    conflicts: int
    cursor: int
    stream_state: str  # ok, gap_unresolved, conflict
    disconnected: bool


@dataclass(frozen=True, slots=True)
class ObservationView:
    lease_id: str
    vm_attempt_id: str
    provider_job_id: str
    requested: RequestedSettings
    observed: ObservedSettings
    model_reported: ObservedSettings
    job_state: JobState
    terminal: bool
    usage: Usage
    cursor: int
    stream_state: str
    gaps: tuple[GapRecord, ...]
    conflicts: int
    output_events: int
    output_bytes: int
    tool_calls: int
    settlements: tuple[SettlementSubmission, ...]
