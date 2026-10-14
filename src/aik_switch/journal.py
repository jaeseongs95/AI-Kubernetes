"""Append-only switch journal in its own SQLite file.

Path validation and the cooperating process lock are reused from
aik_runtime.paths. Every operation opens a fresh connection under that lock in
one IMMEDIATE transaction (DELETE journal, FULL sync). Triggers reject UPDATE
and DELETE. (event_id, phase) is unique: the same event is recorded once.
The per-row fingerprint is an unkeyed SHA-256 consistency check. It detects
accidental damage, but a program with file access can rewrite rows and
recompute it; it is not tamper protection.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from functools import lru_cache
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile

from aik_runtime.errors import StorageBusy
from aik_runtime.models import MAX_INTEGER
from aik_runtime.paths import database_lock, default_database_path, validate_database_path

from .errors import InvalidSwitch, InvalidSwitchJournal, SwitchConflict, SwitchError
from .models import check_id
from aik_platform.fs import sync_directory as _sync_directory


APPLICATION_ID = 0x41494B53  # AIKS; distinct from AIKR, AIKC, AIKP, AIKN and AIKI.
SCHEMA_VERSION = 1
PHASES = ("request", "apply")
RECORD_STATUSES = (
    "accepted", "rejected", "decision_required", "reauth_required",
    "applied", "recheck_failed", "activation_failed", "resumed",
)
MAX_DOCUMENT_BYTES = 64 * 1024
_COLUMNS = "seq, event_id, phase, director_id, status, document, fingerprint"


def _in(values: tuple) -> str:
    return ", ".join(f"'{value}'" for value in values)


_STATEMENTS = (
    "CREATE TABLE switch_records (\n"
    "    seq INTEGER PRIMARY KEY,\n"
    "    event_id TEXT NOT NULL,\n"
    f"    phase TEXT NOT NULL CHECK (phase IN ({_in(PHASES)})),\n"
    "    director_id TEXT NOT NULL,\n"
    f"    status TEXT NOT NULL CHECK (status IN ({_in(RECORD_STATUSES)})),\n"
    "    document TEXT NOT NULL,\n"
    "    fingerprint TEXT NOT NULL,\n"
    "    UNIQUE (event_id, phase)\n"
    ")",
    "CREATE INDEX switch_records_director ON switch_records (director_id, seq)",
    "CREATE TRIGGER switch_records_no_update BEFORE UPDATE ON switch_records "
    "BEGIN SELECT RAISE(ABORT, 'switch records are append-only'); END",
    "CREATE TRIGGER switch_records_no_delete BEFORE DELETE ON switch_records "
    "BEGIN SELECT RAISE(ABORT, 'switch records are append-only'); END",
)


def default_switch_path() -> Path:
    """Beside the runtime DB: %LOCALAPPDATA%\\AIK\\runtime\\switch.sqlite3 on Windows."""
    return default_database_path().parent / "switch.sqlite3"


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
            raise InvalidSwitchJournal("This is not a supported AIK switch journal.")
        if _schema_rows(connection) != _expected_schema():
            raise InvalidSwitchJournal("The switch journal schema differs from its declared version.")
        if full and tuple(row[0] for row in connection.execute("PRAGMA integrity_check")) != ("ok",):
            raise InvalidSwitchJournal("Switch journal integrity validation failed.")
    except sqlite3.Error:
        raise InvalidSwitchJournal("Switch journal validation could not be completed.") from None


def _sqlite_failure(error: sqlite3.Error) -> None:
    if getattr(error, "sqlite_errorcode", 0) & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        raise StorageBusy("SQLite could not acquire the switch journal lock.") from None
    raise SwitchError("The switch journal operation could not be completed.") from None


def _canonical(document: object) -> str:
    try:
        return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise InvalidSwitch("A switch record must be bounded JSON data.") from None


def _fingerprint(event_id: str, phase: str, director_id: str, status: str, text: str) -> str:
    """Unkeyed consistency digest: detects accidental damage, not deliberate rewriting."""
    material = "\0".join(("aik-switch", event_id, phase, director_id, status, text))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _check_header(event_id: object, phase: object, director_id: object, status: object) -> None:
    check_id(event_id, "event_id")
    check_id(director_id, "director_id")
    if type(phase) is not str or phase not in PHASES:
        raise InvalidSwitch("Unknown switch record phase.")
    if type(status) is not str or status not in RECORD_STATUSES:
        raise InvalidSwitch("Unknown switch record status.")


def _decode(row) -> tuple:
    try:
        seq, event_id, phase, director_id, status, text, digest = row
        if type(seq) is not int or type(text) is not str or type(digest) is not str or not digest.isascii():
            raise ValueError
        _check_header(event_id, phase, director_id, status)
        if not hmac.compare_digest(digest, _fingerprint(event_id, phase, director_id, status, text)):
            raise ValueError
        document = json.loads(text)
        if type(document) is not dict or (
            document.get("event_id"), document.get("phase"), document.get("director_id"), document.get("status"),
        ) != (event_id, phase, director_id, status):
            raise ValueError
        if _canonical(document) != text:
            raise ValueError
        return seq, document
    except (InvalidSwitch, TypeError, ValueError, KeyError, RecursionError):
        raise InvalidSwitchJournal("A stored switch record violates its contract.") from None


class SwitchJournal:
    """Append-only switch records; holds IDs, codes and model names, never secrets."""

    def __init__(self, path: Path | str | None = None, *, timeout: float = 5.0):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) \
                or timeout < 0:
            raise InvalidSwitch("Storage timeout must be a finite nonnegative number.")
        self.timeout = float(timeout)
        self.path = validate_database_path(default_switch_path() if path is None else path, create_parent=True)
        with database_lock(self.path, self.timeout):
            self._prepare()

    def _companion(self, suffix: str) -> Path:
        return self.path.with_name(self.path.name + suffix)

    def _check_files(self) -> None:
        for suffix in ("-journal", "-wal", "-shm"):
            candidate = self._companion(suffix)
            validate_database_path(candidate)
            if suffix != "-journal" and candidate.exists():
                raise InvalidSwitchJournal("The switch journal requires DELETE mode without WAL files.")
        if not self.path.exists() and self._companion("-journal").exists():
            raise InvalidSwitchJournal("An orphan rollback journal cannot initialize the switch journal.")

    def _connect(self, path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=self.timeout,
                                     isolation_level=None)
        try:
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA foreign_keys = ON")
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
                raise InvalidSwitchJournal("The switch journal requires DELETE journal mode.")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except BaseException:
            connection.close()
            raise

    def _create(self) -> None:
        descriptor, name = tempfile.mkstemp(prefix=".aik-switch-init-", suffix=".sqlite3", dir=self.path.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            with closing(self._connect(temporary)) as connection:
                _create_schema(connection)
                _validate_schema(connection, full=True)
            os.chmod(temporary, 0o600)
            with temporary.open("r+b") as stream:
                os.fsync(stream.fileno())
            validate_database_path(self.path)
            if self.path.exists():
                raise SwitchError("The switch journal destination changed during initialization.")
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
                self._create()
                return
            with closing(self._connect(self.path)) as connection:
                _validate_schema(connection, full=True)
                for row in connection.execute(f"SELECT {_COLUMNS} FROM switch_records ORDER BY seq"):
                    _decode(row)
        except sqlite3.Error:
            raise InvalidSwitchJournal("The switch journal could not be validated.") from None
        except OSError:
            raise SwitchError("The switch journal files could not be processed safely.") from None

    @contextmanager
    def _transaction(self):
        with database_lock(self.path, self.timeout):
            self._check_files()
            if not self.path.exists():
                raise InvalidSwitchJournal("The switch journal is missing; it is not recreated implicitly.")
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

    def append(self, document: dict) -> tuple:
        """Return ('accepted'|'duplicate', seq, stored document); differing reuse conflicts."""
        if type(document) is not dict:
            raise InvalidSwitch("A switch record must be a dict.")
        event_id, phase = document.get("event_id"), document.get("phase")
        director_id, status = document.get("director_id"), document.get("status")
        _check_header(event_id, phase, director_id, status)
        text = _canonical(document)
        if len(text.encode("utf-8")) > MAX_DOCUMENT_BYTES:
            raise InvalidSwitch("A switch record is too large.")
        digest = _fingerprint(event_id, phase, director_id, status, text)
        with self._transaction() as connection:
            row = connection.execute(f"SELECT {_COLUMNS} FROM switch_records WHERE event_id = ? AND phase = ?",
                                     (event_id, phase)).fetchone()
            if row is not None:
                seq, stored = _decode(row)
                if not hmac.compare_digest(row[6], digest):
                    raise SwitchConflict("A different switch record already uses this event ID.")
                return "duplicate", seq, stored
            cursor = connection.execute(
                "INSERT INTO switch_records (event_id, phase, director_id, status, document, fingerprint) "
                "VALUES (?, ?, ?, ?, ?, ?)", (event_id, phase, director_id, status, text, digest))
            return "accepted", cursor.lastrowid, json.loads(text)

    def get(self, event_id: str, phase: str = "request") -> dict | None:
        check_id(event_id, "event_id")
        if type(phase) is not str or phase not in PHASES:
            raise InvalidSwitch("Unknown switch record phase.")
        with self._transaction() as connection:
            row = connection.execute(f"SELECT {_COLUMNS} FROM switch_records WHERE event_id = ? AND phase = ?",
                                     (event_id, phase)).fetchone()
        return None if row is None else _decode(row)[1]

    def records(self, director_id: str, *, limit: int = 100, after_seq: int = 0) -> tuple:
        """(seq, document) pairs in append order."""
        check_id(director_id, "director_id")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise InvalidSwitch("The query limit must be an integer between 1 and 1000.")
        if type(after_seq) is not int or not 0 <= after_seq <= MAX_INTEGER:
            raise InvalidSwitch("The query position must be a nonnegative bounded integer.")
        with self._transaction() as connection:
            rows = connection.execute(
                f"SELECT {_COLUMNS} FROM switch_records WHERE director_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (director_id, after_seq, limit)).fetchall()
        return tuple(_decode(row) for row in rows)

    def latest(self, director_id: str) -> dict | None:
        check_id(director_id, "director_id")
        with self._transaction() as connection:
            row = connection.execute(
                f"SELECT {_COLUMNS} FROM switch_records WHERE director_id = ? ORDER BY seq DESC LIMIT 1",
                (director_id,)).fetchone()
        return None if row is None else _decode(row)[1]

    def latest_applied(self, director_id: str) -> dict | None:
        check_id(director_id, "director_id")
        with self._transaction() as connection:
            row = connection.execute(
                f"SELECT {_COLUMNS} FROM switch_records WHERE director_id = ? AND phase = 'apply' "
                "AND status = 'applied' ORDER BY seq DESC LIMIT 1", (director_id,)).fetchone()
        return None if row is None else _decode(row)[1]
