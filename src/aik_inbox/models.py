"""Bounded inbox/outbox records. Payloads are small JSON objects without secrets."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import re

from aik_runtime.errors import InvalidRecord
from aik_runtime.models import MAX_INTEGER, identifier

from .errors import InvalidMessage


KINDS = ("user_instruction", "execution_result", "error", "system")
STATES = ("received", "leased", "acked", "failed", "dead")
ORDER_STATUSES = ("in_order", "out_of_order")
OUTBOX_KINDS = ("vm_request", "ags_request", "user_notice")
OUTBOX_STATES = ("pending", "delivered")
MAX_PAYLOAD_BYTES = 16 * 1024
MAX_PAYLOAD_DEPTH = 8
MAX_KEY_LENGTH = 128

# Field names that suggest secret material are rejected (fail-closed). The
# list is heuristic: it can reject harmless names such as "max_tokens".
_SECRET_KEY_MARKERS = (
    "password", "passwd", "passphrase", "secret", "token", "apikey", "accesskey",
    "privatekey", "authorization", "cookie", "sessionkey",
)
_SECRET_VALUE = re.compile(
    r"(?i)(?:\bbearer\s+\S|-----BEGIN [A-Z ]*PRIVATE KEY|\bsk-[A-Za-z0-9_-]{16,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}|\bxox[abpr]-[A-Za-z0-9-]{8,})"
)


def check_identifier(value: object, name: str) -> None:
    try:
        identifier(value, name)
    except InvalidRecord:
        raise InvalidMessage(f"Invalid identifier field: {name}.") from None


def _check_value(value: object, depth: int) -> None:
    if depth > MAX_PAYLOAD_DEPTH:
        raise InvalidMessage("The payload is nested too deeply.")
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if abs(value) > MAX_INTEGER:
            raise InvalidMessage("A payload integer is out of range.")
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise InvalidMessage("A payload number must be finite.")
        return
    if type(value) is str:
        if _SECRET_VALUE.search(value):
            raise InvalidMessage("A payload value looks like secret material and was rejected.")
        return
    if type(value) is list:
        for item in value:
            _check_value(item, depth + 1)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str or not 1 <= len(key) <= MAX_KEY_LENGTH:
                raise InvalidMessage("Payload keys must be short strings.")
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if any(marker in normalized for marker in _SECRET_KEY_MARKERS):
                raise InvalidMessage("A payload field name suggests secret material and was rejected.")
            _check_value(item, depth + 1)
        return
    raise InvalidMessage("Payloads may contain only JSON objects, arrays, strings, numbers, booleans and null.")


def canonical_payload(payload: object) -> str:
    """Return canonical JSON for a bounded secret-free object, or reject it."""
    if type(payload) is not dict:
        raise InvalidMessage("A payload must be a JSON object.")
    _check_value(payload, 1)
    try:
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        size = len(text.encode("utf-8"))
    except (ValueError, TypeError, UnicodeEncodeError):
        raise InvalidMessage("The payload is not encodable JSON.") from None
    if size > MAX_PAYLOAD_BYTES:
        raise InvalidMessage("The payload exceeds the inbox size limit.")
    return text


def fingerprint(*parts: object) -> str:
    encoded = json.dumps(list(parts), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class InboxMessage:
    """An incoming instruction or event. message_id is the idempotency key."""

    message_id: str
    goal_id: str
    source: str
    event_cursor: int
    kind: str
    payload: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        check_identifier(self.message_id, "message_id")
        check_identifier(self.goal_id, "goal_id")
        check_identifier(self.source, "source")
        if type(self.event_cursor) is not int or not 1 <= self.event_cursor <= MAX_INTEGER:
            raise InvalidMessage("event_cursor must be a positive integer.")
        if type(self.kind) is not str or self.kind not in KINDS:
            raise InvalidMessage("Unknown inbox message kind.")
        canonical_payload(self.payload)


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    """A follow-up request recorded atomically with an ACK; delivery is separate."""

    outbox_id: str
    goal_id: str
    kind: str
    payload: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        check_identifier(self.outbox_id, "outbox_id")
        check_identifier(self.goal_id, "goal_id")
        if type(self.kind) is not str or self.kind not in OUTBOX_KINDS:
            raise InvalidMessage("Unknown outbox message kind.")
        canonical_payload(self.payload)


@dataclass(frozen=True, slots=True)
class StoredMessage:
    message: InboxMessage
    seq: int
    order: str
    state: str
    attempts: int
    lease_owner: str | None
    lease_expires_ms: int | None
    received_ms: int
    acked_ms: int | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class StoredOutbox:
    message: OutboxMessage
    seq: int
    source_message_id: str
    state: str
    created_ms: int
    delivered_ms: int | None


@dataclass(frozen=True, slots=True)
class LeasedMessage:
    record: StoredMessage
    lease_token: str
    lease_expires_ms: int


@dataclass(frozen=True, slots=True)
class ReceiveResult:
    """status is 'accepted' or 'duplicate' (idempotently ignored)."""

    status: str
    record: StoredMessage


@dataclass(frozen=True, slots=True)
class AckResult:
    """ACK means the director handled the message. It is not goal completion."""

    record: StoredMessage
    outbox_ids: tuple
    goal_completion: str = "not_determined"


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    reclaimed: tuple
    dead: tuple


@dataclass(frozen=True, slots=True)
class WakeSnapshot:
    available: int
    kinds: tuple
    key: tuple
