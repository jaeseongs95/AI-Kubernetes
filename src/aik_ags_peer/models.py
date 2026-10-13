"""Bounded value types and validation helpers for the PEER bridge."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re

from aik_runtime.errors import InvalidRecord
from aik_runtime.models import identifier

from .errors import AgsPeerError


PEER_STATES = ("delivered", "claimed", "acked", "result")
SEND_PHASES = ("intent", "sent", "duplicate", "send_unknown", "send_failed")
UNKNOWN_DELIVERY = "UNKNOWN_DELIVERY"
_ENDPOINT = re.compile(r"[\x21-\x7e]{1,256}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")


def is_identifier(value: object) -> bool:
    try:
        identifier(value)
    except InvalidRecord:
        return False
    return True


def require_identifier(value: object, code: str = "INVALID_ARGUMENT") -> str:
    if not is_identifier(value):
        raise AgsPeerError(code)
    return value  # type: ignore[return-value]


def optional_identifier(value: object, code: str = "INVALID_ARGUMENT") -> str | None:
    if value is None:
        return None
    return require_identifier(value, code)


def endpoint_part(value: object, code: str = "INVALID_SESSION") -> str:
    """AGS host/sessionId: printable ASCII without whitespace, 1..256 chars."""
    if type(value) is not str or _ENDPOINT.fullmatch(value) is None:
        raise AgsPeerError(code)
    return value


def session_identity(value: object) -> tuple[str, str]:
    """Normalize an AGS identity given as (host, session_id)."""
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return endpoint_part(value[0]), endpoint_part(value[1])
    raise AgsPeerError("INVALID_SESSION")


def identity_from_document(value: object) -> tuple[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"host", "sessionId"}:
        raise AgsPeerError("INVALID_SESSION")
    return endpoint_part(value["host"]), endpoint_part(value["sessionId"])


def identity_document(identity: tuple[str, str]) -> dict:
    return {"host": identity[0], "sessionId": identity[1]}


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def event_id(message_id: str, state: str) -> str:
    return "aikp-" + hashlib.sha256(f"{message_id}\0{state}".encode("utf-8")).hexdigest()[:40]


def utc_now(clock) -> str:
    value = datetime.now(timezone.utc) if clock is None else clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AgsPeerError("INVALID_CLOCK")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("non-finite constant")


def strict_json_loads(text: str):
    """json.loads that rejects duplicate keys and NaN/Infinity."""
    return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


def _timestamp(value: object, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if type(value) is not str or not 1 <= len(value) <= 64:
        raise AgsPeerError("INVALID_RECORD")


@dataclass(frozen=True, slots=True)
class WorkerAddress:
    """Binding of an AIK worker address to one AGS {host, sessionId}."""

    worker_id: str
    worker_address: str
    host: str
    session_id: str
    ags_peer_id: str
    lease_id: str | None
    vm_attempt_id: str | None
    bound_at: str

    def __post_init__(self) -> None:
        require_identifier(self.worker_id, "INVALID_RECORD")
        if self.worker_address != "aik-worker:" + self.worker_id:
            raise AgsPeerError("INVALID_RECORD")
        endpoint_part(self.host, "INVALID_RECORD")
        endpoint_part(self.session_id, "INVALID_RECORD")
        require_identifier(self.ags_peer_id, "INVALID_RECORD")
        optional_identifier(self.lease_id, "INVALID_RECORD")
        optional_identifier(self.vm_attempt_id, "INVALID_RECORD")
        _timestamp(self.bound_at)

    @property
    def identity(self) -> tuple[str, str]:
        return self.host, self.session_id


@dataclass(frozen=True, slots=True)
class SendRecord:
    """One append-only send step. Bodies are never stored, only sha256/length."""

    seq: int
    ags_message_id: str
    state: str
    sender: tuple
    target: tuple
    worker_id: str
    worker_address: str
    ags_peer_id: str
    vm_attempt_id: str | None
    body_sha256: str
    body_length: int
    expires_at: str | None
    error_code: str | None
    recorded_at: str

    def __post_init__(self) -> None:
        if type(self.seq) is not int or self.seq < 1 or self.state not in SEND_PHASES:
            raise AgsPeerError("INVALID_RECORD")
        require_identifier(self.ags_message_id, "INVALID_RECORD")
        for identity in (self.sender, self.target):
            if type(identity) is not tuple or len(identity) != 2:
                raise AgsPeerError("INVALID_RECORD")
            endpoint_part(identity[0], "INVALID_RECORD")
            endpoint_part(identity[1], "INVALID_RECORD")
        require_identifier(self.worker_id, "INVALID_RECORD")
        require_identifier(self.ags_peer_id, "INVALID_RECORD")
        optional_identifier(self.vm_attempt_id, "INVALID_RECORD")
        if self.worker_address != "aik-worker:" + self.worker_id:
            raise AgsPeerError("INVALID_RECORD")
        if type(self.body_sha256) is not str or _SHA256.fullmatch(self.body_sha256) is None:
            raise AgsPeerError("INVALID_RECORD")
        if type(self.body_length) is not int or self.body_length < 0:
            raise AgsPeerError("INVALID_RECORD")
        _timestamp(self.expires_at, optional=True)
        if self.error_code is not None and (type(self.error_code) is not str or _CODE.fullmatch(self.error_code) is None):
            raise AgsPeerError("INVALID_RECORD")
        _timestamp(self.recorded_at)


@dataclass(frozen=True, slots=True)
class PeerEvent:
    """One recorded PEER step; ``cursor`` is continuous from 1."""

    cursor: int
    event_id: str
    state: str
    ags_message_id: str
    ags_peer_id: str
    ags_session_id: str | None
    worker_address: str | None
    worker_id: str | None
    vm_attempt_id: str | None
    body_sha256: str | None
    recorded_at: str

    def __post_init__(self) -> None:
        if type(self.cursor) is not int or self.cursor < 1 or self.state not in PEER_STATES:
            raise AgsPeerError("INVALID_RECORD")
        if self.event_id != event_id(self.ags_message_id, self.state):
            raise AgsPeerError("INVALID_RECORD")
        require_identifier(self.ags_message_id, "INVALID_RECORD")
        require_identifier(self.ags_peer_id, "INVALID_RECORD")
        optional_identifier(self.ags_session_id, "INVALID_RECORD")
        optional_identifier(self.worker_id, "INVALID_RECORD")
        optional_identifier(self.vm_attempt_id, "INVALID_RECORD")
        if self.worker_address is not None and (
            self.worker_id is None or self.worker_address != "aik-worker:" + self.worker_id
        ):
            raise AgsPeerError("INVALID_RECORD")
        if self.body_sha256 is not None and (
            type(self.body_sha256) is not str or _SHA256.fullmatch(self.body_sha256) is None
        ):
            raise AgsPeerError("INVALID_RECORD")
        _timestamp(self.recorded_at)

    def to_peer_message(self) -> dict:
        """Exactly the keys accepted by aik_readmodel.from_peer_message; None values omitted."""
        message = {
            "event_id": self.event_id,
            "state": self.state,
            "ags_message_id": self.ags_message_id,
            "ags_peer_id": self.ags_peer_id,
        }
        for key in ("ags_session_id", "worker_address", "worker_id", "vm_attempt_id"):
            value = getattr(self, key)
            if value is not None:
                message[key] = value
        return message


@dataclass(frozen=True, slots=True)
class ObserveResult:
    """``status`` is the AGS state string, or UNKNOWN_DELIVERY when AGS returned null."""

    ags_message_id: str
    status: str
    events: tuple

    @property
    def unknown(self) -> bool:
        return self.status == UNKNOWN_DELIVERY


@dataclass(frozen=True, slots=True)
class ResultMessage:
    """One claimed message. Only kind=="result" from a bound worker gets ``event``."""

    message_id: str
    sender: tuple
    recipient: tuple
    body: str
    envelope: object
    worker: WorkerAddress | None
    event: PeerEvent | None
    delivery_attempt: int
    created_at: str | None

    @property
    def is_result(self) -> bool:
        return self.envelope is not None and getattr(self.envelope, "kind", None) == "result"
