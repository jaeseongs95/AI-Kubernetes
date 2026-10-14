"""ConnectionProfile CRUD over aik_config.SettingsStore plus a details file.

The lock order is always the details lock first, then the settings lock.
Publication order is chosen so that every reachable partial state stays
non-executable (fail-closed):

- ``register`` and updates that keep or restore ``needs_authentication``
  (``rebind``) commit the settings first and the details second.
- Updates that clear ``needs_authentication`` (``mark_verified``) and deletes
  commit the details first and the settings second.

``verify_auth_context`` compares the whole settings ConnectionProfile with the
profile derived from the details. Any mismatch, including the authentication
state, raises ``InconsistentConnectionStore``. The store does not repair this
automatically.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import fields, replace
from functools import lru_cache
import math
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import tempfile

from aik_config import AgentProfile, ConnectionProfile, Revisioned, SettingsStore
from aik_config.errors import InvalidSetting
from aik_config.models import check_identifier
from aik_runtime.errors import (
    InvalidDatabase, InvalidRecord, RevisionConflict, StorageBusy, StorageError, UnsafeStoragePath,
)
from aik_runtime.models import MAX_INTEGER
from aik_runtime.paths import database_lock, validate_database_path

from .errors import (
    AuthContextMismatch, AuthenticationRequired, ConnectionInUse, CredentialRefUnavailable,
    DuplicateConnection, InconsistentConnectionStore, ReferenceCheckUnavailable, UnknownConnection,
)
from .models import (
    AuthContext, Connection, ConnectionProvenance, ConnectionReference, ConnectionSpec, DeletionImpact,
    label_key, normalize_timestamp, provenance_of, rebuild, utc_now,
)
from aik_platform.fs import sync_file as _sync_file, sync_directory as _sync_directory


CONNECTIONS_APPLICATION_ID = 0x41494B4E  # AIKN; distinct from AIKR runtime and AIKC settings.
_SCHEMA_VERSION = 1
_TABLE = "connection_details"
_FIELDS = tuple(field.name for field in fields(Connection))
_IMMUTABLE = ("connection_id", "workspace_id", "provider", "auth_kind", "execution_kind",
              "credential_ref", "provenance_id")
_PAGE = 1000
# SQLite and process-lock companions of a database file. The connection file
# must not occupy (or be occupied by) a companion name of the settings file.
_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm", ".storage.lock", ".maintenance.lock")


def _text(name: str, *, optional: bool = False) -> str:
    if optional:
        return f'"{name}" TEXT CHECK ("{name}" IS NULL OR typeof("{name}") = \'text\')'
    return f'"{name}" TEXT NOT NULL CHECK (typeof("{name}") = \'text\' AND length("{name}") > 0)'


_DDL = f'CREATE TABLE "{_TABLE}" (\n    ' + ",\n    ".join([
    '"connection_id" TEXT PRIMARY KEY NOT NULL CHECK (typeof("connection_id") = \'text\' '
    'AND length("connection_id") BETWEEN 1 AND 128)',
    _text("workspace_id"), _text("provider"), _text("account_label"), _text("auth_kind"),
    _text("execution_kind"), _text("official_host"), _text("host_profile"),
    _text("account_binding", optional=True), _text("credential_ref", optional=True),
    _text("verified_at", optional=True),
    '"binding_revision" INTEGER NOT NULL CHECK (typeof("binding_revision") = \'integer\' AND "binding_revision" >= 1)',
    '"provenance_id" TEXT NOT NULL UNIQUE CHECK (typeof("provenance_id") = \'text\' AND length("provenance_id") > 0)',
    '"revision" INTEGER NOT NULL CHECK (typeof("revision") = \'integer\' AND "revision" >= 1)',
]) + "\n)"
_COLUMNS = ", ".join(f'"{name}"' for name in (*_FIELDS, "revision"))


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(_DDL)
        connection.execute(f"PRAGMA application_id = {CONNECTIONS_APPLICATION_ID}")
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
        raise StorageBusy("SQLite could not acquire the connection database lock.") from None
    raise StorageError("The connection database operation could not be completed.") from None


def _shares_files(first: Path, second: Path) -> bool:
    """True when two database paths are equal or one is the other's companion."""
    if os.path.normcase(str(first.parent)) != os.path.normcase(str(second.parent)):
        return False
    a, b = first.name.casefold(), second.name.casefold()
    return a == b or any(a == b + suffix or b == a + suffix for suffix in _SIDECAR_SUFFIXES)


