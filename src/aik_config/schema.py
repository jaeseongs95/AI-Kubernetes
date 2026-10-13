"""Versioned settings schema, in a SQLite file separate from runtime storage."""

from __future__ import annotations

from contextlib import closing
from functools import lru_cache
import sqlite3

from aik_runtime.errors import InvalidDatabase

from .models import AUTH_MODES, BUDGET_PERIODS, BUDGET_UNITS, PERMISSION_DECISIONS, PROFILE_ROLES


APPLICATION_ID = 0x41494B43  # AIKC; runtime storage uses AIKR (0x41494B52).
SCHEMA_VERSION = 1

_REVISION = '"revision" INTEGER NOT NULL CHECK (typeof("revision") = \'integer\' AND "revision" >= 1)'
_WS_FK = 'FOREIGN KEY ("workspace_id") REFERENCES "workspaces" ("id") ON DELETE RESTRICT ON UPDATE RESTRICT'


def _key(name: str) -> str:
    return (f'"{name}" TEXT PRIMARY KEY NOT NULL CHECK (typeof("{name}") = \'text\' '
            f'AND length("{name}") BETWEEN 1 AND 128)')


def _text(name: str, *, optional: bool = False) -> str:
    if optional:
        return f'"{name}" TEXT CHECK ("{name}" IS NULL OR typeof("{name}") = \'text\')'
    return f'"{name}" TEXT NOT NULL CHECK (typeof("{name}") = \'text\' AND length("{name}") > 0)'


def _choice(name: str, choices: tuple) -> str:
    allowed = ", ".join(f"'{choice}'" for choice in choices)
    return f'"{name}" TEXT NOT NULL CHECK (typeof("{name}") = \'text\' AND "{name}" IN ({allowed}))'


def _bool(name: str) -> str:
    return f'"{name}" INTEGER NOT NULL CHECK (typeof("{name}") = \'integer\' AND "{name}" IN (0, 1))'


def _positive(name: str, *, optional: bool = False) -> str:
    if optional:
        return f'"{name}" INTEGER CHECK ("{name}" IS NULL OR (typeof("{name}") = \'integer\' AND "{name}" >= 1))'
    return f'"{name}" INTEGER NOT NULL CHECK (typeof("{name}") = \'integer\' AND "{name}" >= 1)'


_TABLES = {
    "workspaces": [
        _key("id"), _text("name"), _text("canonical_root", optional=True), _text("vm_project_id", optional=True),
        _REVISION,
        'CHECK (("canonical_root" IS NULL) = ("vm_project_id" IS NULL))',
    ],
    "connections": [
        _key("id"), _text("workspace_id"), _text("provider"), _choice("auth_mode", AUTH_MODES),
        _bool("needs_authentication"), _REVISION,
        'CHECK ("auth_mode" <> \'none\' OR "needs_authentication" = 0)',
        'UNIQUE ("id", "workspace_id")',
        _WS_FK,
    ],
    "agent_profiles": [
        _key("id"), _text("workspace_id"), _choice("role", PROFILE_ROLES), _text("connection_id"),
        _text("model"), _text("effort", optional=True), _REVISION,
        _WS_FK,
        'FOREIGN KEY ("connection_id", "workspace_id") REFERENCES "connections" ("id", "workspace_id") '
        'ON DELETE RESTRICT ON UPDATE RESTRICT',
    ],
    "workspace_limits": [
        _key("workspace_id"), _positive("max_parallel_workers"), _positive("budget_limit", optional=True),
        _choice("budget_unit", BUDGET_UNITS), _choice("budget_period", BUDGET_PERIODS), _REVISION,
        _WS_FK,
    ],
    "permission_defaults": [
        _key("workspace_id"), _choice("file_write", PERMISSION_DECISIONS),
        _choice("shell_command", PERMISSION_DECISIONS), _choice("network", PERMISSION_DECISIONS), _REVISION,
        _WS_FK,
    ],
    "active_workspace": [
        '"slot" TEXT PRIMARY KEY NOT NULL CHECK ("slot" = \'active\')', _text("workspace_id"), _REVISION,
        _WS_FK,
    ],
}
_INDEXES = (
    'CREATE INDEX "connections_workspace" ON "connections" ("workspace_id")',
    'CREATE INDEX "agent_profiles_workspace" ON "agent_profiles" ("workspace_id")',
    'CREATE INDEX "agent_profiles_connection" ON "agent_profiles" ("connection_id", "workspace_id")',
)


def create_schema(connection: sqlite3.Connection) -> None:
    """Initialize an empty database inside one transaction."""
    if connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is not None:
        raise InvalidDatabase("Settings initialization requires an empty database.")
    own_transaction = not connection.in_transaction
    if own_transaction:
        connection.execute("BEGIN IMMEDIATE")
    try:
        for name, parts in _TABLES.items():
            connection.execute(f'CREATE TABLE "{name}" (\n    ' + ",\n    ".join(parts) + "\n)")
        for statement in _INDEXES:
            connection.execute(statement)
        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        if own_transaction:
            connection.commit()
    except BaseException:
        if own_transaction:
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


def validate_schema(connection: sqlite3.Connection, *, full: bool = False) -> None:
    """Reject foreign identities, unknown versions, unexpected objects and damage."""
    try:
        if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise InvalidDatabase("This is not an AIK settings database.")
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise InvalidDatabase("Unsupported AIK settings schema version.")
        if _schema_rows(connection) != _expected_schema():
            raise InvalidDatabase("The settings schema differs from its declared version.")
        if full:
            if tuple(row[0] for row in connection.execute("PRAGMA integrity_check")) != ("ok",):
                raise InvalidDatabase("Settings database integrity validation failed.")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise InvalidDatabase("Settings database references are inconsistent.")
    except sqlite3.Error:
        raise InvalidDatabase("Settings database validation could not be completed.") from None
