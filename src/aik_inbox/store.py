"""Durable inbox/outbox in a dedicated SQLite file.

Path validation and the cooperating process lock are reused from
aik_runtime.paths. Every operation opens a fresh connection under that lock
and runs in one IMMEDIATE transaction with DELETE journal and FULL sync.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from functools import lru_cache
import hmac
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import uuid

from aik_runtime.errors import StorageBusy
from aik_runtime.models import MAX_INTEGER
from aik_runtime.paths import database_lock, default_database_path, validate_database_path

from .errors import InboxError, InvalidInboxDatabase, InvalidMessage, LeaseLost, MessageConflict
from .models import (
    KINDS, ORDER_STATUSES, OUTBOX_KINDS, OUTBOX_STATES, STATES,
    AckResult, InboxMessage, LeasedMessage, OutboxMessage, ReceiveResult, RecoveryReport,
    StoredMessage, StoredOutbox, WakeSnapshot, canonical_payload, check_identifier, fingerprint,
)
from aik_platform.fs import sync_directory as _sync_directory


APPLICATION_ID = 0x41494B49  # AIKI; distinct from runtime (AIKR) and AIKC.
SCHEMA_VERSION = 1
DEFAULT_LEASE_SECONDS = 60.0
MAX_LEASE_SECONDS = 3600.0
MAX_RETRY_SECONDS = 86400.0
MAX_OUTBOX_PER_ACK = 32
_TOKEN = re.compile(r"[0-9a-f]{32}\Z")


def _in(values: tuple) -> str:
    return ", ".join(f"'{value}'" for value in values)


_STATEMENTS = (
    "CREATE TABLE meta (name TEXT PRIMARY KEY NOT NULL, value INTEGER NOT NULL CHECK (typeof(value) = 'integer'))",
    "CREATE TABLE cursors (source TEXT PRIMARY KEY NOT NULL, "
    "last_cursor INTEGER NOT NULL CHECK (typeof(last_cursor) = 'integer' AND last_cursor >= 1))",
    "CREATE TABLE inbox (\n"
    "    seq INTEGER PRIMARY KEY,\n"
    "    message_id TEXT NOT NULL UNIQUE,\n"
    "    goal_id TEXT NOT NULL,\n"
    "    source TEXT NOT NULL,\n"
    "    event_cursor INTEGER NOT NULL CHECK (typeof(event_cursor) = 'integer' AND event_cursor >= 1),\n"
    f"    kind TEXT NOT NULL CHECK (kind IN ({_in(KINDS)})),\n"
    "    payload TEXT NOT NULL,\n"
    "    fingerprint TEXT NOT NULL,\n"
    f"    order_status TEXT NOT NULL CHECK (order_status IN ({_in(ORDER_STATUSES)})),\n"
    f"    state TEXT NOT NULL CHECK (state IN ({_in(STATES)})),\n"
    "    attempts INTEGER NOT NULL CHECK (typeof(attempts) = 'integer' AND attempts >= 0),\n"
    "    lease_owner TEXT,\n"
    "    lease_token TEXT,\n"
    "    lease_expires_ms INTEGER,\n"
    "    available_ms INTEGER NOT NULL,\n"
    "    received_ms INTEGER NOT NULL,\n"
    "    acked_ms INTEGER,\n"
    "    last_error TEXT,\n"
    "    CHECK ((state = 'leased') = (lease_expires_ms IS NOT NULL))\n"
    ")",
    "CREATE INDEX inbox_state_seq ON inbox (state, seq)",
    "CREATE TABLE outbox (\n"
    "    seq INTEGER PRIMARY KEY,\n"
    "    outbox_id TEXT NOT NULL UNIQUE,\n"
    "    message_id TEXT NOT NULL REFERENCES inbox (message_id) ON DELETE RESTRICT ON UPDATE RESTRICT,\n"
    "    goal_id TEXT NOT NULL,\n"
    f"    kind TEXT NOT NULL CHECK (kind IN ({_in(OUTBOX_KINDS)})),\n"
    "    payload TEXT NOT NULL,\n"
    "    fingerprint TEXT NOT NULL,\n"
    f"    state TEXT NOT NULL CHECK (state IN ({_in(OUTBOX_STATES)})),\n"
    "    created_ms INTEGER NOT NULL,\n"
    "    delivered_ms INTEGER\n"
    ")",
    "CREATE INDEX outbox_state_seq ON outbox (state, seq)",
)

_AVAILABLE = ("(state = 'received' OR (state = 'failed' AND available_ms <= :now) "
              "OR (state = 'leased' AND lease_expires_ms <= :now))")
_MESSAGE_COLUMNS = ("seq, message_id, goal_id, source, event_cursor, kind, payload, order_status, state, "
                    "attempts, lease_owner, lease_expires_ms, received_ms, acked_ms, last_error, "
                    "lease_token, fingerprint")
_OUTBOX_COLUMNS = "seq, outbox_id, message_id, goal_id, kind, payload, state, created_ms, delivered_ms, fingerprint"


def default_inbox_path() -> Path:
    """Beside the runtime DB: %LOCALAPPDATA%\\AIK\\runtime\\inbox.sqlite3 on Windows."""
    return default_database_path().parent / "inbox.sqlite3"


def create_schema(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in _STATEMENTS:
            connection.execute(statement)
        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.execute("INSERT INTO meta (name, value) VALUES ('change_counter', 0)")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _schema_rows(connection: sqlite3.Connection) -> tuple:
    return tuple(tuple(row) for row in connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ))


@lru_cache(maxsize=1)
def _expected_schema() -> tuple:
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
        create_schema(connection)
        return _schema_rows(connection)


def _validate_schema(connection: sqlite3.Connection, *, full: bool) -> None:
    try:
        app_id = connection.execute("PRAGMA application_id").fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if app_id != APPLICATION_ID or version != SCHEMA_VERSION:
            raise InvalidInboxDatabase("This is not a supported AIK inbox database.")
        if _schema_rows(connection) != _expected_schema():
            raise InvalidInboxDatabase("The inbox schema differs from its declared version.")
        if full:
            if tuple(row[0] for row in connection.execute("PRAGMA integrity_check")) != ("ok",):
                raise InvalidInboxDatabase("Inbox database integrity validation failed.")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise InvalidInboxDatabase("Inbox database references are inconsistent.")
            if connection.execute("SELECT 1 FROM meta WHERE name = 'change_counter'").fetchone() is None:
                raise InvalidInboxDatabase("The inbox change counter is missing.")
    except sqlite3.Error:
        raise InvalidInboxDatabase("Inbox database validation could not be completed.") from None


def _sqlite_failure(error: sqlite3.Error) -> None:
    if getattr(error, "sqlite_errorcode", 0) & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        raise StorageBusy("SQLite could not acquire the inbox database lock.") from None
    raise InboxError("The inbox database operation could not be completed.") from None


def _seconds_ms(value: object, name: str, *, maximum: float, positive: bool) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise InvalidMessage(f"{name} must be a finite number.")
    if value < 0 or value > maximum or (positive and value == 0):
        raise InvalidMessage(f"{name} is out of range.")
    return max(1 if positive else 0, int(math.ceil(value * 1000)))


def _check_token(token: object) -> None:
    if type(token) is not str or _TOKEN.fullmatch(token) is None:
        raise LeaseLost("The lease token is not valid.")


def _check_after_seq(value: object) -> None:
    if type(value) is not int or not 0 <= value <= MAX_INTEGER:
        raise InvalidMessage("after_seq must be a nonnegative bounded integer.")


def _decode(row) -> StoredMessage:
    try:
        message = InboxMessage(row[1], row[2], row[3], row[4], row[5], json.loads(row[6]))
        return StoredMessage(message=message, seq=row[0], order=row[7], state=row[8], attempts=row[9],
                             lease_owner=row[10], lease_expires_ms=row[11], received_ms=row[12],
                             acked_ms=row[13], last_error=row[14])
    except (InvalidMessage, TypeError, ValueError, IndexError):
        raise InvalidInboxDatabase("A stored inbox record violates its contract.") from None


def _decode_outbox(row) -> StoredOutbox:
    try:
        message = OutboxMessage(row[1], row[3], row[4], json.loads(row[5]))
        return StoredOutbox(message=message, seq=row[0], source_message_id=row[2], state=row[6],
                            created_ms=row[7], delivered_ms=row[8])
    except (InvalidMessage, TypeError, ValueError, IndexError):
        raise InvalidInboxDatabase("A stored outbox record violates its contract.") from None


def _fetch(connection: sqlite3.Connection, message_id: str):
    return connection.execute(f"SELECT {_MESSAGE_COLUMNS} FROM inbox WHERE message_id = ?", (message_id,)).fetchone()


def _bump(connection: sqlite3.Connection) -> None:
    connection.execute("UPDATE meta SET value = value + 1 WHERE name = 'change_counter'")


def _require_lease(row, token: str, now: int) -> None:
    if (row is None or row[8] != "leased" or row[15] is None
            or not hmac.compare_digest(row[15], token) or row[11] <= now):
        raise LeaseLost("The lease is absent, expired, or owned by another consumer.")


class InboxStore:
    """Persist director inputs; ACK records handling, never goal completion."""

    def __init__(self, path: Path | str | None = None, *, timeout: float = 5.0, clock=None,
                 max_attempts: int = 5, reclaim_all_leases: bool = False):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
            raise InvalidMessage("Storage timeout must be a finite nonnegative number.")
        if clock is not None and not callable(clock):
            raise InvalidMessage("The clock must be callable.")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 1000:
            raise InvalidMessage("max_attempts must be an integer between 1 and 1000.")
        if type(reclaim_all_leases) is not bool:
            raise InvalidMessage("reclaim_all_leases must be a boolean.")
        self.timeout = float(timeout)
        self.max_attempts = max_attempts
        self._clock = clock if clock is not None else time.time
        self.path = validate_database_path(default_inbox_path() if path is None else path, create_parent=True)
        with database_lock(self.path, self.timeout):
            self._prepare()
        # Restart recovery: leases left by a dead process return to the queue.
        self.last_recovery = self.recover(release_all_leases=reclaim_all_leases)

    @property
    def clock(self):
        return self._clock

    def _now_ms(self) -> int:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise InboxError("The injected clock returned an invalid time.")
        return int(value * 1000)

    # -- files and connections -------------------------------------------------

    def _companion(self, suffix: str) -> Path:
        return self.path.with_name(self.path.name + suffix)

    def _check_files(self) -> None:
        for suffix in ("-journal", "-wal", "-shm"):
            candidate = self._companion(suffix)
            validate_database_path(candidate)
            if suffix != "-journal" and candidate.exists():
                raise InvalidInboxDatabase("Inbox storage requires a DELETE-mode database without WAL files.")

    def _connect(self, path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=self.timeout, isolation_level=None)
        try:
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA foreign_keys = ON")
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
                raise InvalidInboxDatabase("Inbox storage requires DELETE journal mode.")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except BaseException:
            connection.close()
            raise

    def _create(self) -> None:
        descriptor, name = tempfile.mkstemp(prefix=".aik-inbox-init-", suffix=".sqlite3", dir=self.path.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            with closing(self._connect(temporary)) as connection:
                create_schema(connection)
                _validate_schema(connection, full=True)
            os.chmod(temporary, 0o600)
            with temporary.open("r+b") as stream:
                os.fsync(stream.fileno())
            validate_database_path(self.path)
            if self.path.exists():
                raise InboxError("The inbox destination changed during initialization.")
            os.replace(temporary, self.path)
            _sync_directory(self.path.parent)
        finally:
            for leftover in (temporary, temporary.with_name(temporary.name + "-journal")):
                if leftover.exists():
                    leftover.unlink()

    def _prepare(self) -> None:
        self._check_files()
        try:
            if not self.path.exists():
                if self._companion("-journal").exists():
                    raise InvalidInboxDatabase("An orphan rollback journal cannot initialize the inbox.")
                self._create()
                return
            with closing(self._connect(self.path)) as connection:
                _validate_schema(connection, full=True)
        except sqlite3.Error:
            raise InvalidInboxDatabase("The inbox database could not be validated.") from None
        except OSError:
            raise InboxError("The inbox database files could not be processed safely.") from None

    @contextmanager
    def _transaction(self):
        with database_lock(self.path, self.timeout):
            self._check_files()
            if not self.path.exists():
                raise InvalidInboxDatabase("The inbox database is missing.")
            try:
                connection = self._connect(self.path)
            except sqlite3.Error as error:
                _sqlite_failure(error)
            try:
                _validate_schema(connection, full=False)
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except BaseException as error:
                if connection.in_transaction:
                    try:
                        connection.rollback()
                    except sqlite3.Error:
                        pass
                if isinstance(error, sqlite3.Error):
                    _sqlite_failure(error)
                raise
            finally:
                connection.close()

    # -- inbox -----------------------------------------------------------------

    def receive(self, message: InboxMessage) -> ReceiveResult:
        """Persist once per event; detect cursor regressions per source.

        An event is a duplicate when its message_id is already stored, or when
        the same source and event_cursor already carry identical content
        (goal_id, kind, payload) under another message_id. Duplicates return
        the stored record and change nothing.
        """
        if type(message) is not InboxMessage:
            raise InvalidMessage("Only InboxMessage records can be received.")
        # Rebuild: object.__setattr__ can bypass a frozen dataclass initializer.
        message = InboxMessage(message.message_id, message.goal_id, message.source,
                               message.event_cursor, message.kind, message.payload)
        payload = canonical_payload(message.payload)
        digest = fingerprint(message.goal_id, message.source, message.event_cursor, message.kind, payload)
        now = self._now_ms()
        with self._transaction() as connection:
            row = _fetch(connection, message.message_id)
            if row is not None:
                if not hmac.compare_digest(row[16], digest):
                    raise MessageConflict("A different event already uses this message_id; the stored event was kept.")
                return ReceiveResult("duplicate", _decode(row))
            last = connection.execute("SELECT last_cursor FROM cursors WHERE source = ?", (message.source,)).fetchone()
            if last is None or message.event_cursor > last[0]:
                # No stored event of this source can have a cursor above the
                # recorded maximum, so an in-order event cannot be a replay.
                order = "in_order"
                connection.execute(
                    "INSERT INTO cursors (source, last_cursor) VALUES (?, ?) "
                    "ON CONFLICT (source) DO UPDATE SET last_cursor = excluded.last_cursor",
                    (message.source, message.event_cursor),
                )
            else:
                same_event = connection.execute(
                    f"SELECT {_MESSAGE_COLUMNS} FROM inbox WHERE source = ? AND event_cursor = ? "
                    "AND fingerprint = ? ORDER BY seq LIMIT 1",
                    (message.source, message.event_cursor, digest),
                ).fetchone()
                if same_event is not None:
                    return ReceiveResult("duplicate", _decode(same_event))
                order = "out_of_order"
            connection.execute(
                "INSERT INTO inbox (message_id, goal_id, source, event_cursor, kind, payload, fingerprint, "
                "order_status, state, attempts, available_ms, received_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'received', 0, ?, ?)",
                (message.message_id, message.goal_id, message.source, message.event_cursor, message.kind,
                 payload, digest, order, now, now),
            )
            _bump(connection)
            return ReceiveResult("accepted", _decode(_fetch(connection, message.message_id)))

    def _reclaim(self, connection: sqlite3.Connection, now: int, release_all: bool) -> RecoveryReport:
        if release_all:
            rows = connection.execute(
                "SELECT seq, message_id, attempts FROM inbox WHERE state = 'leased' ORDER BY seq").fetchall()
        else:
            rows = connection.execute(
                "SELECT seq, message_id, attempts FROM inbox WHERE state = 'leased' AND lease_expires_ms <= ? "
                "ORDER BY seq", (now,)).fetchall()
        reclaimed, dead = [], []
        for seq, message_id, attempts in rows:
            if attempts >= self.max_attempts:
                connection.execute(
                    "UPDATE inbox SET state = 'dead', lease_owner = NULL, lease_token = NULL, "
                    "lease_expires_ms = NULL, last_error = 'lease_expired' WHERE seq = ?", (seq,))
                dead.append(message_id)
            else:
                connection.execute(
                    "UPDATE inbox SET state = 'received', lease_owner = NULL, lease_token = NULL, "
                    "lease_expires_ms = NULL, available_ms = ?, last_error = 'lease_expired' WHERE seq = ?",
                    (now, seq))
                reclaimed.append(message_id)
        if reclaimed:
            _bump(connection)
        return RecoveryReport(tuple(reclaimed), tuple(dead))

    def recover(self, *, release_all_leases: bool = False) -> RecoveryReport:
        """Return expired (or, for a sole restarted owner, all) leases to the queue."""
        if type(release_all_leases) is not bool:
            raise InvalidMessage("release_all_leases must be a boolean.")
        now = self._now_ms()
        with self._transaction() as connection:
            return self._reclaim(connection, now, release_all_leases)

    def lease_next(self, consumer_id: str, *, lease_seconds: float = DEFAULT_LEASE_SECONDS) -> LeasedMessage | None:
        """Lease the oldest available item, or return None for an idle queue."""
        check_identifier(consumer_id, "consumer_id")
        duration = _seconds_ms(lease_seconds, "lease_seconds", maximum=MAX_LEASE_SECONDS, positive=True)
        now = self._now_ms()
        with self._transaction() as connection:
            self._reclaim(connection, now, False)
            row = connection.execute(
                f"SELECT {_MESSAGE_COLUMNS} FROM inbox WHERE {_AVAILABLE} ORDER BY seq LIMIT 1", {"now": now},
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            expires = now + duration
            connection.execute(
                "UPDATE inbox SET state = 'leased', attempts = attempts + 1, lease_owner = ?, lease_token = ?, "
                "lease_expires_ms = ? WHERE seq = ?", (consumer_id, token, expires, row[0]))
            return LeasedMessage(_decode(_fetch(connection, row[1])), token, expires)

    def _outbox_items(self, outbox) -> tuple:
        if type(outbox) not in (tuple, list) or len(outbox) > MAX_OUTBOX_PER_ACK:
            raise InvalidMessage("Outbox items must be a short tuple or list.")
        items = []
        for item in outbox:
            if type(item) is not OutboxMessage:
                raise InvalidMessage("Only OutboxMessage records can be queued.")
            rebuilt = OutboxMessage(item.outbox_id, item.goal_id, item.kind, item.payload)
            payload = canonical_payload(rebuilt.payload)
            items.append((rebuilt, payload, fingerprint(rebuilt.goal_id, rebuilt.kind, payload)))
        if len({item[0].outbox_id for item in items}) != len(items):
            raise InvalidMessage("Outbox identifiers must be unique within one ACK.")
        return tuple(items)

    def ack(self, message_id: str, lease_token: str, outbox=()) -> AckResult:
        """Mark the message handled and queue follow-ups in the same transaction.

        Repeating an ACK with the same token is idempotent only for outbox
        items already recorded for this message; new items are rejected.
        """
        check_identifier(message_id, "message_id")
        _check_token(lease_token)
        items = self._outbox_items(outbox)
        now = self._now_ms()
        with self._transaction() as connection:
            row = _fetch(connection, message_id)
            already = (row is not None and row[8] == "acked" and row[15] is not None
                       and hmac.compare_digest(row[15], lease_token))
            if not already:
                _require_lease(row, lease_token, now)
                connection.execute(
                    "UPDATE inbox SET state = 'acked', acked_ms = ?, lease_expires_ms = NULL WHERE seq = ?",
                    (now, row[0]))
            ids = []
            for item, payload, digest in items:
                existing = connection.execute(
                    "SELECT message_id, fingerprint FROM outbox WHERE outbox_id = ?", (item.outbox_id,)).fetchone()
                if existing is not None:
                    if existing[0] != message_id or not hmac.compare_digest(existing[1], digest):
                        raise MessageConflict("A different outbox item already uses this outbox_id.")
                elif already:
                    raise MessageConflict("An acknowledged message cannot gain new outbox items.")
                else:
                    connection.execute(
                        "INSERT INTO outbox (outbox_id, message_id, goal_id, kind, payload, fingerprint, state, "
                        "created_ms) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                        (item.outbox_id, message_id, item.goal_id, item.kind, payload, digest, now))
                ids.append(item.outbox_id)
            return AckResult(_decode(_fetch(connection, message_id)), tuple(ids))

    def fail(self, message_id: str, lease_token: str, error_code: str, *,
             retry_after: float = 0.0, permanent: bool = False) -> StoredMessage:
        """Record a processing failure: retry later (failed) or stop (dead)."""
        check_identifier(message_id, "message_id")
        _check_token(lease_token)
        check_identifier(error_code, "error_code")
        delay = _seconds_ms(retry_after, "retry_after", maximum=MAX_RETRY_SECONDS, positive=False)
        if type(permanent) is not bool:
            raise InvalidMessage("permanent must be a boolean.")
        now = self._now_ms()
        with self._transaction() as connection:
            row = _fetch(connection, message_id)
            _require_lease(row, lease_token, now)
            state = "dead" if permanent or row[9] >= self.max_attempts else "failed"
            connection.execute(
                "UPDATE inbox SET state = ?, lease_owner = NULL, lease_token = NULL, lease_expires_ms = NULL, "
                "available_ms = ?, last_error = ? WHERE seq = ?", (state, now + delay, error_code, row[0]))
            if state == "failed":
                _bump(connection)
            return _decode(_fetch(connection, message_id))

    def get(self, message_id: str) -> StoredMessage | None:
        check_identifier(message_id, "message_id")
        with self._transaction() as connection:
            row = _fetch(connection, message_id)
            return None if row is None else _decode(row)

    def list_messages(self, state: str | None = None, *, limit: int = 100, after_seq: int = 0) -> tuple:
        if state is not None and (type(state) is not str or state not in STATES):
            raise InvalidMessage("Unknown inbox state.")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise InvalidMessage("The query limit must be an integer between 1 and 1000.")
        _check_after_seq(after_seq)
        with self._transaction() as connection:
            if state is None:
                rows = connection.execute(
                    f"SELECT {_MESSAGE_COLUMNS} FROM inbox WHERE seq > ? ORDER BY seq LIMIT ?", (after_seq, limit))
            else:
                rows = connection.execute(
                    f"SELECT {_MESSAGE_COLUMNS} FROM inbox WHERE seq > ? AND state = ? ORDER BY seq LIMIT ?",
                    (after_seq, state, limit))
            return tuple(_decode(row) for row in rows)

    def wake_snapshot(self) -> WakeSnapshot:
        """Count items the director could lease now; key changes on queue mutation."""
        now = self._now_ms()
        with self._transaction() as connection:
            # Settle expired leases first: an item at max_attempts becomes dead
            # here instead of being counted as leasable work.
            self._reclaim(connection, now, False)
            counter = connection.execute("SELECT value FROM meta WHERE name = 'change_counter'").fetchone()
            if counter is None:
                raise InvalidInboxDatabase("The inbox change counter is missing.")
            rows = connection.execute(
                f"SELECT kind, count(*), max(seq), sum(seq), sum(attempts) FROM inbox WHERE {_AVAILABLE} "
                "GROUP BY kind ORDER BY kind", {"now": now}).fetchall()
        total = sum(row[1] for row in rows)
        key = (counter[0], total, max((row[2] for row in rows), default=0),
               sum(row[3] for row in rows), sum(row[4] for row in rows))
        return WakeSnapshot(total, tuple((row[0], row[1]) for row in rows), key)

    # -- outbox ----------------------------------------------------------------

    def pending_outbox(self, *, limit: int = 100, after_seq: int = 0) -> tuple:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise InvalidMessage("The query limit must be an integer between 1 and 1000.")
        _check_after_seq(after_seq)
        with self._transaction() as connection:
            rows = connection.execute(
                f"SELECT {_OUTBOX_COLUMNS} FROM outbox WHERE state = 'pending' AND seq > ? ORDER BY seq LIMIT ?",
                (after_seq, limit))
            return tuple(_decode_outbox(row) for row in rows)

    def get_outbox(self, outbox_id: str) -> StoredOutbox | None:
        check_identifier(outbox_id, "outbox_id")
        with self._transaction() as connection:
            row = connection.execute(f"SELECT {_OUTBOX_COLUMNS} FROM outbox WHERE outbox_id = ?", (outbox_id,)).fetchone()
            return None if row is None else _decode_outbox(row)

    def mark_delivered(self, outbox_id: str) -> StoredOutbox:
        """Delivery to the owning service only; never goal or task completion.

        There is no delivery claim or fencing: delivery is at-least-once and
        requires a single delivering owner plus outbox_id idempotency downstream.
        """
        check_identifier(outbox_id, "outbox_id")
        now = self._now_ms()
        with self._transaction() as connection:
            row = connection.execute(f"SELECT {_OUTBOX_COLUMNS} FROM outbox WHERE outbox_id = ?", (outbox_id,)).fetchone()
            if row is None:
                raise InboxError("The outbox item does not exist.")
            if row[6] == "pending":
                connection.execute("UPDATE outbox SET state = 'delivered', delivered_ms = ? WHERE seq = ?", (now, row[0]))
                row = connection.execute(f"SELECT {_OUTBOX_COLUMNS} FROM outbox WHERE seq = ?", (row[0],)).fetchone()
            return _decode_outbox(row)
