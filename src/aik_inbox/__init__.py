"""AIK director inbox, outbox, consumer and wake policy (AIK-025).

User instructions and AGS/VM events are persisted in a dedicated SQLite file
before the director sees them. This package does not call any model, does not
decide goal completion (VM owns that verdict) and stores no secret values.
"""

from .consumer import Consumer
from .errors import InboxError, InvalidInboxDatabase, InvalidMessage, LeaseLost, MessageConflict
from .models import (
    KINDS, MAX_PAYLOAD_BYTES, ORDER_STATUSES, OUTBOX_KINDS, OUTBOX_STATES, STATES,
    AckResult, InboxMessage, LeasedMessage, OutboxMessage, ReceiveResult, RecoveryReport,
    StoredMessage, StoredOutbox, WakeSnapshot,
)
from .store import APPLICATION_ID, SCHEMA_VERSION, InboxStore, default_inbox_path
from .wake import WakePolicy, WakeSignal

__all__ = [
    "InboxStore", "Consumer", "WakePolicy", "WakeSignal", "default_inbox_path",
    "APPLICATION_ID", "SCHEMA_VERSION",
    "InboxMessage", "OutboxMessage", "StoredMessage", "StoredOutbox", "LeasedMessage",
    "ReceiveResult", "AckResult", "RecoveryReport", "WakeSnapshot",
    "KINDS", "STATES", "ORDER_STATUSES", "OUTBOX_KINDS", "OUTBOX_STATES", "MAX_PAYLOAD_BYTES",
    "InboxError", "InvalidMessage", "MessageConflict", "LeaseLost", "InvalidInboxDatabase",
]
