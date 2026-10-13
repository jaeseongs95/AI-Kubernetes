"""Append-only dispatch journal in its own SQLite file.

The file follows aik_runtime path rules (no development ledger, no Git
repository, no symlink/hardlink alias) and the cooperating process lock. All
three tables refuse UPDATE and DELETE through triggers. A current state is
the last event; the same state is never written twice.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import sqlite3
from types import MappingProxyType
from typing import Any, Callable, Mapping

from aik_runtime.errors import StorageBusy, StorageError
from aik_runtime.paths import database_lock, validate_database_path

from .errors import DispatchError
from .identity import DispatchIdentity, canonical_json


APPLICATION_ID = 0x41494B44  # AIKD; not the runtime, budget, effect or observer databases.
SCHEMA_VERSION = 1
DISPATCH_STATES = ("reserved", "sent_ack", "started", "send_unknown", "rejected")
TOOL_STATES = ("intent", "receipt", "denied", "effect_unknown", "returned")
_NEXT_DISPATCH = {"reserved": ("sent_ack", "send_unknown", "rejected"), "sent_ack": ("started",)}
_TABLES = ("dispatches", "dispatch_events", "tool_events")


def _states_sql(states) -> str:
    return ", ".join(f"'{state}'" for state in states)


_SCHEMA = (
    'CREATE TABLE "dispatches" (\n'
    '    "request_id" TEXT PRIMARY KEY NOT NULL CHECK (typeof("request_id") = \'text\'),\n'
    '    "identity_json" TEXT NOT NULL,\n'
    '    "identity_digest" TEXT NOT NULL,\n'
    '    "created_at" TEXT NOT NULL\n'
    ')',
    'CREATE TABLE "dispatch_events" (\n'
    '    "seq" INTEGER PRIMARY KEY,\n'
    '    "request_id" TEXT NOT NULL REFERENCES "dispatches" ("request_id") ON DELETE RESTRICT ON UPDATE RESTRICT,\n'
    f'    "state" TEXT NOT NULL CHECK ("state" IN ({_states_sql(DISPATCH_STATES)})),\n'
    '    "data_json" TEXT NOT NULL,\n'
    '    "at" TEXT NOT NULL,\n'
    '    UNIQUE ("request_id", "state")\n'
    ')',
    'CREATE TABLE "tool_events" (\n'
    '    "seq" INTEGER PRIMARY KEY,\n'
    '    "request_id" TEXT NOT NULL REFERENCES "dispatches" ("request_id") ON DELETE RESTRICT ON UPDATE RESTRICT,\n'
    '    "tool_call_id" TEXT NOT NULL,\n'
    f'    "state" TEXT NOT NULL CHECK ("state" IN ({_states_sql(TOOL_STATES)})),\n'
    '    "data_json" TEXT NOT NULL,\n'
    '    "at" TEXT NOT NULL,\n'
    '    UNIQUE ("request_id", "tool_call_id", "state")\n'
    ')',
    'CREATE INDEX "tool_events_request" ON "tool_events" ("request_id", "seq")',
    *(
        f'CREATE TRIGGER "{table}_no_{action.lower()}" BEFORE {action} ON "{table}" '
        "BEGIN SELECT RAISE(ABORT, 'append-only dispatch journal'); END"
        for table in _TABLES for action in ("UPDATE", "DELETE")
    ),
)


def _schema_rows(connection: sqlite3.Connection) -> tuple:
    return tuple(tuple(row) for row in connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ))


@lru_cache(maxsize=1)
def _expected_schema() -> tuple:
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
        for statement in _SCHEMA:
            connection.execute(statement)
        return _schema_rows(connection)


@dataclass(frozen=True, slots=True)
class DispatchEvent:
    seq: int
    state: str
    data: Mapping[str, Any]
    at: str


@dataclass(frozen=True, slots=True)
class ToolEvent:
    seq: int
    tool_call_id: str
    state: str
    data: Mapping[str, Any]
    at: str


@dataclass(frozen=True, slots=True)
class DispatchRecord:
    """Journal view. ``state`` is the last dispatch event, or None (API path registration)."""

    identity: DispatchIdentity
    state: str | None
    events: tuple

    @property
    def provider_job_id(self) -> str | None:
        for event in self.events:
            if event.state == "sent_ack":
                return event.data.get("provider_job_id")
        return None


def _decode_data(text: str) -> Mapping[str, Any]:
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError
    return MappingProxyType(value)


class DispatchJournal:
    """AIK-owned append-only journal. Its records are AIK observations, never VM verdicts."""

    def __init__(self, path: Path | str, *, clock: Callable[[], datetime] | None = None, timeout: float = 5.0):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) \
                or timeout < 0:
            raise ValueError("Journal timeout must be a finite nonnegative number.")
        self._timeout = float(timeout)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._path = validate_database_path(path, create_parent=True)
        with self._session(create=True):
            pass

    @property
    def path(self) -> Path:
        return self._path

    # -- connection handling -------------------------------------------------

    def _connect(self, create: bool) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._path.as_uri() + ("?mode=rwc" if create else "?mode=rw"),
            uri=True, timeout=self._timeout, isolation_level=None,
        )
        try:
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA foreign_keys = ON")
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
                raise DispatchError("INVALID_JOURNAL", subject="journal_mode")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except BaseException:
            connection.close()
            raise

    def _check(self, connection: sqlite3.Connection, *, initialize: bool) -> None:
        app_id = connection.execute("PRAGMA application_id").fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        empty = connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is None
        if initialize and app_id == 0 and version == 0 and empty:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass
            return
        if app_id != APPLICATION_ID or version != SCHEMA_VERSION:
            raise DispatchError("INVALID_JOURNAL", subject="header")
        if _schema_rows(connection) != _expected_schema():
            raise DispatchError("INVALID_JOURNAL", subject="schema")
        if initialize:
            if tuple(row[0] for row in connection.execute("PRAGMA integrity_check")) != ("ok",):
                raise DispatchError("INVALID_JOURNAL", subject="integrity")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise DispatchError("INVALID_JOURNAL", subject="references")

    @contextmanager
    def _session(self, *, create: bool = False):
        try:
            with database_lock(self._path, self._timeout):
                if not create and not self._path.is_file():
                    raise DispatchError("INVALID_JOURNAL", subject="missing")
                with closing(self._connect(create)) as connection:
                    self._check(connection, initialize=create)
                    yield connection
        except (DispatchError, StorageError):
            raise
        except sqlite3.IntegrityError:
            raise DispatchError("INVALID_STATE", subject="journal_constraint") from None
        except sqlite3.Error as error:
            if getattr(error, "sqlite_errorcode", 0) & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise StorageBusy("SQLite could not acquire the dispatch journal lock.") from None
            raise DispatchError("INVALID_JOURNAL", subject="sqlite") from None
        except OSError:
            raise DispatchError("INVALID_JOURNAL", subject="io") from None

    @contextmanager
    def _write(self):
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise

    def _now(self) -> str:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise DispatchError("INVALID_STATE", subject="clock")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _data(data: Mapping[str, Any]) -> str:
        if not isinstance(data, Mapping):
            raise DispatchError("INVALID_STATE", subject="event_data")
        try:
            return canonical_json(dict(data))
        except (TypeError, ValueError):
            raise DispatchError("INVALID_STATE", subject="event_data") from None

    @staticmethod
    def _require_identity(identity: object) -> DispatchIdentity:
        if type(identity) is not DispatchIdentity:
            raise DispatchError("INVALID_STATE", subject="identity")
        return identity

    @staticmethod
    def _require_exists(connection: sqlite3.Connection, request_id: str) -> None:
        if connection.execute(
            'SELECT 1 FROM "dispatches" WHERE "request_id" = ?', (request_id,),
        ).fetchone() is None:
            raise DispatchError("INVALID_STATE", subject="request_id")

    # -- writes --------------------------------------------------------------

    def _insert_dispatch(self, connection, identity: DispatchIdentity) -> None:
        connection.execute(
            'INSERT INTO "dispatches" ("request_id", "identity_json", "identity_digest", "created_at") '
            "VALUES (?, ?, ?, ?)",
            (identity.request_id, canonical_json(identity.to_dict()), identity.identity_digest, self._now()),
        )

    def register(self, identity: DispatchIdentity) -> bool:
        """Register an identity without a dispatch event. True when newly created."""
        identity = self._require_identity(identity)
        data = None
        with self._write() as connection:
            row = connection.execute(
                'SELECT "identity_digest" FROM "dispatches" WHERE "request_id" = ?', (identity.request_id,),
            ).fetchone()
            if row is not None:
                if row[0] != identity.identity_digest:
                    raise DispatchError("CONFLICT", subject="request_id")
                data = False
            else:
                self._insert_dispatch(connection, identity)
                data = True
        return data

    def reserve(self, identity: DispatchIdentity, data: Mapping[str, Any]) -> DispatchRecord:
        """Create the record and its ``reserved`` event in one transaction."""
        identity = self._require_identity(identity)
        payload = self._data(data)
        with self._write() as connection:
            row = connection.execute(
                'SELECT "identity_digest" FROM "dispatches" WHERE "request_id" = ?', (identity.request_id,),
            ).fetchone()
            if row is not None:
                raise DispatchError("CONFLICT" if row[0] != identity.identity_digest else "INVALID_STATE",
                                    subject="request_id")
            self._insert_dispatch(connection, identity)
            connection.execute(
                'INSERT INTO "dispatch_events" ("request_id", "state", "data_json", "at") VALUES (?, ?, ?, ?)',
                (identity.request_id, "reserved", payload, self._now()),
            )
        return self.record(identity.request_id)

    def append_dispatch_event(self, request_id: str, state: str, data: Mapping[str, Any]) -> DispatchRecord:
        if state not in DISPATCH_STATES or state == "reserved":
            raise DispatchError("INVALID_STATE", subject="state")
        payload = self._data(data)
        with self._write() as connection:
            self._require_exists(connection, request_id)
            last = connection.execute(
                'SELECT "state" FROM "dispatch_events" WHERE "request_id" = ? ORDER BY "seq" DESC LIMIT 1',
                (request_id,),
            ).fetchone()
            current = None if last is None else last[0]
            if state not in _NEXT_DISPATCH.get(current, ()):
                raise DispatchError("INVALID_STATE", subject="transition")
            connection.execute(
                'INSERT INTO "dispatch_events" ("request_id", "state", "data_json", "at") VALUES (?, ?, ?, ?)',
                (request_id, state, payload, self._now()),
            )
        return self.record(request_id)

    def tool_event(self, request_id: str, tool_call_id: str, state: str, data: Mapping[str, Any]) -> None:
        if state not in TOOL_STATES:
            raise DispatchError("INVALID_STATE", subject="tool_state")
        if type(tool_call_id) is not str or not 1 <= len(tool_call_id) <= 128:
            raise DispatchError("INVALID_STATE", subject="tool_call_id")
        payload = self._data(data)
        with self._write() as connection:
            self._require_exists(connection, request_id)
            states = {row[0] for row in connection.execute(
                'SELECT "state" FROM "tool_events" WHERE "request_id" = ? AND "tool_call_id" = ?',
                (request_id, tool_call_id),
            )}
            if state == "intent":
                allowed = not states
            elif state in ("receipt", "denied", "effect_unknown"):
                allowed = states == {"intent"}
            else:
                allowed = bool(states & {"receipt", "denied"}) and "returned" not in states
            if not allowed:
                raise DispatchError("INVALID_STATE", subject="tool_transition")
            connection.execute(
                'INSERT INTO "tool_events" ("request_id", "tool_call_id", "state", "data_json", "at") '
                "VALUES (?, ?, ?, ?, ?)",
                (request_id, tool_call_id, state, payload, self._now()),
            )

    # -- reads ---------------------------------------------------------------

    def find(self, request_id: str) -> DispatchRecord | None:
        with self._session() as connection:
            row = connection.execute(
                'SELECT "identity_json", "identity_digest" FROM "dispatches" WHERE "request_id" = ?', (request_id,),
            ).fetchone()
            rows = connection.execute(
                'SELECT "seq", "state", "data_json", "at" FROM "dispatch_events" WHERE "request_id" = ? '
                'ORDER BY "seq"', (request_id,),
            ).fetchall()
        if row is None:
            return None
        try:
            identity = DispatchIdentity.from_dict(json.loads(row[0]))
            events = tuple(DispatchEvent(seq, state, _decode_data(data), at) for seq, state, data, at in rows)
        except Exception:
            raise DispatchError("INVALID_JOURNAL", subject="record") from None
        if identity.identity_digest != row[1] or identity.request_id != request_id:
            raise DispatchError("INVALID_JOURNAL", subject="identity_digest")
        return DispatchRecord(identity, events[-1].state if events else None, events)

    def record(self, request_id: str) -> DispatchRecord:
        found = self.find(request_id)
        if found is None:
            raise DispatchError("INVALID_STATE", subject="request_id")
        return found

    def tool_events(self, request_id: str) -> tuple:
        with self._session() as connection:
            rows = connection.execute(
                'SELECT "seq", "tool_call_id", "state", "data_json", "at" FROM "tool_events" '
                'WHERE "request_id" = ? ORDER BY "seq"', (request_id,),
            ).fetchall()
        try:
            return tuple(ToolEvent(seq, call, state, _decode_data(data), at) for seq, call, state, data, at in rows)
        except Exception:
            raise DispatchError("INVALID_JOURNAL", subject="tool_events") from None

    def request_ids_in_state(self, state: str) -> tuple:
        if state not in DISPATCH_STATES:
            raise DispatchError("INVALID_STATE", subject="state")
        with self._session() as connection:
            rows = connection.execute(
                'SELECT e."request_id" FROM "dispatch_events" AS e WHERE e."seq" = '
                '(SELECT MAX(x."seq") FROM "dispatch_events" AS x WHERE x."request_id" = e."request_id") '
                'AND e."state" = ? ORDER BY e."seq"', (state,),
            ).fetchall()
        return tuple(row[0] for row in rows)
