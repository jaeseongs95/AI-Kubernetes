"""AIK-owned durable journal for hosted VM runtime bindings (AIK-031 hosted path).

The journal is a separate SQLite file. It is not the runtime metadata store,
the development task ledger, or any VM/AGS ledger. It records only AIK-owned
cross-references:

* the AIK assignment (route/connection/lease) that a VM Attempt used before its
  first provider effect, together with the AIK placement's Task ID,
* the VM runtime intents that were bound to that Attempt,
* which provider thread belongs to which connection,
* the owner-confirmed task execution binding,
* a before-effect log of hosted runtime effects,
* targets whose effect outcome is unknown,
* write-ahead in-flight effects (``inflight_effects``).

An in-flight row is inserted in the same transaction as the before-effect log
entry and is deleted only after the inner runtime returned normally. A row
that survives (inner failure, unreadable result, process termination during
the inner call, or a failed delete) blocks later effects of the same Attempt
or thread until explicit reconciliation. ``interrupt`` is never blocked by
unknown or in-flight state; it is logged with ``while_unresolved``.

It never stores prompts, provider payloads, credentials or VM verdicts, and it
does not decide Task/Attempt state. Paths follow
``paths.validate_database_path``: no development ledger, no Git repository,
and no aliases. Every operation opens a fresh connection under the shared
process lock and uses BEGIN IMMEDIATE, DELETE journal mode and FULL
synchronization, like ``storage.RuntimeStore``. Before SQLite opens an
existing file, its 100-byte header (SQLite magic and this journal's
application ID) is checked, so a foreign file with a hot rollback journal is
never modified. An empty file accompanied by a rollback journal is refused.
This AIK-owned journal otherwise relies on SQLite's own rollback-journal
recovery and does not validate a private copy first.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from functools import lru_cache
import math
import os
from pathlib import Path
import re
import sqlite3

from .errors import InvalidDatabase, InvalidRecord, StorageBusy, StorageError
from .models import identifier
from .paths import database_lock, validate_database_path


APPLICATION_ID = 0x41494B42  # AIKB: not the runtime store, planning, VM or AGS database.
SCHEMA_VERSION = 1
OPERATIONS = ("create_thread", "start_turn", "resume", "interrupt")
THREAD_KINDS = ("task", "preparation")
TEXT_LIMIT = 65536
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SQLITE_MAGIC = b"SQLite format 3\x00"
_IMMUTABLE_TABLES = ("attempt_bindings", "intent_bindings", "thread_routes", "owner_confirmations", "effect_log")
_ATTEMPT_KEYS = frozenset({
    "vm_attempt_id", "vm_project_id", "assignment_json", "assignment_digest", "vm_intent_id", "vm_call_id",
    "trace_thread_id", "trace_turn_id", "placement_vm_task_id",
})


class JournalConflict(StorageError):
    """A durable record already exists with different values; nothing was written."""

    def __init__(self, subject: str):
        super().__init__(f"Binding journal conflict: {subject}.")
        self.subject = subject


class JournalBlocked(StorageError):
    """The target has an unknown or in-flight effect; only explicit reconciliation clears it."""

    def __init__(self, subject: str):
        super().__init__(f"Binding journal target requires reconciliation: {subject}.")
        self.subject = subject


def _id_column(name: str, *, key: bool = False, optional: bool = False) -> str:
    check = f"typeof(\"{name}\") = 'text' AND length(\"{name}\") BETWEEN 1 AND 128"
    if optional:
        return f'"{name}" TEXT CHECK ("{name}" IS NULL OR ({check}))'
    primary = "PRIMARY KEY " if key else ""
    return f'"{name}" TEXT {primary}NOT NULL CHECK ({check})'


def _text_column(name: str) -> str:
    return (f"\"{name}\" TEXT NOT NULL CHECK (typeof(\"{name}\") = 'text' "
            f"AND length(\"{name}\") BETWEEN 1 AND {TEXT_LIMIT})")


def _digest_column(name: str) -> str:
    return f"\"{name}\" TEXT NOT NULL CHECK (typeof(\"{name}\") = 'text' AND length(\"{name}\") = 71)"


def _flag_column(name: str) -> str:
    return f'"{name}" INTEGER NOT NULL CHECK ("{name}" IN (0, 1))'


_OPERATION = ("\"operation\" TEXT NOT NULL CHECK (\"operation\" IN "
              "('create_thread', 'start_turn', 'resume', 'interrupt'))")
_ATTEMPT_FK = ('FOREIGN KEY ("vm_attempt_id") REFERENCES "attempt_bindings" ("vm_attempt_id") '
               'ON DELETE RESTRICT ON UPDATE RESTRICT')

_TABLES = {
    "attempt_bindings": [
        _id_column("vm_attempt_id", key=True), _id_column("vm_project_id"), _text_column("assignment_json"),
        _digest_column("assignment_digest"), _id_column("placement_vm_task_id"), _text_column("recorded_at"),
    ],
    "intent_bindings": [
        _id_column("vm_intent_id", key=True), _id_column("vm_attempt_id"), _id_column("vm_call_id", optional=True),
        _OPERATION, _id_column("thread_id", optional=True), _id_column("turn_id", optional=True),
        "\"ordinal\" INTEGER NOT NULL UNIQUE CHECK (typeof(\"ordinal\") = 'integer' AND \"ordinal\" >= 1)",
        _text_column("recorded_at"), _ATTEMPT_FK,
    ],
    "thread_routes": [
        _id_column("connection_id"), _id_column("thread_id"), _id_column("vm_attempt_id", optional=True),
        "\"kind\" TEXT NOT NULL CHECK (\"kind\" IN ('task', 'preparation'))",
        _text_column("assignment_json"), _digest_column("assignment_digest"), _text_column("recorded_at"),
        'UNIQUE ("connection_id", "thread_id")',
        "CHECK ((\"kind\" = 'task') = (\"vm_attempt_id\" IS NOT NULL))",
        _ATTEMPT_FK,
    ],
    "owner_confirmations": [
        _id_column("vm_attempt_id", key=True), _text_column("binding_json"), _digest_column("binding_digest"),
        _text_column("confirmed_at"), _ATTEMPT_FK,
    ],
    "unknown_attempts": [_id_column("vm_attempt_id", key=True), _id_column("reason"), _text_column("recorded_at")],
    "unknown_threads": [_id_column("thread_id", key=True), _id_column("reason"), _text_column("recorded_at")],
    "effect_log": [
        '"sequence" INTEGER PRIMARY KEY', _id_column("connection_id"), _id_column("thread_id", optional=True),
        _id_column("vm_attempt_id", optional=True), _id_column("vm_intent_id", optional=True), _OPERATION,
        _flag_column("lease_usable"), _flag_column("while_unresolved"), _text_column("recorded_at"),
    ],
    "inflight_effects": [
        '"id" INTEGER PRIMARY KEY AUTOINCREMENT', _id_column("vm_attempt_id", optional=True),
        _id_column("thread_id", optional=True), _OPERATION, _text_column("recorded_at"),
    ],
}
_INDEXES = (
    'CREATE INDEX "thread_routes_thread" ON "thread_routes" ("thread_id")',
    'CREATE INDEX "intent_bindings_attempt" ON "intent_bindings" ("vm_attempt_id", "ordinal")',
    'CREATE INDEX "inflight_effects_attempt" ON "inflight_effects" ("vm_attempt_id")',
    'CREATE INDEX "inflight_effects_thread" ON "inflight_effects" ("thread_id")',
)


def _statements() -> tuple:
    statements = [f'CREATE TABLE "{name}" (\n    ' + ",\n    ".join(parts) + "\n)" for name, parts in _TABLES.items()]
    statements.extend(_INDEXES)
    for table in _IMMUTABLE_TABLES:
        for event in ("UPDATE", "DELETE"):
            statements.append(
                f'CREATE TRIGGER "{table}_no_{event.lower()}" BEFORE {event} ON "{table}" '
                "BEGIN SELECT RAISE(ABORT, 'immutable binding journal record'); END"
            )
    return tuple(statements)


def _schema_rows(connection: sqlite3.Connection) -> tuple:
    return tuple(tuple(row) for row in connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ))


@lru_cache(maxsize=1)
def _expected_schema() -> tuple:
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
        for statement in _statements():
            connection.execute(statement)
        return _schema_rows(connection)


def _failure(error: sqlite3.Error) -> StorageError:
    if getattr(error, "sqlite_errorcode", 0) & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return StorageBusy("SQLite could not acquire the binding journal lock.")
    return StorageError("The binding journal operation could not be completed.")


def _header_state(path: Path) -> str:
    """'empty' for a zero-byte file, 'journal' for this journal's header; anything else is foreign."""
    with path.open("rb") as stream:
        header = stream.read(100)
    if not header:
        return "empty"
    if len(header) < 100 or header[:16] != _SQLITE_MAGIC \
            or int.from_bytes(header[68:72], "big") != APPLICATION_ID:
        raise InvalidDatabase("The binding journal path holds a foreign or damaged file.")
    return "journal"


