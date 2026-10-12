"""Atomic persistence for bounded AIK metadata, without a second task ledger.

Every operation uses a fresh SQLite connection under the stable process lock.
Existing files are validated on a private copy before SQLite may recover their
rollback journal. Schema upgrades also finish on that copy before publication;
ordinary writes use the real database and SQLite's durable rollback journal.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import fields
import errno
import math
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from . import schema
from .errors import (
    InvalidDatabase, InvalidRecord, MigrationError, RevisionConflict,
    StorageBusy, StorageError,
)
from .models import MAX_INTEGER, MODEL_TABLES, Pool, Versioned, identifier
from .paths import database_lock, default_database_path, validate_database_path


# Copy the allowlist so callers cannot redirect SQL by mutating MODEL_TABLES.
_TABLES = dict(MODEL_TABLES)
_COLUMNS = {model: tuple(field.name for field in fields(model)) for model in _TABLES}


class _RetainedRecovery(MigrationError):
    """An I/O failure prevented restoration; keep both original snapshots."""

    def __init__(self, recovery_directory: Path):
        super().__init__(
            "Runtime migration durability or recovery could not be confirmed; original snapshots were retained.",
            recovery_directory=recovery_directory,
        )


@contextmanager
def _snapshot_directory(parent: Path):
    temporary = Path(tempfile.mkdtemp(prefix=".aik-runtime-check-", dir=parent))
    preserve = False
    try:
        yield temporary
    except _RetainedRecovery:
        preserve = True
        raise
    finally:
        if not preserve:
            shutil.rmtree(temporary)


def _model_table(model) -> str:
    try:
        return _TABLES[model]
    except (KeyError, TypeError):
        raise InvalidRecord("Only supported runtime metadata models may be stored.") from None


def _revision(value: object, *, allow_create: bool) -> None:
    minimum = 0 if allow_create else 1
    if type(value) is not int or not minimum <= value <= MAX_INTEGER:
        raise InvalidRecord("An expected revision must be a bounded integer.")


def _record(value):
    model = type(value)
    _model_table(model)
    # Reconstruct even frozen dataclasses: object.__setattr__ can bypass their
    # initializer, and only exact model classes belong to this storage API.
    return model(**{name: getattr(value, name) for name in _COLUMNS[model]})


def _decode(model, row) -> Versioned:
    try:
        values = dict(zip(_COLUMNS[model], row[:-1]))
        if model is Pool:
            enabled = values["enabled"]
            if type(enabled) is not int or enabled not in (0, 1):
                raise InvalidRecord("Invalid stored boolean.")
            values["enabled"] = bool(enabled)
        _revision(row[-1], allow_create=False)
        return Versioned(model(**values), row[-1])
    except (InvalidRecord, TypeError, ValueError, KeyError, IndexError):
        raise InvalidDatabase("A stored runtime record violates its metadata contract.") from None


def _column_sql(model) -> str:
    return ", ".join(f'"{name}"' for name in (*_COLUMNS[model], "revision"))


def _validate_data(connection: sqlite3.Connection) -> int:
    version = schema.validate_schema(connection)
    for model, table in _TABLES.items():
        for row in connection.execute(f'SELECT {_column_sql(model)} FROM "{table}"'):
            _decode(model, row)
    return version


def _sqlite_failure(error: sqlite3.Error) -> None:
    if getattr(error, "sqlite_errorcode", 0) & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        raise StorageBusy("SQLite could not acquire the runtime database lock.") from None
    raise StorageError("The runtime database operation could not be completed.") from None


def _sync_file(path: Path) -> None:
    with path.open("r+b") as stream:
        stream.flush()
        os.fsync(stream.fileno())


def _sync_directory(path: Path) -> None:
    # Windows does not expose portable directory fsync through Python. File
    # contents are flushed there, but rename power-loss durability is OS-owned.
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


def _restore_snapshot(source: Path, destination: Path, staging: Path) -> None:
    # Keep the immutable raw snapshot until restoration of the whole pair has
    # succeeded, so a second I/O failure cannot consume the only recovery copy.
    shutil.copyfile(source, staging)
    os.chmod(staging, 0o600)
    _sync_file(staging)
    os.replace(staging, destination)
    _sync_directory(destination.parent)


def _companion(path: Path, suffix: str) -> Path:
    return path.with_name(path.name + suffix)


def _check_companions(path: Path) -> Path:
    for suffix in ("-journal", "-wal", "-shm"):
        candidate = _companion(path, suffix)
        validate_database_path(candidate)
        if suffix != "-journal" and candidate.exists():
            raise InvalidDatabase("Runtime storage requires a DELETE-mode database without WAL companions.")
    journal = _companion(path, "-journal")
    if not path.exists() and journal.exists():
        raise InvalidDatabase("An orphan rollback journal cannot initialize runtime storage.")
    return journal


class _Transaction:
    """A bounded repository view valid only inside its owning transaction."""

    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection
        self._active = True

    def _require_active(self) -> None:
        if not self._active or not self._connection.in_transaction:
            raise StorageError("The runtime transaction is no longer active.")

    @contextmanager
    def _write(self):
        self._require_active()
        self._connection.execute('SAVEPOINT "aik_record_write"')
        try:
            yield
        except BaseException:
            self._connection.execute('ROLLBACK TO "aik_record_write"')
            self._connection.execute('RELEASE "aik_record_write"')
            raise
        else:
            self._connection.execute('RELEASE "aik_record_write"')

    def save(self, value, expected_revision: int = 0) -> Versioned:
        self._require_active()
        value = _record(value)
        _revision(expected_revision, allow_create=True)
        model = type(value)
        table = _TABLES[model]
        names = _COLUMNS[model]
        values = [getattr(value, name) for name in names]
        try:
            with self._write():
                if expected_revision == 0:
                    if self._connection.execute(f'SELECT 1 FROM "{table}" WHERE "id" = ?', (value.id,)).fetchone():
                        raise RevisionConflict("The runtime record already exists.")
                    placeholders = ", ".join("?" for _ in range(len(values) + 1))
                    self._connection.execute(
                        f'INSERT INTO "{table}" ({_column_sql(model)}) VALUES ({placeholders})',
                        [*values, 1],
                    )
                    revision = 1
                else:
                    if expected_revision == MAX_INTEGER:
                        raise RevisionConflict("The runtime revision cannot be incremented.")
                    revision = expected_revision + 1
                    assignments = ", ".join(f'"{name}" = ?' for name in names if name != "id")
                    updated = self._connection.execute(
                        f'UPDATE "{table}" SET {assignments}, "revision" = ? WHERE "id" = ? AND "revision" = ?',
                        [*(getattr(value, name) for name in names if name != "id"), revision, value.id, expected_revision],
                    )
                    if updated.rowcount != 1:
                        raise RevisionConflict("The runtime record revision has changed or the record is absent.")
        except sqlite3.IntegrityError:
            raise InvalidRecord("Runtime metadata references or constraints are inconsistent.") from None
        except sqlite3.Error as error:
            _sqlite_failure(error)
        return Versioned(value, revision)

    def get(self, model, id: str) -> Versioned | None:
        self._require_active()
        table = _model_table(model)
        identifier(id)
        try:
            row = self._connection.execute(
                f'SELECT {_column_sql(model)} FROM "{table}" WHERE "id" = ?', (id,),
            ).fetchone()
        except sqlite3.Error as error:
            _sqlite_failure(error)
        return None if row is None else _decode(model, row)

    def list(self, model, limit: int = 100, after_id: str | None = None) -> tuple:
        self._require_active()
        table = _model_table(model)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise InvalidRecord("The query limit must be an integer between 1 and 1000.")
        if after_id is not None:
            identifier(after_id, "after_id")
        try:
            if after_id is None:
                rows = self._connection.execute(
                    f'SELECT {_column_sql(model)} FROM "{table}" ORDER BY "id" LIMIT ?', (limit,),
                )
            else:
                rows = self._connection.execute(
                    f'SELECT {_column_sql(model)} FROM "{table}" WHERE "id" > ? ORDER BY "id" LIMIT ?',
                    (after_id, limit),
                )
            return tuple(_decode(model, row) for row in rows)
        except sqlite3.Error as error:
            _sqlite_failure(error)

    def delete(self, model, id: str, expected_revision: int) -> None:
        self._require_active()
        table = _model_table(model)
        identifier(id)
        _revision(expected_revision, allow_create=False)
        try:
            with self._write():
                deleted = self._connection.execute(
                    f'DELETE FROM "{table}" WHERE "id" = ? AND "revision" = ?', (id, expected_revision),
                )
                if deleted.rowcount != 1:
                    raise RevisionConflict("The runtime record revision has changed or the record is absent.")
        except sqlite3.IntegrityError:
            raise InvalidRecord("A referenced runtime record cannot be deleted.") from None
        except sqlite3.Error as error:
            _sqlite_failure(error)


class RuntimeStore:
    """Persist only AIK-owned metadata in an external, versioned SQLite file.

    Expected revision zero creates a record. Updates and deletes require the
    observed positive revision; all records in transaction() commit together.
    Instances hold no idle connections and can observe another instance's
    committed writes or a successfully published schema upgrade.
    """

    def __init__(self, path: Path | None = None, timeout: float = 5.0):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
            raise InvalidRecord("Storage timeout must be a finite nonnegative number.")
        self.timeout = float(timeout)
        self.path = validate_database_path(default_database_path() if path is None else path, create_parent=True)
        with database_lock(self.path, self.timeout):
            self._prepare()

    def _connect(self, path: Path, *, create: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(
            path.as_uri() + ("?mode=rwc" if create else "?mode=rw"),
            uri=True, timeout=self.timeout, isolation_level=None,
        )
        try:
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA foreign_keys = ON")
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
                raise InvalidDatabase("Runtime storage requires DELETE journal mode.")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except BaseException:
            connection.close()
            raise

    def _prepare(self) -> None:
        """Validate and, when needed, create or upgrade while owning the lock."""
        validate_database_path(self.path)
        journal = _check_companions(self.path)
        try:
            with _snapshot_directory(self.path.parent) as temporary:
                checked = temporary / "checked.sqlite3"
                if not self.path.exists():
                    with closing(self._connect(checked, create=True)) as connection:
                        schema.create_schema(connection)
                        _validate_data(connection)
                    os.chmod(checked, 0o600)
                    _sync_file(checked)
                    validate_database_path(self.path)
                    _check_companions(self.path)
                    if self.path.exists():
                        raise StorageError("The runtime destination changed during initialization.")
                    os.replace(checked, self.path)
                    _sync_directory(self.path.parent)
                    return

                shutil.copyfile(self.path, checked)
                if journal.exists():
                    shutil.copyfile(journal, _companion(checked, "-journal"))
                try:
                    with closing(self._connect(checked)) as connection:
                        old_version = _validate_data(connection)
                        if old_version == schema.SCHEMA_VERSION:
                            return
                        # Neither migration code nor SQLite recovery has touched
                        # the original file. Any rejected upgrade ends here.
                        schema.migrate(connection, old_version)
                        _validate_data(connection)
                except sqlite3.Error:
                    raise InvalidDatabase("The runtime database could not be safely validated.") from None

                os.chmod(checked, 0o600)
                _sync_file(checked)
                self._publish_upgrade(checked, old_version, temporary)
        except StorageError:
            raise
        except sqlite3.Error as error:
            _sqlite_failure(error)
        except OSError:
            raise StorageError("The runtime database files could not be processed safely.") from None

    def _publish_upgrade(self, checked: Path, old_version: int, temporary: Path) -> None:
        """Recover a validated source journal before replacing its main file.

        Keeping the raw source pair permits restoration if recovery or atomic
        publication fails. A journal is never discarded before its database has
        been recovered: process death between recovery and replacement leaves a
        valid old database, and death after replacement leaves the new schema.
        """
        validate_database_path(self.path)
        journal = _check_companions(self.path)
        original = temporary / "original.sqlite3"
        original_journal = _companion(original, "-journal")
        shutil.copyfile(self.path, original)
        had_journal = journal.exists()
        if had_journal:
            shutil.copyfile(journal, original_journal)
            _sync_file(original_journal)
        _sync_file(original)
        _sync_directory(temporary)
        _sync_directory(self.path.parent)
        recovery_started = False
        published = False
        try:
            if had_journal:
                recovery_started = True
                with closing(self._connect(self.path)) as connection:
                    if _validate_data(connection) != old_version:
                        raise InvalidDatabase("The runtime source changed before migration publication.")
                    if journal.exists():
                        # A writer killed before journal activation may leave a
                        # cold journal. Let SQLite finalize it in DELETE mode.
                        connection.execute("BEGIN IMMEDIATE")
                        connection.execute(f"PRAGMA user_version = {old_version}")
                        connection.commit()
                if journal.exists():
                    raise MigrationError("The source rollback journal could not be finalized.")
            validate_database_path(self.path)
            _check_companions(self.path)
            os.replace(checked, self.path)
            published = True
            _sync_directory(self.path.parent)
        except BaseException:
            if published:
                # The new main file is already visible. Installing an old hot
                # journal beside it would create a mismatched pair, so retain
                # the raw snapshots for an explicit recovery decision instead.
                raise _RetainedRecovery(temporary) from None
            if recovery_started:
                # Restore the journal first: a crash then remains recoverable.
                # All SQLite handles have closed, including on Windows.
                try:
                    if had_journal:
                        _restore_snapshot(original_journal, journal, temporary / "restore-journal")
                    _restore_snapshot(original, self.path, temporary / "restore-database")
                    _sync_directory(self.path.parent)
                except OSError:
                    raise _RetainedRecovery(temporary) from None
            raise MigrationError("Runtime schema publication failed; the source was preserved.") from None

    @contextmanager
    def transaction(self):
        """Commit all contained record operations together, or roll them back."""
        with database_lock(self.path, self.timeout):
            self._prepare()
            validate_database_path(self.path)
            _check_companions(self.path)
            connection = None
            transaction = None
            try:
                connection = self._connect(self.path)
                connection.execute("BEGIN IMMEDIATE")
                transaction = _Transaction(connection)
                yield transaction
                connection.commit()
            except BaseException as error:
                if connection is not None and connection.in_transaction:
                    connection.rollback()
                if isinstance(error, sqlite3.Error):
                    _sqlite_failure(error)
                raise
            finally:
                if transaction is not None:
                    transaction._active = False
                if connection is not None:
                    connection.close()

    def save(self, value, expected_revision: int = 0) -> Versioned:
        with self.transaction() as transaction:
            return transaction.save(value, expected_revision)

    def get(self, model, id: str) -> Versioned | None:
        with self.transaction() as transaction:
            return transaction.get(model, id)

    def list(self, model, limit: int = 100, after_id: str | None = None) -> tuple:
        with self.transaction() as transaction:
            return transaction.list(model, limit, after_id)

    def delete(self, model, id: str, expected_revision: int) -> None:
        with self.transaction() as transaction:
            transaction.delete(model, id, expected_revision)
