"""Append-only SQLite journal of every placement decision (application ID AIKJ).

Path rules and the cooperating process lock are reused from aik_runtime.paths.
The journal is a separate file; it is not the runtime database, not a Task
ledger and holds no VM Task body. Rows cannot be updated or deleted.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from aik_runtime.errors import InvalidDatabase, StorageBusy, StorageError
from aik_runtime.paths import database_lock, default_database_path, validate_database_path
from aik_runtime.storage import _sync_directory, _sync_file

from .models import (
    PlacementDecision, PlacementError, canonical_json, check_id, compute_digest, decision_document,
)


APPLICATION_ID = 0x41494B4A  # AIKJ; distinct from runtime (AIKR), budget (AIKB), profiles (AIKP).
SCHEMA_VERSION = 1
_STATEMENTS = (
    "CREATE TABLE decisions (seq INTEGER PRIMARY KEY, decision_id TEXT NOT NULL UNIQUE, "
    "status TEXT NOT NULL CHECK (status IN ('placed', 'wait', 'rejected')), "
    "decided_at TEXT NOT NULL, digest TEXT NOT NULL, document TEXT NOT NULL)",
    "CREATE TRIGGER decisions_no_update BEFORE UPDATE ON decisions "
    "BEGIN SELECT RAISE(ABORT, 'append-only'); END",
    "CREATE TRIGGER decisions_no_delete BEFORE DELETE ON decisions "
    "BEGIN SELECT RAISE(ABORT, 'append-only'); END",
)


def default_journal_path() -> Path:
    """Next to the runtime database, as its own file."""
    return default_database_path().with_name("placement-journal.sqlite3")


def _create_schema(connection: sqlite3.Connection) -> None:
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
        _create_schema(connection)
        return _schema_rows(connection)


def _validate_schema(connection: sqlite3.Connection, *, full: bool) -> None:
    try:
        if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID \
                or connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise InvalidDatabase("This is not a supported AIK placement journal.")
        if _schema_rows(connection) != _expected_schema():
            raise InvalidDatabase("The placement journal schema differs from its declared version.")
        if full and tuple(row[0] for row in connection.execute("PRAGMA integrity_check")) != ("ok",):
            raise InvalidDatabase("Placement journal integrity validation failed.")
    except sqlite3.Error:
        raise InvalidDatabase("Placement journal validation could not be completed.") from None


def _sqlite_failure(error: sqlite3.Error) -> None:
    if getattr(error, "sqlite_errorcode", 0) & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        raise StorageBusy("SQLite could not acquire the placement journal lock.") from None
    raise StorageError("The placement journal operation could not be completed.") from None


class PlacementJournal:
    """Append-only record of placed, wait and rejected decisions."""

    def __init__(self, path: Path | str | None = None, *, timeout: float = 5.0):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) \
                or timeout < 0:
            raise PlacementError("INVALID_TIMEOUT")
        self.timeout = float(timeout)
        self.path = validate_database_path(default_journal_path() if path is None else path, create_parent=True)
        with database_lock(self.path, self.timeout):
            self._initialize()

    def _connect(self, path: Path, *, create: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(
            path.as_uri() + ("?mode=rwc" if create else "?mode=rw"),
            uri=True, timeout=self.timeout, isolation_level=None,
        )
        try:
            connection.execute("PRAGMA trusted_schema = OFF")
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
                raise InvalidDatabase("The placement journal requires DELETE journal mode.")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except BaseException:
            connection.close()
            raise

    def _check_files(self) -> bool:
        validate_database_path(self.path)
        for suffix in ("-wal", "-shm"):
            companion = self.path.with_name(self.path.name + suffix)
            validate_database_path(companion)
            if companion.exists():
                raise InvalidDatabase("The placement journal requires a DELETE-mode database.")
        journal = self.path.with_name(self.path.name + "-journal")
        validate_database_path(journal)
        if not self.path.exists():
            if journal.exists():
                raise InvalidDatabase("An orphan rollback journal cannot initialize the placement journal.")
            return False
        with self.path.open("rb") as stream:
            header = stream.read(100)
        if len(header) < 100 or header[:16] != b"SQLite format 3\x00" \
                or int.from_bytes(header[68:72], "big") != APPLICATION_ID:
            raise InvalidDatabase("This file is not an AIK placement journal.")
        return True

    def _initialize(self) -> None:
        try:
            if self._check_files():
                with closing(self._connect(self.path)) as connection:
                    _validate_schema(connection, full=True)
                return
            temporary = Path(tempfile.mkdtemp(prefix=".aik-placement-init-", dir=self.path.parent))
            try:
                created = temporary / "journal.sqlite3"
                with closing(self._connect(created, create=True)) as connection:
                    _create_schema(connection)
                    _validate_schema(connection, full=True)
                os.chmod(created, 0o600)
                _sync_file(created)
                if self._check_files():
                    raise StorageError("The placement journal destination changed during initialization.")
                os.replace(created, self.path)
                _sync_directory(self.path.parent)
            finally:
                shutil.rmtree(temporary, ignore_errors=True)
        except StorageError:
            raise
        except sqlite3.Error as error:
            _sqlite_failure(error)
        except OSError:
            raise StorageError("The placement journal files could not be processed safely.") from None

    @contextmanager
    def _transaction(self):
        with database_lock(self.path, self.timeout):
            try:
                exists = self._check_files()
            except OSError:
                raise StorageError("The placement journal files could not be inspected.") from None
            if not exists:
                raise InvalidDatabase("The placement journal is missing; it is not recreated implicitly.")
            connection = None
            try:
                connection = self._connect(self.path)
                connection.execute("BEGIN IMMEDIATE")
                _validate_schema(connection, full=False)
                yield connection
                connection.commit()
            except sqlite3.Error as error:
                if connection is not None and connection.in_transaction:
                    connection.rollback()
                _sqlite_failure(error)
            except BaseException:
                if connection is not None and connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                if connection is not None:
                    connection.close()

    def append(self, decision: PlacementDecision) -> int:
        """Append one decision after verifying its digest; returns its sequence number."""
        if type(decision) is not PlacementDecision:
            raise PlacementError("INVALID_DECISION")
        check_id(decision.decision_id, "decision_id")
        document = decision_document(decision)
        if type(decision.digest) is not str or compute_digest(document) != decision.digest:
            raise PlacementError("DIGEST_MISMATCH")
        text = canonical_json({**document, "digest": decision.digest})
        with self._transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO decisions (decision_id, status, decided_at, digest, document) VALUES (?, ?, ?, ?, ?)",
                (decision.decision_id, decision.status, decision.decided_at, decision.digest, text),
            )
            return cursor.lastrowid

    def decisions(self, after_seq: int = 0, limit: int = 100) -> tuple[dict, ...]:
        """Decisions in sequence order after after_seq; each dict carries its seq and verified digest."""
        if type(after_seq) is not int or after_seq < 0:
            raise PlacementError("INVALID_AFTER_SEQ")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise PlacementError("INVALID_LIMIT")
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT seq, digest, document FROM decisions WHERE seq > ? ORDER BY seq LIMIT ?",
                (after_seq, limit),
            ).fetchall()
        records = []
        for seq, digest, text in rows:
            try:
                document = json.loads(text)
                if type(document) is not dict:
                    raise ValueError
                stored = document.pop("digest")
            except (KeyError, TypeError, ValueError):
                raise InvalidDatabase("A stored placement decision is malformed.") from None
            if stored != digest or compute_digest(document) != digest:
                raise InvalidDatabase("A stored placement decision does not match its digest.")
            records.append({**document, "digest": digest, "seq": seq})
        return tuple(records)