def _input_id(value: object, field: str, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    identifier(value, field)


def _input_text(value: object, field: str) -> None:
    if type(value) is not str or not value or len(value) > TEXT_LIMIT:
        raise InvalidRecord(f"Invalid text field: {field}.")


def _input_digest(value: object, field: str) -> None:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise InvalidRecord(f"Invalid digest field: {field}.")


def _damaged() -> InvalidDatabase:
    return InvalidDatabase("A binding journal record violates its contract.")


def _stored_id(value: object, *, optional: bool = False):
    if value is None and optional:
        return None
    try:
        identifier(value)
    except InvalidRecord:
        raise _damaged() from None
    return value


def _stored_text(value: object) -> str:
    if type(value) is not str or not value or len(value) > TEXT_LIMIT:
        raise _damaged()
    return value


def _stored_digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise _damaged()
    return value


def _stored_operation(value: object) -> str:
    if type(value) is not str or value not in OPERATIONS:
        raise _damaged()
    return value


class BindingJournal:
    """Durable rows for the hosted runtime; the typed API lives in aik_vm_bridge.hosted_runtime."""

    def __init__(self, path: Path | str, *, timeout: float = 5.0):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
            raise InvalidRecord("Binding journal timeout must be a finite nonnegative number.")
        self.timeout = float(timeout)
        self.path = validate_database_path(path, create_parent=True)
        with database_lock(self.path, self.timeout):
            self._prepare()

    # ------------------------------------------------------------ plumbing
    def _companion(self, suffix: str) -> Path:
        return self.path.with_name(self.path.name + suffix)

    def _check_companions(self) -> None:
        for suffix in ("-journal", "-wal", "-shm"):
            candidate = self._companion(suffix)
            validate_database_path(candidate)
            if suffix != "-journal" and candidate.exists():
                raise InvalidDatabase("The binding journal requires DELETE mode without WAL companions.")
        if not self.path.exists() and self._companion("-journal").exists():
            raise InvalidDatabase("An orphan rollback journal cannot initialize the binding journal.")

    def _require_header(self) -> None:
        """The existing file must carry this journal's header before SQLite may touch it."""
        failed = False
        state = None
        try:
            state = _header_state(self.path)
        except OSError:
            failed = True
        if failed:
            raise StorageError("The binding journal file could not be processed safely.") from None
        if state != "journal":
            raise InvalidDatabase("The binding journal file is not initialized.")

    def _connect(self, *, create: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path.as_uri() + ("?mode=rwc" if create else "?mode=rw"),
            uri=True, timeout=self.timeout, isolation_level=None,
        )
        try:
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA foreign_keys = ON")
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
                raise InvalidDatabase("The binding journal requires DELETE journal mode.")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except BaseException:
            connection.close()
            raise

    @staticmethod
    def _validate(connection: sqlite3.Connection, *, full: bool) -> None:
        app_id = connection.execute("PRAGMA application_id").fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if app_id != APPLICATION_ID or version != SCHEMA_VERSION:
            raise InvalidDatabase("This is not a supported AIK binding journal.")
        if _schema_rows(connection) != _expected_schema():
            raise InvalidDatabase("The binding journal schema differs from its declared version.")
        if full:
            if tuple(row[0] for row in connection.execute("PRAGMA quick_check")) != ("ok",):
                raise InvalidDatabase("Binding journal integrity validation failed.")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise InvalidDatabase("Binding journal references are inconsistent.")

    def _prepare(self) -> None:
        self._check_companions()
        try:
            state = "absent"
            if self.path.exists():
                # Inspect the header before SQLite may recover a hot journal,
                # so a foreign file is never modified.
                state = _header_state(self.path)
                if state == "empty" and self._companion("-journal").exists():
                    raise InvalidDatabase("An empty binding journal file with a rollback journal is not recoverable.")
            if state != "journal":
                with closing(self._connect(create=True)) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        if connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is not None:
                            raise InvalidDatabase("Binding journal initialization requires an empty database.")
                        for statement in _statements():
                            connection.execute(statement)
                        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
                os.chmod(self.path, 0o600)
                validate_database_path(self.path)
            with closing(self._connect()) as connection:
                self._validate(connection, full=True)
        except StorageError:
            raise
        except sqlite3.Error as error:
            raise _failure(error) from None
        except OSError:
            raise StorageError("The binding journal file could not be processed safely.") from None

    @contextmanager
    def _transaction(self):
        with database_lock(self.path, self.timeout):
            self._check_companions()
            if not self.path.exists():
                raise InvalidDatabase("The binding journal file is absent.")
            self._require_header()
            failure = None
            connection = None
            try:
                connection = self._connect()
                self._validate(connection, full=False)
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except BaseException as error:
                if connection is not None and connection.in_transaction:
                    try:
                        connection.rollback()
                    except sqlite3.Error:
                        pass
                if isinstance(error, sqlite3.IntegrityError):
                    failure = JournalConflict("constraint")
                elif isinstance(error, sqlite3.Error):
                    failure = _failure(error)
                else:
                    raise
            finally:
                if connection is not None:
                    connection.close()
            if failure is not None:
                raise failure from None

    @staticmethod
    def _blocking(connection: sqlite3.Connection, attempt_id: str | None, thread_id: str | None) -> str | None:
        """The first durable reason that blocks a new effect on these targets, or None."""
        if thread_id is not None and connection.execute(
                'SELECT 1 FROM "unknown_threads" WHERE "thread_id" = ?', (thread_id,)).fetchone():
            return "thread_id"
        if attempt_id is not None and connection.execute(
                'SELECT 1 FROM "unknown_attempts" WHERE "vm_attempt_id" = ?', (attempt_id,)).fetchone():
            return "vm_attempt_id"
        if thread_id is not None and connection.execute(
                'SELECT 1 FROM "inflight_effects" WHERE "thread_id" = ? LIMIT 1', (thread_id,)).fetchone():
            return "thread_in_flight"
        if attempt_id is not None and connection.execute(
                'SELECT 1 FROM "inflight_effects" WHERE "vm_attempt_id" = ? LIMIT 1', (attempt_id,)).fetchone():
            return "attempt_in_flight"
        return None

    # --------------------------------------------------------------- writes
    def record_effect(self, *, connection_id: str, operation: str, lease_usable: bool, recorded_at: str,
                      thread_id: str | None = None, attempt=None, inflight: bool = False,
                      unresolved_hint: bool = False) -> int | None:
        """Atomically check guards, bind Attempt and intent (if any), and log the effect.

        ``attempt`` is a mapping with exactly ``vm_attempt_id``, ``vm_project_id``,
        ``assignment_json``, ``assignment_digest``, ``vm_intent_id``,
        ``vm_call_id``, ``trace_thread_id``, ``trace_turn_id`` and
        ``placement_vm_task_id`` (the latter may be None only when the Attempt
        binding already exists).

        Unknown and in-flight state of the Attempt or thread blocks every
        operation except ``interrupt``. An interrupt proceeds and its log row
        has ``while_unresolved`` set when such state (or ``unresolved_hint``
        from the caller's process memory) exists. With ``inflight=True`` a
        write-ahead in-flight row is inserted in the same transaction and its
        ID is returned; otherwise None is returned.
        """
        _input_id(connection_id, "connection_id")
        if type(operation) is not str or operation not in OPERATIONS:
            raise InvalidRecord("Unknown hosted runtime operation.")
        if type(lease_usable) is not bool:
            raise InvalidRecord("lease_usable must be a boolean.")
        if type(inflight) is not bool or type(unresolved_hint) is not bool:
            raise InvalidRecord("inflight and unresolved_hint must be booleans.")
        bypass = operation == "interrupt"
        if unresolved_hint and not bypass:
            raise InvalidRecord("Only interrupt may proceed while its target is unresolved.")
        _input_text(recorded_at, "recorded_at")
        _input_id(thread_id, "thread_id", optional=True)
        values = None
        if attempt is not None:
            if not isinstance(attempt, dict) or set(attempt) != _ATTEMPT_KEYS:
                raise InvalidRecord("Invalid attempt binding record.")
            values = dict(attempt)
            for name in ("vm_attempt_id", "vm_project_id", "vm_intent_id"):
                _input_id(values[name], name)
            for name in ("vm_call_id", "trace_thread_id", "trace_turn_id", "placement_vm_task_id"):
                _input_id(values[name], name, optional=True)
            _input_text(values["assignment_json"], "assignment_json")
            _input_digest(values["assignment_digest"], "assignment_digest")
        with self._transaction() as connection:
            attempt_id = None if values is None else values["vm_attempt_id"]
            subject = self._blocking(connection, attempt_id, thread_id)
            if subject is not None and not bypass:
                raise JournalBlocked(subject)
            while_unresolved = unresolved_hint or subject is not None
            intent_id = None
            if values is not None:
                row = connection.execute(
                    'SELECT "vm_project_id", "assignment_digest", "placement_vm_task_id" FROM "attempt_bindings" '
                    'WHERE "vm_attempt_id" = ?', (attempt_id,),
                ).fetchone()
                if row is None:
                    if values["placement_vm_task_id"] is None:
                        raise InvalidRecord("A new Attempt binding requires the placement Task ID.")
                    connection.execute(
                        'INSERT INTO "attempt_bindings" ("vm_attempt_id", "vm_project_id", "assignment_json", '
                        '"assignment_digest", "placement_vm_task_id", "recorded_at") VALUES (?, ?, ?, ?, ?, ?)',
                        (attempt_id, values["vm_project_id"], values["assignment_json"],
                         values["assignment_digest"], values["placement_vm_task_id"], recorded_at),
                    )
                elif tuple(row[:2]) != (values["vm_project_id"], values["assignment_digest"]):
                    raise JournalConflict("assignment")
                elif values["placement_vm_task_id"] is not None and row[2] != values["placement_vm_task_id"]:
                    raise JournalConflict("placement_vm_task_id")
                intent_id = values["vm_intent_id"]
                intent = (attempt_id, values["vm_call_id"], operation, values["trace_thread_id"],
                          values["trace_turn_id"])
                row = connection.execute(
                    'SELECT "vm_attempt_id", "vm_call_id", "operation", "thread_id", "turn_id" '
                    'FROM "intent_bindings" WHERE "vm_intent_id" = ?', (intent_id,),
                ).fetchone()
                if row is None:
                    ordinal = connection.execute(
                        'SELECT COALESCE(MAX("ordinal"), 0) + 1 FROM "intent_bindings"').fetchone()[0]
                    connection.execute(
                        'INSERT INTO "intent_bindings" ("vm_intent_id", "vm_attempt_id", "vm_call_id", "operation", '
                        '"thread_id", "turn_id", "ordinal", "recorded_at") VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                        (intent_id, *intent, ordinal, recorded_at),
                    )
                elif tuple(row) != intent:
                    raise JournalConflict("vm_runtime_intent_id")
            connection.execute(
                'INSERT INTO "effect_log" ("connection_id", "thread_id", "vm_attempt_id", "vm_intent_id", '
                '"operation", "lease_usable", "while_unresolved", "recorded_at") VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (connection_id, thread_id, attempt_id, intent_id, operation, int(lease_usable),
                 int(while_unresolved), recorded_at),
            )
            if not inflight:
                return None
            cursor = connection.execute(
                'INSERT INTO "inflight_effects" ("vm_attempt_id", "thread_id", "operation", "recorded_at") '
                "VALUES (?, ?, ?, ?)", (attempt_id, thread_id, operation, recorded_at),
            )
            return cursor.lastrowid

    def finish_effect(self, inflight_id: int) -> bool:
        """Delete one in-flight row after the inner runtime returned normally."""
        if type(inflight_id) is not int or inflight_id < 1:
            raise InvalidRecord("Invalid in-flight effect ID.")
        with self._transaction() as connection:
            return connection.execute('DELETE FROM "inflight_effects" WHERE "id" = ?',
                                      (inflight_id,)).rowcount > 0

    def record_thread(self, *, connection_id: str, thread_id: str, kind: str, vm_attempt_id: str | None,
                      assignment_json: str, assignment_digest: str, recorded_at: str) -> bool:
        """Record a thread route. Returns True when another connection has the same thread ID."""
        _input_id(connection_id, "connection_id")
        _input_id(thread_id, "thread_id")
        _input_id(vm_attempt_id, "vm_attempt_id", optional=True)
        if type(kind) is not str or kind not in THREAD_KINDS or (kind == "task") != (vm_attempt_id is not None):
            raise InvalidRecord("Invalid thread route kind.")
        _input_text(assignment_json, "assignment_json")
        _input_digest(assignment_digest, "assignment_digest")
        _input_text(recorded_at, "recorded_at")
        with self._transaction() as connection:
            rows = connection.execute(
                'SELECT "connection_id", "vm_attempt_id", "kind", "assignment_digest" FROM "thread_routes" '
                'WHERE "thread_id" = ?', (thread_id,),
            ).fetchall()
            others = any(row[0] != connection_id for row in rows)
            same = [tuple(row[1:]) for row in rows if row[0] == connection_id]
            if same:
                if same[0] != (vm_attempt_id, kind, assignment_digest):
                    raise JournalConflict("thread_route")
                return others
            if vm_attempt_id is not None:
                row = connection.execute(
                    'SELECT "assignment_digest" FROM "attempt_bindings" WHERE "vm_attempt_id" = ?', (vm_attempt_id,),
                ).fetchone()
                if row is None or row[0] != assignment_digest:
                    raise JournalConflict("assignment")
            connection.execute(
                'INSERT INTO "thread_routes" ("connection_id", "thread_id", "vm_attempt_id", "kind", '
                '"assignment_json", "assignment_digest", "recorded_at") VALUES (?, ?, ?, ?, ?, ?, ?)',
                (connection_id, thread_id, vm_attempt_id, kind, assignment_json, assignment_digest, recorded_at),
            )
            return others

    def confirm_owner(self, *, vm_attempt_id: str, binding_json: str, binding_digest: str,
                      confirmed_at: str) -> str:
        """Store the owner-confirmed binding once; an equal digest is idempotent."""
        _input_id(vm_attempt_id, "vm_attempt_id")
        _input_text(binding_json, "binding_json")
        _input_digest(binding_digest, "binding_digest")
        _input_text(confirmed_at, "confirmed_at")
        with self._transaction() as connection:
            if connection.execute('SELECT 1 FROM "attempt_bindings" WHERE "vm_attempt_id" = ?',
                                  (vm_attempt_id,)).fetchone() is None:
                raise JournalConflict("attempt_binding")
            row = connection.execute(
                'SELECT "binding_digest" FROM "owner_confirmations" WHERE "vm_attempt_id" = ?', (vm_attempt_id,),
            ).fetchone()
            if row is not None:
                if row[0] != binding_digest:
                    raise JournalConflict("persisted_binding")
                return binding_digest
            connection.execute(
                'INSERT INTO "owner_confirmations" ("vm_attempt_id", "binding_json", "binding_digest", '
                '"confirmed_at") VALUES (?, ?, ?, ?)', (vm_attempt_id, binding_json, binding_digest, confirmed_at),
            )
            return binding_digest

    def mark_unknown(self, *, reason: str, recorded_at: str, vm_attempt_id: str | None = None,
                     thread_id: str | None = None) -> None:
        _input_id(reason, "reason")
        _input_text(recorded_at, "recorded_at")
        _input_id(vm_attempt_id, "vm_attempt_id", optional=True)
        _input_id(thread_id, "thread_id", optional=True)
        if vm_attempt_id is None and thread_id is None:
            raise InvalidRecord("An unknown mark requires a target.")
        with self._transaction() as connection:
            if vm_attempt_id is not None:
                connection.execute(
                    'INSERT OR IGNORE INTO "unknown_attempts" ("vm_attempt_id", "reason", "recorded_at") '
                    'VALUES (?, ?, ?)', (vm_attempt_id, reason, recorded_at),
                )
            if thread_id is not None:
                connection.execute(
                    'INSERT OR IGNORE INTO "unknown_threads" ("thread_id", "reason", "recorded_at") VALUES (?, ?, ?)',
                    (thread_id, reason, recorded_at),
                )

    def reconcile_attempt(self, vm_attempt_id: str) -> bool:
        """Clear the Attempt's unknown mark and every in-flight row of that Attempt."""
        _input_id(vm_attempt_id, "vm_attempt_id")
        with self._transaction() as connection:
            unknown = connection.execute('DELETE FROM "unknown_attempts" WHERE "vm_attempt_id" = ?',
                                         (vm_attempt_id,)).rowcount
            inflight = connection.execute('DELETE FROM "inflight_effects" WHERE "vm_attempt_id" = ?',
                                          (vm_attempt_id,)).rowcount
            return unknown + inflight > 0

    def reconcile_thread(self, thread_id: str) -> bool:
        """Clear the thread's unknown mark and every in-flight row of that thread."""
        _input_id(thread_id, "thread_id")
        with self._transaction() as connection:
            unknown = connection.execute('DELETE FROM "unknown_threads" WHERE "thread_id" = ?',
                                         (thread_id,)).rowcount
            inflight = connection.execute('DELETE FROM "inflight_effects" WHERE "thread_id" = ?',
                                          (thread_id,)).rowcount
            return unknown + inflight > 0

    # ---------------------------------------------------------------- reads
    def thread_routes(self, thread_id: str) -> tuple:
        _input_id(thread_id, "thread_id")
        with self._transaction() as connection:
            rows = connection.execute(
                'SELECT "connection_id", "thread_id", "vm_attempt_id", "kind", "assignment_json", '
                '"assignment_digest" FROM "thread_routes" WHERE "thread_id" = ? ORDER BY "connection_id"',
                (thread_id,),
            ).fetchall()
        result = []
        for row in rows:
            kind = row[3]
            if kind not in THREAD_KINDS:
                raise _damaged()
            result.append({
                "connection_id": _stored_id(row[0]), "thread_id": _stored_id(row[1]),
                "vm_attempt_id": _stored_id(row[2], optional=True), "kind": kind,
                "assignment_json": _stored_text(row[4]), "assignment_digest": _stored_digest(row[5]),
            })
        return tuple(result)

    def attempt(self, vm_attempt_id: str) -> dict | None:
        _input_id(vm_attempt_id, "vm_attempt_id")
        with self._transaction() as connection:
            row = connection.execute(
                'SELECT "vm_project_id", "assignment_json", "assignment_digest", "placement_vm_task_id" '
                'FROM "attempt_bindings" WHERE "vm_attempt_id" = ?', (vm_attempt_id,),
            ).fetchone()
            if row is None:
                return None
            intents = connection.execute(
                'SELECT "vm_intent_id", "vm_call_id", "operation", "thread_id", "turn_id" FROM "intent_bindings" '
                'WHERE "vm_attempt_id" = ? ORDER BY "ordinal"', (vm_attempt_id,),
            ).fetchall()
            unknown = connection.execute('SELECT 1 FROM "unknown_attempts" WHERE "vm_attempt_id" = ?',
                                         (vm_attempt_id,)).fetchone() is not None
        return {
            "vm_attempt_id": vm_attempt_id, "vm_project_id": _stored_id(row[0]),
            "assignment_json": _stored_text(row[1]), "assignment_digest": _stored_digest(row[2]),
            "placement_vm_task_id": _stored_id(row[3]),
            "intents": tuple({
                "vm_intent_id": _stored_id(item[0]), "vm_call_id": _stored_id(item[1], optional=True),
                "operation": _stored_operation(item[2]), "thread_id": _stored_id(item[3], optional=True),
                "turn_id": _stored_id(item[4], optional=True),
            } for item in intents),
            "unknown": unknown,
        }

    def owner_confirmation(self, vm_attempt_id: str) -> dict | None:
        _input_id(vm_attempt_id, "vm_attempt_id")
        with self._transaction() as connection:
            row = connection.execute(
                'SELECT "binding_json", "binding_digest" FROM "owner_confirmations" WHERE "vm_attempt_id" = ?',
                (vm_attempt_id,),
            ).fetchone()
        if row is None:
            return None
        return {"binding_json": _stored_text(row[0]), "binding_digest": _stored_digest(row[1])}

    def unknown_targets(self) -> tuple:
        with self._transaction() as connection:
            attempts = [row[0] for row in connection.execute('SELECT "vm_attempt_id" FROM "unknown_attempts"')]
            threads = [row[0] for row in connection.execute('SELECT "thread_id" FROM "unknown_threads"')]
        return (frozenset(_stored_id(item) for item in attempts), frozenset(_stored_id(item) for item in threads))

    def inflight(self) -> tuple:
        with self._transaction() as connection:
            rows = connection.execute(
                'SELECT "id", "vm_attempt_id", "thread_id", "operation" FROM "inflight_effects" ORDER BY "id"'
            ).fetchall()
        result = []
        for row in rows:
            if type(row[0]) is not int or row[0] < 1:
                raise _damaged()
            result.append({
                "id": row[0], "vm_attempt_id": _stored_id(row[1], optional=True),
                "thread_id": _stored_id(row[2], optional=True), "operation": _stored_operation(row[3]),
            })
        return tuple(result)

    def effect_log(self) -> tuple:
        with self._transaction() as connection:
            rows = connection.execute(
                'SELECT "sequence", "connection_id", "thread_id", "vm_attempt_id", "vm_intent_id", "operation", '
                '"lease_usable", "while_unresolved", "recorded_at" FROM "effect_log" ORDER BY "sequence"'
            ).fetchall()
        result = []
        for row in rows:
            if type(row[0]) is not int or row[6] not in (0, 1) or row[7] not in (0, 1):
                raise _damaged()
            result.append({
                "sequence": row[0], "connection_id": _stored_id(row[1]),
                "thread_id": _stored_id(row[2], optional=True), "vm_attempt_id": _stored_id(row[3], optional=True),
                "vm_intent_id": _stored_id(row[4], optional=True), "operation": _stored_operation(row[5]),
                "lease_usable": bool(row[6]), "while_unresolved": bool(row[7]), "recorded_at": _stored_text(row[8]),
            })
        return tuple(result)
