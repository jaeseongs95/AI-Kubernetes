"""Versioned schema for AIK-owned metadata, separate from every owner ledger.

Version 2 adds database-enforced pool consistency to the version 1 records.
Migration functions run inside one transaction on a private validated copy;
they never commit independently or execute caller-supplied SQL.
"""

from __future__ import annotations

from contextlib import closing
from functools import lru_cache
import sqlite3

from .errors import InvalidDatabase, MigrationError


SQLITE_APPLICATION_ID = 0x41494B52  # AIKR; not the planning, VM, or AGS database.
SCHEMA_VERSION = 2
SUPPORTED_VERSIONS = (1, 2)
TABLE_COLUMNS = {
    "credential_refs": ("id", "reference"),
    "pools": ("id", "provider", "connection_id", "credential_ref_id", "max_parallel", "enabled"),
    "execution_profiles": ("id", "pool_id", "role", "model", "effort"),
    "placements": (
        "id", "pool_id", "profile_id", "vm_instance_id", "vm_project_id", "vm_task_id",
        "vm_attempt_id", "ags_session_id", "state",
    ),
    "leases": (
        "id", "placement_id", "pool_id", "worker_id", "fencing_token", "expires_at", "state", "provider_job_id",
    ),
}

_REVISION = '"revision" INTEGER NOT NULL CHECK (typeof("revision") = \'integer\' AND "revision" >= 1)'
_ID = '"id" TEXT PRIMARY KEY NOT NULL CHECK (typeof("id") = \'text\' AND length("id") BETWEEN 1 AND 128)'


def _text(name: str, *, optional: bool = False) -> str:
    if optional:
        return f'"{name}" TEXT CHECK ("{name}" IS NULL OR typeof("{name}") = \'text\')'
    return f'"{name}" TEXT NOT NULL CHECK (typeof("{name}") = \'text\' AND length("{name}") > 0)'


def _table_sql(version: int) -> dict[str, str]:
    definitions = {
        "credential_refs": [_ID, _text("reference"), _REVISION],
        "pools": [
            _ID, _text("provider"), _text("connection_id"), _text("credential_ref_id", optional=True),
            '"max_parallel" INTEGER NOT NULL CHECK (typeof("max_parallel") = \'integer\' AND "max_parallel" >= 1)',
            '"enabled" INTEGER NOT NULL CHECK (typeof("enabled") = \'integer\' AND "enabled" IN (0, 1))',
            _REVISION,
            'FOREIGN KEY ("credential_ref_id") REFERENCES "credential_refs" ("id") ON DELETE RESTRICT ON UPDATE RESTRICT',
        ],
        "execution_profiles": [
            _ID, _text("pool_id"), _text("role"), _text("model"), _text("effort", optional=True), _REVISION,
            'FOREIGN KEY ("pool_id") REFERENCES "pools" ("id") ON DELETE RESTRICT ON UPDATE RESTRICT',
        ],
        "placements": [
            _ID, _text("pool_id"), _text("profile_id"), _text("vm_instance_id"), _text("vm_project_id"),
            _text("vm_task_id"), _text("vm_attempt_id"), _text("ags_session_id", optional=True), _text("state"), _REVISION,
            'FOREIGN KEY ("pool_id") REFERENCES "pools" ("id") ON DELETE RESTRICT ON UPDATE RESTRICT',
            'FOREIGN KEY ("profile_id") REFERENCES "execution_profiles" ("id") ON DELETE RESTRICT ON UPDATE RESTRICT',
        ],
        "leases": [
            _ID, _text("placement_id"), _text("pool_id"), _text("worker_id"),
            '"fencing_token" INTEGER NOT NULL CHECK (typeof("fencing_token") = \'integer\' AND "fencing_token" >= 1)',
            _text("expires_at"), _text("state"), _text("provider_job_id", optional=True), _REVISION,
            'FOREIGN KEY ("placement_id") REFERENCES "placements" ("id") ON DELETE RESTRICT ON UPDATE RESTRICT',
            'FOREIGN KEY ("pool_id") REFERENCES "pools" ("id") ON DELETE RESTRICT ON UPDATE RESTRICT',
        ],
    }
    if version >= 2:
        definitions["placements"].extend([
            'UNIQUE ("id", "pool_id")',
            'FOREIGN KEY ("profile_id", "pool_id") REFERENCES "execution_profiles" ("id", "pool_id") ON DELETE RESTRICT ON UPDATE RESTRICT',
        ])
        definitions["leases"].append(
            'FOREIGN KEY ("placement_id", "pool_id") REFERENCES "placements" ("id", "pool_id") ON DELETE RESTRICT ON UPDATE RESTRICT'
        )
    return {name: f'CREATE TABLE "{name}" (\n    ' + ",\n    ".join(parts) + "\n)" for name, parts in definitions.items()}


