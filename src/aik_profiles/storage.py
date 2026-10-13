"""Profile repository in its own SQLite file, beside the settings file.

aik_config.SettingsStore keeps AgentProfile/Workspace records unchanged; this
adapter only reads Workspace and ConnectionProfile from it for reference
checks. Every profile is one row with its own revision, so a director update
never touches worker or reviewer rows. Execution decisions are appended.
Path validation and the process lock are reused from aik_runtime.paths.

Schema version 2 adds a tombstone per deleted profile ID. A profile recreated
with the same ID continues after the deleted revision, so an approval issued
for an earlier incarnation can never match the new one. A version 1 file is
upgraded in place inside one SQLite transaction (DELETE journal, FULL sync).
"""

from __future__ import annotations

from contextlib import closing, contextmanager
import errno
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from aik_config import ConnectionProfile, Revisioned, SettingsStore, Workspace, default_settings_path
from aik_config.errors import CrossWorkspaceReference, InvalidReference
from aik_runtime.errors import InvalidDatabase, RevisionConflict, StorageBusy, StorageError
from aik_runtime.models import MAX_INTEGER
from aik_runtime.paths import database_lock, validate_database_path

from .errors import InvalidProfile
from .execution import ExecutionDecision, rejected_decision, validate_approval, verify_execution
from .models import ROLE_NAMES, check_profile_identifier, parse_json, profile_from_dict, profile_to_dict, rebuild_profile


APPLICATION_ID = 0x41494B50  # AIKP; distinct from runtime (AIKR) and settings (AIKC).
SCHEMA_VERSION = 2
SUPPORTED_VERSIONS = (1, 2)
_V1_STATEMENTS = (
    'CREATE TABLE "profiles" (\n'
    '    "id" TEXT PRIMARY KEY NOT NULL CHECK (typeof("id") = \'text\' AND length("id") BETWEEN 1 AND 128),\n'
    '    "workspace_id" TEXT NOT NULL CHECK (typeof("workspace_id") = \'text\' AND length("workspace_id") BETWEEN 1 AND 128),\n'
    '    "role" TEXT NOT NULL CHECK ("role" IN (\'director\', \'worker\', \'reviewer\')),\n'
    '    "document" TEXT NOT NULL CHECK (typeof("document") = \'text\'),\n'
    '    "revision" INTEGER NOT NULL CHECK (typeof("revision") = \'integer\' AND "revision" >= 1)\n'
    ')',
    'CREATE INDEX "profiles_workspace_role" ON "profiles" ("workspace_id", "role", "id")',
    'CREATE TABLE "execution_records" (\n'
    '    "seq" INTEGER PRIMARY KEY,\n'
    '    "profile_id" TEXT NOT NULL CHECK (typeof("profile_id") = \'text\'),\n'
    '    "profile_revision" INTEGER NOT NULL CHECK (typeof("profile_revision") = \'integer\' AND "profile_revision" >= 1),\n'
    '    "status" TEXT NOT NULL CHECK ("status" IN (\'bound\', \'rejected\')),\n'
    '    "decided_at" TEXT NOT NULL CHECK (typeof("decided_at") = \'text\'),\n'
    '    "document" TEXT NOT NULL CHECK (typeof("document") = \'text\')\n'
    ')',
    'CREATE INDEX "execution_records_profile" ON "execution_records" ("profile_id", "seq")',
)
_V2_STATEMENTS = (
    'CREATE TABLE "profile_tombstones" (\n'
    '    "id" TEXT PRIMARY KEY NOT NULL CHECK (typeof("id") = \'text\' AND length("id") BETWEEN 1 AND 128),\n'
    '    "revision" INTEGER NOT NULL CHECK (typeof("revision") = \'integer\' AND "revision" >= 1)\n'
    ')',
)
_SELECT_PROFILES = 'SELECT "id", "workspace_id", "role", "document", "revision" FROM "profiles"'
_SQLITE_SUFFIXES = ("-journal", "-wal", "-shm", ".storage.lock")


