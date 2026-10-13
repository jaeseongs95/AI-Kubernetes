"""AIK-038 lifecycle database: gate state, worker ownership and append-only events.

A separate SQLite file (application_id "AIKL"). The aik_runtime path rules
apply: development ledgers, Git repositories, aliases and special files are
rejected before anything is created. An existing file is opened only when its
header carries this application ID; a foreign file is never rewritten.
Event tables refuse UPDATE and DELETE through triggers.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import errno
from functools import lru_cache
import math
import os
from pathlib import Path
import sqlite3
import tempfile

from aik_runtime.errors import InvalidDatabase, StorageError
from aik_runtime.paths import database_lock, validate_database_path

from .errors import LifecycleError


APPLICATION_ID = 0x41494B4C  # "AIKL"
SCHEMA_VERSION = 1
_MAGIC = b"SQLite format 3\x00"


def _append_only(table: str) -> tuple[str, str]:
    return (
        f'CREATE TRIGGER "{table}_no_update" BEFORE UPDATE ON "{table}" '
        f"BEGIN SELECT RAISE(ABORT, 'append-only'); END",
        f'CREATE TRIGGER "{table}_no_delete" BEFORE DELETE ON "{table}" '
        f"BEGIN SELECT RAISE(ABORT, 'append-only'); END",
    )


_STATEMENTS = (
    'CREATE TABLE "gate_events" (\n'
    '    "seq" INTEGER PRIMARY KEY,\n'
    '    "scope" TEXT NOT NULL,\n'
    '    "action" TEXT NOT NULL CHECK ("action" IN (\'pause\', \'resume\', \'drain\')),\n'
    '    "reason" TEXT,\n'
    '    "recorded_at" TEXT NOT NULL\n'
    ')',
    'CREATE TABLE "gate_state" (\n'
    '    "scope" TEXT PRIMARY KEY NOT NULL,\n'
    '    "state" TEXT NOT NULL CHECK ("state" IN (\'open\', \'paused\', \'draining\')),\n'
    '    "reason" TEXT,\n'
    '    "event_seq" INTEGER NOT NULL REFERENCES "gate_events" ("seq")\n'
    ')',
    'CREATE TABLE "workers" (\n'
    '    "worker_id" TEXT PRIMARY KEY NOT NULL,\n'
    '    "ownership" TEXT NOT NULL CHECK ("ownership" IN (\'owned\', \'borrowed\')),\n'
    '    "pool_id" TEXT NOT NULL,\n'
    '    "registered_at" TEXT NOT NULL\n'
    ')',
    'CREATE TABLE "lifecycle_events" (\n'
    '    "seq" INTEGER PRIMARY KEY,\n'
    '    "lease_id" TEXT NOT NULL,\n'
    '    "kind" TEXT NOT NULL,\n'
    '    "detail" TEXT NOT NULL,\n'
    '    "recorded_at" TEXT NOT NULL\n'
    ')',
    'CREATE INDEX "lifecycle_events_lease" ON "lifecycle_events" ("lease_id", "seq")',
    *_append_only("gate_events"),
    *_append_only("workers"),
    *_append_only("lifecycle_events"),
)


def create_schema(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in _STATEMENTS:
            connection.execute(statement)
        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
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


def timestamp(clock) -> str:
    """UTC ISO-8601 stamp from an injected clock (aware datetime) or the system clock."""
    if clock is None:
        value = datetime.now(timezone.utc)
    else:
        try:
            value = clock()
        except Exception:
            raise LifecycleError("CLOCK_UNAVAILABLE") from None
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise LifecycleError("INVALID_CLOCK")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sync_directory(path: Path) -> None:
    # Windows has no portable directory fsync through Python; rename
    # power-loss durability there is owned by the OS (not verified here).
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        os.close(descriptor)


class LifecycleDatabase:
    """One lifecycle SQLite file; every operation holds the shared process lock."""

    def __init__(self, path, *, timeout: float = 5.0):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout < 0):
            raise LifecycleError("INVALID_TIMEOUT")
        self.timeout = float(timeout)
        self.path = validate_database_path(path, create_parent=True)
        with database_lock(self.path, self.timeout):
            self._prepare()

    # ------------------------------------------------------------ opening
    def _companions(self) -> Path:
        for suffix in ("-wal", "-shm"):
            candidate = self.path.with_name(self.path.name + suffix)
            validate_database_path(candidate)
            if candidate.exists():
                raise InvalidDatabase("Lifecycle storage requires a DELETE-mode database without WAL companions.")
        journal = self.path.with_name(self.path.name + "-journal")
        validate_database_path(journal)
        return journal

    def _check_header(self) -> None:
        try:
            with self.path.open("rb") as stream:
                header = stream.read(100)
        except OSError:
            raise StorageError("The lifecycle database could not be read.") from None
        if (len(header) < 100 or header[:16] != _MAGIC
                or int.from_bytes(header[68:72], "big") != APPLICATION_ID):
            raise InvalidDatabase("This is not an AIK lifecycle database.")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path.as_uri() + "?mode=rw", uri=True, timeout=self.timeout, isolation_level=None,
        )
        try:
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA foreign_keys = ON")
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
                raise InvalidDatabase("Lifecycle storage requires DELETE journal mode.")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except BaseException:
            connection.close()
            raise

    @staticmethod
    def _validate(connection: sqlite3.Connection) -> None:
        if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise InvalidDatabase("This is not an AIK lifecycle database.")
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise InvalidDatabase("Unsupported lifecycle schema version.")
        if _schema_rows(connection) != _expected_schema():
            raise InvalidDatabase("The lifecycle schema differs from its declared version.")

    def _create(self) -> None:
        descriptor, name = tempfile.mkstemp(prefix=".aik-lifecycle-", suffix=".tmp", dir=self.path.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            with closing(sqlite3.connect(str(temporary), isolation_level=None)) as connection:
                create_schema(connection)
            os.chmod(temporary, 0o600)
            with temporary.open("r+b") as stream:
                os.fsync(stream.fileno())
            validate_database_path(self.path)
            if self.path.exists():
                raise StorageError("The lifecycle destination changed during initialization.")
            os.replace(temporary, self.path)
            _sync_directory(self.path.parent)
        finally:
            for leftover in (temporary, temporary.with_name(temporary.name + "-journal")):
                if leftover.exists():
                    leftover.unlink()

    def _prepare(self) -> None:
        journal = self._companions()
        try:
            if not self.path.exists():
                if journal.exists():
                    raise InvalidDatabase("An orphan rollback journal cannot initialize lifecycle storage.")
                self._create()
                return
            self._check_header()
            with closing(self._connect()) as connection:
                self._validate(connection)
        except sqlite3.Error:
            raise InvalidDatabase("The lifecycle database could not be validated.") from None
        except OSError:
            raise StorageError("The lifecycle database files could not be processed safely.") from None

    # -------------------------------------------------------- transactions
    @contextmanager
    def transaction(self):
        """BEGIN IMMEDIATE under the process lock; commit or roll back as a whole."""
        with database_lock(self.path, self.timeout):
            self._companions()
            if not self.path.exists():
                raise InvalidDatabase("The lifecycle database is missing.")
            self._check_header()
            connection = None
            try:
                connection = self._connect()
                self._validate(connection)
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except BaseException as error:
                if connection is not None and connection.in_transaction:
                    connection.rollback()
                if isinstance(error, sqlite3.Error):
                    raise StorageError("The lifecycle database operation could not be completed.") from None
                raise
            finally:
                if connection is not None:
                    connection.close()
