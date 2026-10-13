"""PEER bridge between the AIK orchestrator and worker sessions over AGS.

Rules taken from the AGS lifecycle contract: the broker issues message IDs;
an unknown delivery is never resent automatically (only an explicit
``resend_known`` of the same ID, which AGS treats as a duplicate); ACK means
"processed", not "task complete" or "approved"; there is no exactly-once.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .addresses import AddressBook
from .envelope import decode
from .errors import AgsPeerError
from .journal import PeerJournal
from .models import (
    UNKNOWN_DELIVERY, ObserveResult, PeerEvent, ResultMessage, SendRecord, WorkerAddress,
    event_id, identity_document, identity_from_document, is_identifier, require_identifier,
    session_identity, sha256_hex, utc_now,
)


MAX_TTL_SECONDS = 86400
_STATUS_FIELDS = (("firstDeliveredAt", "delivered"), ("claimedAt", "claimed"), ("acknowledgedAt", "acked"))


def _invalid() -> AgsPeerError:
    return AgsPeerError("AGS_INVALID_RESPONSE", outcome="unknown")


def _optional_timestamp(value: object) -> str | None:
    return value if type(value) is str and 1 <= len(value) <= 64 else None


class PeerBridge:
    def __init__(self, cli, addresses: AddressBook, journal_path: Path | str, *,
                 user_scope: str, clock=None, timeout: float = 5.0):
        if not callable(getattr(cli, "call", None)) or not isinstance(addresses, AddressBook):
            raise AgsPeerError("INVALID_ARGUMENT")
        require_identifier(user_scope, "INVALID_USER_SCOPE")
        if clock is not None and not callable(clock):
            raise AgsPeerError("INVALID_CLOCK")
        self._cli = cli
        self._addresses = addresses
        self._user_scope = user_scope
        self._clock = clock
        self._journal = PeerJournal(journal_path, timeout=timeout)

    @property
    def user_scope(self) -> str:
        return self._user_scope

    @property
    def journal(self) -> PeerJournal:
        return self._journal

    # -- sending -------------------------------------------------------------

    def send(self, sender, worker_id: str, envelope_body: str, *, ttl_seconds: int = 600) -> SendRecord:
        sender = session_identity(sender)
        require_identifier(worker_id)
        if decode(envelope_body) is None:
            raise AgsPeerError("INVALID_ENVELOPE")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= MAX_TTL_SECONDS:
            raise AgsPeerError("INVALID_ARGUMENT")
        address = self._addresses.resolve_worker(worker_id)
        if address is None:
            raise AgsPeerError("UNKNOWN_WORKER")
        target = address.identity
        data = self._cli.call("prepare", {
            "sender": identity_document(sender), "target": identity_document(target),
            "body": envelope_body, "ttlSeconds": ttl_seconds,
        })
        message_id = data.get("messageId") if isinstance(data, Mapping) else None
        if not is_identifier(message_id) or self._journal.intent(message_id) is not None:
            raise _invalid()
        intent = self._journal.record_send({
            "ags_message_id": message_id, "phase": "intent", "sender": sender, "target": target,
            "worker_id": address.worker_id, "worker_address": address.worker_address,
            "ags_peer_id": address.ags_peer_id, "vm_attempt_id": address.vm_attempt_id,
            "body_sha256": sha256_hex(envelope_body), "body_length": len(envelope_body.encode("utf-8")),
            "expires_at": _optional_timestamp(data.get("expiresAt")), "error_code": None,
            "recorded_at": utc_now(self._clock),
        })
        return self._send_prepared(intent)

    def resend_known(self, message_id: str) -> SendRecord:
        """Explicit re-send of an already prepared ID; AGS reports it as duplicate if delivered."""
        require_identifier(message_id)
        intent = self._journal.intent(message_id)
        if intent is None:
            raise AgsPeerError("UNKNOWN_MESSAGE")
        return self._send_prepared(intent)

    def _follow(self, intent: SendRecord, phase: str, error_code: str | None) -> dict:
        return {
            "ags_message_id": intent.ags_message_id, "phase": phase, "sender": intent.sender,
            "target": intent.target, "worker_id": intent.worker_id, "worker_address": intent.worker_address,
            "ags_peer_id": intent.ags_peer_id, "vm_attempt_id": intent.vm_attempt_id,
            "body_sha256": intent.body_sha256, "body_length": intent.body_length,
            "expires_at": intent.expires_at, "error_code": error_code, "recorded_at": utc_now(self._clock),
        }

    def _send_prepared(self, intent: SendRecord) -> SendRecord:
        try:
            data = self._cli.call("send", {
                "sender": identity_document(intent.sender), "messageId": intent.ags_message_id,
            })
        except AgsPeerError as error:
            if error.outcome == "unknown":
                # AGS guidance: delivery is unknown; never resend automatically.
                return self._journal.record_send(self._follow(intent, "send_unknown", error.code))
            self._journal.record_send(self._follow(intent, "send_failed", error.code))
            raise
        if (not isinstance(data, Mapping) or data.get("messageId") != intent.ags_message_id
                or type(data.get("duplicate")) is not bool):
            return self._journal.record_send(self._follow(intent, "send_unknown", "AGS_INVALID_RESPONSE"))
        return self._journal.record_send(self._follow(intent, "duplicate" if data["duplicate"] else "sent", None))

    def send_history(self, message_id: str) -> tuple[SendRecord, ...]:
        require_identifier(message_id)
        return self._journal.send_history(message_id)

    # -- observing -----------------------------------------------------------

    def _event_row(self, message_id: str, state: str, address: WorkerAddress, body_sha256: str | None) -> dict:
        return {
            "event_id": event_id(message_id, state), "state": state, "ags_message_id": message_id,
            "ags_peer_id": address.ags_peer_id,
            "ags_session_id": address.session_id if is_identifier(address.session_id) else None,
            "worker_address": address.worker_address, "worker_id": address.worker_id,
            "vm_attempt_id": address.vm_attempt_id, "body_sha256": body_sha256,
            "recorded_at": utc_now(self._clock),
        }

    def observe(self, sender, message_id: str) -> ObserveResult:
        sender = session_identity(sender)
        require_identifier(message_id)
        intent = self._journal.intent(message_id)
        if intent is None:
            raise AgsPeerError("UNKNOWN_MESSAGE")
        if intent.sender != sender:
            raise AgsPeerError("SENDER_MISMATCH")
        data = self._cli.call("status", {"sender": identity_document(sender), "messageId": message_id})
        if not isinstance(data, Mapping) or "status" not in data:
            raise _invalid()
        status = data["status"]
        if status is None:
            # Unknown delivery: record nothing and do not resend.
            return ObserveResult(message_id, UNKNOWN_DELIVERY, ())
        if not isinstance(status, Mapping) or status.get("messageId") != message_id:
            raise _invalid()
        state = status.get("state")
        if type(state) is not str or not 1 <= len(state) <= 64:
            raise _invalid()
        reached = []
        for field, name in _STATUS_FIELDS:
            value = status.get(field)
            if value is None:
                continue
            if type(value) is not str:
                raise _invalid()
            reached.append(name)
        address = self._addresses.resolve_worker(intent.worker_id)
        if address is None:
            raise AgsPeerError("INVALID_STORE")
        events = self._journal.record_events(
            [self._event_row(message_id, name, address, intent.body_sha256) for name in reached]
        )
        return ObserveResult(message_id, state, events)

    # -- receiving -----------------------------------------------------------

    def claim_results(self, orchestrator, *, max_messages: int | None = None) -> tuple[ResultMessage, ...]:
        """Claim messages for the orchestrator; never acknowledges them."""
        orchestrator = session_identity(orchestrator)
        payload: dict = {"target": identity_document(orchestrator)}
        if max_messages is not None:
            if type(max_messages) is not int or not 1 <= max_messages <= 100:
                raise AgsPeerError("INVALID_ARGUMENT")
            payload["maxMessages"] = max_messages
        data = self._cli.call("claim", payload)
        messages = data.get("messages") if isinstance(data, Mapping) else None
        if not isinstance(messages, list):
            raise _invalid()
        parsed = []
        for item in messages:
            if not isinstance(item, Mapping):
                raise _invalid()
            message_id = item.get("messageId")
            body = item.get("body")
            attempt = item.get("deliveryAttempt")
            created = item.get("createdAt")
            try:
                sender = identity_from_document(item.get("sender"))
                recipient = identity_from_document(item.get("recipient"))
            except AgsPeerError:
                raise _invalid() from None
            if (not is_identifier(message_id) or type(body) is not str or recipient != orchestrator
                    or type(attempt) is not int or attempt < 1
                    or (created is not None and type(created) is not str)):
                raise _invalid()
            parsed.append((message_id, sender, recipient, body, attempt, created))
        results = []
        for message_id, sender, recipient, body, attempt, created in parsed:
            envelope = decode(body)
            worker = None
            event = None
            if envelope is not None and envelope.kind == "result":
                worker = self._addresses.resolve_session(*sender)
                if worker is not None:
                    self._journal.record_events([self._event_row(message_id, "result", worker, sha256_hex(body))])
                    event = self._journal.get_event(message_id, "result")
            results.append(ResultMessage(message_id, sender, recipient, body, envelope, worker, event, attempt, created))
        return tuple(results)

    def acknowledge(self, orchestrator, message_ids) -> int:
        """Explicit ACK after the caller processed messages. ACK is not completion."""
        orchestrator = session_identity(orchestrator)
        if not isinstance(message_ids, (list, tuple)) or not 1 <= len(message_ids) <= 1000:
            raise AgsPeerError("INVALID_ARGUMENT")
        ids = [require_identifier(value) for value in message_ids]
        data = self._cli.call("acknowledge", {"target": identity_document(orchestrator), "messageIds": ids})
        count = data.get("acknowledged") if isinstance(data, Mapping) else None
        if type(count) is not int or not 0 <= count <= len(ids):
            raise _invalid()
        return count

    def pending(self, target) -> int:
        target = session_identity(target)
        data = self._cli.call("pending", {"target": identity_document(target)})
        count = data.get("count") if isinstance(data, Mapping) else None
        if type(count) is not int or count < 0:
            raise _invalid()
        return count

    # -- read model export ---------------------------------------------------

    def events_after(self, cursor: int = 0, *, limit: int = 1000) -> tuple[tuple[dict, int], ...]:
        """(message, cursor) pairs for aik_readmodel.from_peer_message(message, cursor, user_scope)."""
        return tuple((event.to_peer_message(), event.cursor)
                     for event in self._journal.events_after(cursor, limit=limit))

    def to_source_events(self, cursor: int = 0, *, limit: int = 1000) -> tuple:
        """Convenience conversion; imports aik_readmodel only when called."""
        from aik_readmodel.adapters import from_peer_message

        return tuple(from_peer_message(message, position, self._user_scope)
                     for message, position in self.events_after(cursor, limit=limit))

    def recorded_events(self, cursor: int = 0, *, limit: int = 1000) -> tuple[PeerEvent, ...]:
        return self._journal.events_after(cursor, limit=limit)