_PROFILE_POOL_INDEX = 'CREATE UNIQUE INDEX "profiles_id_pool" ON "execution_profiles" ("id", "pool_id")'
_V2_INDEXES = (
    'CREATE INDEX "placements_vm_attempt" ON "placements" ("vm_instance_id", "vm_attempt_id")',
    'CREATE INDEX "leases_placement" ON "leases" ("placement_id")',
    'CREATE INDEX "leases_pool" ON "leases" ("pool_id")',
)


def create_schema(connection: sqlite3.Connection, version: int = SCHEMA_VERSION) -> None:
    """Initialize an empty connection; version 1 remains available for upgrades.

    The caller may provide an existing transaction. Otherwise this operation
    begins and commits its own transaction. No executescript implicit commits.
    """
    if type(version) is not int or version not in SUPPORTED_VERSIONS:
        raise InvalidDatabase("Unsupported runtime schema version.")
    if connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is not None:
        raise InvalidDatabase("Runtime initialization requires an empty database.")
    own_transaction = not connection.in_transaction
    if own_transaction:
        connection.execute("BEGIN IMMEDIATE")
    try:
        statements = _table_sql(version)
        for name in TABLE_COLUMNS:
            connection.execute(statements[name])
            if name == "execution_profiles" and version >= 2:
                connection.execute(_PROFILE_POOL_INDEX)
        if version >= 2:
            for statement in _V2_INDEXES:
                connection.execute(statement)
        connection.execute(f"PRAGMA application_id = {SQLITE_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {version}")
        if own_transaction:
            connection.commit()
    except BaseException:
        if own_transaction:
            connection.rollback()
        raise


def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    # Temporary tables have no owner authority or persisted schema. Dropping
    # children first keeps foreign_keys enabled for the entire migration.
    connection.execute('CREATE TEMP TABLE "_aik_saved_placements" AS SELECT * FROM "placements"')
    connection.execute('CREATE TEMP TABLE "_aik_saved_leases" AS SELECT * FROM "leases"')
    connection.execute('DROP TABLE "leases"')
    connection.execute('DROP TABLE "placements"')
    connection.execute(_PROFILE_POOL_INDEX)
    statements = _table_sql(2)
    connection.execute(statements["placements"])
    connection.execute(statements["leases"])
    for name in ("placements", "leases"):
        columns = ", ".join(f'"{field}"' for field in (*TABLE_COLUMNS[name], "revision"))
        connection.execute(f'INSERT INTO "{name}" ({columns}) SELECT {columns} FROM "_aik_saved_{name}"')
        connection.execute(f'DROP TABLE "_aik_saved_{name}"')
    for statement in _V2_INDEXES:
        connection.execute(statement)


MIGRATIONS = {1: _migrate_v1_to_v2}


def migrate(connection: sqlite3.Connection, version: int) -> None:
    """Upgrade a private validated copy atomically, or preserve its old state."""
    if version not in SUPPORTED_VERSIONS or connection.in_transaction:
        raise MigrationError("Migration requires a supported idle runtime database.")
    connection.execute("BEGIN IMMEDIATE")
    try:
        while version < SCHEMA_VERSION:
            MIGRATIONS[version](connection)
            version += 1
            connection.execute(f"PRAGMA user_version = {version}")
        validate_schema(connection, version)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise MigrationError("Runtime schema migration failed; the source was not replaced.") from None


def _schema_rows(connection: sqlite3.Connection) -> tuple:
    return tuple(tuple(row) for row in connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ))


@lru_cache(maxsize=2)
def _expected_schema(version: int) -> tuple:
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
        create_schema(connection, version)
        return _schema_rows(connection)


def validate_schema(connection: sqlite3.Connection, version: int | None = None) -> int:
    """Reject foreign identities, unexpected objects, damage and dangling FKs."""
    try:
        app_id = connection.execute("PRAGMA application_id").fetchone()[0]
        observed_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if app_id != SQLITE_APPLICATION_ID or observed_version not in SUPPORTED_VERSIONS:
            raise InvalidDatabase("This is not a supported AIK runtime database.")
        if version is not None and observed_version != version:
            raise InvalidDatabase("The runtime schema version changed unexpectedly.")
        if _schema_rows(connection) != _expected_schema(observed_version):
            raise InvalidDatabase("The runtime schema differs from its declared version.")
        if tuple(row[0] for row in connection.execute("PRAGMA integrity_check")) != ("ok",):
            raise InvalidDatabase("Runtime database integrity validation failed.")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise InvalidDatabase("Runtime database references are inconsistent.")
        return observed_version
    except sqlite3.Error:
        raise InvalidDatabase("Runtime database validation could not be completed.") from None
