"""Typed connection records. No field holds secret material."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timezone
import unicodedata

from aik_config.errors import InvalidSetting
from aik_config.models import AUTH_MODES, check_identifier, check_positive_integer
from aik_vault import is_credential_ref


AUTH_KINDS = AUTH_MODES  # ("none", "api_key", "host_session"), shared with aik_config
EXECUTION_KINDS = ("api", "subscription", "local")
EXECUTION_AUTH = {"api": "api_key", "subscription": "host_session", "local": "none"}
MAX_LABEL_LENGTH = 128


def check_label(value: object) -> None:
    if (
        type(value) is not str or not 1 <= len(value) <= MAX_LABEL_LENGTH or value != value.strip()
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        raise InvalidSetting("An account label must be 1-128 printable characters without edge spaces.")


def label_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def normalize_timestamp(value: object) -> str:
    if type(value) is not str:
        raise InvalidSetting("A verification time requires a timezone-aware ISO timestamp.")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError
        return stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError):
        raise InvalidSetting("A verification time requires a timezone-aware ISO timestamp.") from None


def utc_now() -> str:
    return normalize_timestamp(datetime.now(timezone.utc).isoformat())


def _choice(value: object, field: str, choices: tuple) -> None:
    if type(value) is not str or value not in choices:
        raise InvalidSetting(f"Unknown value for field: {field}.")


def _check_binding_fields(record) -> None:
    check_identifier(record.connection_id, "connection_id")
    check_identifier(record.workspace_id, "workspace_id")
    check_identifier(record.provider, "provider")
    check_label(record.account_label)
    _choice(record.auth_kind, "auth_kind", AUTH_KINDS)
    _choice(record.execution_kind, "execution_kind", EXECUTION_KINDS)
    if EXECUTION_AUTH[record.execution_kind] != record.auth_kind:
        raise InvalidSetting("The authentication kind does not match the execution kind.")
    check_identifier(record.official_host, "official_host")
    check_identifier(record.host_profile, "host_profile")
    if record.auth_kind == "none":
        if record.account_binding is not None:
            raise InvalidSetting("A connection without authentication has no account binding.")
    else:
        check_identifier(record.account_binding, "account_binding")
    if record.auth_kind == "api_key":
        if not is_credential_ref(record.credential_ref):
            raise InvalidSetting("An API connection requires an aik_vault opaque credential reference.")
    elif record.credential_ref is not None:
        raise InvalidSetting("Only API connections reference a vault credential.")


@dataclass(frozen=True, slots=True)
class ConnectionSpec:
    """Registration input. Verification happens later against an observed context."""

    connection_id: str
    workspace_id: str
    provider: str
    account_label: str
    auth_kind: str
    execution_kind: str
    official_host: str
    host_profile: str
    account_binding: str | None = None
    credential_ref: str | None = None

    def __post_init__(self) -> None:
        _check_binding_fields(self)


@dataclass(frozen=True, slots=True)
class Connection:
    """A stored connection. connection_id and provenance_id never change."""

    connection_id: str
    workspace_id: str
    provider: str
    account_label: str
    auth_kind: str
    execution_kind: str
    official_host: str
    host_profile: str
    account_binding: str | None
    credential_ref: str | None
    verified_at: str | None
    binding_revision: int
    provenance_id: str

    def __post_init__(self) -> None:
        _check_binding_fields(self)
        check_positive_integer(self.binding_revision, "binding_revision")
        check_identifier(self.provenance_id, "provenance_id")
        if self.verified_at is not None:
            if self.auth_kind == "none":
                raise InvalidSetting("A connection without authentication has no verification time.")
            object.__setattr__(self, "verified_at", normalize_timestamp(self.verified_at))


@dataclass(frozen=True, slots=True)
class AuthContext:
    """The authentication context observed by an official host adapter (not by this package)."""

    connection_id: str
    provider: str
    auth_kind: str
    execution_kind: str
    official_host: str
    host_profile: str
    account_binding: str | None
    binding_revision: int

    def __post_init__(self) -> None:
        for field in ("connection_id", "provider", "official_host", "host_profile"):
            check_identifier(getattr(self, field), field)
        _choice(self.auth_kind, "auth_kind", AUTH_KINDS)
        _choice(self.execution_kind, "execution_kind", EXECUTION_KINDS)
        if self.account_binding is not None:
            check_identifier(self.account_binding, "account_binding")
        check_positive_integer(self.binding_revision, "binding_revision")


@dataclass(frozen=True, slots=True)
class ConnectionProvenance:
    """Immutable identity for task provenance; the display label is excluded."""

    connection_id: str
    provenance_id: str
    workspace_id: str
    provider: str
    auth_kind: str
    execution_kind: str
    official_host: str
    host_profile: str
    account_binding: str | None
    binding_revision: int


@dataclass(frozen=True, slots=True)
class ConnectionReference:
    """One record that refers to a connection. removable=False always blocks deletion."""

    kind: str
    id: str
    workspace_id: str | None
    removable: bool

    def __post_init__(self) -> None:
        check_identifier(self.kind, "kind")
        check_identifier(self.id, "id")
        if self.workspace_id is not None:
            check_identifier(self.workspace_id, "workspace_id")
        if type(self.removable) is not bool:
            raise InvalidSetting("removable must be a boolean.")


@dataclass(frozen=True, slots=True)
class DeletionImpact:
    connection_id: str
    references: tuple

    def __post_init__(self) -> None:
        check_identifier(self.connection_id, "connection_id")
        if type(self.references) is not tuple or any(type(item) is not ConnectionReference for item in self.references):
            raise InvalidSetting("An impact report requires a tuple of connection references.")

    @property
    def in_use(self) -> bool:
        return bool(self.references)

    @property
    def blocking(self) -> tuple:
        return tuple(item for item in self.references if not item.removable)


def rebuild(value, model):
    """Reconstruct exact model instances; object.__setattr__ forgeries are revalidated."""
    if type(value) is not model:
        raise InvalidSetting("An unsupported connection record type was supplied.")
    return model(**{field.name: getattr(value, field.name) for field in fields(model)})


def provenance_of(connection: Connection) -> ConnectionProvenance:
    return ConnectionProvenance(
        connection.connection_id, connection.provenance_id, connection.workspace_id, connection.provider,
        connection.auth_kind, connection.execution_kind, connection.official_host, connection.host_profile,
        connection.account_binding, connection.binding_revision,
    )