def _statements(version: int) -> tuple:
    return _V1_STATEMENTS + (_V2_STATEMENTS if version >= 2 else ())


_STATEMENTS = _statements(SCHEMA_VERSION)


def default_profiles_path() -> Path:
    """Next to settings.sqlite3, as its own file."""
    return default_settings_path().with_name("profiles.sqlite3")


def _create_schema(connection: sqlite3.Connection, version: int = SCHEMA_VERSION) -> None:
    if type(version) is not int or version not in SUPPORTED_VERSIONS:
        raise InvalidDatabase("Unsupported profile schema version.")
    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in _statements(version):
            connection.execute(statement)
        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {version}")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _schema_rows(connection: sqlite3.Connection) -> tuple:
    return tuple(tuple(row) for row in connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ))


@lru_cache(maxsize=2)
def _expected_schema(version: int = SCHEMA_VERSION) -> tuple:
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
        _create_schema(connection, version)
        return _schema_rows(connection)


def _validate_schema(connection: sqlite3.Connection, *, full: bool, version: int = SCHEMA_VERSION) -> None:
    try:
        if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID \
                or connection.execute("PRAGMA user_version").fetchone()[0] != version \
                or version not in SUPPORTED_VERSIONS:
            raise InvalidDatabase("This is not a supported AIK profile database.")
        if _schema_rows(connection) != _expected_schema(version):
            raise InvalidDatabase("The profile schema differs from its declared version.")
        if full and tuple(row[0] for row in connection.execute("PRAGMA integrity_check")) != ("ok",):
            raise InvalidDatabase("Profile database integrity validation failed.")
    except sqlite3.Error:
        raise InvalidDatabase("Profile database validation could not be completed.") from None


