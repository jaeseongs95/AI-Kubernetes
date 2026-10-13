"""AIK-010 connection metadata tests with synthetic data only.

No provider is contacted. Secrets are synthetic markers stored only in the
test-only FakeVaultBackend; the connection and settings files must hold only
opaque references. Linux runs are development verification, not Windows support.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_config import (  # noqa: E402
    AgentProfile, ConnectionProfile, InvalidDatabase, InvalidReference, InvalidSetting,
    RevisionConflict, SettingsStore, UnsafeStoragePath, Workspace,
)
from aik_connection import (  # noqa: E402
    AuthContext, AuthContextMismatch, AuthenticationRequired, ConnectionInUse, ConnectionReference,
    ConnectionSpec, ConnectionStore, CredentialRefUnavailable, DuplicateConnection, RuntimePoolReferences,
    UnknownConnection, VaultCredentialRefChecker,
)
from aik_runtime import Pool, RuntimeStore  # noqa: E402
from aik_vault import FakeVaultBackend  # noqa: E402


SECRET_A = "SYNTHETIC_API_KEY_A_MUST_NOT_BE_STORED"
SECRET_B = "SYNTHETIC_API_KEY_B_MUST_NOT_BE_STORED"
VENDOR = "synthetic-vendor"


def context_of(connection, **changes) -> AuthContext:
    """Simulates what an official host adapter would observe (no real host here)."""
    values = dict(connection_id=connection.connection_id, provider=connection.provider,
                  auth_kind=connection.auth_kind, execution_kind=connection.execution_kind,
                  official_host=connection.official_host, host_profile=connection.host_profile,
                  account_binding=connection.account_binding, binding_revision=connection.binding_revision)
    values.update(changes)
    return AuthContext(**values)


class ConnectionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aik-connection-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = SettingsStore(self.root / "config" / "settings.sqlite3")
        self.settings.save(Workspace("ws-1", "합성 작업공간"))
        self.vault = FakeVaultBackend(self.root / "vault", key=bytes(range(32)))
        self.ref_a = self.vault.put(SECRET_A)
        self.ref_b = self.vault.put(SECRET_B)
        self.checker = VaultCredentialRefChecker(self.vault)
        self.store = ConnectionStore(self.settings, credential_checker=self.checker)

    def subscription(self, **changes) -> ConnectionSpec:
        values = dict(connection_id="conn-sub-a", workspace_id="ws-1", provider=VENDOR,
                      account_label="개인 구독 A", auth_kind="host_session", execution_kind="subscription",
                      official_host="synthetic-cli", host_profile="default", account_binding="account-a")
        values.update(changes)
        return ConnectionSpec(**values)

    def api(self, **changes) -> ConnectionSpec:
        values = dict(connection_id="conn-api-a", workspace_id="ws-1", provider=VENDOR,
                      account_label="API 키 A", auth_kind="api_key", execution_kind="api",
                      official_host="synthetic-api", host_profile="default", account_binding="account-a",
                      credential_ref=self.ref_a)
        values.update(changes)
        return ConnectionSpec(**values)

    def assert_no_secret_bytes(self) -> None:
        for path in (self.store.path, self.settings.path):
            data = path.read_bytes()
            for secret in (SECRET_A, SECRET_B):
                self.assertNotIn(secret.encode("utf-8"), data)

    def test_same_vendor_accounts_and_subscription_api_paths_register_separately_and_persist(self) -> None:
        sub_a = self.store.register(self.subscription())
        sub_b = self.store.register(self.subscription(connection_id="conn-sub-b", account_label="개인 구독 B",
                                                      account_binding="account-b"))
        api_a = self.store.register(self.api())
        for entry in (sub_a, sub_b, api_a):
            self.assertEqual(entry.revision, 1)
            self.assertEqual(entry.value.binding_revision, 1)
            self.assertIsNone(entry.value.verified_at)
        self.assertEqual(len({e.value.provenance_id for e in (sub_a, sub_b, api_a)}), 3)
        self.assertEqual((api_a.value.auth_kind, api_a.value.execution_kind, api_a.value.credential_ref),
                         ("api_key", "api", self.ref_a))
        self.assertEqual((sub_a.value.account_label, sub_a.value.official_host, sub_a.value.host_profile),
                         ("개인 구독 A", "synthetic-cli", "default"))

        reopened = ConnectionStore(self.settings)
        self.assertEqual(reopened.get("conn-api-a"), api_a)
        self.assertEqual([e.value.connection_id for e in reopened.list(workspace_id="ws-1")],
                         ["conn-api-a", "conn-sub-a", "conn-sub-b"])
        self.assertEqual(self.settings.get(ConnectionProfile, "conn-api-a").value,
                         ConnectionProfile("conn-api-a", "ws-1", VENDOR, "api_key", True))
        self.assertEqual(self.settings.get(ConnectionProfile, "conn-sub-a").value,
                         ConnectionProfile("conn-sub-a", "ws-1", VENDOR, "host_session", True))
        self.assert_no_secret_bytes()

    def test_duplicates_are_rejected_without_partial_records(self) -> None:
        self.store.register(self.subscription())
        self.store.register(self.api())
        duplicates = (
            self.subscription(account_label="다른 이름", account_binding="account-z"),  # same ID
            self.subscription(connection_id="conn-sub-x", account_label="중복 계정"),  # same account+path
            self.subscription(connection_id="conn-sub-y", account_label="개인 구독 a", account_binding="account-y"),
            self.api(connection_id="conn-api-b", account_label="API 키 B", account_binding="account-b"),  # same ref
        )
        for spec in duplicates:
            with self.subTest(connection_id=spec.connection_id, label=spec.account_label):
                with self.assertRaises(DuplicateConnection):
                    self.store.register(spec)
        self.assertEqual([e.value.connection_id for e in self.store.list()], ["conn-api-a", "conn-sub-a"])
        self.assertEqual([e.value.id for e in self.settings.list(ConnectionProfile)], ["conn-api-a", "conn-sub-a"])
        self.assertEqual(self.store.register(self.api(connection_id="conn-api-b", account_label="API 키 B",
                                                      account_binding="account-b", credential_ref=self.ref_b)).revision, 1)

    def test_legacy_settings_connection_with_same_id_is_not_overwritten(self) -> None:
        legacy = ConnectionProfile("conn-legacy", "ws-1", VENDOR, auth_mode="host_session", needs_authentication=True)
        self.settings.save(legacy)
        with self.assertRaises(DuplicateConnection):
            self.store.register(self.subscription(connection_id="conn-legacy"))
        self.assertIsNone(self.store.get("conn-legacy"))
        self.assertEqual(self.settings.get(ConnectionProfile, "conn-legacy").revision, 1)

    def test_missing_workspace_writes_nothing(self) -> None:
        with self.assertRaises(InvalidReference):
            self.store.register(self.subscription(workspace_id="ws-missing"))
        self.assertEqual(self.store.list(), ())
        self.assertEqual(self.settings.list(ConnectionProfile), ())

    def test_credential_reference_must_be_an_existing_vault_reference(self) -> None:
        with self.assertRaises(InvalidSetting):
            self.api(credential_ref="secret://local/not-vault")
        with self.assertRaises(CredentialRefUnavailable):
            self.store.register(self.api(credential_ref="secret://aik-vault/" + "0" * 32))
        without_checker = ConnectionStore(self.settings)
        with self.assertRaises(CredentialRefUnavailable):
            without_checker.register(self.api())
        self.assertEqual(self.store.list(), ())
        forged = self.api()
        object.__setattr__(forged, "credential_ref", SECRET_A)
        with self.assertRaises(InvalidSetting) as rejected:
            self.store.register(forged)
        self.assertNotIn(SECRET_A, str(rejected.exception))
        self.assertEqual(self.store.list(), ())
        self.assert_no_secret_bytes()

    def test_invalid_specs_are_rejected(self) -> None:
        invalid = (
            dict(credential_ref=None),
            dict(auth_kind="host_session"),
            dict(account_label="\x00bad"),
            dict(account_label=" edge"),
            dict(account_binding="user@example.com"),
            dict(account_binding=None),
            dict(execution_kind="unknown"),
        )
        for changes in invalid:
            with self.subTest(changes=sorted(changes)):
                with self.assertRaises(InvalidSetting):
                    self.api(**changes)
        with self.assertRaises(InvalidSetting):
            self.subscription(credential_ref=self.ref_a)
        with self.assertRaises(InvalidSetting):
            ConnectionSpec("conn-local", "ws-1", VENDOR, "로컬", "none", "local", "local-runner", "default",
                           account_binding="account-a")

    def test_auth_context_must_match_and_be_verified_for_the_current_binding_revision(self) -> None:
        created = self.store.register(self.subscription()).value
        context = context_of(created)
        with self.assertRaises(AuthenticationRequired):
            self.store.verify_auth_context(context)
        with self.assertRaises(AuthContextMismatch):
            self.store.mark_verified("conn-sub-a", context_of(created, account_binding="account-b"), 1)
        self.assertIsNone(self.store.get("conn-sub-a").value.verified_at)
        with self.assertRaises(InvalidSetting):
            self.store.mark_verified("conn-sub-a", context, 1, verified_at="2030-01-01T00:00:00")

        verified = self.store.mark_verified("conn-sub-a", context, 1, verified_at="2030-01-01T09:00:00+09:00")
        self.assertEqual(verified.value.verified_at, "2030-01-01T00:00:00Z")
        self.assertEqual(verified.revision, 2)
        self.assertFalse(self.settings.get(ConnectionProfile, "conn-sub-a").value.needs_authentication)
        provenance = self.store.verify_auth_context(context)
        self.assertEqual((provenance.connection_id, provenance.provenance_id, provenance.binding_revision),
                         ("conn-sub-a", created.provenance_id, 1))
        for changes in (dict(account_binding="account-b"), dict(binding_revision=2), dict(official_host="other-cli"),
                        dict(host_profile="other"), dict(provider="other-vendor"),
                        dict(execution_kind="api", auth_kind="api_key")):
            with self.subTest(changes=sorted(changes)):
                with self.assertRaises(AuthContextMismatch):
                    self.store.verify_auth_context(context_of(created, **changes))
        with self.assertRaises(UnknownConnection):
            self.store.verify_auth_context(context_of(created, connection_id="conn-unknown"))

        rebound = self.store.rebind("conn-sub-a", 2, account_binding="account-c")
        self.assertEqual((rebound.value.binding_revision, rebound.value.verified_at), (2, None))
        self.assertEqual(rebound.value.provenance_id, created.provenance_id)
        self.assertTrue(self.settings.get(ConnectionProfile, "conn-sub-a").value.needs_authentication)
        with self.assertRaises(AuthContextMismatch):
            self.store.verify_auth_context(context)
        with self.assertRaises(AuthenticationRequired):
            self.store.verify_auth_context(context_of(rebound.value))
        with self.assertRaises(InvalidSetting):
            self.store.rebind("conn-sub-a", 3, account_binding="account-c")

    def test_api_connection_verification_requires_the_vault_reference_to_remain(self) -> None:
        created = self.store.register(self.api()).value
        context = context_of(created)
        self.store.mark_verified("conn-api-a", context, 1, verified_at="2030-01-01T00:00:00Z")
        self.assertEqual(self.store.verify_auth_context(context).provenance_id, created.provenance_id)
        self.vault.delete(self.ref_a)
        with self.assertRaises(CredentialRefUnavailable):
            self.store.verify_auth_context(context)
        self.assert_no_secret_bytes()

    def test_local_connection_needs_no_verification(self) -> None:
        local = ConnectionSpec("conn-local", "ws-1", VENDOR, "로컬 실행", "none", "local", "local-runner", "default")
        created = self.store.register(local).value
        self.assertEqual(self.store.verify_auth_context(context_of(created)).connection_id, "conn-local")
        with self.assertRaises(InvalidSetting):
            self.store.mark_verified("conn-local", context_of(created), 1)

    def test_rename_changes_only_the_label_and_keeps_provenance(self) -> None:
        created = self.store.register(self.subscription())
        self.store.register(self.subscription(connection_id="conn-sub-b", account_label="개인 구독 B",
                                              account_binding="account-b"))
        self.settings.save(AgentProfile("worker-1", "ws-1", "worker", "conn-sub-a", "synthetic-model"))
        before = self.store.provenance("conn-sub-a")
        renamed = self.store.rename("conn-sub-a", "업무 구독", expected_revision=1)
        self.assertEqual(renamed.revision, 2)
        self.assertEqual(renamed.value, replace(created.value, account_label="업무 구독"))
        self.assertEqual(self.store.provenance("conn-sub-a"), before)
        self.assertEqual(self.settings.get(ConnectionProfile, "conn-sub-a").revision, 1)
        self.assertEqual(self.settings.get(AgentProfile, "worker-1").value.connection_id, "conn-sub-a")
        with self.assertRaises(RevisionConflict):
            self.store.rename("conn-sub-a", "오래된 이름", expected_revision=1)
        with self.assertRaises(DuplicateConnection):
            self.store.rename("conn-sub-a", "개인 구독 B", expected_revision=2)
        with self.assertRaises(InvalidSetting):
            self.store.rename("conn-sub-a", "bad\nlabel", expected_revision=2)
        self.assertEqual(self.store.get("conn-sub-a"), renamed)

    def test_delete_reports_impact_and_requires_acknowledged_force_for_used_connections(self) -> None:
        self.store.register(self.subscription())
        self.settings.save(AgentProfile("worker-1", "ws-1", "worker", "conn-sub-a", "synthetic-model"))
        impact = self.store.impact("conn-sub-a")
        self.assertEqual(impact.references, (ConnectionReference("agent_profile", "worker-1", "ws-1", True),))
        self.assertTrue(impact.in_use)
        with self.assertRaises(ConnectionInUse) as refused:
            self.store.delete("conn-sub-a", 1)
        self.assertEqual(refused.exception.impact, impact)
        with self.assertRaises(ConnectionInUse):
            self.store.delete("conn-sub-a", 1, force=True)
        self.settings.save(AgentProfile("director-1", "ws-1", "director", "conn-sub-a", "synthetic-model"))
        with self.assertRaises(ConnectionInUse) as stale:
            self.store.delete("conn-sub-a", 1, force=True, acknowledged_impact=impact)
        self.assertEqual(len(stale.exception.impact.references), 2)
        self.assertIsNotNone(self.store.get("conn-sub-a"))
        self.assertIsNotNone(self.settings.get(AgentProfile, "worker-1"))
        with self.assertRaises(RevisionConflict):
            self.store.delete("conn-sub-a", 2, force=True, acknowledged_impact=stale.exception.impact)

        result = self.store.delete("conn-sub-a", 1, force=True, acknowledged_impact=stale.exception.impact)
        self.assertEqual(result, stale.exception.impact)
        self.assertIsNone(self.store.get("conn-sub-a"))
        self.assertIsNone(self.settings.get(ConnectionProfile, "conn-sub-a"))
        self.assertIsNone(self.settings.get(AgentProfile, "worker-1"))
        self.assertIsNone(self.settings.get(AgentProfile, "director-1"))
        with self.assertRaises(UnknownConnection):
            self.store.provenance("conn-sub-a")

    def test_unused_delete_and_external_runtime_references_always_block(self) -> None:
        self.store.register(self.subscription())
        self.assertEqual(self.store.delete("conn-sub-a", 1).references, ())
        self.assertIsNone(self.settings.get(ConnectionProfile, "conn-sub-a"))

        runtime = RuntimeStore(self.root / "runtime" / "runtime.sqlite3")
        runtime.save(Pool("pool-1", "synthetic", "conn-api-a"), expected_revision=0)
        store = ConnectionStore(self.settings, credential_checker=self.checker,
                                reference_sources=(RuntimePoolReferences(runtime),))
        store.register(self.api())
        impact = store.impact("conn-api-a")
        self.assertEqual(impact.blocking, (ConnectionReference("runtime_pool", "pool-1", None, False),))
        with self.assertRaises(ConnectionInUse):
            store.delete("conn-api-a", 1, force=True, acknowledged_impact=impact)
        self.assertIsNotNone(store.get("conn-api-a"))
        self.assertIsNotNone(runtime.get(Pool, "pool-1"))

    def test_foreign_or_shared_files_are_rejected_and_preserved(self) -> None:
        with self.assertRaises(UnsafeStoragePath):
            ConnectionStore(self.settings, path=self.settings.path)
        foreign = self.root / "config" / "foreign.sqlite3"
        with closing(sqlite3.connect(foreign)) as connection:
            connection.execute("CREATE TABLE unrelated (payload TEXT)")
            connection.commit()
        before = foreign.read_bytes()
        with self.assertRaises(InvalidDatabase):
            ConnectionStore(self.settings, path=foreign)
        self.assertEqual(foreign.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
