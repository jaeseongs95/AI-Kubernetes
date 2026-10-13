"""Connection scope repository in its own SQLite file beside the settings file.

Path validation and the process lock are reused from aik_runtime.paths. The
file has its own application ID (AIKW). Lock order is always scope DB, then
settings DB. The file also keeps transfer records waiting for reconnection.
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

from aik_config import AgentProfile, ConnectionProfile, Revisioned, SettingsStore, Workspace
from aik_config.errors import InvalidReference
from aik_runtime.errors import (
    InvalidDatabase, InvalidRecord, RevisionConflict, StorageBusy, StorageError, UnsafeStoragePath,
)
from aik_runtime.models import MAX_INTEGER
from aik_runtime.paths import database_lock, validate_database_path
from aik_runtime.storage import _sync_directory, _sync_file

from .errors import ConnectionNotInScope, InvalidScope
from .models import (
    PENDING_KINDS, ConnectionScope, PendingRecord, ScopeDecision, ScopeViolation, check_scope_identifier,
)


SCOPES_APPLICATION_ID = 0x41494B57  # AIKW; distinct from AIKR, AIKC, AIKN and AIKP.
_SCHEMA_VERSION = 1
_PAGE = 1000
_STATEMENTS = (
    'CREATE TABLE "scopes" (\n'
    '    "workspace_id" TEXT PRIMARY KEY NOT NULL CHECK (typeof("workspace_id") = \'text\' '
    'AND length("workspace_id") BETWEEN 1 AND 128),\n'
    '    "document" TEXT NOT NULL CHECK (typeof("document") = \'text\'),\n'
    '    "revision" INTEGER NOT NULL CHECK (typeof("revision") = \'integer\' AND "revision" >= 1)\n'
    ')',
    'CREATE TABLE "pending_restores" (\n'
    '    "kind" TEXT NOT NULL CHECK ("kind" IN (\'connection\', \'agent_profile\', \'execution_profile\')),\n'
    '    "record_id" TEXT NOT NULL CHECK (typeof("record_id") = \'text\' AND length("record_id") BETWEEN 1 AND 128),\n'
    '    "workspace_id" TEXT NOT NULL CHECK (typeof("workspace_id") = \'text\'),\n'
    '    "document" TEXT NOT NULL CHECK (typeof("document") = \'text\'),\n'
    '    PRIMARY KEY ("kind", "record_id")\n'
    ')',
)


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in _STATEMENTS:
            connection.execute(statement)
        connection.execute(f"PRAGMA application_id = {SCOPES_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _schema_rows(connection: sqlite3.Connection) -> tuple:
    return tuple(tuple(row) for row in connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ))


@lru_cache(maxsize=1)
def _expected_rows() -> tuple:
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
        _create_schema(connection)
        return _schema_rows(connection)


def _sqlite_failure(error: sqlite3.Error) -> None:
    if getattr(error, "sqlite_errorcode", 0) & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        raise StorageBusy("SQLite could not acquire the scope database lock.") from None
    raise StorageError("The scope database operation could not be completed.") from None


def _json_text(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _revision(value: object, *, allow_create: bool) -> None:
    minimum = 0 if allow_create else 1
    if type(value) is not int or not minimum <= value <= MAX_INTEGER:
        raise InvalidScope("An expected revision must be a bounded integer.")


def _rebuild(scope) -> ConnectionScope:
    if type(scope) is not ConnectionScope:
        raise InvalidScope("Only ConnectionScope records can be stored.")
    return ConnectionScope(scope.workspace_id, scope.allowed_connection_ids, scope.shared_connection_ids)


def _scope_document(scope: ConnectionScope) -> str:
    return _json_text({"allowed": list(scope.allowed_connection_ids), "shared": list(scope.shared_connection_ids)})


def _decode_scope(row) -> Revisioned:
    try:
        workspace_id, document, revision = row
        data = json.loads(document)
        if type(data) is not dict or set(data) != {"allowed", "shared"} \
                or type(data["allowed"]) is not list or type(data["shared"]) is not list:
            raise ValueError
        if type(revision) is not int or not 1 <= revision <= MAX_INTEGER:
            raise ValueError
        return Revisioned(ConnectionScope(workspace_id, tuple(data["allowed"]), tuple(data["shared"])), revision)
    except (InvalidRecord, TypeError, ValueError, RecursionError):
        raise InvalidDatabase("A stored connection scope violates its contract.") from None


def _decode_pending(row) -> tuple:
    try:
        kind, record_id, workspace_id, document = row
        data = json.loads(document)
        if type(data) is not dict or set(data) != {"requires", "record"} \
                or type(data["requires"]) is not list or type(data["record"]) is not dict:
            raise ValueError
        return PendingRecord(kind, record_id, workspace_id, tuple(data["requires"])), data["record"]
    except (InvalidRecord, TypeError, ValueError, RecursionError):
        raise InvalidDatabase("A stored pending restore violates its contract.") from None


class ScopeStore:
    """Explicit per-workspace connection allowlists with a fail-closed guard."""

    def __init__(self, settings: SettingsStore, path: Path | str | None = None, *, timeout: float = 5.0):
        if not isinstance(settings, SettingsStore):
            raise InvalidScope("A settings store is required.")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) \
                or timeout < 0:
            raise InvalidScope("Storage timeout must be a finite nonnegative number.")
        self.settings = settings
        self.timeout = float(timeout)
        requested = settings.path.with_name("scopes.sqlite3") if path is None else path
        self.path = validate_database_path(requested, create_parent=True)
        if self.path == settings.path:
            raise UnsafeStoragePath("Scope storage requires a file separate from the settings database.")
        with database_lock(self.path, self.timeout):
            self._initialize()

    # ------------------------------------------------------------ file layer
    def _connect(self, path: Path, *, create: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(
            path.as_uri() + ("?mode=rwc" if create else "?mode=rw"),
            uri=True, timeout=self.timeout, isolation_level=None,
        )
        try:
            connection.execute("PRAGMA trusted_schema = OFF")
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
                raise InvalidDatabase("Scope storage requires DELETE journal mode.")
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
                raise InvalidDatabase("Scope storage requires a DELETE-mode database without WAL companions.")
        journal = self.path.with_name(self.path.name + "-journal")
        validate_database_path(journal)
        if not self.path.exists():
            if journal.exists():
                raise InvalidDatabase("An orphan rollback journal cannot initialize scope storage.")
            return False
        with self.path.open("rb") as stream:
            header = stream.read(100)
        if len(header) < 100 or header[:16] != b"SQLite format 3\x00" \
                or int.from_bytes(header[68:72], "big") != SCOPES_APPLICATION_ID:
            raise InvalidDatabase("This file is not an AIK scope database.")
        return True

    def _validate(self, connection: sqlite3.Connection, *, full: bool) -> None:
        try:
            app_id = connection.execute("PRAGMA application_id").fetchone()[0]
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if app_id != SCOPES_APPLICATION_ID or version != _SCHEMA_VERSION:
                raise InvalidDatabase("This is not a supported AIK scope database.")
            if _schema_rows(connection) != _expected_rows():
                raise InvalidDatabase("The scope schema differs from its declared version.")
            if full:
                if tuple(row[0] for row in connection.execute("PRAGMA integrity_check")) != ("ok",):
                    raise InvalidDatabase("Scope database integrity validation failed.")
                scopes = connection.execute('SELECT "workspace_id", "document", "revision" FROM "scopes"').fetchall()
                pending = connection.execute(
                    'SELECT "kind", "record_id", "workspace_id", "document" FROM "pending_restores"').fetchall()
        except sqlite3.Error:
            raise InvalidDatabase("Scope database validation could not be completed.") from None
        if full:
            for row in scopes:
                _decode_scope(row)
            for row in pending:
                _decode_pending(row)

    def _initialize(self) -> None:
        try:
            if self._check_files():
                with closing(self._connect(self.path)) as connection:
                    self._validate(connection, full=True)
                return
            temporary = Path(tempfile.mkdtemp(prefix=".aik-scopes-init-", dir=self.path.parent))
            try:
                created = temporary / "scopes.sqlite3"
                with closing(self._connect(created, create=True)) as connection:
                    _create_schema(connection)
                    self._validate(connection, full=True)
                os.chmod(created, 0o600)
                _sync_file(created)
                if self._check_files():
                    raise StorageError("The scope destination changed during initialization.")
                os.replace(created, self.path)
                _sync_directory(self.path.parent)
            finally:
                shutil.rmtree(temporary, ignore_errors=True)
        except StorageError:
            raise
        except sqlite3.Error as error:
            _sqlite_failure(error)
        except OSError:
            raise StorageError("The scope database files could not be processed safely.") from None

    @contextmanager
    def _transaction(self):
        with database_lock(self.path, self.timeout):
            try:
                exists = self._check_files()
            except OSError:
                raise StorageError("The scope database files could not be inspected.") from None
            if not exists:
                raise InvalidDatabase("The scope database is missing; it is not recreated implicitly.")
            connection = None
            try:
                connection = self._connect(self.path)
                connection.execute("BEGIN IMMEDIATE")
                self._validate(connection, full=False)
                yield connection
                connection.commit()
            except BaseException as error:
                if connection is not None and connection.in_transaction:
                    connection.rollback()
                if isinstance(error, sqlite3.Error):
                    _sqlite_failure(error)
                raise
            finally:
                if connection is not None:
                    connection.close()

    @staticmethod
    def _fetch(db: sqlite3.Connection, workspace_id: str) -> Revisioned | None:
        row = db.execute('SELECT "workspace_id", "document", "revision" FROM "scopes" WHERE "workspace_id" = ?',
                         (workspace_id,)).fetchone()
        return None if row is None else _decode_scope(row)

    # ----------------------------------------------------------- scope CRUD
    def save(self, scope, expected_revision: int = 0) -> Revisioned:
        """Create (revision 0) or replace a workspace scope with a revision check."""
        scope = _rebuild(scope)
        _revision(expected_revision, allow_create=True)
        document = _scope_document(scope)
        with self._transaction() as db:
            if self.settings.get(Workspace, scope.workspace_id) is None:
                raise InvalidReference("The scoped workspace does not exist.")
            row = db.execute('SELECT "revision" FROM "scopes" WHERE "workspace_id" = ?',
                             (scope.workspace_id,)).fetchone()
            if expected_revision == 0:
                if row is not None:
                    raise RevisionConflict("A scope already exists for the workspace.")
                db.execute('INSERT INTO "scopes" ("workspace_id", "document", "revision") VALUES (?, ?, 1)',
                           (scope.workspace_id, document))
                revision = 1
            else:
                if row is None or row[0] != expected_revision or expected_revision == MAX_INTEGER:
                    raise RevisionConflict("The scope revision has changed or the scope is absent.")
                revision = expected_revision + 1
                updated = db.execute(
                    'UPDATE "scopes" SET "document" = ?, "revision" = ? WHERE "workspace_id" = ? AND "revision" = ?',
                    (document, revision, scope.workspace_id, expected_revision))
                if updated.rowcount != 1:
                    raise RevisionConflict("The scope revision has changed.")
        return Revisioned(scope, revision)

    def get(self, workspace_id: str) -> Revisioned | None:
        check_scope_identifier(workspace_id, "workspace_id")
        with self._transaction() as db:
            return self._fetch(db, workspace_id)

    def list(self) -> tuple:
        with self._transaction() as db:
            rows = db.execute('SELECT "workspace_id", "document", "revision" FROM "scopes" '
                              'ORDER BY "workspace_id"').fetchall()
        return tuple(_decode_scope(row) for row in rows)

    def delete(self, workspace_id: str, expected_revision: int) -> None:
        """Removing a scope denies every connection to the workspace."""
        check_scope_identifier(workspace_id, "workspace_id")
        _revision(expected_revision, allow_create=False)
        with self._transaction() as db:
            deleted = db.execute('DELETE FROM "scopes" WHERE "workspace_id" = ? AND "revision" = ?',
                                 (workspace_id, expected_revision))
            if deleted.rowcount != 1:
                raise RevisionConflict("The scope revision has changed or the scope is absent.")

    # ---------------------------------------------------------------- guard
    def check(self, workspace_id: str, connection_id: str) -> ScopeDecision:
        """Return the allowed use or raise ConnectionNotInScope (fail-closed)."""
        check_scope_identifier(workspace_id, "workspace_id")
        check_scope_identifier(connection_id, "connection_id")
        with self._transaction() as db:
            scope = self._fetch(db, workspace_id)
            pending = db.execute(
                'SELECT 1 FROM "pending_restores" WHERE "kind" = \'connection\' AND "record_id" = ?',
                (connection_id,)).fetchone()
        if scope is None:
            raise ConnectionNotInScope("NO_SCOPE", workspace_id, connection_id)
        if connection_id not in scope.value.allowed_connection_ids:
            raise ConnectionNotInScope("NOT_ALLOWED", workspace_id, connection_id)
        if pending is not None:
            raise ConnectionNotInScope("RECONNECTION_REQUIRED", workspace_id, connection_id)
        entry = self.settings.get(ConnectionProfile, connection_id)
        if entry is None:
            raise ConnectionNotInScope("UNKNOWN_CONNECTION", workspace_id, connection_id)
        owner = entry.value.workspace_id
        if owner == workspace_id:
            return ScopeDecision(workspace_id, connection_id, owner, False)
        with self._transaction() as db:
            owner_scope = self._fetch(db, owner)
        if owner_scope is None or connection_id not in owner_scope.value.shared_connection_ids:
            raise ConnectionNotInScope("NOT_SHARED", workspace_id, connection_id)
        return ScopeDecision(workspace_id, connection_id, owner, True)

    def is_allowed(self, workspace_id: str, connection_id: str) -> bool:
        try:
            self.check(workspace_id, connection_id)
        except ConnectionNotInScope:
            return False
        return True

    def require(self, workspace_id: str, connection_ids) -> tuple:
        """Check every connection (for example before issuing a broker grant)."""
        if type(connection_ids) not in (tuple, list) or not connection_ids:
            raise InvalidScope("Connection IDs must be a non-empty tuple or list.")
        return tuple(self.check(workspace_id, connection_id) for connection_id in connection_ids)

    def _reason(self, workspace_id: str, connection_id: str) -> str | None:
        try:
            self.check(workspace_id, connection_id)
        except ConnectionNotInScope as denial:
            return denial.reason
        return None

    def violations(self, workspace_id: str, profile_store=None) -> tuple:
        """Stored profiles of the workspace that refer to connections outside its scope."""
        check_scope_identifier(workspace_id, "workspace_id")
        found = []
        after = None
        while True:
            page = self.settings.list(AgentProfile, workspace_id=workspace_id, limit=_PAGE, after_key=after)
            for entry in page:
                reason = self._reason(workspace_id, entry.value.connection_id)
                if reason is not None:
                    found.append(ScopeViolation("agent_profile", entry.value.id, entry.value.connection_id, reason))
            if len(page) < _PAGE:
                break
            after = page[-1].value.id
        if profile_store is not None:
            after = None
            while True:
                page = profile_store.list(workspace_id, limit=_PAGE, after_id=after)
                for entry in page:
                    profile = entry.value
                    for connection_id in dict.fromkeys(
                            choice.connection_id for choice in (profile.primary, *profile.fallbacks)):
                        reason = self._reason(workspace_id, connection_id)
                        if reason is not None:
                            found.append(ScopeViolation("execution_profile", profile.id, connection_id, reason))
                if len(page) < _PAGE:
                    break
                after = page[-1].value.id
        return tuple(found)

    # ------------------------------------------------------ pending restores
    def pending_records(self, workspace_id: str | None = None) -> tuple:
        records = tuple(record for record, _ in self._pending())
        if workspace_id is None:
            return records
        check_scope_identifier(workspace_id, "workspace_id")
        return tuple(record for record in records if record.workspace_id == workspace_id)

    def _pending(self, kind: str | None = None, record_id: str | None = None) -> list:
        conditions, parameters = [], []
        if kind is not None:
            conditions.append('"kind" = ?')
            parameters.append(kind)
        if record_id is not None:
            conditions.append('"record_id" = ?')
            parameters.append(record_id)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        with self._transaction() as db:
            rows = db.execute('SELECT "kind", "record_id", "workspace_id", "document" FROM "pending_restores"'
                              + where + ' ORDER BY "kind", "record_id"', parameters).fetchall()
        return [_decode_pending(row) for row in rows]

    def _add_pending(self, kind: str, record_id: str, workspace_id: str, requires: tuple, record: dict) -> None:
        PendingRecord(kind, record_id, workspace_id, tuple(requires))
        if kind not in PENDING_KINDS or type(record) is not dict:
            raise InvalidScope("Unsupported pending restore.")
        document = _json_text({"requires": list(requires), "record": record})
        try:
            with self._transaction() as db:
                db.execute('INSERT INTO "pending_restores" ("kind", "record_id", "workspace_id", "document") '
                           'VALUES (?, ?, ?, ?)', (kind, record_id, workspace_id, document))
        except StorageError as error:
            if isinstance(error.__context__, sqlite3.IntegrityError):
                raise RevisionConflict("The pending restore already exists.") from None
            raise

    def _remove_pending(self, kind: str, record_id: str) -> None:
        with self._transaction() as db:
            db.execute('DELETE FROM "pending_restores" WHERE "kind" = ? AND "record_id" = ?', (kind, record_id))

    def _discard_connection(self, connection_id: str) -> tuple:
        removed = []
        with self._transaction() as db:
            rows = db.execute('SELECT "kind", "record_id", "workspace_id", "document" FROM "pending_restores"'
                              ).fetchall()
            for row in rows:
                record, _ = _decode_pending(row)
                if (record.kind == "connection" and record.record_id == connection_id) \
                        or connection_id in record.requires:
                    db.execute('DELETE FROM "pending_restores" WHERE "kind" = ? AND "record_id" = ?',
                               (record.kind, record.record_id))
                    removed.append(record.record_id)
        return tuple(removed)