def _decode(row) -> Revisioned:
    try:
        values = dict(zip(_FIELDS, row[:-1]))
        revision = row[-1]
        if type(revision) is not int or not 1 <= revision <= MAX_INTEGER:
            raise InvalidSetting("Invalid stored revision.")
        return Revisioned(Connection(**values), revision)
    except (InvalidSetting, InvalidRecord, TypeError, ValueError, KeyError, IndexError):
        raise InvalidDatabase("A stored connection record violates its contract.") from None


def _revision(value: object) -> None:
    if type(value) is not int or not 1 <= value <= MAX_INTEGER:
        raise InvalidSetting("An expected revision must be a bounded positive integer.")


def _settings_profile(connection: Connection) -> ConnectionProfile:
    return ConnectionProfile(
        connection.connection_id, connection.workspace_id, connection.provider,
        auth_mode=connection.auth_kind,
        needs_authentication=connection.auth_kind != "none" and connection.verified_at is None,
    )


def _same_reference(profile: ConnectionProfile, connection: Connection) -> bool:
    return (profile.workspace_id, profile.provider, profile.auth_mode) == (
        connection.workspace_id, connection.provider, connection.auth_kind)


def _match(connection: Connection, context: AuthContext) -> None:
    registered = (connection.connection_id, connection.provider, connection.auth_kind, connection.execution_kind,
                  connection.official_host, connection.host_profile, connection.account_binding,
                  connection.binding_revision)
    observed = (context.connection_id, context.provider, context.auth_kind, context.execution_kind,
                context.official_host, context.host_profile, context.account_binding, context.binding_revision)
    if registered != observed:
        raise AuthContextMismatch("The authentication context does not match the registered connection binding.")


def _check_duplicates(rows, candidate: Connection, exclude: str | None) -> None:
    for entry in rows:
        other = entry.value
        if other.connection_id == exclude:
            continue
        if other.connection_id == candidate.connection_id or other.provenance_id == candidate.provenance_id:
            raise DuplicateConnection("The connection ID is already registered.")
        if other.workspace_id == candidate.workspace_id and \
                label_key(other.account_label) == label_key(candidate.account_label):
            raise DuplicateConnection("The account label is already used in this workspace.")
        if candidate.account_binding is not None and (
            other.workspace_id, other.provider, other.execution_kind, other.official_host, other.account_binding,
        ) == (
            candidate.workspace_id, candidate.provider, candidate.execution_kind, candidate.official_host,
            candidate.account_binding,
        ):
            raise DuplicateConnection("The account and execution path are already registered.")
        if candidate.credential_ref is not None and other.credential_ref == candidate.credential_ref:
            raise DuplicateConnection("The credential reference is already bound to another connection.")


