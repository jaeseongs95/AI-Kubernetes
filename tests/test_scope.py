"""AIK-021: workspace connection scope and settings transfer on synthetic data.

No provider, network or real credential is used. A fake vault checker plays the
role of one user's vault: a reference resolves only where its value was stored.
A second, independent directory simulates another Windows user or machine; this
is a Linux development simulation, not a Windows verification (NOT_RUN).
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import secrets
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_config import (  # noqa: E402
    AgentProfile, ConnectionProfile, InvalidReference, RevisionConflict, SettingsStore, StorageError,
    UnsafeStoragePath, Workspace, WorkspaceBinding, WorkspaceBindingRequired, WorkspaceLimits, InvalidDatabase,
)
from aik_connection import AuthContext, ConnectionSpec, ConnectionStore, CredentialRefUnavailable  # noqa: E402
from aik_profiles import DirectorProfile, ExecutionChoice, ProfileStore  # noqa: E402
from aik_vault import is_credential_ref  # noqa: E402
from aik_scope import (  # noqa: E402
    ConnectionNotInScope, ConnectionScope, InvalidScope, InvalidTransferDocument, ScopeStore,
    SettingsTransfer, TransferBlocked, TransferIncomplete,
)


API_SECRET = "SYNTHETIC_API_KEY_MUST_NOT_BE_EXPORTED_0123"
HOST_TOKEN = "SYNTHETIC_HOST_TOKEN_MUST_NOT_BE_EXPORTED_4567"
REENTERED_SECRET = "SYNTHETIC_REENTERED_KEY_MUST_NOT_LEAK_8910"


class FakeVault:
    """Test-only stand-in for one user's vault; the plaintext never leaves it."""

    def __init__(self):
        self._values = {}

    def put(self, value: str) -> str:
        reference = "secret://aik-vault/" + secrets.token_hex(16)
        self._values[reference] = value
        return reference

    def check(self, credential_ref: str) -> None:
        if credential_ref not in self._values:
            raise CredentialRefUnavailable("The synthetic vault has no credential for the reference.")


class SyntheticBindingAuthority:
    """Stands in for the AIK-084 binding authority: it reports the synthetic root of ws-a."""

    def binding_for(self, workspace_id):
        if workspace_id == "ws-a":
            return WorkspaceBinding("ws-a", "/synthetic/root-a", "vm-project-a")
        return None


class Machine:
    def __init__(self, root: Path, *, vault: FakeVault | None = None, with_profiles: bool = True):
        self.settings = SettingsStore(root / "config" / "settings.sqlite3",
                                      binding_authority=SyntheticBindingAuthority())
        self.vault = FakeVault() if vault is None else vault
        self.connections = ConnectionStore(self.settings, credential_checker=self.vault)
        self.profiles = ProfileStore(root / "config" / "profiles.sqlite3", settings=self.settings) \
            if with_profiles else None
        self.scopes = ScopeStore(self.settings)
        self.transfer = SettingsTransfer(self.settings, self.connections, self.scopes, self.profiles)


def all_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from all_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from all_keys(item)


class ScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aik-scope-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.src = Machine(self.root / "source")
        self.seed(self.src)

    def seed(self, machine: Machine) -> None:
        machine.settings.save(Workspace("ws-a", "작업공간 A", canonical_root="/synthetic/root-a",
                                        vm_project_id="vm-project-a"))
        machine.settings.save(Workspace("ws-b", "작업공간 B"))
        self.api_ref = machine.vault.put(API_SECRET)
        self.assertTrue(is_credential_ref(self.api_ref))
        machine.connections.register(ConnectionSpec(
            "conn-api", "ws-a", "vendor", "API 키", "api_key", "api", "vendor-api", "default",
            account_binding="account-a", credential_ref=self.api_ref))
        machine.connections.register(ConnectionSpec(
            "conn-sub", "ws-a", "vendor", "구독", "host_session", "subscription", "vendor-cli", "default",
            account_binding="account-a"))
        machine.connections.register(ConnectionSpec(
            "conn-local", "ws-a", "local", "로컬", "none", "local", "local-host", "default"))
        machine.connections.register(ConnectionSpec(
            "conn-b", "ws-b", "vendor", "B 구독", "host_session", "subscription", "vendor-cli", "default",
            account_binding="account-b"))
        machine.connections.mark_verified(
            "conn-sub", AuthContext("conn-sub", "vendor", "host_session", "subscription", "vendor-cli", "default",
                                    "account-a", 1), expected_revision=1, verified_at="2030-01-01T00:00:00Z")
        machine.settings.save(AgentProfile("director-a", "ws-a", "director", "conn-api", "synthetic-model"))
        machine.settings.save(AgentProfile("worker-a", "ws-a", "worker", "conn-sub", "synthetic-small"))
        machine.settings.save(WorkspaceLimits("ws-a", max_parallel_workers=2))
        self.exec_profile = DirectorProfile(
            "exec-director-a", "ws-a", ExecutionChoice("conn-api", "synthetic-model", "high", ("shell",)),
            max_calls=10, fallbacks=(ExecutionChoice("conn-local", "local-model"),))
        machine.profiles.save(self.exec_profile)
        machine.scopes.save(ConnectionScope("ws-a", ("conn-api", "conn-sub", "conn-local"), ("conn-sub",)))
        machine.scopes.save(ConnectionScope("ws-b", ("conn-b", "conn-sub")))

    def assert_nothing_written(self, machine: Machine) -> None:
        self.assertEqual(machine.settings.list(Workspace), ())
        self.assertEqual(machine.connections.list(), ())
        self.assertEqual(machine.scopes.list(), ())
        self.assertEqual(machine.scopes.pending_records(), ())

    # ------------------------------------------------------------- scope guard
    def test_explicit_scope_allows_listed_and_blocks_unlisted_connections(self) -> None:
        scopes = self.src.scopes
        decision = scopes.check("ws-a", "conn-api")
        self.assertEqual((decision.owner_workspace_id, decision.shared), ("ws-a", False))
        with self.assertRaises(ConnectionNotInScope) as denied:
            scopes.check("ws-b", "conn-api")
        self.assertEqual(denied.exception.reason, "NOT_ALLOWED")
        self.src.settings.save(Workspace("ws-c", "작업공간 C"))
        with self.assertRaises(ConnectionNotInScope) as denied:
            scopes.check("ws-c", "conn-b")
        self.assertEqual(denied.exception.reason, "NO_SCOPE")
        scopes.save(ConnectionScope("ws-c", ("conn-ghost",)))
        with self.assertRaises(ConnectionNotInScope) as denied:
            scopes.check("ws-c", "conn-ghost")
        self.assertEqual(denied.exception.reason, "UNKNOWN_CONNECTION")
        self.assertFalse(scopes.is_allowed("ws-c", "conn-ghost"))
        self.assertEqual(len(scopes.require("ws-a", ("conn-api", "conn-local"))), 2)
        with self.assertRaises(ConnectionNotInScope):
            scopes.require("ws-a", ("conn-api", "conn-b"))

    def test_cross_workspace_use_requires_the_owner_to_share(self) -> None:
        scopes = self.src.scopes
        decision = scopes.check("ws-b", "conn-sub")
        self.assertEqual((decision.owner_workspace_id, decision.shared), ("ws-a", True))
        scopes.save(ConnectionScope("ws-b", ("conn-b", "conn-sub", "conn-api")), expected_revision=1)
        with self.assertRaises(ConnectionNotInScope) as denied:
            scopes.check("ws-b", "conn-api")
        self.assertEqual(denied.exception.reason, "NOT_SHARED")
        # A workspace cannot share another workspace's connection.
        scopes.save(ConnectionScope("ws-b", ("conn-b", "conn-sub", "conn-api"), ("conn-api",)), expected_revision=2)
        self.assertFalse(scopes.is_allowed("ws-b", "conn-api"))
        scopes.save(ConnectionScope("ws-a", ("conn-api", "conn-sub", "conn-local"), ("conn-api", "conn-sub")),
                    expected_revision=1)
        self.assertTrue(scopes.check("ws-b", "conn-api").shared)

    def test_profiles_referring_to_unlisted_connections_are_reported(self) -> None:
        self.src.scopes.save(ConnectionScope("ws-a", ("conn-api",)), expected_revision=1)
        found = {(item.kind, item.record_id, item.connection_id, item.reason)
                 for item in self.src.scopes.violations("ws-a", self.src.profiles)}
        self.assertEqual(found, {
            ("agent_profile", "worker-a", "conn-sub", "NOT_ALLOWED"),
            ("execution_profile", "exec-director-a", "conn-local", "NOT_ALLOWED"),
        })

    def test_f1_guarded_profile_saves_refuse_unlisted_connections(self) -> None:
        scopes = self.src.scopes
        scopes.save(ConnectionScope("ws-a", ("conn-api",)), expected_revision=1)
        with self.assertRaises(ConnectionNotInScope) as denied:
            scopes.save_agent_profile(AgentProfile("worker-guarded", "ws-a", "worker", "conn-sub", "synthetic-small"))
        self.assertEqual(denied.exception.reason, "NOT_ALLOWED")
        self.assertIsNone(self.src.settings.get(AgentProfile, "worker-guarded"))
        allowed = scopes.save_agent_profile(
            AgentProfile("worker-guarded-ok", "ws-a", "worker", "conn-api", "synthetic-small"))
        self.assertEqual(allowed.revision, 1)
        self.assertIsNotNone(self.src.settings.get(AgentProfile, "worker-guarded-ok"))

        denied_profile = DirectorProfile(
            "exec-guarded-denied", "ws-a", ExecutionChoice("conn-api", "synthetic-model"), max_calls=1,
            fallbacks=(ExecutionChoice("conn-local", "local-model"),))
        with self.assertRaises(ConnectionNotInScope) as denied:
            scopes.save_execution_profile(self.src.profiles, denied_profile)
        self.assertEqual((denied.exception.reason, denied.exception.connection_id), ("NOT_ALLOWED", "conn-local"))
        self.assertIsNone(self.src.profiles.get("exec-guarded-denied"))
        allowed_profile = DirectorProfile(
            "exec-guarded-ok", "ws-a", ExecutionChoice("conn-api", "synthetic-model"), max_calls=1)
        self.assertEqual(scopes.save_execution_profile(self.src.profiles, allowed_profile).revision, 1)
        self.assertEqual(self.src.profiles.get("exec-guarded-ok").value, allowed_profile)

    def test_scope_records_are_validated_revisioned_and_persistent(self) -> None:
        for arguments in ((["conn-api"],), (("conn-api", "conn-api"),), (("../x",),)):
            with self.subTest(arguments=repr(arguments)):
                with self.assertRaises(InvalidScope):
                    ConnectionScope("ws-a", *arguments)
        with self.assertRaises(InvalidReference):
            self.src.scopes.save(ConnectionScope("ws-missing", ("conn-api",)))
        with self.assertRaises(RevisionConflict):
            self.src.scopes.save(ConnectionScope("ws-a", ()))
        with self.assertRaises(RevisionConflict):
            self.src.scopes.save(ConnectionScope("ws-a", ()), expected_revision=7)
        reopened = ScopeStore(self.src.settings)
        self.assertEqual(reopened.get("ws-a").value.allowed_connection_ids, ("conn-api", "conn-local", "conn-sub"))
        with self.assertRaises(UnsafeStoragePath):
            ScopeStore(self.src.settings, path=self.src.settings.path)
        with self.assertRaises(InvalidDatabase):
            ScopeStore(self.src.settings, path=self.src.connections.path)

    def test_f10_scope_store_does_not_reuse_runtime_private_sync_helpers(self) -> None:
        from aik_runtime import storage as runtime_storage
        from aik_scope import store as scope_store
        self.assertIsNot(scope_store._sync_file, runtime_storage._sync_file)
        self.assertIsNot(scope_store._sync_directory, runtime_storage._sync_directory)

    # ----------------------------------------------------------------- export
    def test_export_contains_no_secret_token_verification_or_binding(self) -> None:
        document = self.src.transfer.export()
        text = json.dumps(document, ensure_ascii=False)
        for marker in (API_SECRET, HOST_TOKEN, "/synthetic/root-a", "vm-project-a", "2030-01-01"):
            self.assertNotIn(marker, text)
        keys = set(all_keys(document))
        self.assertTrue(keys.isdisjoint({"verified_at", "binding_revision", "provenance_id", "canonical_root",
                                         "vm_project_id", "secret", "token", "api_key", "password"}))
        self.assertIs(document["contains_secret_values"], False)
        self.assertIs(document["restores_goals_and_evidence"], False)
        self.assertEqual(document["full_state_restore"], "AIK-058")
        for item in document["connections"]:
            if item["auth_kind"] == "api_key":
                self.assertTrue(is_credential_ref(item["credential_ref"]))
            else:
                self.assertIsNone(item["credential_ref"])
        self.assertNotIn(API_SECRET.encode(), self.src.scopes.path.read_bytes())

    def test_f7_export_refuses_unexpected_settings_fields(self) -> None:
        genuine = self.src.settings.export_settings()
        mutations = {
            "connection token field": lambda d: d["workspaces"][0]["connections"][0]
            .__setitem__("token", HOST_TOKEN),
            "top-level token field": lambda d: d.__setitem__("token", HOST_TOKEN),
            "nested value under an allowed key": lambda d: d["workspaces"][0]
            .__setitem__("name", {"token": HOST_TOKEN}),
            "limits extra field": lambda d: d["workspaces"][0]["limits"].__setitem__("api_key", HOST_TOKEN),
            "secret flag": lambda d: d.__setitem__("contains_secret_values", True),
        }
        for name, mutate in mutations.items():
            with self.subTest(mutation=name):
                candidate = copy.deepcopy(genuine)
                mutate(candidate)
                with mock.patch.object(self.src.settings, "export_settings", return_value=candidate):
                    with self.assertRaises(TransferBlocked) as blocked:
                        self.src.transfer.export()
                self.assertNotIn(HOST_TOKEN, str(blocked.exception))
        self.assertEqual(self.src.transfer.export()["settings"], genuine)

    def test_export_is_blocked_for_settings_connections_without_details(self) -> None:
        self.src.settings.save(ConnectionProfile("conn-raw", "ws-a", "vendor"))
        with self.assertRaises(TransferBlocked):
            self.src.transfer.export()

    # ----------------------------------------------------------------- import
    def test_new_user_or_machine_restores_unresolvable_reference_as_reconnection_required(self) -> None:
        document = json.loads(json.dumps(self.src.transfer.export()))
        dst = Machine(self.root / "other-machine")
        report = dst.transfer.import_document(document)

        self.assertEqual(report.workspace_ids, ("ws-a", "ws-b"))
        self.assertEqual(report.workspaces_requiring_binding, ("ws-a", "ws-b"))
        self.assertEqual(report.connections_requiring_reconnection, ("conn-api",))
        self.assertEqual(set(report.connections_requiring_authentication), {"conn-api", "conn-sub", "conn-b"})
        self.assertEqual(set(report.pending_profile_ids), {"director-a", "exec-director-a"})
        self.assertFalse(report.execution_ready)
        self.assertFalse(report.goals_and_evidence_restored)
        self.assertEqual((report.full_state_restore_task, report.gui_entry_task), ("AIK-058", "AIK-088"))
        self.assertIn("BIND_WORKSPACE_ROOT:ws-a", report.required_actions)
        self.assertIn("RECONNECT_CREDENTIAL:conn-api", report.required_actions)

        self.assertFalse(dst.settings.get(Workspace, "ws-a").value.bound)
        dst.settings.select_workspace("ws-a")
        with self.assertRaises(WorkspaceBindingRequired):
            dst.settings.resolve_execution_target()
        self.assertIsNone(dst.connections.get("conn-api"))
        self.assertIsNone(dst.settings.get(ConnectionProfile, "conn-api"))
        self.assertIsNone(dst.connections.get("conn-sub").value.verified_at)
        self.assertTrue(dst.settings.get(ConnectionProfile, "conn-sub").value.needs_authentication)
        self.assertIsNotNone(dst.connections.get("conn-local"))
        self.assertIsNotNone(dst.settings.get(AgentProfile, "worker-a"))
        self.assertIsNone(dst.settings.get(AgentProfile, "director-a"))
        self.assertEqual(dst.settings.get(WorkspaceLimits, "ws-a").value.max_parallel_workers, 2)
        self.assertEqual(dst.scopes.get("ws-a").value, self.src.scopes.get("ws-a").value)
        with self.assertRaises(ConnectionNotInScope) as denied:
            dst.scopes.check("ws-a", "conn-api")
        self.assertEqual(denied.exception.reason, "RECONNECTION_REQUIRED")
        self.assertTrue(dst.scopes.check("ws-b", "conn-sub").shared)
        self.assertEqual(len(ScopeStore(dst.settings).pending_records()), 3)
        with self.assertRaises(TransferBlocked):
            dst.transfer.export()

        with self.assertRaises(CredentialRefUnavailable):
            dst.transfer.reconnect("conn-api", "secret://aik-vault/" + secrets.token_hex(16))
        self.assertEqual(len(dst.transfer.pending_records()), 3)

        new_ref = dst.vault.put(REENTERED_SECRET)
        result = dst.transfer.reconnect("conn-api", new_ref)
        self.assertEqual(set(result.restored_record_ids), {"director-a", "exec-director-a"})
        self.assertEqual(result.pending_record_ids, ())
        self.assertTrue(result.requires_authentication)
        restored = dst.connections.get("conn-api").value
        self.assertEqual((restored.credential_ref, restored.verified_at), (new_ref, None))
        self.assertTrue(dst.settings.get(ConnectionProfile, "conn-api").value.needs_authentication)
        self.assertEqual(dst.profiles.get("exec-director-a").value, self.exec_profile)
        self.assertEqual(dst.scopes.check("ws-a", "conn-api").owner_workspace_id, "ws-a")
        self.assertEqual(dst.transfer.pending_records(), ())
        exported_again = json.dumps(dst.transfer.export(), ensure_ascii=False)
        self.assertNotIn(REENTERED_SECRET, exported_again)
        self.assertNotIn(API_SECRET, exported_again)

    def test_same_user_restore_keeps_resolvable_reference_but_requires_authentication(self) -> None:
        document = self.src.transfer.export()
        dst = Machine(self.root / "same-user-restore", vault=self.src.vault)
        report = dst.transfer.import_document(document)
        self.assertEqual(report.connections_requiring_reconnection, ())
        self.assertEqual(report.pending_profile_ids, ())
        connection = dst.connections.get("conn-api").value
        self.assertEqual((connection.credential_ref, connection.verified_at), (self.api_ref, None))
        self.assertIn("AUTHENTICATE:conn-api", report.required_actions)
        self.assertFalse(report.execution_ready)

    def test_invalid_documents_are_rejected_without_any_write(self) -> None:
        document = self.src.transfer.export()
        dst = Machine(self.root / "rejecting-machine")
        mutations = {
            "connection secret field": lambda d: d["connections"][0].__setitem__("api_key", API_SECRET),
            "secret flag": lambda d: d.__setitem__("contains_secret_values", True),
            "goal restore claim": lambda d: d.__setitem__("restores_goals_and_evidence", True),
            "missing connection detail": lambda d: d["connections"].pop(),
            "settings token field": lambda d: d["settings"]["workspaces"][0]["connections"][0]
            .__setitem__("token", HOST_TOKEN),
            "scope string": lambda d: d["scopes"][0].__setitem__("allowed_connection_ids", "conn-api"),
            "cross workspace profile": lambda d: d["execution_profiles"][0]["primary"]
            .__setitem__("connection_id", "conn-b"),
            "future version": lambda d: d.__setitem__("format_version", 2),
            "unknown top field": lambda d: d.__setitem__("extra", 1),
        }
        for name, mutate in mutations.items():
            with self.subTest(mutation=name):
                candidate = copy.deepcopy(document)
                mutate(candidate)
                with self.assertRaises(InvalidTransferDocument) as rejected:
                    dst.transfer.import_document(candidate)
                self.assertNotIn(API_SECRET, str(rejected.exception))
                self.assertNotIn(HOST_TOKEN, str(rejected.exception))
                self.assert_nothing_written(dst)

    def test_f1_import_refuses_profiles_outside_the_document_scope(self) -> None:
        document = self.src.transfer.export()
        dst = Machine(self.root / "scope-checking-machine")

        narrowed = copy.deepcopy(document)
        for item in narrowed["scopes"]:
            if item["workspace_id"] == "ws-a":
                item["allowed_connection_ids"] = ["conn-api", "conn-local"]
                item["shared_connection_ids"] = []
        with self.assertRaises(TransferBlocked) as blocked:
            dst.transfer.import_document(narrowed)
        found = {(item.kind, item.record_id, item.connection_id, item.reason)
                 for item in blocked.exception.violations}
        self.assertEqual(found, {("agent_profile", "worker-a", "conn-sub", "NOT_ALLOWED")})
        self.assert_nothing_written(dst)

        unscoped = copy.deepcopy(document)
        unscoped["scopes"] = [item for item in unscoped["scopes"] if item["workspace_id"] != "ws-a"]
        with self.assertRaises(TransferBlocked) as blocked:
            dst.transfer.import_document(unscoped)
        found = {(item.kind, item.record_id, item.connection_id, item.reason)
                 for item in blocked.exception.violations}
        self.assertEqual(found, {
            ("agent_profile", "director-a", "conn-api", "NO_SCOPE"),
            ("agent_profile", "worker-a", "conn-sub", "NO_SCOPE"),
            ("execution_profile", "exec-director-a", "conn-api", "NO_SCOPE"),
            ("execution_profile", "exec-director-a", "conn-local", "NO_SCOPE"),
        })
        self.assert_nothing_written(dst)

    def test_conflicting_or_unsupported_destinations_are_refused_before_writes(self) -> None:
        document = self.src.transfer.export()
        before = self.src.settings.list(Workspace)
        with self.assertRaises(RevisionConflict):
            self.src.transfer.import_document(document)
        self.assertEqual(self.src.settings.list(Workspace), before)
        no_profiles = Machine(self.root / "no-profiles", with_profiles=False)
        with self.assertRaises(TransferBlocked):
            no_profiles.transfer.import_document(document)
        self.assertEqual(no_profiles.settings.list(Workspace), ())

    def test_f5_failure_before_any_write_is_reported_as_blocked(self) -> None:
        document = self.src.transfer.export()
        dst = Machine(self.root / "racing-machine")
        with mock.patch.object(dst.settings, "import_settings",
                               side_effect=RevisionConflict("synthetic concurrent creation")):
            with self.assertRaises(TransferBlocked) as blocked:
                dst.transfer.import_document(document)
        self.assertNotIsInstance(blocked.exception, TransferIncomplete)
        self.assert_nothing_written(dst)

    def test_partial_import_failure_is_reported_as_incomplete(self) -> None:
        document = self.src.transfer.export()
        dst = Machine(self.root / "failing-machine")
        with mock.patch.object(dst.scopes, "save", side_effect=StorageError("synthetic scope failure")):
            with self.assertRaises(TransferIncomplete) as failure:
                dst.transfer.import_document(document)
        self.assertEqual(failure.exception.stage, "scopes")
        self.assertIn("workspace:ws-a", failure.exception.completed)
        self.assertFalse(dst.settings.get(Workspace, "ws-a").value.bound)
        with self.assertRaises(ConnectionNotInScope):
            dst.scopes.check("ws-a", "conn-sub")

    def test_discarding_a_pending_connection_keeps_it_denied(self) -> None:
        dst = Machine(self.root / "discarding-machine")
        dst.transfer.import_document(self.src.transfer.export())
        removed = dst.transfer.discard_pending("conn-api")
        self.assertEqual(set(removed), {"conn-api", "director-a", "exec-director-a"})
        self.assertEqual(dst.transfer.pending_records(), ())
        with self.assertRaises(ConnectionNotInScope) as denied:
            dst.scopes.check("ws-a", "conn-api")
        self.assertEqual(denied.exception.reason, "UNKNOWN_CONNECTION")
        dst.transfer.export()

    def test_f3_discarding_a_connection_that_is_not_pending_is_refused(self) -> None:
        dst = Machine(self.root / "discard-guard-machine")
        dst.transfer.import_document(self.src.transfer.export())
        with self.assertRaises(TransferBlocked):
            dst.transfer.discard_pending("conn-local")
        self.assertEqual(len(dst.transfer.pending_records()), 3)
        result = dst.transfer.reconnect("conn-api", dst.vault.put(REENTERED_SECRET))
        self.assertEqual(set(result.restored_record_ids), {"director-a", "exec-director-a"})
        self.assertEqual(dst.profiles.get("exec-director-a").value, self.exec_profile)

    def test_f4_reconnect_conflict_is_reported_and_restore_can_be_retried(self) -> None:
        dst = Machine(self.root / "conflict-machine")
        dst.transfer.import_document(self.src.transfer.export())
        dst.settings.save(AgentProfile("director-a", "ws-a", "director", "conn-sub", "synthetic-model"))

        result = dst.transfer.reconnect("conn-api", dst.vault.put(REENTERED_SECRET))
        self.assertEqual(result.restored_record_ids, ("exec-director-a",))
        self.assertEqual(result.conflict_record_ids, ("director-a",))
        self.assertEqual(result.pending_record_ids, ("director-a",))
        self.assertIsNotNone(dst.connections.get("conn-api"))
        self.assertEqual(dst.profiles.get("exec-director-a").value, self.exec_profile)

        current = dst.settings.get(AgentProfile, "director-a")
        dst.settings.delete(AgentProfile, "director-a", current.revision)
        retried = dst.transfer.restore_pending()
        self.assertEqual(retried.restored_record_ids, ("director-a",))
        self.assertEqual(retried.conflict_record_ids, ())
        self.assertEqual(retried.pending_record_ids, ())
        self.assertEqual(dst.transfer.pending_records(), ())
        self.assertEqual(dst.settings.get(AgentProfile, "director-a").value,
                         AgentProfile("director-a", "ws-a", "director", "conn-api", "synthetic-model"))


if __name__ == "__main__":
    unittest.main()
