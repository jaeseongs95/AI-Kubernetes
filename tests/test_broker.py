"""AIK-018 credential broker tests with synthetic connections and a fake vault.

No provider, network or real credential is used. FakeVaultBackend is test
only. Linux runs are development verification, not Windows support evidence.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import traceback
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_broker import (  # noqa: E402
    AdapterFailed, AdapterHandle, BrokerDenied, ChildResult, CredentialBroker, CredentialUnavailable,
    InjectionError, RuntimeLeaseChecker, SecretLeakBlocked, run_injected,
)
from aik_connection.models import Connection  # noqa: E402
from aik_runtime import ExecutionProfile, Lease, Placement, Pool, RuntimeStore  # noqa: E402
from aik_security.redaction import OutputRedactor  # noqa: E402
from aik_vault import FakeVaultBackend  # noqa: E402


SECRET = "SYNTHETIC-broker-value-7f3a9c2e41"
SECRET_B = "SYNTHETIC-second-value-0b8d6e5a93"
SECRET_C = "SYNTHETIC-foreign-value-c4e2a17f60"
ALL_SECRETS = (SECRET, SECRET_B, SECRET_C)

READ_STDIN = r'''
import hashlib, sys
data = sys.stdin.buffer.read()
print(hashlib.sha256(data).hexdigest())
print("echo:" + data.decode("utf-8"))
'''
READ_ENV = r'''
import hashlib, os, sys
value = os.environ[sys.argv[1]]
print(hashlib.sha256(value.encode("utf-8")).hexdigest())
print("echo:" + value, file=sys.stderr)
'''
SPAWN_GRANDCHILD = r'''
import subprocess, sys
from pathlib import Path
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path(sys.argv[1]).write_text(str(grandchild.pid), encoding="utf-8")
'''


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class FakeClock:
    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeConnections:
    def __init__(self):
        self.records = {}

    def add(self, connection: Connection) -> None:
        self.records[connection.connection_id] = connection

    def get(self, connection_id):
        value = self.records.get(connection_id)
        return None if value is None else SimpleNamespace(value=value, revision=1)


class FakeLeases:
    def __init__(self, active):
        self.active = set(active)

    def __call__(self, lease_id, fencing_token):
        return (lease_id, fencing_token) in self.active


def api_connection(connection_id, workspace_id, ref, *, provider="vendor", verified=True, binding_revision=1):
    return Connection(
        connection_id=connection_id, workspace_id=workspace_id, provider=provider,
        account_label=f"label {connection_id}", auth_kind="api_key", execution_kind="api",
        official_host="vendor-api", host_profile="default", account_binding=f"account-{connection_id}",
        credential_ref=ref, verified_at="2030-01-01T00:00:00Z" if verified else None,
        binding_revision=binding_revision, provenance_id=f"prov-{connection_id}",
    )


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    stat = Path(f"/proc/{pid}/stat")
    if not stat.exists():
        return not Path("/proc").is_dir()
    try:
        state = stat.read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


class BrokerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aik-broker-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.vault = FakeVaultBackend(self.root / "vault", key=bytes(range(32)))
        self.ref_a = self.vault.put(SECRET)
        self.ref_b = self.vault.put(SECRET_B)
        self.ref_c = self.vault.put(SECRET_C)
        self.connections = FakeConnections()
        self.connections.add(api_connection("conn-a", "ws-1", self.ref_a))
        self.connections.add(api_connection("conn-b", "ws-1", self.ref_b))
        self.connections.add(api_connection("conn-other", "ws-2", self.ref_c))
        self.leases = FakeLeases({("lease-1", 1), ("lease-2", 1)})
        self.clock = FakeClock(1000.0)
        self.sink = []
        self.broker = CredentialBroker(self.vault, self.connections, self.leases, clock=self.clock,
                                       audit_sink=self.sink.append)
        self.adapter = self.broker.register_adapter("adapter-vendor", "vendor")

    def grant(self, **overrides):
        values = dict(workspace_id="ws-1", task_id="task-1", adapter_id="adapter-vendor",
                      connection_ids=("conn-a",), lease_id="lease-1", fencing_token=1,
                      ttl_seconds=30, max_uses=1)
        values.update(overrides)
        return self.broker.issue_grant(**values)

    def inject(self, grant, consumer=None, handle=None, **overrides):
        values = dict(workspace_id=grant.workspace_id, task_id=grant.task_id,
                      connection_id=grant.connection_ids[0])
        values.update(overrides)
        return self.broker.inject(self.adapter if handle is None else handle, grant.grant_id,
                                  consumer=consumer if consumer is not None else (lambda secret: "done"),
                                  **values)

    def assert_code(self, error_type, code, function, *args, **kwargs):
        with self.assertRaises(error_type) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assert_no_secret("".join(traceback.format_exception(caught.exception)))
        self.assert_no_secret(repr(caught.exception))
        return caught.exception

    def assert_no_secret(self, text: str) -> None:
        for secret in ALL_SECRETS:
            self.assertNotIn(secret, text)
            # aik_security.redaction: a registered redactor must find nothing
            # beyond what an empty registry would redact.
            self.assertEqual(OutputRedactor([secret]).redact_text(text), OutputRedactor().redact_text(text))


class GrantScopeTests(BrokerTestCase):
    def test_authorized_adapter_receives_secret_only_inside_callback(self) -> None:
        grant = self.grant()
        self.assert_no_secret(repr(grant))
        self.assertNotIn(self.ref_a, repr(grant))  # workers get connection IDs, not even the ref
        received = []
        result = self.inject(grant, consumer=lambda secret: received.append(digest(secret)) or "done")
        self.assertEqual(result, "done")
        self.assertEqual(received, [digest(SECRET)])

    def test_single_use_grant_rejects_reuse_without_calling_adapter(self) -> None:
        grant = self.grant()
        self.inject(grant)
        calls = []
        self.assert_code(BrokerDenied, "GRANT_EXHAUSTED", self.inject, grant, consumer=calls.append)
        self.assertEqual(calls, [])
        self.assertEqual(self.broker.active_grants(), ())

    def test_concurrent_reuse_yields_exactly_one_resolution(self) -> None:
        grant = self.grant()
        barrier = threading.Barrier(8)
        outcomes, lock = [], threading.Lock()

        def worker():
            barrier.wait()
            try:
                self.inject(grant)
                outcome = "ok"
            except BrokerDenied as error:
                outcome = error.code
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(sorted(outcomes), ["GRANT_EXHAUSTED"] * 7 + ["ok"])

    def test_expired_grant_is_rejected_and_clock_rollback_does_not_revive_it(self) -> None:
        grant = self.grant(ttl_seconds=10, max_uses=3)
        self.clock.now = 1010.0
        self.assert_code(BrokerDenied, "GRANT_EXPIRED", self.inject, grant)
        self.clock.now = 1005.0
        self.assert_code(BrokerDenied, "GRANT_EXPIRED", self.inject, grant)

    def test_other_workspace_is_rejected_at_issue_and_at_injection(self) -> None:
        self.assert_code(BrokerDenied, "WORKSPACE_MISMATCH", self.grant, connection_ids=("conn-other",))
        grant = self.grant(max_uses=2)
        self.assert_code(BrokerDenied, "WORKSPACE_MISMATCH", self.inject, grant, workspace_id="ws-2")
        self.assert_code(BrokerDenied, "TASK_MISMATCH", self.inject, grant, task_id="task-2")
        # Scope mismatches do not consume a use.
        self.assertEqual(self.inject(grant), "done")

    def test_connection_outside_grant_scope_is_rejected(self) -> None:
        grant = self.grant()
        self.assert_code(BrokerDenied, "CONNECTION_OUT_OF_SCOPE", self.inject, grant, connection_id="conn-b")
        self.assert_code(BrokerDenied, "CONNECTION_OUT_OF_SCOPE", self.inject, grant, connection_id="conn-other")

    def test_multi_connection_grant_resolves_each_named_connection(self) -> None:
        grant = self.grant(connection_ids=("conn-a", "conn-b"), max_uses=2)
        got = []
        self.inject(grant, consumer=lambda s: got.append(digest(s)), connection_id="conn-a")
        self.inject(grant, consumer=lambda s: got.append(digest(s)), connection_id="conn-b")
        self.assertEqual(got, [digest(SECRET), digest(SECRET_B)])

    def test_renderer_style_and_forged_callers_are_rejected(self) -> None:
        grant = self.grant(max_uses=2)
        for handle in ({"adapter_id": "adapter-vendor"}, "adapter-vendor",
                       AdapterHandle("adapter-vendor", "vendor")):
            with self.subTest(handle=type(handle).__name__):
                calls = []
                self.assert_code(BrokerDenied, "ADAPTER_NOT_ALLOWED", self.inject, grant,
                                 consumer=calls.append, handle=handle)
                self.assertEqual(calls, [])
        other = self.broker.register_adapter("adapter-other", "vendor")
        self.assert_code(BrokerDenied, "ADAPTER_NOT_ALLOWED", self.inject, grant, handle=other)
        self.assertEqual(self.inject(grant), "done")

    def test_broker_and_renderer_ipc_expose_no_raw_secret_api(self) -> None:
        public = {name for name in dir(CredentialBroker) if not name.startswith("_")}
        self.assertEqual(public, {
            "register_adapter", "unregister_adapter", "issue_grant", "inject", "run_with_credential",
            "revoke", "revoke_task", "revoke_lease", "audit_records", "active_grants",
        })
        source = ROOT / "src" / "aik_ipc" / "desktop.cjs"
        if not source.is_file():
            self.skipTest("Desktop IPC source is unavailable.")
        match = re.search(r"const COMMANDS = new Set\(\[([^\]]*)\]\)", source.read_text(encoding="utf-8"))
        self.assertIsNotNone(match)
        self.assertEqual(sorted(re.findall(r'"([^"]+)"', match.group(1))),
                         ["protocol.handshake", "system.status"])

    def test_unregistered_adapter_handle_loses_access(self) -> None:
        grant = self.grant()
        self.assertTrue(self.broker.unregister_adapter(self.adapter))
        self.assert_code(BrokerDenied, "ADAPTER_NOT_ALLOWED", self.inject, grant)

    def test_ended_lease_rejects_issue_and_injection(self) -> None:
        grant = self.grant(max_uses=2)
        self.leases.active.discard(("lease-1", 1))
        self.assert_code(BrokerDenied, "LEASE_INACTIVE", self.inject, grant)
        self.assert_code(BrokerDenied, "LEASE_INACTIVE", self.grant)
        self.assert_code(BrokerDenied, "LEASE_INACTIVE", self.grant, lease_id="lease-2", fencing_token=2)

    def test_revoked_task_and_lease_grants_are_rejected(self) -> None:
        first = self.grant()
        second = self.grant(lease_id="lease-2", task_id="task-2")
        self.assertEqual(self.broker.revoke_task("task-1"), 1)
        self.assertEqual(self.broker.revoke_lease("lease-2"), 1)
        self.assert_code(BrokerDenied, "GRANT_REVOKED", self.inject, first)
        self.assert_code(BrokerDenied, "GRANT_REVOKED", self.inject, second)
        self.assert_code(BrokerDenied, "GRANT_UNKNOWN", self.broker.inject, self.adapter, "grant-unknown",
                         workspace_id="ws-1", task_id="task-1", connection_id="conn-a", consumer=lambda s: None)

    def test_unverified_rebound_and_non_api_connections_are_rejected(self) -> None:
        grant = self.grant(max_uses=3)
        self.connections.add(api_connection("conn-a", "ws-1", self.ref_a, verified=False))
        self.assert_code(BrokerDenied, "AUTH_REQUIRED", self.inject, grant)
        self.connections.add(api_connection("conn-a", "ws-1", self.ref_a, binding_revision=2))
        self.assert_code(BrokerDenied, "BINDING_CHANGED", self.inject, grant)
        self.connections.add(Connection(
            "conn-sub", "ws-1", "vendor", "subscription", "host_session", "subscription", "vendor-cli",
            "default", "account-sub", None, "2030-01-01T00:00:00Z", 1, "prov-sub"))
        self.assert_code(BrokerDenied, "NO_BROKER_SECRET", self.grant, connection_ids=("conn-sub",))
        self.connections.add(api_connection("conn-x", "ws-1", self.ref_b, provider="othervendor"))
        self.assert_code(BrokerDenied, "PROVIDER_MISMATCH", self.grant, connection_ids=("conn-x",))
        self.assert_code(BrokerDenied, "CONNECTION_UNAVAILABLE", self.grant, connection_ids=("conn-missing",))

    def test_invalid_grant_parameters_are_rejected(self) -> None:
        for overrides in ({"ttl_seconds": 0}, {"ttl_seconds": 4000}, {"ttl_seconds": True},
                          {"max_uses": 0}, {"max_uses": 17}, {"connection_ids": ()},
                          {"connection_ids": ("conn-a", "conn-a")}, {"fencing_token": 0},
                          {"workspace_id": "../ws"}):
            with self.subTest(overrides=overrides):
                self.assert_code(BrokerDenied, "INVALID_REQUEST", self.grant, **overrides)
        self.assert_code(BrokerDenied, "ADAPTER_NOT_ALLOWED", self.grant, adapter_id="adapter-missing")

    def test_runtime_lease_checker_follows_state_token_and_expiry(self) -> None:
        store = RuntimeStore(self.root / "runtime" / "runtime.sqlite3")
        lease = Lease("lease-rt", "placement-1", "pool-1", "worker-1", 1, "2030-01-01T00:00:00Z", state="active")
        with store.transaction() as tx:
            tx.save(Pool("pool-1", "synthetic", "connection-1"), expected_revision=0)
            tx.save(ExecutionProfile("profile-1", "pool-1", "worker", "synthetic-model"), expected_revision=0)
            tx.save(Placement("placement-1", "pool-1", "profile-1", "vm-instance-1", "vm-project-1",
                              "vm-task-1", "vm-attempt-1"), expected_revision=0)
            tx.save(lease, expected_revision=0)
        now = [datetime(2029, 1, 1, tzinfo=timezone.utc)]
        checker = RuntimeLeaseChecker(store, now=lambda: now[0])
        self.assertTrue(checker("lease-rt", 1))
        self.assertFalse(checker("lease-rt", 2))
        self.assertFalse(checker("lease-missing", 1))
        broker = CredentialBroker(self.vault, self.connections, checker, clock=self.clock)
        adapter = broker.register_adapter("adapter-vendor", "vendor")
        grant = broker.issue_grant(workspace_id="ws-1", task_id="task-1", adapter_id="adapter-vendor",
                                   connection_ids=("conn-a",), lease_id="lease-rt", fencing_token=1,
                                   ttl_seconds=30, max_uses=3)
        store.save(replace(lease, state="released"), expected_revision=1)
        with self.assertRaises(BrokerDenied) as caught:
            broker.inject(adapter, grant.grant_id, workspace_id="ws-1", task_id="task-1",
                          connection_id="conn-a", consumer=lambda s: None)
        self.assertEqual(caught.exception.code, "LEASE_INACTIVE")
        store.save(lease, expected_revision=2)
        now[0] = datetime(2031, 1, 1, tzinfo=timezone.utc)
        self.assertFalse(checker("lease-rt", 1))
        with self.assertRaises(BrokerDenied) as caught:
            broker.inject(adapter, grant.grant_id, workspace_id="ws-1", task_id="task-1",
                          connection_id="conn-a", consumer=lambda s: None)
        self.assertEqual(caught.exception.code, "LEASE_INACTIVE")


class LeakBoundaryTests(BrokerTestCase):
    def test_returning_the_secret_is_blocked(self) -> None:
        for leaking in (lambda s: s, lambda s: "prefix " + s, lambda s: s.encode("utf-8"),
                        lambda s: {"value": [s]},
                        lambda s: base64.b64encode(s.encode()).decode("ascii")):
            with self.subTest():
                grant = self.grant()
                self.assert_code(SecretLeakBlocked, "SECRET_IN_OUTPUT", self.inject, grant, consumer=leaking)

    def test_adapter_exception_carrying_secret_is_replaced_without_chain(self) -> None:
        grant = self.grant()

        def failing(secret):
            raise RuntimeError("provider rejected " + secret)

        error = self.assert_code(AdapterFailed, "ADAPTER_FAILED", self.inject, grant, consumer=failing)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_vault_failure_is_reported_without_secret(self) -> None:
        grant = self.grant()
        self.vault.delete(self.ref_a)
        error = self.assert_code(CredentialUnavailable, "CREDENTIAL_UNAVAILABLE", self.inject, grant)
        self.assertIsNone(error.__context__)

    def test_audit_logs_and_sink_contain_ids_but_no_secret(self) -> None:
        with self.assertLogs("aik_broker", level="INFO") as logs:
            grant = self.grant(max_uses=2)
            self.inject(grant)
            try:
                self.inject(grant, consumer=lambda s: s)
            except SecretLeakBlocked:
                pass
            try:
                self.inject(grant)
            except BrokerDenied:
                pass
        self.assert_no_secret("\n".join(logs.output))
        records = self.broker.audit_records()
        self.assert_no_secret(repr(records))
        self.assert_no_secret(json.dumps(self.sink, ensure_ascii=False))
        events = [(record.event, record.code) for record in records]
        self.assertIn(("resolve_allowed", None), events)
        self.assertIn(("injection_failed", "SECRET_IN_OUTPUT"), events)
        self.assertIn(("resolve_denied", "GRANT_EXHAUSTED"), events)
        allowed = [record for record in records if record.event == "resolve_allowed"]
        self.assertEqual({(r.grant_id, r.workspace_id, r.task_id, r.adapter_id, r.connection_id) for r in allowed},
                         {(grant.grant_id, "ws-1", "task-1", "adapter-vendor", "conn-a")})
        self.assertTrue(all(entry["kind"] == "log" for entry in self.sink))

    def test_failing_audit_sink_blocks_resolution(self) -> None:
        def broken_sink(_payload):
            raise OSError("synthetic audit outage")

        broker = CredentialBroker(self.vault, self.connections, self.leases, clock=self.clock,
                                  audit_sink=broken_sink)
        adapter = broker.register_adapter("adapter-vendor", "vendor")
        grant = broker.issue_grant(workspace_id="ws-1", task_id="task-1", adapter_id="adapter-vendor",
                                   connection_ids=("conn-a",), lease_id="lease-1", fencing_token=1)
        calls = []
        with self.assertRaises(Exception) as caught:
            broker.inject(adapter, grant.grant_id, workspace_id="ws-1", task_id="task-1",
                          connection_id="conn-a", consumer=calls.append)
        self.assertEqual(getattr(caught.exception, "code", None), "AUDIT_UNAVAILABLE")
        self.assertEqual(calls, [])


class ChildInjectionTests(BrokerTestCase):
    def scan_files_for_secret(self, directory: Path) -> None:
        for path in directory.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                for secret in ALL_SECRETS:
                    self.assertNotIn(secret.encode("utf-8"), data)

    def test_stdin_delivery_through_broker_redacts_child_output(self) -> None:
        grant = self.grant()
        result = self.broker.run_with_credential(
            self.adapter, grant.grant_id, workspace_id="ws-1", task_id="task-1", connection_id="conn-a",
            argv=[sys.executable, "-c", READ_STDIN], cwd=self.work, timeout=30)
        self.assertIsInstance(result, ChildResult)
        self.assertEqual(result.returncode, 0)
        self.assertIn(digest(SECRET), result.stdout)
        self.assertIn("[REDACTED]", result.stdout)
        self.assert_no_secret(result.stdout + result.stderr)
        self.scan_files_for_secret(self.work)

    def test_env_delivery_uses_private_copy_and_leaves_parent_environment_clean(self) -> None:
        name = "AIK_SYNTHETIC_INJECTED"
        result = run_injected([sys.executable, "-c", READ_ENV, name], SECRET, delivery="env",
                              env_name=name, cwd=self.work, timeout=30)
        self.assertEqual(result.returncode, 0)
        self.assertIn(digest(SECRET), result.stdout)
        self.assertIn("[REDACTED]", result.stderr)
        self.assert_no_secret(result.stdout + result.stderr)
        self.assertNotIn(name, os.environ)
        self.assertFalse(any(SECRET in value for value in os.environ.values()))
        self.scan_files_for_secret(self.work)

    def test_secret_in_argv_or_base_environment_is_rejected_before_start(self) -> None:
        marker = self.work / "started.marker"
        script = f"open({str(marker)!r}, 'w').close()"
        encoded = base64.b64encode(SECRET.encode()).decode("ascii")
        for argv in ([sys.executable, "-c", script, SECRET], [sys.executable, "-c", script, "--key=" + encoded]):
            with self.subTest(argv_kind=argv[-1][:5]):
                self.assert_code(InjectionError, "ARGV_CONTAINS_SECRET", run_injected, argv, SECRET)
        self.assert_code(InjectionError, "ENV_CONTAINS_SECRET", run_injected, [sys.executable, "-c", script],
                         SECRET, base_env={"LEAKED": SECRET})
        self.assertFalse(marker.exists())

    def test_broker_reports_child_failures_with_fixed_codes(self) -> None:
        grant = self.grant()
        error = self.assert_code(AdapterFailed, "ARGV_CONTAINS_SECRET", self.broker.run_with_credential,
                                 self.adapter, grant.grant_id, workspace_id="ws-1", task_id="task-1",
                                 connection_id="conn-a", argv=[sys.executable, "-c", "pass", SECRET])
        self.assertIsNone(error.__context__)
        self.assert_code(BrokerDenied, "INVALID_REQUEST", self.broker.run_with_credential,
                         self.adapter, grant.grant_id, workspace_id="ws-1", task_id="task-1",
                         connection_id="conn-a", argv=[], delivery="stdin")

    @unittest.skipIf(os.name == "nt", "POSIX process-group cleanup; Windows cleanup is NOT_RUN.")
    def test_timeout_kills_child_and_its_process_group(self) -> None:
        pid_file = self.work / "grandchild.pid"
        started = time.monotonic()
        self.assert_code(InjectionError, "CHILD_TIMEOUT", run_injected,
                         [sys.executable, "-c", SPAWN_GRANDCHILD, str(pid_file)], SECRET,
                         cwd=self.work, timeout=3)
        self.assertLess(time.monotonic() - started, 20)
        self.assertTrue(pid_file.is_file(), "The child did not start its grandchild.")
        pid = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 5
        while process_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(process_alive(pid))


if __name__ == "__main__":
    unittest.main()