class ConnectionStore:
    """CRUD for connection metadata. It never stores secrets or contacts providers."""

    # Private hook: the SQLite connection class used for the details file.
    _connection_factory = sqlite3.Connection

    def __init__(self, settings: SettingsStore, path: Path | str | None = None, *,
                 credential_checker=None, reference_sources=(), timeout: float = 5.0):
        if not isinstance(settings, SettingsStore):
            raise InvalidSetting("A settings store is required.")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) \
                or timeout < 0:
            raise InvalidSetting("Storage timeout must be a finite nonnegative number.")
        self.timeout = float(timeout)
        self._settings = settings
        self._checker = credential_checker
        self._sources = tuple(reference_sources)
        requested = settings.path.with_name("connections.sqlite3") if path is None else path
        self.path = validate_database_path(requested, create_parent=True)
        settings_path = Path(settings.path)
        if self.path == settings_path or _shares_files(self.path, settings_path) \
                or _shares_files(self.path, settings_path.resolve(strict=False)):
            raise UnsafeStoragePath(
                "Connection details require a file separate from the settings database and its companions."
            )
        with database_lock(self.path, self.timeout):
            self._initialize()

    # ------------------------------------------------------------ file layer
    def _connect(self, path: Path, *, create: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(
            path.as_uri() + ("?mode=rwc" if create else "?mode=rw"),
            uri=True, timeout=self.timeout, isolation_level=None, factory=self._connection_factory,
        )
        try:
            connection.execute("PRAGMA trusted_schema = OFF")
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
                raise InvalidDatabase("Connection storage requires DELETE journal mode.")
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
                raise InvalidDatabase("Connection storage requires a DELETE-mode database without WAL companions.")
        journal = self.path.with_name(self.path.name + "-journal")
        validate_database_path(journal)
        if not self.path.exists():
            if journal.exists():
                raise InvalidDatabase("An orphan rollback journal cannot initialize connection storage.")
            return False
        with self.path.open("rb") as stream:
            header = stream.read(100)
        if len(header) < 100 or header[:16] != b"SQLite format 3\x00" \
                or int.from_bytes(header[68:72], "big") != CONNECTIONS_APPLICATION_ID:
            raise InvalidDatabase("This file is not an AIK connection database.")
        return True

    def _validate(self, connection: sqlite3.Connection, *, full: bool) -> None:
        try:
            app_id = connection.execute("PRAGMA application_id").fetchone()[0]
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if app_id != CONNECTIONS_APPLICATION_ID or version != _SCHEMA_VERSION:
                raise InvalidDatabase("This is not a supported AIK connection database.")
            if _schema_rows(connection) != _expected_rows():
                raise InvalidDatabase("The connection schema differs from its declared version.")
            if full:
                if tuple(row[0] for row in connection.execute("PRAGMA integrity_check")) != ("ok",):
                    raise InvalidDatabase("Connection database integrity validation failed.")
                rows = connection.execute(f'SELECT {_COLUMNS} FROM "{_TABLE}"').fetchall()
        except sqlite3.Error:
            raise InvalidDatabase("Connection database validation could not be completed.") from None
        if full:
            for row in rows:
                _decode(row)

    def _initialize(self) -> None:
        try:
            if self._check_files():
                with closing(self._connect(self.path)) as connection:
                    self._validate(connection, full=True)
                return
            temporary = Path(tempfile.mkdtemp(prefix=".aik-connections-init-", dir=self.path.parent))
            try:
                created = temporary / "connections.sqlite3"
                with closing(self._connect(created, create=True)) as connection:
                    _create_schema(connection)
                    self._validate(connection, full=True)
                os.chmod(created, 0o600)
                _sync_file(created)
                if self._check_files():
                    raise StorageError("The connection destination changed during initialization.")
                os.replace(created, self.path)
                _sync_directory(self.path.parent)
            finally:
                shutil.rmtree(temporary, ignore_errors=True)
        except StorageError:
            raise
        except sqlite3.Error as error:
            _sqlite_failure(error)
        except OSError:
            raise StorageError("The connection database files could not be processed safely.") from None

    @contextmanager
    def _details(self):
        with database_lock(self.path, self.timeout):
            try:
                exists = self._check_files()
            except OSError:
                raise StorageError("The connection database files could not be inspected.") from None
            if not exists:
                raise InvalidDatabase("The connection database is missing; it is not recreated implicitly.")
            connection = None
            try:
                connection = self._connect(self.path)
                connection.execute("BEGIN IMMEDIATE")
                self._validate(connection, full=False)
                yield connection
                if connection.in_transaction:
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
    def _fetch(db: sqlite3.Connection, connection_id: str) -> Revisioned | None:
        row = db.execute(f'SELECT {_COLUMNS} FROM "{_TABLE}" WHERE "connection_id" = ?', (connection_id,)).fetchone()
        return None if row is None else _decode(row)

    @staticmethod
    def _all(db: sqlite3.Connection, workspace_id: str | None = None) -> tuple:
        if workspace_id is None:
            rows = db.execute(f'SELECT {_COLUMNS} FROM "{_TABLE}" ORDER BY "connection_id"')
        else:
            rows = db.execute(f'SELECT {_COLUMNS} FROM "{_TABLE}" WHERE "workspace_id" = ? ORDER BY "connection_id"',
                              (workspace_id,))
        return tuple(_decode(row) for row in rows)

    @staticmethod
    def _commit_published(db: sqlite3.Connection) -> None:
        try:
            db.commit()
        except sqlite3.Error:
            raise InconsistentConnectionStore(
                "The settings reference may have been published but connection details did not commit; "
                "the connection is not executable until repaired."
            ) from None

    # --------------------------------------------------------------- helpers
    def _check_credential(self, credential_ref: str) -> None:
        if self._checker is None:
            raise CredentialRefUnavailable("No vault adapter is available to confirm the credential reference.")
        try:
            self._checker.check(credential_ref)
        except CredentialRefUnavailable:
            raise
        except Exception:
            raise CredentialRefUnavailable("The vault credential could not be confirmed.") from None

    def _settings_reference(self, connection: Connection):
        entry = self._settings.get(ConnectionProfile, connection.connection_id)
        if entry is None or not _same_reference(entry.value, connection):
            raise InvalidDatabase("The settings connection reference is missing or differs from its details.")
        if entry.value != _settings_profile(connection):
            # For example a partially published rebind or mark_verified: the
            # authentication state in settings and details disagree.
            raise InconsistentConnectionStore(
                "The settings authentication state differs from the connection details; "
                "the connection is not executable until repaired."
            )
        return entry

    def _external_references(self, connection_id: str) -> tuple:
        found = []
        for source in self._sources:
            try:
                references = tuple(source.references_to(connection_id))
            except Exception:
                raise ReferenceCheckUnavailable("A reference source could not report its references.") from None
            for reference in references:
                if type(reference) is not ConnectionReference:
                    raise ReferenceCheckUnavailable("A reference source returned an unsupported record.")
                # External records are never removed by this package.
                found.append(ConnectionReference(reference.kind, reference.id, reference.workspace_id, False))
        return tuple(found)

    @staticmethod
    def _profile_references(stx, connection: Connection) -> tuple:
        profiles, after = [], None
        while True:
            page = stx.list(AgentProfile, workspace_id=connection.workspace_id, limit=_PAGE, after_key=after)
            profiles.extend(entry for entry in page if entry.value.connection_id == connection.connection_id)
            if len(page) < _PAGE:
                break
            after = page[-1].value.id
        return tuple(profiles)

    @staticmethod
    def _impact(connection_id: str, profiles: tuple, external: tuple, external_checked: bool) -> DeletionImpact:
        references = [ConnectionReference("agent_profile", entry.value.id, entry.value.workspace_id, True)
                      for entry in profiles]
        references.extend(external)
        references.sort(key=lambda item: (item.kind, item.id))
        return DeletionImpact(connection_id, tuple(references), external_checked)

    def _publish_update(self, db: sqlite3.Connection, connection_id: str, updated: Connection) -> None:
        """Publish an updated binding to settings and details in a fail-closed order."""
        desired = _settings_profile(updated)
        details_committed = False
        relaxing = False
        try:
            with self._settings.transaction() as stx:
                entry = stx.get(ConnectionProfile, connection_id)
                if entry is None or not _same_reference(entry.value, updated):
                    raise InvalidDatabase("The settings connection reference is missing or differs from its details.")
                relaxing = bool(entry.value.needs_authentication) and not desired.needs_authentication
                if entry.value != desired:
                    stx.save(desired, expected_revision=entry.revision)
                if relaxing:
                    # Clearing needs_authentication: details first, so a later
                    # settings failure still reports "needs authentication".
                    try:
                        db.commit()
                    except sqlite3.Error:
                        raise StorageError(
                            "The connection details did not commit; nothing was published."
                        ) from None
                    details_committed = True
        except Exception:
            if details_committed:
                raise InconsistentConnectionStore(
                    "Connection details were committed but the settings update did not commit; "
                    "the connection is not executable until repaired."
                ) from None
            raise
        if not relaxing:
            # Keeping or setting needs_authentication: settings first, so a
            # later details failure cannot leave settings looking authenticated.
            self._commit_published(db)

    def _update(self, connection_id: str, expected_revision: int, mutate, *, sync_settings: bool) -> Revisioned:
        check_identifier(connection_id, "connection_id")
        _revision(expected_revision)
        with self._details() as db:
            current = self._fetch(db, connection_id)
            if current is None:
                raise UnknownConnection("The connection is not registered.")
            if current.revision != expected_revision:
                raise RevisionConflict("The connection revision has changed.")
            if expected_revision == MAX_INTEGER:
                raise RevisionConflict("The connection revision cannot be incremented.")
            updated = mutate(current.value)
            if any(getattr(updated, name) != getattr(current.value, name) for name in _IMMUTABLE):
                raise InvalidSetting("Connection identity, provider and execution path are immutable.")
            _check_duplicates(self._all(db), updated, exclude=connection_id)
            revision = expected_revision + 1
            others = [name for name in _FIELDS if name != "connection_id"]
            result = db.execute(
                f'UPDATE "{_TABLE}" SET ' + ", ".join(f'"{name}" = ?' for name in others)
                + ', "revision" = ? WHERE "connection_id" = ? AND "revision" = ?',
                [*(getattr(updated, name) for name in others), revision, connection_id, expected_revision],
            )
            if result.rowcount != 1:
                raise RevisionConflict("The connection revision has changed.")
            if sync_settings:
                self._publish_update(db, connection_id, updated)
        return Revisioned(updated, revision)

    # ------------------------------------------------------------ public API
    def register(self, spec: ConnectionSpec) -> Revisioned:
        """Register a new connection. Revision 1, binding revision 1, unverified."""
        spec = rebuild(spec, ConnectionSpec)
        if spec.credential_ref is not None:
            self._check_credential(spec.credential_ref)
        values = {field.name: getattr(spec, field.name) for field in fields(ConnectionSpec)}
        connection = Connection(**values, verified_at=None, binding_revision=1,
                                provenance_id="prov-" + secrets.token_hex(16))
        with self._details() as db:
            _check_duplicates(self._all(db), connection, exclude=None)
            placeholders = ", ".join("?" for _ in range(len(_FIELDS) + 1))
            try:
                db.execute(f'INSERT INTO "{_TABLE}" ({_COLUMNS}) VALUES ({placeholders})',
                           [*(getattr(connection, name) for name in _FIELDS), 1])
            except sqlite3.IntegrityError:
                raise DuplicateConnection("The connection conflicts with a registered record.") from None
            try:
                with self._settings.transaction() as stx:
                    stx.save(_settings_profile(connection), expected_revision=0)
            except RevisionConflict:
                raise DuplicateConnection("A settings connection with this ID already exists.") from None
            self._commit_published(db)
        return Revisioned(connection, 1)

    def get(self, connection_id: str) -> Revisioned | None:
        check_identifier(connection_id, "connection_id")
        with self._details() as db:
            return self._fetch(db, connection_id)

    def list(self, workspace_id: str | None = None) -> tuple:
        if workspace_id is not None:
            check_identifier(workspace_id, "workspace_id")
        with self._details() as db:
            return self._all(db, workspace_id)

    def rename(self, connection_id: str, account_label: str, expected_revision: int) -> Revisioned:
        """Change only the display label; IDs and provenance are unchanged."""
        return self._update(connection_id, expected_revision,
                            lambda current: replace(current, account_label=account_label), sync_settings=False)

    def mark_verified(self, connection_id: str, observed: AuthContext, expected_revision: int,
                      verified_at: str | None = None) -> Revisioned:
        """Record a confirmation time after the observed context matched the binding."""
        observed = rebuild(observed, AuthContext)
        if observed.connection_id != connection_id:
            raise AuthContextMismatch("The authentication context belongs to another connection.")
        stamp = utc_now() if verified_at is None else normalize_timestamp(verified_at)

        def mutate(current: Connection) -> Connection:
            if current.auth_kind == "none":
                raise InvalidSetting("A connection without authentication cannot be verified.")
            _match(current, observed)
            if current.credential_ref is not None:
                self._check_credential(current.credential_ref)
            return replace(current, verified_at=stamp)

        return self._update(connection_id, expected_revision, mutate, sync_settings=True)

    def rebind(self, connection_id: str, expected_revision: int, *, official_host: str | None = None,
               host_profile: str | None = None, account_binding: str | None = None) -> Revisioned:
        """Change host/profile/account binding: binding revision +1, verification cleared."""

        def mutate(current: Connection) -> Connection:
            changes = {name: value for name, value in (
                ("official_host", official_host), ("host_profile", host_profile), ("account_binding", account_binding),
            ) if value is not None and value != getattr(current, name)}
            if not changes:
                raise InvalidSetting("A rebind must change the official host, host profile or account binding.")
            return replace(current, **changes, verified_at=None, binding_revision=current.binding_revision + 1)

        return self._update(connection_id, expected_revision, mutate, sync_settings=True)

    def provenance(self, connection_id: str) -> ConnectionProvenance:
        entry = self.get(connection_id)
        if entry is None:
            raise UnknownConnection("The connection is not registered.")
        return provenance_of(entry.value)

    def verify_auth_context(self, context: AuthContext) -> ConnectionProvenance:
        """Fail-closed check that the execution context matches the registered account."""
        context = rebuild(context, AuthContext)
        with self._details() as db:
            current = self._fetch(db, context.connection_id)
            if current is None:
                raise UnknownConnection("The connection is not registered.")
            connection = current.value
            self._settings_reference(connection)
        _match(connection, context)
        if connection.auth_kind != "none" and connection.verified_at is None:
            raise AuthenticationRequired("The connection binding has not been verified for its current revision.")
        if connection.credential_ref is not None:
            self._check_credential(connection.credential_ref)
        return provenance_of(connection)

    def impact(self, connection_id: str) -> DeletionImpact:
        check_identifier(connection_id, "connection_id")
        with self._details() as db:
            current = self._fetch(db, connection_id)
            if current is None:
                raise UnknownConnection("The connection is not registered.")
            external = self._external_references(connection_id)
            with self._settings.transaction() as stx:
                profiles = self._profile_references(stx, current.value)
        return self._impact(connection_id, profiles, external, bool(self._sources))

    def delete(self, connection_id: str, expected_revision: int, *, force: bool = False,
               acknowledged_impact: DeletionImpact | None = None) -> DeletionImpact:
        """Delete an unused connection; force removes only acknowledged settings profiles."""
        check_identifier(connection_id, "connection_id")
        _revision(expected_revision)
        if type(force) is not bool:
            raise InvalidSetting("force must be a boolean.")
        with self._details() as db:
            current = self._fetch(db, connection_id)
            if current is None:
                raise UnknownConnection("The connection is not registered.")
            if current.revision != expected_revision:
                raise RevisionConflict("The connection revision has changed.")
            external = self._external_references(connection_id)
            details_committed = False
            try:
                with self._settings.transaction() as stx:
                    profiles = self._profile_references(stx, current.value)
                    impact = self._impact(connection_id, profiles, external, bool(self._sources))
                    if impact.in_use:
                        if not force or impact.blocking or acknowledged_impact != impact:
                            raise ConnectionInUse(impact)
                        for entry in profiles:
                            stx.delete(AgentProfile, entry.value.id, entry.revision)
                    reference = stx.get(ConnectionProfile, connection_id)
                    if reference is not None:
                        if not _same_reference(reference.value, current.value):
                            raise InvalidDatabase("The settings connection reference differs from its details.")
                        stx.delete(ConnectionProfile, connection_id, reference.revision)
                    deleted = db.execute(f'DELETE FROM "{_TABLE}" WHERE "connection_id" = ? AND "revision" = ?',
                                         (connection_id, expected_revision))
                    if deleted.rowcount != 1:
                        raise RevisionConflict("The connection revision has changed.")
                    db.commit()  # Details first: a later settings failure leaves a non-executable reference.
                    details_committed = True
            except Exception:
                if details_committed:
                    raise InconsistentConnectionStore(
                        "Connection details were deleted but the settings deletion did not commit."
                    ) from None
                raise
        return impact
