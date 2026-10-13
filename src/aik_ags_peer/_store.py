"""Append-only SQLite base following the aik_validation journal pattern."""

from __future__ import annotations

from contextlib import closing, contextmanager
import math
import os
from pathlib import Path
import sqlite3

from aik_runtime.errors import StorageBusy, StorageError
from aik_runtime.paths import database_lock, validate_database_path

from .errors import AgsPeerError


APPLICATION_ID = 0x41494B50  # AIKP
_MAGIC = b"SQLite format 3\x00"
_EXPECTED: dict = {}


def _schema_rows(connection: sqlite3.Connection) -> tuple:
    return tuple(tuple(row) for row in connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ))


def _create(connection: sqlite3.Connection, statements: tuple, version: int) -> None:
    for statement in statements:
        connection.execute(statement)
    connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    connection.execute(f"PRAGMA user_version = {version}")


def _expected(statements: tuple, version: int) -> tuple:
    key = (statements, version)
    if key not in _EXPECTED:
        with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
            _create(connection, statements, version)
            _EXPECTED[key] = _schema_rows(connection)
    return _EXPECTED[key]


class AppendOnlyDatabase:
    """Validated private SQLite file; schema objects must match exactly."""

    STATEMENTS: tuple = ()
    SCHEMA_VERSION = 1

    def __init__(self, path: Path | str, *, timeout: float = 5.0):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout < 0):
            raise AgsPeerError("INVALID_TIMEOUT")
        self._timeout = float(timeout)
        try:
            self._path = validate_database_path(path, create_parent=True)
        except StorageError:
            raise AgsPeerError("UNSAFE_PATH") from None
        self._prepare()

    @property
    def path(self) -> Path:
        return self._path

    @contextmanager
    def _locked(self):
        try:
            with database_lock(self._path, self._timeout):
                validate_database_path(self._path)
                self._check_companions()
                yield
        except StorageBusy:
            raise AgsPeerError("STORE_BUSY") from None
        except StorageError:
            raise AgsPeerError("STORE_UNAVAILABLE") from None

    def _check_companions(self) -> None:
        for suffix in ("-wal", "-shm"):
            if self._path.with_name(self._path.name + suffix).exists():
                raise AgsPeerError("INVALID_STORE")

    def _connect(self, *, create: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._path.as_uri() + ("?mode=rwc" if create else "?mode=rw"),
            uri=True, timeout=self._timeout, isolation_level=None,
        )
        try:
            connection.execute("PRAGMA trusted_schema = OFF")
            if str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "delete":
                raise AgsPeerError("INVALID_STORE")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except BaseException:
            connection.close()
            raise

    def _check_rows(self, connection: sqlite3.Connection) -> None:
        """Subclass hook for row-level invariants."""

    def _validate(self, connection: sqlite3.Connection) -> None:
        if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise AgsPeerError("INVALID_STORE")
        if connection.execute("PRAGMA user_version").fetchone()[0] != self.SCHEMA_VERSION:
            raise AgsPeerError("INVALID_STORE")
        if _schema_rows(connection) != _expected(self.STATEMENTS, self.SCHEMA_VERSION):
            raise AgsPeerError("INVALID_STORE")
        if tuple(row[0] for row in connection.execute("PRAGMA quick_check")) != ("ok",):
            raise AgsPeerError("INVALID_STORE")
        self._check_rows(connection)

    def _prepare(self) -> None:
        with self._locked():
            try:
                if not self._path.exists() or self._path.stat().st_size == 0:
                    with closing(self._connect(create=True)) as connection:
                        if connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is not None:
                            raise AgsPeerError("INVALID_STORE")
                        connection.execute("BEGIN IMMEDIATE")
                        try:
                            _create(connection, self.STATEMENTS, self.SCHEMA_VERSION)
                            connection.commit()
                        except BaseException:
                            connection.rollback()
                            raise
                    os.chmod(self._path, 0o600)
                    return
                with self._path.open("rb") as stream:
                    if stream.read(16) != _MAGIC:
                        raise AgsPeerError("INVALID_STORE")
                with closing(self._connect()) as connection:
                    self._validate(connection)
            except sqlite3.Error:
                raise AgsPeerError("INVALID_STORE") from None
            except OSError:
                raise AgsPeerError("STORE_UNAVAILABLE") from None

    @contextmanager
    def _session(self):
        with self._locked():
            try:
                with closing(self._connect()) as connection:
                    self._validate(connection)
                    yield connection
            except sqlite3.Error:
                raise AgsPeerError("STORE_UNAVAILABLE") from None

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