def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    """Add the tombstone table and bump the version in one transaction."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        _validate_schema(connection, full=False, version=1)
        for statement in _V2_STATEMENTS:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 2")
        _validate_schema(connection, full=False, version=2)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _json_text(document: dict) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _decode_row(row) -> Revisioned:
    try:
        profile_id, workspace_id, role, document, revision = row
        profile = profile_from_dict(parse_json(document))
        if (profile.id, profile.workspace_id, profile.role) != (profile_id, workspace_id, role):
            raise ValueError
        if type(revision) is not int or not 1 <= revision <= MAX_INTEGER:
            raise ValueError
        return Revisioned(profile, revision)
    except (InvalidProfile, TypeError, ValueError):
        raise InvalidDatabase("A stored execution profile violates its contract.") from None


def _revision(value: object, *, allow_create: bool) -> None:
    minimum = 0 if allow_create else 1
    if type(value) is not int or not minimum <= value <= MAX_INTEGER:
        raise InvalidProfile("An expected revision must be a bounded integer.")


def _sqlite_failure(error: sqlite3.Error) -> None:
    if getattr(error, "sqlite_errorcode", 0) & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        raise StorageBusy("SQLite could not acquire the profile database lock.") from None
    raise StorageError("The profile database operation could not be completed.") from None


def _sync_file(path: Path) -> None:
    with path.open("r+b") as stream:
        stream.flush()
        os.fsync(stream.fileno())


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return  # No portable directory fsync on Windows; rename durability is OS-owned.
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        os.close(descriptor)


def _sqlite_names(path: Path) -> set:
    """A database path together with its SQLite companions and storage lock."""
    return {path, *(path.with_name(path.name + suffix) for suffix in _SQLITE_SUFFIXES)}


class ProfileStore:
    """Director/worker/reviewer execution profiles with independent revisions."""

    def __init__(self, path: Path | str | None = None, *, settings: SettingsStore, timeout: float = 5.0):
        if not isinstance(settings, SettingsStore):
            raise InvalidProfile("A SettingsStore is required for reference checks.")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) \
                or timeout < 0:
            raise InvalidProfile("Storage timeout must be a finite nonnegative number.")
        self.settings = settings
        self.timeout = float(timeout)
        self.path = validate_database_path(default_profiles_path() if path is None else path, create_parent=True)
        # Reject the settings file itself and every name SQLite or the storage
        # lock uses beside it (and vice versa), before any file is created.
        if _sqlite_names(self.path) & _sqlite_names(Path(settings.path)):
            raise InvalidProfile("Profile storage must use its own file, not the settings database or its companions.")
        with database_lock(self.path, self.timeout):
            self._initialize()

    # ---------------------------------------------------------------- files
    def _connect(self, path: Path, *, create: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(
            path.as_uri() + ("?mode=rwc" if create else "?mode=rw"),
            uri=True, timeout=self.timeout, isolation_level=None,
        )
        try:
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA foreign_keys = ON")
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
                raise InvalidDatabase("Profile storage requires DELETE journal mode.")
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
                raise InvalidDatabase("Profile storage requires a DELETE-mode database without WAL companions.")
        journal = self.path.with_name(self.path.name + "-journal")
        validate_database_path(journal)
        if not self.path.exists():
            if journal.exists():
                raise InvalidDatabase("An orphan rollback journal cannot initialize profile storage.")
            return False
        with self.path.open("rb") as stream:
            header = stream.read(100)
        if len(header) < 100 or header[:16] != b"SQLite format 3\x00" \
                or int.from_bytes(header[68:72], "big") != APPLICATION_ID:
            raise InvalidDatabase("This file is not an AIK profile database.")
        return True

    def _initialize(self) -> None:
        try:
            if self._check_files():
                with closing(self._connect(self.path)) as connection:
                    declared = connection.execute("PRAGMA user_version").fetchone()[0]
                    version = declared if declared in SUPPORTED_VERSIONS else SCHEMA_VERSION
                    _validate_schema(connection, full=True, version=version)
                    for row in connection.execute(_SELECT_PROFILES):
                        _decode_row(row)
                    if version < SCHEMA_VERSION:
                        _migrate_v1_to_v2(connection)
                        _validate_schema(connection, full=True)
                return
            temporary = Path(tempfile.mkdtemp(prefix=".aik-profiles-init-", dir=self.path.parent))
            try:
                created = temporary / "profiles.sqlite3"
                with closing(self._connect(created, create=True)) as connection:
                    _create_schema(connection)
                    _validate_schema(connection, full=True)
                os.chmod(created, 0o600)
                _sync_file(created)
                if self._check_files():
                    raise StorageError("The profile destination changed during initialization.")
                os.replace(created, self.path)
                _sync_directory(self.path.parent)
            finally:
                shutil.rmtree(temporary, ignore_errors=True)
        except StorageError:
            raise
        except sqlite3.Error as error:
            _sqlite_failure(error)
        except OSError:
            raise StorageError("The profile database files could not be processed safely.") from None

    @contextmanager
    def _transaction(self):
        with database_lock(self.path, self.timeout):
            try:
                exists = self._check_files()
            except OSError:
                raise StorageError("The profile database files could not be inspected.") from None
            if not exists:
                raise InvalidDatabase("The profile database is missing; it is not recreated implicitly.")
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

    # ----------------------------------------------------------- references
    def _check_references(self, profile) -> None:
        """Workspace and every choice's connection must exist in that workspace."""
        with self.settings.transaction() as tx:
            if tx.get(Workspace, profile.workspace_id) is None:
                raise InvalidReference("The referenced workspace does not exist.")
            for choice in (profile.primary, *profile.fallbacks):
                found = tx.get(ConnectionProfile, choice.connection_id)
                if found is None:
                    raise InvalidReference("The referenced connection does not exist.")
                if found.value.workspace_id != profile.workspace_id:
                    raise CrossWorkspaceReference("A profile cannot use another workspace's connection.")

    @staticmethod
    def _fetch(connection: sqlite3.Connection, profile_id: str) -> Revisioned | None:
        row = connection.execute(_SELECT_PROFILES + ' WHERE "id" = ?', (profile_id,)).fetchone()
        return None if row is None else _decode_row(row)

    @staticmethod
    def _creation_revision(connection: sqlite3.Connection, profile_id: str) -> int:
        """1 for a new ID; one past the deleted revision for a recreated ID."""
        tombstone = connection.execute(
            'SELECT "revision" FROM "profile_tombstones" WHERE "id" = ?', (profile_id,),
        ).fetchone()
        if tombstone is None:
            return 1
        previous = tombstone[0]
        if type(previous) is not int or not 1 <= previous <= MAX_INTEGER:
            raise InvalidDatabase("A stored profile tombstone violates its contract.")
        if previous == MAX_INTEGER:
            raise RevisionConflict("The execution profile revision cannot be incremented.")
        return previous + 1

    # ------------------------------------------------------------------ API
    def save(self, profile, expected_revision: int = 0) -> Revisioned:
        value = rebuild_profile(profile)
        _revision(expected_revision, allow_create=True)
        document = _json_text(profile_to_dict(value))
        with self._transaction() as connection:
            self._check_references(value)
            row = connection.execute(
                'SELECT "workspace_id", "role", "revision" FROM "profiles" WHERE "id" = ?', (value.id,),
            ).fetchone()
            if expected_revision == 0:
                if row is not None:
                    raise RevisionConflict("The execution profile already exists.")
                revision = self._creation_revision(connection, value.id)
                connection.execute(
                    'INSERT INTO "profiles" ("id", "workspace_id", "role", "document", "revision") '
                    'VALUES (?, ?, ?, ?, ?)',
                    (value.id, value.workspace_id, value.role, document, revision),
                )
            else:
                if row is None or row[2] != expected_revision:
                    raise RevisionConflict("The execution profile revision has changed or the profile is absent.")
                if (row[0], row[1]) != (value.workspace_id, value.role):
                    raise InvalidProfile("An execution profile cannot change its workspace or role.")
                if expected_revision == MAX_INTEGER:
                    raise RevisionConflict("The execution profile revision cannot be incremented.")
                revision = expected_revision + 1
                updated = connection.execute(
                    'UPDATE "profiles" SET "document" = ?, "revision" = ? WHERE "id" = ? AND "revision" = ?',
                    (document, revision, value.id, expected_revision),
                )
                if updated.rowcount != 1:
                    raise RevisionConflict("The execution profile revision has changed.")
        return Revisioned(value, revision)

    def get(self, profile_id: str) -> Revisioned | None:
        check_profile_identifier(profile_id)
        with self._transaction() as connection:
            return self._fetch(connection, profile_id)

    def list(self, workspace_id: str, role: str | None = None, limit: int = 100,
             after_id: str | None = None) -> tuple:
        check_profile_identifier(workspace_id, "workspace_id")
        if role is not None and (type(role) is not str or role not in ROLE_NAMES):
            raise InvalidProfile("Unknown value for field: role.")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise InvalidProfile("The query limit must be an integer between 1 and 1000.")
        conditions, parameters = ['"workspace_id" = ?'], [workspace_id]
        if role is not None:
            conditions.append('"role" = ?')
            parameters.append(role)
        if after_id is not None:
            check_profile_identifier(after_id, "after_id")
            conditions.append('"id" > ?')
            parameters.append(after_id)
        with self._transaction() as connection:
            rows = connection.execute(
                _SELECT_PROFILES + " WHERE " + " AND ".join(conditions) + ' ORDER BY "id" LIMIT ?',
                (*parameters, limit),
            )
            return tuple(_decode_row(row) for row in rows)

    def delete(self, profile_id: str, expected_revision: int) -> None:
        check_profile_identifier(profile_id)
        _revision(expected_revision, allow_create=False)
        with self._transaction() as connection:
            deleted = connection.execute(
                'DELETE FROM "profiles" WHERE "id" = ? AND "revision" = ?', (profile_id, expected_revision),
            )
            if deleted.rowcount != 1:
                raise RevisionConflict("The execution profile revision has changed or the profile is absent.")
            # Keep the last revision so a recreated ID cannot reuse old approvals.
            connection.execute(
                'INSERT OR REPLACE INTO "profile_tombstones" ("id", "revision") VALUES (?, ?)',
                (profile_id, expected_revision),
            )

    def prepare_execution(self, profile_id: str, inventory, *, approval=None, clock=None) -> ExecutionDecision:
        """Verify capability right before execution and append the decision record."""
        check_profile_identifier(profile_id)
        approval = validate_approval(approval)
        with self._transaction() as connection:
            entry = self._fetch(connection, profile_id)
            if entry is None:
                raise InvalidReference("The requested execution profile does not exist.")
            try:
                self._check_references(entry.value)
            except CrossWorkspaceReference:
                decision = rejected_decision(entry, ("CROSS_WORKSPACE_REFERENCE",), clock=clock)
            except InvalidReference:
                decision = rejected_decision(entry, ("REFERENCE_MISSING",), clock=clock)
            else:
                decision = verify_execution(entry, inventory, approval=approval, clock=clock)
            connection.execute(
                'INSERT INTO "execution_records" ("profile_id", "profile_revision", "status", "decided_at", "document") '
                'VALUES (?, ?, ?, ?, ?)',
                (decision.profile_id, decision.profile_revision, decision.status, decision.decided_at,
                 _json_text(decision.to_document())),
            )
        return decision

    def _record_rows(self, profile_id, limit, after_seq, before_seq, newest_first) -> tuple:
        if profile_id is not None:
            check_profile_identifier(profile_id)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise InvalidProfile("The query limit must be an integer between 1 and 1000.")
        for value, field in ((after_seq, "after_seq"), (before_seq, "before_seq")):
            if value is not None and (type(value) is not int or not 0 <= value <= MAX_INTEGER):
                raise InvalidProfile(f"Invalid record cursor field: {field}.")
        if type(newest_first) is not bool:
            raise InvalidProfile("newest_first must be a boolean.")
        conditions, parameters = [], []
        if profile_id is not None:
            conditions.append('"profile_id" = ?')
            parameters.append(profile_id)
        if after_seq is not None:
            conditions.append('"seq" > ?')
            parameters.append(after_seq)
        if before_seq is not None:
            conditions.append('"seq" < ?')
            parameters.append(before_seq)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        order = "DESC" if newest_first else "ASC"
        with self._transaction() as connection:
            rows = connection.execute(
                'SELECT "seq", "document" FROM "execution_records"' + where + f' ORDER BY "seq" {order} LIMIT ?',
                (*parameters, limit),
            ).fetchall()
        records = []
        for seq, document in rows:
            if type(seq) is not int:
                raise InvalidDatabase("A stored execution record sequence is malformed.")
            try:
                parsed = parse_json(document)
            except InvalidProfile:
                raise InvalidDatabase("A stored execution record is not valid JSON.") from None
            if type(parsed) is not dict:
                raise InvalidDatabase("A stored execution record is malformed.")
            records.append((seq, parsed))
        return tuple(records)

    def execution_records(self, profile_id: str | None = None, limit: int = 100, *,
                          after_seq: int | None = None, before_seq: int | None = None,
                          newest_first: bool = False) -> tuple:
        """Decision documents; newest_first=True returns the latest decisions first."""
        return tuple(document for _, document in
                     self._record_rows(profile_id, limit, after_seq, before_seq, newest_first))

    def execution_record_entries(self, profile_id: str | None = None, limit: int = 100, *,
                                 after_seq: int | None = None, before_seq: int | None = None,
                                 newest_first: bool = False) -> tuple:
        """(seq, document) pairs; seq is the cursor for after_seq/before_seq paging."""
        return self._record_rows(profile_id, limit, after_seq, before_seq, newest_first)
