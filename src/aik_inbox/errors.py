"""Inbox errors carry classifications only, never payload or secret values."""

from aik_runtime.errors import StorageError


class InboxError(StorageError):
    """The inbox operation could not be completed."""


class InvalidMessage(InboxError, ValueError):
    """A message, outbox item or argument violates the bounded contract."""


class MessageConflict(InboxError):
    """A different record already uses this idempotency key; the stored one was kept."""


class LeaseLost(InboxError):
    """The caller's lease is absent, expired, or belongs to another consumer."""


class InvalidInboxDatabase(InboxError):
    """The file is damaged, foreign, or uses an unsupported inbox schema."""
