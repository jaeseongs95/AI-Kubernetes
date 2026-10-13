"""Thin consumer bound to one consumer ID and lease duration."""

from __future__ import annotations

from .errors import InvalidMessage
from .models import AckResult, LeasedMessage, StoredMessage, check_identifier
from .store import DEFAULT_LEASE_SECONDS, InboxStore


class Consumer:
    """Lease, ACK or fail inbox items. ACK is handling, not goal completion."""

    def __init__(self, store: InboxStore, consumer_id: str, *, lease_seconds: float = DEFAULT_LEASE_SECONDS):
        if not isinstance(store, InboxStore):
            raise InvalidMessage("The consumer requires an InboxStore.")
        check_identifier(consumer_id, "consumer_id")
        self.store = store
        self.consumer_id = consumer_id
        self.lease_seconds = lease_seconds

    def next(self) -> LeasedMessage | None:
        return self.store.lease_next(self.consumer_id, lease_seconds=self.lease_seconds)

    def ack(self, leased: LeasedMessage, outbox=()) -> AckResult:
        if type(leased) is not LeasedMessage:
            raise InvalidMessage("ACK requires a leased message.")
        return self.store.ack(leased.record.message.message_id, leased.lease_token, outbox)

    def fail(self, leased: LeasedMessage, error_code: str, *, retry_after: float = 0.0,
             permanent: bool = False) -> StoredMessage:
        if type(leased) is not LeasedMessage:
            raise InvalidMessage("A failure report requires a leased message.")
        return self.store.fail(leased.record.message.message_id, leased.lease_token, error_code,
                               retry_after=retry_after, permanent=permanent)
