"""SQLite read model. NON-AUTHORITATIVE: a rebuildable projection for display.

event_log, conflicts and meta are append-only (enforced by triggers). gaps
rows may only be closed once. The projection tables are derived data and are
rebuilt from event_log by ``rebuild()``.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from types import MappingProxyType

from aik_runtime.errors import StorageBusy
from aik_runtime.paths import database_lock, validate_database_path

from .errors import InvalidEvent, InvalidReadModelDatabase, ReadModelError, ScopeMismatch
from .models import (
    EXPANDING_KEYS, REF_KEYS, ApplyReport, AttemptView, ConflictRecord, CursorStatus, DeliveryState,
    PeerView, SourceEvent, SourceState, canonical_json, check_id, revalidate,
)


APPLICATION_ID = 0x41494B4D  # AIKM: AIK read Model, distinct from every authoritative ledger.
SCHEMA_VERSION = 1
AUTHORITY = "non-authoritative"
MAX_BATCH = 1000
MAX_VIEW_NODES = 4096
MAX_VIEW_EVENTS = 16384
_PROJECTION_TABLES = (
    ("source_cursor", ("source", "applied_cursor", "max_seen_cursor")),
    ("projected_events", ("source", "cursor", "kind", "state")),
    ("event_refs", ("source", "cursor", "ref_key", "ref_value")),
    ("links", ("source", "cursor", "key_a", "value_a", "key_b", "value_b")),
    ("attempts", ("vm_attempt_id", "mentions")),
)


def _append_only(table: str) -> tuple:
    return (
        f'CREATE TRIGGER "{table}_no_update" BEFORE UPDATE ON "{table}" '
        f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END",
        f'CREATE TRIGGER "{table}_no_delete" BEFORE DELETE ON "{table}" '
        f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END",
    )


_STATEMENTS = (
    'CREATE TABLE "meta" (\n'
    '    -- AIK read model metadata; this database is a non-authoritative projection\n'
    '    "name" TEXT PRIMARY KEY NOT NULL,\n'
    '    "value" TEXT NOT NULL\n'
    ')',
    'CREATE TABLE "event_log" (\n'
    '    -- copies of source events; VM, AGS and AIK source ledgers stay authoritative\n'
    '    "seq" INTEGER PRIMARY KEY,\n'
    '    "source" TEXT NOT NULL,\n'
    '    "cursor" INTEGER NOT NULL CHECK (typeof("cursor") = \'integer\' AND "cursor" >= 1),\n'
    '    "event_id" TEXT NOT NULL,\n'
    '    "digest" TEXT NOT NULL,\n'
    '    "payload" TEXT NOT NULL,\n'
    '    "received_at" TEXT NOT NULL,\n'
    '    UNIQUE ("source", "cursor"),\n'
    '    UNIQUE ("source", "event_id")\n'
    ')',
    'CREATE TABLE "conflicts" (\n'
    '    "seq" INTEGER PRIMARY KEY,\n'
    '    "source" TEXT NOT NULL,\n'
    '    "cursor" INTEGER NOT NULL,\n'
    '    "event_id" TEXT NOT NULL,\n'
    '    "code" TEXT NOT NULL CHECK ("code" IN (\'CURSOR_CONFLICT\', \'EVENT_ID_CONFLICT\')),\n'
    '    "existing_digest" TEXT NOT NULL,\n'
    '    "observed_digest" TEXT NOT NULL,\n'
    '    "recorded_at" TEXT NOT NULL\n'
    ')',
    'CREATE TABLE "gaps" (\n'
    '    "seq" INTEGER PRIMARY KEY,\n'
    '    "source" TEXT NOT NULL,\n'
    '    "start_cursor" INTEGER NOT NULL CHECK ("start_cursor" >= 1),\n'
    '    "end_cursor" INTEGER NOT NULL CHECK ("end_cursor" >= "start_cursor"),\n'
    '    "opened_at" TEXT NOT NULL,\n'
    '    "closed_at" TEXT\n'
    ')',
    'CREATE TABLE "source_cursor" (\n'
    '    -- derived projection state; rebuilt from event_log\n'
    '    "source" TEXT PRIMARY KEY NOT NULL,\n'
    '    "applied_cursor" INTEGER NOT NULL,\n'
    '    "max_seen_cursor" INTEGER NOT NULL\n'
    ')',
    'CREATE TABLE "projected_events" (\n'
    '    "source" TEXT NOT NULL,\n'
    '    "cursor" INTEGER NOT NULL,\n'
    '    "kind" TEXT NOT NULL,\n'
    '    "state" TEXT,\n'
    '    PRIMARY KEY ("source", "cursor")\n'
    ')',
    'CREATE TABLE "event_refs" (\n'
    '    "source" TEXT NOT NULL,\n'
    '    "cursor" INTEGER NOT NULL,\n'
    '    "ref_key" TEXT NOT NULL,\n'
    '    "ref_value" TEXT NOT NULL,\n'
    '    PRIMARY KEY ("source", "cursor", "ref_key", "ref_value")\n'
    ')',
    'CREATE INDEX "event_refs_value" ON "event_refs" ("ref_key", "ref_value")',
    'CREATE TABLE "links" (\n'
    '    "source" TEXT NOT NULL,\n'
    '    "cursor" INTEGER NOT NULL,\n'
    '    "key_a" TEXT NOT NULL,\n'
    '    "value_a" TEXT NOT NULL,\n'
    '    "key_b" TEXT NOT NULL,\n'
    '    "value_b" TEXT NOT NULL,\n'
    '    PRIMARY KEY ("source", "cursor", "key_a", "value_a", "key_b", "value_b")\n'
    ')',
    'CREATE TABLE "attempts" (\n'
    '    "vm_attempt_id" TEXT PRIMARY KEY NOT NULL,\n'
    '    "mentions" INTEGER NOT NULL\n'
    ')',
    *_append_only("event_log"),
    *_append_only("conflicts"),
    *_append_only("meta"),
    'CREATE TRIGGER "gaps_no_delete" BEFORE DELETE ON "gaps" '
    "BEGIN SELECT RAISE(ABORT, 'gaps are append-only'); END",
    'CREATE TRIGGER "gaps_close_once" BEFORE UPDATE ON "gaps" WHEN OLD."closed_at" IS NOT NULL '
    'OR NEW."closed_at" IS NULL OR NEW."source" IS NOT OLD."source" '
    'OR NEW."start_cursor" IS NOT OLD."start_cursor" OR NEW."end_cursor" IS NOT OLD."end_cursor" '
    'OR NEW."opened_at" IS NOT OLD."opened_at" '
    "BEGIN SELECT RAISE(ABORT, 'a gap may only be closed once'); END",
)


def _create_tables(connection: sqlite3.Connection) -> None:
    for statement in _STATEMENTS:
        connection.execute(statement)
    connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _schema_rows(connection: sqlite3.Connection) -> tuple:
    return tuple(tuple(row) for row in connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ))


@lru_cache(maxsize=1)
def _expected_schema() -> tuple:
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
        connection.execute("BEGIN")
        _create_tables(connection)
        connection.commit()
        return _schema_rows(connection)


class ReadModel:
    """Non-authoritative projection of VM, AGS and AIK events for one OS user scope."""

    def __init__(self, path: Path | str, *, user_scope: str, clock=None, timeout: float = 5.0):
        try:
            check_id(user_scope, "user_scope")
        except InvalidEvent:
            raise ReadModelError("INVALID_USER_SCOPE") from None
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) \
                or timeout < 0:
            raise ReadModelError("INVALID_TIMEOUT")
        self._user_scope = user_scope
        self._timeout = float(timeout)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._path = validate_database_path(path, create_parent=True)
        try:
            with database_lock(self._path, self._timeout):
                with closing(self._connect()) as connection:
                    self._prepare(connection)
        except sqlite3.Error:
            raise InvalidReadModelDatabase("INVALID_DATABASE") from None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def user_scope(self) -> str:
        return self._user_scope

    # -- connection management -------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path.as_uri() + "?mode=rwc", uri=True,
                                     timeout=self._timeout, isolation_level=None)
        try:
            connection.execute("PRAGMA trusted_schema = OFF")
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
                raise InvalidReadModelDatabase("UNSUPPORTED_JOURNAL_MODE")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except BaseException:
            connection.close()
            raise

    def _prepare(self, connection: sqlite3.Connection) -> None:
        app_id = connection.execute("PRAGMA application_id").fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        empty = connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is None
        if empty and app_id == 0 and version == 0:
            connection.execute("BEGIN IMMEDIATE")
            try:
                _create_tables(connection)
                connection.execute('INSERT INTO "meta" VALUES (?, ?)', ("authority", AUTHORITY))
                connection.execute('INSERT INTO "meta" VALUES (?, ?)', ("user_scope", self._user_scope))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        self._validate(connection, full=True)

    def _validate(self, connection: sqlite3.Connection, *, full: bool) -> None:
        app_id = connection.execute("PRAGMA application_id").fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if app_id != APPLICATION_ID or version != SCHEMA_VERSION:
            raise InvalidReadModelDatabase("FOREIGN_OR_UNSUPPORTED_DATABASE")
        if _schema_rows(connection) != _expected_schema():
            raise InvalidReadModelDatabase("SCHEMA_MISMATCH")
        if full and tuple(row[0] for row in connection.execute("PRAGMA quick_check")) != ("ok",):
            raise InvalidReadModelDatabase("INTEGRITY_CHECK_FAILED")
        meta = dict(connection.execute('SELECT "name", "value" FROM "meta"').fetchall())
        if meta.get("authority") != AUTHORITY or "user_scope" not in meta:
            raise InvalidReadModelDatabase("INVALID_METADATA")
        if meta["user_scope"] != self._user_scope:
            raise ScopeMismatch("SCOPE_MISMATCH")

    @contextmanager
    def _session(self):
        with database_lock(self._path, self._timeout):
            try:
                with closing(self._connect()) as connection:
                    self._validate(connection, full=False)
                    yield connection
            except sqlite3.Error as error:
                if getattr(error, "sqlite_errorcode", 0) & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise StorageBusy("The read model database is busy.") from None
                raise InvalidReadModelDatabase("DATABASE_OPERATION_FAILED") from None

    def _now(self) -> str:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ReadModelError("INVALID_CLOCK")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    # -- writing ---------------------------------------------------------------

    def apply(self, events) -> ApplyReport:
        """Store events, detect duplicates/conflicts/gaps and project the contiguous prefix."""
        try:
            batch = tuple(events)
        except TypeError:
            raise InvalidEvent("INVALID_BATCH") from None
        if len(batch) > MAX_BATCH:
            raise InvalidEvent("BATCH_TOO_LARGE")
        checked = tuple(revalidate(event) for event in batch)
        if any(event.user_scope != self._user_scope for event in checked):
            # The whole batch is rejected; nothing from another user is recorded.
            raise ScopeMismatch("SCOPE_MISMATCH")
        now = self._now()
        counts = dict.fromkeys(("stored", "applied", "duplicates", "conflicts", "gaps_opened", "gaps_closed"), 0)
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for event in checked:
                    self._apply_one(connection, event, now, counts)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return ApplyReport(**counts)

    def _apply_one(self, connection, event: SourceEvent, now: str, counts: dict) -> None:
        source, cursor = event.source, event.cursor
        row = connection.execute('SELECT "digest" FROM "event_log" WHERE "source" = ? AND "cursor" = ?',
                                 (source, cursor)).fetchone()
        if row is not None:
            if row[0] == event.digest:
                counts["duplicates"] += 1
            else:
                self._conflict(connection, event, "CURSOR_CONFLICT", row[0], now)
                counts["conflicts"] += 1
            return
        row = connection.execute('SELECT "digest" FROM "event_log" WHERE "source" = ? AND "event_id" = ?',
                                 (source, event.event_id)).fetchone()
        if row is not None:
            self._conflict(connection, event, "EVENT_ID_CONFLICT", row[0], now)
            counts["conflicts"] += 1
            return
        applied, max_seen = self._cursor(connection, source)
        connection.execute(
            'INSERT INTO "event_log" ("source", "cursor", "event_id", "digest", "payload", "received_at") '
            "VALUES (?, ?, ?, ?, ?, ?)",
            (source, cursor, event.event_id, event.digest, canonical_json(event.to_document()), now),
        )
        counts["stored"] += 1
        if cursor > max_seen:
            if cursor > max_seen + 1:
                self._open_gap(connection, source, max_seen + 1, cursor - 1, now)
                counts["gaps_opened"] += 1
            max_seen = cursor
        else:
            gap = connection.execute(
                'SELECT "seq", "start_cursor", "end_cursor" FROM "gaps" WHERE "source" = ? '
                'AND "closed_at" IS NULL AND "start_cursor" <= ? AND "end_cursor" >= ?',
                (source, cursor, cursor),
            ).fetchone()
            if gap is None:
                raise InvalidReadModelDatabase("GAP_STATE_INCONSISTENT")
            seq, start, end = gap
            connection.execute('UPDATE "gaps" SET "closed_at" = ? WHERE "seq" = ?', (now, seq))
            counts["gaps_closed"] += 1
            for low, high in ((start, cursor - 1), (cursor + 1, end)):
                if low <= high:
                    self._open_gap(connection, source, low, high, now)
                    counts["gaps_opened"] += 1
        new_applied = self._advance(connection, source, applied)
        counts["applied"] += new_applied - applied
        self._store_cursor(connection, source, new_applied, max_seen)

    @staticmethod
    def _conflict(connection, event: SourceEvent, code: str, existing: str, now: str) -> None:
        connection.execute(
            'INSERT INTO "conflicts" ("source", "cursor", "event_id", "code", "existing_digest", '
            '"observed_digest", "recorded_at") VALUES (?, ?, ?, ?, ?, ?, ?)',
            (event.source, event.cursor, event.event_id, code, existing, event.digest, now),
        )

    @staticmethod
    def _open_gap(connection, source: str, start: int, end: int, now: str) -> None:
        connection.execute('INSERT INTO "gaps" ("source", "start_cursor", "end_cursor", "opened_at") '
                           "VALUES (?, ?, ?, ?)", (source, start, end, now))

    @staticmethod
    def _cursor(connection, source: str) -> tuple[int, int]:
        row = connection.execute('SELECT "applied_cursor", "max_seen_cursor" FROM "source_cursor" '
                                 'WHERE "source" = ?', (source,)).fetchone()
        return (0, 0) if row is None else (row[0], row[1])

    @staticmethod
    def _store_cursor(connection, source: str, applied: int, max_seen: int) -> None:
        connection.execute(
            'INSERT INTO "source_cursor" VALUES (?, ?, ?) ON CONFLICT ("source") DO UPDATE SET '
            '"applied_cursor" = excluded."applied_cursor", "max_seen_cursor" = excluded."max_seen_cursor"',
            (source, applied, max_seen),
        )

    def _advance(self, connection, source: str, applied: int) -> int:
        while True:
            row = connection.execute('SELECT "payload", "digest" FROM "event_log" WHERE "source" = ? '
                                     'AND "cursor" = ?', (source, applied + 1)).fetchone()
            if row is None:
                return applied
            event = self._load(row[0], row[1])
            if event.source != source or event.cursor != applied + 1:
                raise InvalidReadModelDatabase("EVENT_LOG_DAMAGED")
            self._project(connection, event)
            applied += 1

    @staticmethod
    def _load(text: str, digest: str) -> SourceEvent:
        try:
            event = SourceEvent.from_document(json.loads(text))
        except (InvalidEvent, TypeError, ValueError):
            raise InvalidReadModelDatabase("EVENT_LOG_DAMAGED") from None
        if event.digest != digest:
            raise InvalidReadModelDatabase("EVENT_LOG_DAMAGED")
        return event

    @staticmethod
    def _project(connection, event: SourceEvent) -> None:
        source, cursor = event.source, event.cursor
        connection.execute('INSERT INTO "projected_events" VALUES (?, ?, ?, ?)',
                           (source, cursor, event.kind, event.state))
        items = sorted(event.refs.items())
        for key, value in items:
            connection.execute('INSERT INTO "event_refs" VALUES (?, ?, ?, ?)', (source, cursor, key, value))
        for index, (key_a, value_a) in enumerate(items):
            for key_b, value_b in items[index + 1:]:
                connection.execute('INSERT INTO "links" VALUES (?, ?, ?, ?, ?, ?)',
                                   (source, cursor, key_a, value_a, key_b, value_b))
        attempt = event.refs.get("vm_attempt_id")
        if attempt is not None:
            connection.execute('INSERT INTO "attempts" VALUES (?, 1) ON CONFLICT ("vm_attempt_id") '
                               'DO UPDATE SET "mentions" = "mentions" + 1', (attempt,))

    def rebuild(self) -> str:
        """Clear the projection and replay event_log per source in cursor order."""
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for table, _ in _PROJECTION_TABLES:
                    connection.execute(f'DELETE FROM "{table}"')
                sources = connection.execute('SELECT "source", MAX("cursor") FROM "event_log" '
                                             'GROUP BY "source" ORDER BY "source"').fetchall()
                for source, max_seen in sources:
                    applied = self._advance(connection, source, 0)
                    open_gaps = connection.execute('SELECT COUNT(*) FROM "gaps" WHERE "source" = ? '
                                                   'AND "closed_at" IS NULL', (source,)).fetchone()[0]
                    if (applied == max_seen) != (open_gaps == 0):
                        raise InvalidReadModelDatabase("GAP_STATE_INCONSISTENT")
                    self._store_cursor(connection, source, applied, max_seen)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            return self._projection_digest(connection)

    # -- reading ---------------------------------------------------------------

    @staticmethod
    def _projection_digest(connection) -> str:
        document = {}
        for table, columns in _PROJECTION_TABLES:
            names = ", ".join(f'"{column}"' for column in columns)
            document[table] = [list(row) for row in connection.execute(
                f'SELECT {names} FROM "{table}" ORDER BY {names}')]
        return "sha256:" + hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()

    def projection_digest(self) -> str:
        with self._session() as connection:
            return self._projection_digest(connection)

    @staticmethod
    def _cursor_statuses(connection) -> dict:
        statuses = {}
        for source, applied, max_seen in connection.execute(
                'SELECT "source", "applied_cursor", "max_seen_cursor" FROM "source_cursor" ORDER BY "source"'
        ).fetchall():
            gaps = tuple((start, end) for start, end in connection.execute(
                'SELECT "start_cursor", "end_cursor" FROM "gaps" WHERE "source" = ? AND "closed_at" IS NULL '
                'ORDER BY "start_cursor"', (source,)))
            conflicts = connection.execute('SELECT COUNT(*) FROM "conflicts" WHERE "source" = ?',
                                           (source,)).fetchone()[0]
            statuses[source] = CursorStatus(source, applied, max_seen, gaps, conflicts)
        return statuses

    def cursor_status(self) -> MappingProxyType:
        with self._session() as connection:
            return MappingProxyType(self._cursor_statuses(connection))

    def conflicts(self) -> tuple:
        with self._session() as connection:
            return tuple(ConflictRecord(*row) for row in connection.execute(
                'SELECT "seq", "source", "cursor", "event_id", "code", "existing_digest", "observed_digest", '
                '"recorded_at" FROM "conflicts" ORDER BY "seq"'))

    def attempt_view(self, vm_attempt_id: str) -> AttemptView:
        """All IDs linked to one VM attempt, latest state per source and cursor staleness."""
        check_id(vm_attempt_id, "vm_attempt_id")
        with self._session() as connection:
            start = ("vm_attempt_id", vm_attempt_id)
            nodes, frontier, events = {start}, [start], set()
            while frontier:
                key, value = frontier.pop()
                hits = connection.execute('SELECT "source", "cursor" FROM "event_refs" WHERE "ref_key" = ? '
                                          'AND "ref_value" = ?', (key, value)).fetchall()
                for hit in hits:
                    if hit in events:
                        continue
                    events.add(hit)
                    for node in connection.execute('SELECT "ref_key", "ref_value" FROM "event_refs" '
                                                   'WHERE "source" = ? AND "cursor" = ?', hit).fetchall():
                        if node not in nodes:
                            nodes.add(node)
                            if node[0] in EXPANDING_KEYS:
                                frontier.append(node)
                if len(nodes) > MAX_VIEW_NODES or len(events) > MAX_VIEW_EVENTS:
                    raise ReadModelError("VIEW_TOO_LARGE")
            found = bool(events)
            ids = {}
            for key in REF_KEYS:
                values = sorted(value for node_key, value in nodes if node_key == key)
                if values and found:
                    ids[key] = tuple(values)
            latest = {}
            for source, cursor in events:
                if cursor > latest.get(source, 0):
                    latest[source] = cursor
            states = {}
            for source in sorted(latest):
                kind, state = connection.execute('SELECT "kind", "state" FROM "projected_events" '
                                                 'WHERE "source" = ? AND "cursor" = ?',
                                                 (source, latest[source])).fetchone()
                states[source] = SourceState(source, kind, state, latest[source])
            cursors = self._cursor_statuses(connection)
            return AttemptView(vm_attempt_id, found, MappingProxyType(ids), MappingProxyType(states),
                               MappingProxyType(cursors), any(status.stale for status in cursors.values()))

    def peer_view(self, ags_peer_id: str) -> PeerView:
        """Worker addresses, sessions and delivery states of one AGS local peer (ags_peer source only)."""
        check_id(ags_peer_id, "ags_peer_id")
        with self._session() as connection:
            cursors = [row[0] for row in connection.execute(
                'SELECT "cursor" FROM "event_refs" WHERE "source" = \'ags_peer\' AND "ref_key" = \'ags_peer_id\' '
                'AND "ref_value" = ? ORDER BY "cursor"', (ags_peer_id,))]
            addresses, workers, sessions, history, latest = set(), set(), set(), [], {}
            for cursor in cursors:
                refs = dict(connection.execute('SELECT "ref_key", "ref_value" FROM "event_refs" '
                                               'WHERE "source" = \'ags_peer\' AND "cursor" = ?', (cursor,)).fetchall())
                state = connection.execute('SELECT "state" FROM "projected_events" WHERE "source" = \'ags_peer\' '
                                           'AND "cursor" = ?', (cursor,)).fetchone()[0]
                for key, target in (("worker_address", addresses), ("worker_id", workers),
                                    ("ags_session_id", sessions)):
                    if key in refs:
                        target.add(refs[key])
                entry = DeliveryState(refs.get("ags_message_id"), state, cursor)
                history.append(entry)
                latest[entry.ags_message_id] = entry
            status = self._cursor_statuses(connection).get("ags_peer")
            deliveries = tuple(sorted(latest.values(), key=lambda item: item.cursor))
            return PeerView(ags_peer_id, bool(cursors), tuple(sorted(addresses)), tuple(sorted(workers)),
                            tuple(sorted(sessions)), deliveries, tuple(history),
                            status is not None and status.stale)
