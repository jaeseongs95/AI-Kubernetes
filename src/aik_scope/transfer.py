"""Settings export/import across workspaces, users and machines.

Export: aik_config settings export + non-secret aik_connection fields +
scopes + aik_profiles documents. No API key, host token, credential value,
verification state, workspace binding or history is exported.

Import: the whole document is validated first (the settings section by a
rolled-back dry run of SettingsStore.import_settings), conflicts are checked,
then records are written store by store. An API connection whose vault
reference does not resolve on this user/machine is kept as a pending
"reconnection required" record together with the profiles that depend on it.
"""

from __future__ import annotations

from dataclasses import dataclass, fields

from aik_config import AgentProfile, SettingsStore
from aik_connection import ConnectionSpec, ConnectionStore, CredentialRefUnavailable
from aik_profiles import ProfileStore, profile_from_dict, profile_to_dict
from aik_runtime.errors import InvalidDatabase, InvalidRecord, RevisionConflict

from .errors import InvalidScope, InvalidTransferDocument, TransferBlocked, TransferIncomplete
from .models import (
    FULL_STATE_RESTORE_TASK, ConnectionScope, ReconnectReport, TransferReport, check_scope_identifier,
)
from .store import ScopeStore


TRANSFER_FORMAT = "aik.connection-scope-transfer"
TRANSFER_VERSION = 1
TRANSFER_EXCLUDED = (
    "secret_values", "api_keys", "official_host_tokens", "authentication_sessions", "verification_state",
    "canonical_root_binding", "vm_project_binding", "active_workspace_selection", "revisions_and_history",
    "runtime_state", "goals_and_evidence", "execution_records", "vm_ags_ledgers_and_artifacts",
)
MAX_TRANSFER_RECORDS = 10000
_PAGE = 1000
_TOP_KEYS = frozenset({
    "format", "format_version", "contains_secret_values", "restores_goals_and_evidence", "full_state_restore",
    "excluded", "settings", "connections", "scopes", "execution_profiles",
})
_CONNECTION_KEYS = tuple(field.name for field in fields(ConnectionSpec))
_PENDING_CONNECTION_KEYS = tuple(name for name in _CONNECTION_KEYS if name != "credential_ref")
_SCOPE_KEYS = frozenset({"workspace_id", "allowed_connection_ids", "shared_connection_ids"})
_AGENT_KEYS = tuple(field.name for field in fields(AgentProfile))


class _DryRun(Exception):
    """Raised inside a settings transaction to roll back a validation import."""


@dataclass(frozen=True)
class _Plan:
    workspace_only: dict
    workspace_ids: tuple
    specs: tuple
    agent_profiles: tuple
    execution_profiles: tuple
    scopes: tuple


def _exact(value: object, keys: frozenset, what: str) -> dict:
    if type(value) is not dict or set(value) != keys:
        raise InvalidTransferDocument(f"The transfer {what} has missing or unsupported fields.")
    return value


def _profile_connections(profile) -> tuple:
    return tuple(dict.fromkeys(choice.connection_id for choice in (profile.primary, *profile.fallbacks)))


