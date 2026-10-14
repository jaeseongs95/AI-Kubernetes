"""AIK-046/047 ConnectionService with fake connectors and the test-only vault.

No provider or host is contacted. Secrets are synthetic markers; the tests
check that no DTO, error or stored file ever carries them.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_config import AgentProfile, SettingsStore  # noqa: E402
from aik_connect import (  # noqa: E402
    CheckResult, CheckState, ConnectError, ConnectionService, ConnectorInfo, HostLogin, KeyVerifier,
    LoginState, LoginStatus,
)
from aik_vault import CredentialNotFound, FakeVaultBackend, VaultAccessDenied, VaultUnavailable  # noqa: E402

SECRET = "sk-synthetic-key-AAAA-must-not-leak"
SECRET_2 = "sk-synthetic-key-BBBB-must-not-leak"


class FakeVerifier:
    info = ConnectorInfo("vendor-a", "api", "vendor-a-api", "default", "Vendor A API")

    def __init__(self, vault):
        self.vault = vault
        self.results = []
        self.seen = []

    def accepts(self, secret):
        return secret.startswith("sk-")

    def verify(self, connection):
        self.seen.append((connection.connection_id, connection.binding_revision))
        assert self.vault.resolve(connection.credential_ref).startswith("sk-")
        return self.results.pop(0) if self.results else CheckResult(CheckState.VERIFIED)


class FakeHost:
    info = ConnectorInfo("vendor-b", "subscription", "vendor-b-cli", "default", "Vendor B 구독")

    def __init__(self):
        self.state = LoginState.IDLE
        self.account = "acct-1"
        self.cancelable = True
        self.logged_out = []
        self.started = []

    def start_login(self, connection_id):
        self.started.append(connection_id)
        self.state = LoginState.PENDING
        return self.login_status()

    def login_status(self):
        return LoginStatus(self.state, self.cancelable)

    def cancel_login(self):
        if not self.cancelable or self.state is not LoginState.PENDING:
            return False
        self.state = LoginState.CANCELLED
        return True

    def connect(self, connection_id):
        if self.state is not LoginState.SIGNED_IN:
            return CheckResult(CheckState.UNAUTHENTICATED, code="NOT_SIGNED_IN")
        return CheckResult(CheckState.VERIFIED, account_binding=self.account)

    def check(self, connection):
        return self.connect(connection.connection_id)

    def logout(self, connection):
        self.logged_out.append(connection.connection_id)
        self.state = LoginState.IDLE


class ConnectionServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="aik-connect-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.vault = FakeVaultBackend(self.root / "vault", key=bytes(range(32)))
        self.verifier = FakeVerifier(self.vault)
        self.host = FakeHost()
        self.settings_path = self.root / "config" / "settings.sqlite3"
        self.opened = 0

        def vault_factory():
            self.opened += 1
            return self.vault

        self.service = ConnectionService(self.settings_path, (self.verifier, self.host), vault_factory=vault_factory)

    def assert_no_secret(self, value):
        text = json.dumps(value, ensure_ascii=False)
        for secret in (SECRET, SECRET_2):
            self.assertNotIn(secret, text)
        self.assertNotIn("secret://", text)

    def files_without_secret(self):
        for path in self.root.rglob("*.sqlite3"):
            data = path.read_bytes()
            self.assertNotIn(SECRET.encode(), data)
            self.assertNotIn(SECRET_2.encode(), data)

    def test_ports_are_checked_at_composition(self):
        self.assertIsInstance(self.verifier, KeyVerifier)
        self.assertIsInstance(self.host, HostLogin)
        with self.assertRaises(TypeError):
            ConnectionService(self.settings_path, (object(),), vault_factory=lambda: self.vault)
        with self.assertRaises(ValueError):
            ConnectionService(self.settings_path, (self.verifier, FakeVerifier(self.vault)),
                              vault_factory=lambda: self.vault)
        with self.assertRaises(ValueError):
            ConnectionService(Path("relative.sqlite3"), (), vault_factory=lambda: self.vault)

    def test_reads_create_nothing_and_never_open_the_vault(self):
        self.assertEqual(self.service.list(), {"store": "ABSENT", "connections": []})
        self.assertEqual(self.service.login_state()["state"], "idle")
        catalog = self.service.catalog()["connectors"]
        self.assertEqual([(c["provider"], c["method"]) for c in catalog],
                         [("vendor-a", "api_key"), ("vendor-b", "host_login")])
        self.assertFalse(self.settings_path.exists())
        self.assertEqual(self.opened, 0)
        with self.assertRaises(ConnectError) as raised:
            self.service.check("conn-missing")
        self.assertEqual(raised.exception.code, "UNKNOWN_CONNECTION")

    def test_add_key_stores_only_a_reference_and_verifies(self):
        result = self.service.add_api_key("vendor-a", "업무 키", SECRET)
        self.assert_no_secret(result)
        connection = result["connection"]
        self.assertEqual((connection["state"], connection["method"], result["check"]["state"]),
                         ("verified", "api_key", "verified"))
        self.assertIsNotNone(connection["verified_at"])
        self.assertEqual(connection["usage"], {"references": 0, "blocking": 0, "external_checked": False})
        listed = self.service.list()
        self.assertEqual(listed["store"], "READY")
        self.assertEqual(len(listed["connections"]), 1)
        self.assert_no_secret(listed)
        self.files_without_secret()

    def test_add_key_rejections_leave_nothing_behind(self):
        for provider, label, secret, code in (
            ("vendor-x", "a", SECRET, "UNSUPPORTED_PROVIDER"),
            ("vendor-b", "a", SECRET, "UNSUPPORTED_PROVIDER"),  # subscription connector, not an API path
            ("vendor-a", " edge", SECRET, "INVALID_LABEL"),
            ("vendor-a", "a", "not-a-key", "INVALID_SECRET"),
            ("vendor-a", "a", "", "INVALID_SECRET"),
            ("vendor-a", "a", 5, "INVALID_SECRET"),
        ):
            with self.subTest(code=code, provider=provider), self.assertRaises(ConnectError) as raised:
                self.service.add_api_key(provider, label, secret)
            self.assertEqual(raised.exception.code, code)
            self.assertNotIn(SECRET, str(raised.exception))
        self.assertFalse(self.settings_path.exists())

    def test_duplicate_label_deletes_the_new_vault_entry(self):
        self.service.add_api_key("vendor-a", "같은 이름", SECRET)
        before = sorted(p.name for p in (self.root / "vault").iterdir())
        with self.assertRaises(ConnectError) as raised:
            self.service.add_api_key("vendor-a", "같은 이름", SECRET_2)
        self.assertEqual(raised.exception.code, "DUPLICATE_CONNECTION")
        self.assertEqual(sorted(p.name for p in (self.root / "vault").iterdir()), before)

    def test_vault_failures_become_codes(self):
        class Locked(FakeVaultBackend):
            def put(self, secret):
                raise VaultAccessDenied()

        locked = Locked(self.root / "locked", key=bytes(32))
        service = ConnectionService(self.settings_path, (FakeVerifier(locked),), vault_factory=lambda: locked)
        with self.assertRaises(ConnectError) as raised:
            service.add_api_key("vendor-a", "a", SECRET)
        self.assertEqual(raised.exception.code, "VAULT_LOCKED")

        def unavailable():
            raise VaultUnavailable()

        service = ConnectionService(self.settings_path, (FakeVerifier(locked),), vault_factory=unavailable)
        with self.assertRaises(ConnectError) as raised:
            service.add_api_key("vendor-a", "b", SECRET)
        self.assertEqual(raised.exception.code, "VAULT_UNAVAILABLE")

    def test_failed_check_is_reported_and_clears_an_earlier_verification(self):
        added = self.service.add_api_key("vendor-a", "키", SECRET)["connection"]
        self.verifier.results.append(CheckResult(CheckState.UNAUTHENTICATED, code="KEY_REJECTED"))
        checked = self.service.check(added["connection_id"])
        self.assertEqual(checked["check"], {"state": "unauthenticated", "code": "KEY_REJECTED"})
        self.assertEqual(checked["connection"]["state"], "unauthenticated")
        self.assertIsNone(checked["connection"]["verified_at"])
        # An unknown outcome (for example a network failure) does not erase anything.
        self.verifier.results.append(CheckResult(CheckState.VERIFIED))
        self.service.check(added["connection_id"])
        self.verifier.results.append(CheckResult(CheckState.UNKNOWN, code="NETWORK"))
        unknown = self.service.check(added["connection_id"])
        self.assertEqual(unknown["connection"]["state"], "unknown")
        self.assertIsNotNone(unknown["connection"]["verified_at"])

    def test_verifier_exceptions_become_unknown(self):
        added = self.service.add_api_key("vendor-a", "키", SECRET)["connection"]

        def boom(connection):
            raise RuntimeError(SECRET)

        self.verifier.verify = boom
        checked = self.service.check(added["connection_id"])
        self.assertEqual(checked["check"], {"state": "unknown", "code": "CHECK_FAILED"})
        self.assert_no_secret(checked)

    def test_rotation_keeps_the_reference_and_moves_the_binding(self):
        added = self.service.add_api_key("vendor-a", "키", SECRET)["connection"]
        with self.assertRaises(ConnectError) as raised:
            self.service.rotate_key(added["connection_id"], added["revision"] + 5, SECRET_2)
        self.assertEqual(raised.exception.code, "REVISION_CONFLICT")
        rotated = self.service.rotate_key(added["connection_id"], added["revision"], SECRET_2)
        self.assertEqual(rotated["connection"]["binding_revision"], added["binding_revision"] + 1)
        self.assertEqual(rotated["check"]["state"], "verified")
        self.assertEqual(self.verifier.seen[-1], (added["connection_id"], added["binding_revision"] + 1))
        store = self.service._store
        reference = store.get(added["connection_id"]).value.credential_ref
        self.assertEqual(self.vault.resolve(reference), SECRET_2)
        self.assert_no_secret(rotated)

    def test_rename_and_delete_with_impact(self):
        added = self.service.add_api_key("vendor-a", "키", SECRET)["connection"]
        renamed = self.service.rename(added["connection_id"], added["revision"], "새 이름")["connection"]
        self.assertEqual(renamed["account_label"], "새 이름")
        settings = SettingsStore(self.settings_path)
        settings.save(AgentProfile("director-1", "local", "director", added["connection_id"], "model-x"))
        impact = self.service.impact(added["connection_id"])
        self.assertEqual(impact["references"], [{"kind": "agent_profile", "id": "director-1", "removable": True}])
        with self.assertRaises(ConnectError) as raised:
            self.service.delete(added["connection_id"], renamed["revision"], False)
        self.assertEqual(raised.exception.code, "CONNECTION_IN_USE")
        reference = self.service._store.get(added["connection_id"]).value.credential_ref
        deleted = self.service.delete(added["connection_id"], renamed["revision"], True)
        self.assertEqual(deleted, {"connection_id": added["connection_id"], "removed_references": 1,
                                   "secret_deleted": True})
        with self.assertRaises(CredentialNotFound):
            self.vault.resolve(reference)
        self.assertEqual(self.service.list()["connections"], [])

    def test_api_connections_have_no_separate_logout(self):
        added = self.service.add_api_key("vendor-a", "키", SECRET)["connection"]
        with self.assertRaises(ConnectError) as raised:
            self.service.logout(added["connection_id"], added["revision"])
        self.assertEqual(raised.exception.code, "WRONG_METHOD")

    def test_subscription_login_flow(self):
        started = self.service.start_login("vendor-b", "개인 구독")
        self.assertEqual((started["state"], started["cancel_supported"]), ("pending", True))
        with self.assertRaises(ConnectError) as raised:
            self.service.start_login("vendor-b", "다른 구독")
        self.assertEqual(raised.exception.code, "LOGIN_IN_PROGRESS")
        with self.assertRaises(ConnectError) as raised:
            self.service.complete_login()
        self.assertEqual(raised.exception.code, "LOGIN_PENDING")
        self.host.state = LoginState.SIGNED_IN
        done = self.service.complete_login()
        self.assertEqual((done["connection"]["state"], done["connection"]["method"]), ("verified", "host_login"))
        self.assertEqual(self.service.login_state()["state"], "idle")
        connection = done["connection"]
        # Logout asks the host and clears the verification.
        out = self.service.logout(connection["connection_id"], connection["revision"])["connection"]
        self.assertEqual(self.host.logged_out, [connection["connection_id"]])
        self.assertEqual((out["state"], out["verified_at"]), ("unauthenticated", None))
        # Re-authentication of the same connection must see the same account.
        self.service.start_login(None, connection_id=connection["connection_id"])
        self.host.state = LoginState.SIGNED_IN
        self.host.account = "acct-other"
        with self.assertRaises(ConnectError) as raised:
            self.service.complete_login()
        self.assertEqual(raised.exception.code, "ACCOUNT_CHANGED")
        self.host.account = "acct-1"
        again = self.service.complete_login()["connection"]
        self.assertEqual(again["state"], "verified")
        # A host now signed in to another account makes the stored connection ACCOUNT_CHANGED.
        self.host.account = "acct-other"
        checked = self.service.check(connection["connection_id"])
        self.assertEqual(checked["check"]["state"], "account_changed")
        self.assertIsNone(checked["connection"]["verified_at"])

    def test_login_cancel_and_failures(self):
        with self.assertRaises(ConnectError) as raised:
            self.service.cancel_login()
        self.assertEqual(raised.exception.code, "NO_LOGIN")
        self.service.start_login("vendor-b", "구독")
        self.assertTrue(self.service.cancel_login()["cancelled"])
        self.assertEqual(self.service.login_state()["state"], "idle")
        self.host.cancelable = False
        self.service.start_login("vendor-b", "구독")
        cancelled = self.service.cancel_login()
        self.assertEqual((cancelled["cancelled"], cancelled["login"]["state"]), (False, "pending"))
        self.host.state = LoginState.FAILED
        with self.assertRaises(ConnectError) as raised:
            self.service.complete_login()
        self.assertEqual(raised.exception.code, "NOT_SIGNED_IN")

        def broken(connection_id):
            raise RuntimeError("host path /home/someone")

        self.host.start_login = broken
        with self.assertRaises(ConnectError) as raised:
            self.service.start_login("vendor-b", "구독 2")
        self.assertEqual(raised.exception.code, "LOGIN_START_FAILED")


if __name__ == "__main__":
    unittest.main()
