"""Source events and view records of the non-authoritative read model.

Fields hold external IDs, cursors, kinds, states and digests. There is no
field for task bodies, output text, tool arguments or secret material.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import json
import re
from types import MappingProxyType

from aik_runtime.errors import InvalidRecord
from aik_runtime.models import MAX_INTEGER, identifier

from .errors import InvalidEvent


SOURCES = ("vm", "aik_runtime", "dispatch", "lifecycle", "evidence", "ags_peer")
REF_KEYS = (
    "vm_project_id", "vm_goal_id", "vm_task_id", "vm_attempt_id", "placement_id", "lease_id",
    "request_id", "provider_job_id", "artifact_id", "ags_workflow_id", "ags_session_id",
    "ags_peer_id", "ags_message_id", "worker_address", "worker_id",
)
# Attempt views traverse only through attempt-specific IDs. Shared IDs such as
# a project, task, worker or peer are reported but never expanded, so another
# attempt that reuses the same worker is not merged into this view.
EXPANDING_KEYS = frozenset({
    "vm_attempt_id", "placement_id", "lease_id", "request_id", "provider_job_id",
    "artifact_id", "ags_session_id", "ags_message_id",
})
PEER_STATES = ("delivered", "claimed", "acked", "result")
MAX_PAYLOAD_ENTRIES = 32
MAX_PAYLOAD_BYTES = 16 * 1024
MAX_TEXT = 1024
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_PAYLOAD_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_ADDRESS = re.compile(r"[\x21-\x7e]{1,256}\Z")
_DOCUMENT_KEYS = frozenset({"source", "cursor", "event_id", "user_scope", "kind", "refs", "payload"})


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def check_id(value: object, name: str) -> None:
    try:
        identifier(value, name)
    except InvalidRecord:
        raise InvalidEvent("INVALID_" + name.upper()) from None


def check_token(value: object, name: str) -> None:
    if type(value) is not str or _TOKEN.fullmatch(value) is None:
        raise InvalidEvent("INVALID_" + name.upper())


def check_ref(key: str, value: object) -> None:
    if key == "worker_address":
        if type(value) is not str or _ADDRESS.fullmatch(value) is None:
            raise InvalidEvent("INVALID_WORKER_ADDRESS")
    else:
        check_id(value, key)


def _check_payload_value(key: str, value: object) -> None:
    if key == "state":
        if value is not None:
            check_token(value, "state")
        return
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if not -MAX_INTEGER <= value <= MAX_INTEGER:
            raise InvalidEvent("INVALID_PAYLOAD_VALUE")
        return
    if type(value) is str:
        if len(value) > MAX_TEXT or any(ord(char) < 32 for char in value):
            raise InvalidEvent("INVALID_PAYLOAD_VALUE")
        return
    raise InvalidEvent("INVALID_PAYLOAD_VALUE")


@dataclass(frozen=True, slots=True)
class SourceEvent:
    """One event copied from an authoritative source ledger.

    ``cursor`` is the source's own monotonically increasing number (VM uses its
    history_cursor). ``digest`` is the sha256 of the canonical JSON of every
    other field and is recomputed on construction.
    """

    source: str
    cursor: int
    event_id: str
    user_scope: str
    kind: str
    refs: Mapping
    payload: Mapping = field(default_factory=dict)
    digest: str = field(init=False, default="", compare=False)

    def __post_init__(self) -> None:
        if type(self.source) is not str or self.source not in SOURCES:
            raise InvalidEvent("INVALID_SOURCE")
        if type(self.cursor) is not int or not 1 <= self.cursor <= MAX_INTEGER:
            raise InvalidEvent("INVALID_CURSOR")
        check_id(self.event_id, "event_id")
        check_id(self.user_scope, "user_scope")
        check_token(self.kind, "kind")
        if not isinstance(self.refs, Mapping) or len(self.refs) > len(REF_KEYS):
            raise InvalidEvent("INVALID_REFS")
        refs = {}
        for key, value in self.refs.items():
            if type(key) is not str or key not in REF_KEYS:
                raise InvalidEvent("INVALID_REF_KEY")
            check_ref(key, value)
            refs[key] = value
        if not isinstance(self.payload, Mapping) or len(self.payload) > MAX_PAYLOAD_ENTRIES:
            raise InvalidEvent("INVALID_PAYLOAD")
        payload = {}
        for key, value in self.payload.items():
            if type(key) is not str or _PAYLOAD_KEY.fullmatch(key) is None:
                raise InvalidEvent("INVALID_PAYLOAD_KEY")
            _check_payload_value(key, value)
            payload[key] = value
        object.__setattr__(self, "refs", MappingProxyType(dict(sorted(refs.items()))))
        object.__setattr__(self, "payload", MappingProxyType(dict(sorted(payload.items()))))
        text = canonical_json(self.to_document())
        if len(text.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise InvalidEvent("EVENT_TOO_LARGE")
        object.__setattr__(self, "digest", sha256_text(text))

    @property
    def state(self) -> str | None:
        return self.payload.get("state")

    def to_document(self) -> dict:
        return {
            "source": self.source, "cursor": self.cursor, "event_id": self.event_id,
            "user_scope": self.user_scope, "kind": self.kind,
            "refs": dict(self.refs), "payload": dict(self.payload),
        }

    @classmethod
    def from_document(cls, document: object) -> "SourceEvent":
        if not isinstance(document, Mapping) or set(document) != _DOCUMENT_KEYS:
            raise InvalidEvent("INVALID_EVENT_DOCUMENT")
        return cls(document["source"], document["cursor"], document["event_id"], document["user_scope"],
                   document["kind"], document["refs"], document["payload"])


def revalidate(event: object) -> SourceEvent:
    """Reconstruct exact instances so object.__setattr__ forgeries are checked again."""
    if type(event) is not SourceEvent:
        raise InvalidEvent("INVALID_EVENT")
    try:
        return SourceEvent.from_document(event.to_document())
    except InvalidEvent:
        raise
    except (AttributeError, TypeError, ValueError):
        raise InvalidEvent("INVALID_EVENT") from None


@dataclass(frozen=True, slots=True)
class ApplyReport:
    stored: int
    applied: int
    duplicates: int
    conflicts: int
    gaps_opened: int
    gaps_closed: int


@dataclass(frozen=True, slots=True)
class SourceState:
    """The latest applied event of one source; states of sources are never mixed."""

    source: str
    kind: str
    state: str | None
    cursor: int


@dataclass(frozen=True, slots=True)
class CursorStatus:
    source: str
    applied_cursor: int
    max_seen_cursor: int
    open_gaps: tuple
    conflicts: int

    @property
    def stale(self) -> bool:
        """True while newer events wait behind a cursor gap."""
        return bool(self.open_gaps)


@dataclass(frozen=True, slots=True)
class DeliveryState:
    ags_message_id: str | None
    state: str | None
    cursor: int


@dataclass(frozen=True, slots=True)
class AttemptView:
    """Display projection for one VM attempt. Never a VM verdict."""

    vm_attempt_id: str
    found: bool
    ids: Mapping
    states: Mapping
    cursors: Mapping
    stale: bool
    authoritative: bool = False


@dataclass(frozen=True, slots=True)
class PeerView:
    """Display projection of one AGS local peer's deliveries and worker addresses."""

    ags_peer_id: str
    found: bool
    worker_addresses: tuple
    worker_ids: tuple
    ags_session_ids: tuple
    deliveries: tuple
    history: tuple
    stale: bool
    authoritative: bool = False


@dataclass(frozen=True, slots=True)
class ConflictRecord:
    """A replay that disagreed with the stored event; the stored event was kept."""

    seq: int
    source: str
    cursor: int
    event_id: str
    code: str
    existing_digest: str
    observed_digest: str
    recorded_at: str