class SettingsTransfer:
    """Export/import settings without secrets; restores need reconnection and rebinding."""

    def __init__(self, settings: SettingsStore, connections: ConnectionStore, scopes: ScopeStore,
                 profiles: ProfileStore | None = None):
        if not isinstance(settings, SettingsStore) or not isinstance(connections, ConnectionStore) \
                or not isinstance(scopes, ScopeStore):
            raise InvalidScope("Settings, connection and scope stores are required.")
        if profiles is not None and not isinstance(profiles, ProfileStore):
            raise InvalidScope("The profile store must be an aik_profiles ProfileStore.")
        if scopes.settings.path != settings.path or (profiles is not None and profiles.settings.path != settings.path):
            raise InvalidScope("All stores must share the same settings database.")
        self._settings = settings
        self._connections = connections
        self._scopes = scopes
        self._profiles = profiles

    # --------------------------------------------------------------- export
    def export(self, workspace_ids=None) -> dict:
        """Settings-only export. Execution profiles are included only with a profile store."""
        settings_document = self._settings.export_settings(workspace_ids)
        entries = settings_document["workspaces"]
        selected = {entry["id"] for entry in entries}
        if any(record.workspace_id in selected for record in self._scopes.pending_records()):
            raise TransferBlocked("Pending reconnections must be completed or discarded before exporting.")
        connections, scopes, profiles = [], [], []
        for entry in entries:
            workspace_id = entry["id"]
            details = {item.value.connection_id: item.value for item in self._connections.list(workspace_id)}
            if set(details) != {item["id"] for item in entry["connections"]}:
                raise TransferBlocked("Settings connections and connection details differ; repair before exporting.")
            for item in entry["connections"]:
                detail = details[item["id"]]
                if (detail.provider, detail.auth_kind) != (item["provider"], item["auth_mode"]):
                    raise TransferBlocked("Settings connections and connection details differ.")
                # Only ConnectionSpec fields: no verification time, provenance or revisions.
                connections.append({name: getattr(detail, name) for name in _CONNECTION_KEYS})
            scope = self._scopes.get(workspace_id)
            if scope is not None:
                scopes.append({
                    "workspace_id": workspace_id,
                    "allowed_connection_ids": list(scope.value.allowed_connection_ids),
                    "shared_connection_ids": list(scope.value.shared_connection_ids),
                })
            if self._profiles is not None:
                after = None
                while True:
                    page = self._profiles.list(workspace_id, limit=_PAGE, after_id=after)
                    profiles.extend(profile_to_dict(item.value) for item in page)
                    if len(page) < _PAGE:
                        break
                    after = page[-1].value.id
        return {
            "format": TRANSFER_FORMAT,
            "format_version": TRANSFER_VERSION,
            "contains_secret_values": False,
            "restores_goals_and_evidence": False,
            "full_state_restore": FULL_STATE_RESTORE_TASK,
            "excluded": list(TRANSFER_EXCLUDED),
            "settings": settings_document,
            "connections": connections,
            "scopes": scopes,
            "execution_profiles": profiles,
        }

    # --------------------------------------------------------------- import
    def import_document(self, document) -> TransferReport:
        """Validate everything, check conflicts, then restore in a fail-closed state."""
        plan = self._parse(document)
        self._preflight(plan)
        return self._apply(plan)

    def _dry_run(self, settings_document) -> None:
        try:
            with self._settings.transaction() as tx:
                tx.import_settings(settings_document)
                raise _DryRun
        except _DryRun:
            return
        except RevisionConflict:
            raise
        except (InvalidRecord, TypeError, KeyError, AttributeError):
            raise InvalidTransferDocument("The settings section of the transfer document is invalid.") from None

    def _parse(self, document) -> _Plan:
        top = _exact(document, _TOP_KEYS, "document")
        if top["format"] != TRANSFER_FORMAT or type(top["format_version"]) is not int \
                or top["format_version"] != TRANSFER_VERSION:
            raise InvalidTransferDocument("The document is not a supported AIK settings transfer.")
        if top["contains_secret_values"] is not False:
            raise InvalidTransferDocument("Settings transfers must not contain secret values.")
        if top["restores_goals_and_evidence"] is not False or top["full_state_restore"] != FULL_STATE_RESTORE_TASK:
            raise InvalidTransferDocument("A settings transfer cannot restore Goals, evidence or full product state.")
        if type(top["excluded"]) is not list or any(type(item) is not str for item in top["excluded"]):
            raise InvalidTransferDocument("The transfer exclusion list is invalid.")
        for name in ("connections", "scopes", "execution_profiles"):
            if type(top[name]) is not list or len(top[name]) > MAX_TRANSFER_RECORDS:
                raise InvalidTransferDocument("A transfer section must be a bounded list.")
        if top["execution_profiles"] and self._profiles is None:
            raise TransferBlocked("Execution profiles require a profile store at the destination.")

        self._dry_run(top["settings"])  # Validates the settings section and its conflicts; writes nothing.
        workspaces = top["settings"]["workspaces"]
        workspace_ids = tuple(entry["id"] for entry in workspaces)
        settings_connections = {
            item["id"]: (entry["id"], item["provider"], item["auth_mode"])
            for entry in workspaces for item in entry["connections"]
        }
        agent_profiles = tuple(
            AgentProfile(item["id"], entry["id"], item["role"], item["connection_id"], item["model"],
                         effort=item["effort"])
            for entry in workspaces for item in entry["profiles"]
        )
        workspace_only = dict(top["settings"], workspaces=[
            dict(entry, connections=[], profiles=[]) for entry in workspaces
        ])
        try:
            specs = tuple(ConnectionSpec(**_exact(item, frozenset(_CONNECTION_KEYS), "connection"))
                          for item in top["connections"])
            details = {spec.connection_id: (spec.workspace_id, spec.provider, spec.auth_kind) for spec in specs}
            if len(details) != len(specs) or details != settings_connections:
                raise InvalidTransferDocument("Connection details do not match the settings connections.")
            scopes, scoped = [], set()
            for item in top["scopes"]:
                item = _exact(item, _SCOPE_KEYS, "scope")
                if type(item["allowed_connection_ids"]) is not list or type(item["shared_connection_ids"]) is not list:
                    raise InvalidTransferDocument("Scope connection IDs must be lists.")
                scope = ConnectionScope(item["workspace_id"], tuple(item["allowed_connection_ids"]),
                                        tuple(item["shared_connection_ids"]))
                if scope.workspace_id not in workspace_ids or scope.workspace_id in scoped:
                    raise InvalidTransferDocument("A scope refers to an absent or repeated workspace.")
                scoped.add(scope.workspace_id)
                scopes.append(scope)
            execution_profiles, seen = [], set()
            for item in top["execution_profiles"]:
                profile = profile_from_dict(item)
                if profile.workspace_id not in workspace_ids or profile.id in seen:
                    raise InvalidTransferDocument("An execution profile refers to an absent workspace or repeats.")
                seen.add(profile.id)
                for connection_id in _profile_connections(profile):
                    owner = details.get(connection_id)
                    if owner is None or owner[0] != profile.workspace_id:
                        raise InvalidTransferDocument("An execution profile refers to a connection outside its workspace.")
                execution_profiles.append(profile)
        except InvalidTransferDocument:
            raise
        except (InvalidRecord, TypeError, ValueError, KeyError):
            raise InvalidTransferDocument("A transfer record violates its contract.") from None
        return _Plan(workspace_only, workspace_ids, specs, agent_profiles, tuple(execution_profiles), tuple(scopes))

    def _preflight(self, plan: _Plan) -> None:
        existing = self._connections.list()
        ids = {entry.value.connection_id for entry in existing}
        refs = {entry.value.credential_ref for entry in existing if entry.value.credential_ref is not None}
        for spec in plan.specs:
            if spec.connection_id in ids:
                raise RevisionConflict("A transferred connection is already registered.")
            if spec.credential_ref is not None and spec.credential_ref in refs:
                raise RevisionConflict("A transferred credential reference is already bound here.")
        pending = {(record.kind, record.record_id) for record in self._scopes.pending_records()}
        keys = [("connection", spec.connection_id) for spec in plan.specs]
        keys += [("agent_profile", profile.id) for profile in plan.agent_profiles]
        keys += [("execution_profile", profile.id) for profile in plan.execution_profiles]
        if any(key in pending for key in keys):
            raise RevisionConflict("A transferred record is already pending restoration.")
        for scope in plan.scopes:
            if self._scopes.get(scope.workspace_id) is not None:
                raise RevisionConflict("A scope already exists for a transferred workspace.")
        for profile in plan.execution_profiles:
            if self._profiles.get(profile.id) is not None:
                raise RevisionConflict("A transferred execution profile already exists.")

    def _apply(self, plan: _Plan) -> TransferReport:
        stage, completed = "settings", []
        pending_connections, pending_profiles = [], []
        try:
            self._settings.import_settings(plan.workspace_only)
            completed.extend(f"workspace:{item}" for item in plan.workspace_ids)
            stage = "connections"
            for spec in plan.specs:
                try:
                    self._connections.register(spec)
                except CredentialRefUnavailable:
                    # Unresolvable on this user/machine: keep non-secret fields only.
                    record = {name: getattr(spec, name) for name in _PENDING_CONNECTION_KEYS}
                    self._scopes._add_pending("connection", spec.connection_id, spec.workspace_id, (), record)
                    pending_connections.append(spec.connection_id)
                    completed.append(f"pending_connection:{spec.connection_id}")
                else:
                    completed.append(f"connection:{spec.connection_id}")
            blocked = set(pending_connections)
            stage = "agent_profiles"
            ready = []
            for profile in plan.agent_profiles:
                if profile.connection_id in blocked:
                    record = {name: getattr(profile, name) for name in _AGENT_KEYS}
                    self._scopes._add_pending("agent_profile", profile.id, profile.workspace_id,
                                              (profile.connection_id,), record)
                    pending_profiles.append(profile.id)
                else:
                    ready.append(profile)
            if ready:
                with self._settings.transaction() as tx:
                    for profile in ready:
                        tx.save(profile, expected_revision=0)
                completed.extend(f"agent_profile:{profile.id}" for profile in ready)
            stage = "execution_profiles"
            for profile in plan.execution_profiles:
                requires = _profile_connections(profile)
                if blocked.intersection(requires):
                    self._scopes._add_pending("execution_profile", profile.id, profile.workspace_id, requires,
                                              profile_to_dict(profile))
                    pending_profiles.append(profile.id)
                else:
                    self._profiles.save(profile, expected_revision=0)
                    completed.append(f"execution_profile:{profile.id}")
            stage = "scopes"
            for scope in plan.scopes:
                self._scopes.save(scope, expected_revision=0)
                completed.append(f"scope:{scope.workspace_id}")
        except Exception as error:
            raise TransferIncomplete(stage, tuple(completed)) from error
        return TransferReport(
            workspace_ids=plan.workspace_ids,
            workspaces_requiring_binding=plan.workspace_ids,
            connections_requiring_authentication=tuple(
                spec.connection_id for spec in plan.specs if spec.auth_kind != "none"),
            connections_requiring_reconnection=tuple(pending_connections),
            pending_profile_ids=tuple(pending_profiles),
        )

    # ---------------------------------------------------------- reconnection
    def pending_records(self, workspace_id: str | None = None) -> tuple:
        return self._scopes.pending_records(workspace_id)

    def reconnect(self, connection_id: str, credential_ref: str | None = None) -> ReconnectReport:
        """Register a pending connection with a reference valid here, then restore its dependents.

        The connection stays unverified: the official host adapter must still
        confirm it. The workspace root must still be bound through AIK-084.
        """
        check_scope_identifier(connection_id, "connection_id")
        found = self._scopes._pending("connection", connection_id)
        if not found:
            raise TransferBlocked("No reconnection is pending for the connection.")
        _record, data = found[0]
        if set(data) != set(_PENDING_CONNECTION_KEYS):
            raise InvalidDatabase("A pending connection record violates its contract.")
        spec = ConnectionSpec(**data, credential_ref=credential_ref)
        existing = self._connections.get(connection_id)
        if existing is None:
            self._connections.register(spec)
        elif any(getattr(existing.value, name) != getattr(spec, name) for name in _CONNECTION_KEYS):
            raise TransferBlocked("A different connection is already registered under the pending ID.")
        self._scopes._remove_pending("connection", connection_id)
        restored = self._restore_dependents()
        remaining = tuple(record.record_id for record in self._scopes.pending_records()
                          if connection_id in record.requires)
        return ReconnectReport(connection_id, restored, remaining)

    def discard_pending(self, connection_id: str) -> tuple:
        """Abandon a pending connection and the profile records waiting for it."""
        check_scope_identifier(connection_id, "connection_id")
        return self._scopes._discard_connection(connection_id)

    def _restore_dependents(self) -> tuple:
        records = self._scopes._pending()
        waiting = {record.record_id for record, _ in records if record.kind == "connection"}
        restored = []
        for record, data in records:
            if record.kind == "connection" or waiting.intersection(record.requires):
                continue
            try:
                if record.kind == "agent_profile":
                    profile = AgentProfile(**data)
                    current = self._settings.get(AgentProfile, profile.id)
                    if current is None:
                        self._settings.save(profile, expected_revision=0)
                    elif current.value != profile:
                        raise TransferBlocked("A different profile already uses a pending profile ID.")
                else:
                    if self._profiles is None:
                        continue
                    profile = profile_from_dict(data)
                    current = self._profiles.get(profile.id)
                    if current is None:
                        self._profiles.save(profile, expected_revision=0)
                    elif current.value != profile:
                        raise TransferBlocked("A different profile already uses a pending profile ID.")
            except TypeError:
                raise InvalidDatabase("A pending profile record violates its contract.") from None
            self._scopes._remove_pending(record.kind, record.record_id)
            restored.append(record.record_id)
        return tuple(restored)
