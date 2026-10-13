"""Supervisor single-instance, lifecycle and restart tests with synthetic data.

Real child processes contend for the lock and one is SIGKILLed. Linux results
are development verification only; the Windows branch is mock-tested and the
real Windows behavior is NOT_RUN.
"""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_runtime import (  # noqa: E402
    ExecutionProfile, Lease, Placement, Pool, RuntimeStore, StorageError, UnsafeStoragePath, Versioned,
)
from aik_supervisor import (  # noqa: E402
    Attachment, InvalidEndpoint, PlacementRejected, Supervisor, SupervisorError,
    SupervisorUnavailable, default_endpoint, default_state_root, instance_paths, launch,
    read_endpoint, try_acquire,
)
from aik_supervisor import instance as instance_module  # noqa: E402
from aik_supervisor import supervisor as supervisor_module  # noqa: E402


CHILD = r'''
import json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from aik_supervisor import launch
root, go, stop = Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])
deadline = time.monotonic() + 30
while not go.exists() and time.monotonic() < deadline:
    time.sleep(0.005)
result = launch("ws-alpha", state_root=root, attach_timeout=10)
print(json.dumps({"role": result.role, "instance_id": result.record.instance_id}), flush=True)
if result.role == "owner":
    deadline = time.monotonic() + 30
    while not stop.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    result.shutdown()
'''
STORE_CHILD = r'''
import json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from aik_runtime import RuntimeStore
from aik_supervisor import launch
store = RuntimeStore(Path(sys.argv[3]))
result = launch("ws-alpha", state_root=Path(sys.argv[2]), store=store, attach_timeout=10)
print(json.dumps({"role": result.role, "instance_id": result.record.instance_id,
                  "targets": [entry.value.id for entry in result.observation_targets]}), flush=True)
time.sleep(60)
'''
FORBIDDEN_WORDS = ("secret", "token", "password", "api_key", "apikey", "credential", "authorization")


class SupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aik-supervisor-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"

    def own(self, **kwargs) -> Supervisor:
        result = launch("ws-alpha", state_root=self.state, attach_timeout=0, **kwargs)
        self.assertIsInstance(result, Supervisor)
        self.addCleanup(result.shutdown)
        return result

    def spawn(self, go: Path, stop: Path) -> subprocess.Popen:
        process = subprocess.Popen(
            [sys.executable, "-c", CHILD, str(ROOT / "src"), str(self.state), str(go), str(stop)],
            cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        )
        self.addCleanup(lambda: process.poll() is None and (process.kill(), process.communicate(timeout=5)))
        return process

    def read_result(self, process: subprocess.Popen) -> dict:
        line = process.stdout.readline()
        if not line:
            self.fail("Child exited without a result: " + process.stderr.read())
        return json.loads(line)

    def seed_store(self) -> tuple[RuntimeStore, Lease]:
        """Synthetic graph with one active (in-flight) lease."""
        store = RuntimeStore(self.root / "runtime" / "runtime.sqlite3")
        active = Lease("lease-1", "placement-1", "pool-1", "worker-1", 1, "2030-01-01T00:00:00Z", state="active")
        with store.transaction() as tx:
            tx.save(Pool("pool-1", "synthetic", "connection-1"))
            tx.save(ExecutionProfile("profile-1", "pool-1", "worker", "synthetic-model"))
            tx.save(Placement("placement-1", "pool-1", "profile-1", "vm-instance-1", "vm-project-1",
                              "vm-task-1", "vm-attempt-1", state="assigned"))
            tx.save(active)
        return store, active

    @staticmethod
    def new_placement(number: int) -> Placement:
        return Placement(f"placement-new-{number}", "pool-1", "profile-1", "vm-instance-1",
                         "vm-project-1", "vm-task-1", f"vm-attempt-new-{number}")

    def test_two_concurrent_processes_produce_one_owner_and_one_attachment(self) -> None:
        go, stop = self.root / "go", self.root / "stop"
        processes = [self.spawn(go, stop), self.spawn(go, stop)]
        go.write_text("go", encoding="utf-8")
        results = [self.read_result(process) for process in processes]
        self.assertEqual(sorted(result["role"] for result in results), ["attached", "owner"])
        self.assertEqual(results[0]["instance_id"], results[1]["instance_id"])
        stop.write_text("stop", encoding="utf-8")
        for process in processes:
            process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0)
        self.assertFalse(instance_paths("ws-alpha", state_root=self.state).endpoint.exists())

    def test_closing_and_reopening_the_window_attaches_to_the_same_runtime(self) -> None:
        owner = self.own()
        owner.close_window()
        self.assertEqual((owner.lifecycle, owner.window), ("RUNNING", "WINDOW_CLOSED"))
        second = launch("ws-alpha", state_root=self.state, attach_timeout=1)
        self.assertIsInstance(second, Attachment)
        self.assertEqual(second.record.instance_id, owner.instance_id)
        owner.open_window()
        self.assertEqual(owner.window, "WINDOW_OPEN")
        # A different workspace is a different instance.
        other = launch("ws-beta", state_root=self.state, attach_timeout=0)
        self.addCleanup(other.shutdown)
        self.assertIsInstance(other, Supervisor)
        self.assertNotEqual(other.instance_id, owner.instance_id)

    def test_sigkilled_owner_releases_lock_and_restart_replaces_stale_endpoint(self) -> None:
        go, stop = self.root / "go", self.root / "never-stop"
        go.write_text("go", encoding="utf-8")
        process = self.spawn(go, stop)
        old = self.read_result(process)
        self.assertEqual(old["role"], "owner")
        endpoint = instance_paths("ws-alpha", state_root=self.state).endpoint
        process.kill()
        process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(read_endpoint(endpoint).instance_id, old["instance_id"])
        restarted = self.own()
        self.assertTrue(restarted.stale_endpoint_replaced)
        self.assertEqual(restarted.previous_instance_id, old["instance_id"])
        self.assertNotEqual(restarted.instance_id, old["instance_id"])
        current = read_endpoint(endpoint)
        self.assertEqual((current.instance_id, current.pid), (restarted.instance_id, os.getpid()))

    def test_pause_and_shutdown_reject_new_placements_and_keep_in_flight_leases(self) -> None:
        store = RuntimeStore(self.root / "runtime" / "runtime.sqlite3")
        placement = Placement("placement-1", "pool-1", "profile-1", "vm-instance-1", "vm-project-1",
                              "vm-task-1", "vm-attempt-1", state="assigned")
        active = Lease("lease-1", "placement-1", "pool-1", "worker-1", 1, "2030-01-01T00:00:00Z", state="active")
        with store.transaction() as tx:
            tx.save(Pool("pool-1", "synthetic", "connection-1"))
            tx.save(ExecutionProfile("profile-1", "pool-1", "worker", "synthetic-model"))
            tx.save(placement)
            tx.save(active)
            tx.save(Lease("lease-done", "placement-1", "pool-1", "worker-1", 2,
                          "2030-01-01T00:00:00Z", state="released"))
        new = lambda n: Placement(f"placement-new-{n}", "pool-1", "profile-1", "vm-instance-1",
                                  "vm-project-1", "vm-task-1", f"vm-attempt-new-{n}")
        owner = self.own(store=store)
        self.assertEqual([entry.value.id for entry in owner.observation_targets], ["lease-1"])
        owner.pause()
        with self.assertRaises(PlacementRejected):
            owner.admit_placement(new(1))
        self.assertIsNone(store.get(Placement, "placement-new-1"))
        owner.resume()
        self.assertEqual(owner.admit_placement(new(2)).revision, 1)
        report = owner.shutdown()
        self.assertEqual((report.in_flight_lease_ids, report.scan_complete), (("lease-1",), True))
        self.assertEqual(owner.lifecycle, "STOPPED")
        with self.assertRaises(PlacementRejected):
            owner.admit_placement(new(3))
        with self.assertRaises(SupervisorError):
            owner.open_window()
        self.assertEqual(store.get(Lease, "lease-1"), Versioned(active, 1))
        self.assertFalse(owner.paths.endpoint.exists())
        restarted = self.own(store=store)
        self.assertFalse(restarted.stale_endpoint_replaced)
        self.assertEqual([entry.value.id for entry in restarted.observation_targets], ["lease-1"])

    def test_invalid_workspace_ids_and_unsafe_state_roots_are_rejected(self) -> None:
        for workspace in ("", "../x", "a/b", "x" * 65, "ws.alpha", "CON", 7):
            with self.subTest(workspace=workspace):
                with self.assertRaises(ValueError):
                    launch(workspace, state_root=self.state, attach_timeout=0)
        with self.assertRaises(ValueError):
            launch("ws-alpha", component="Bad/Component", state_root=self.state)
        inside = ROOT / "supervisor-test-state-must-not-exist"
        for root in ("relative/state", inside):
            with self.subTest(root=str(root)):
                with self.assertRaises(UnsafeStoragePath):
                    launch("ws-alpha", state_root=root, attach_timeout=0)
        self.assertFalse(inside.exists())

    def test_endpoint_record_has_fixed_fields_and_no_secret_keywords(self) -> None:
        owner = self.own()
        raw = owner.paths.endpoint.read_text(encoding="utf-8")
        data = json.loads(raw)
        self.assertEqual(set(data), {"format_version", "instance_id", "workspace_id", "component",
                                     "pid", "started_at", "endpoint"})
        self.assertEqual(data["endpoint"]["kind"], "named-pipe" if os.name == "nt" else "unix-socket")
        for word in FORBIDDEN_WORDS:
            self.assertNotIn(word, raw.lower())
        forged = dict(data, token="synthetic-value")
        owner.paths.endpoint.write_text(json.dumps(forged), encoding="utf-8")
        with self.assertRaises(InvalidEndpoint):
            read_endpoint(owner.paths.endpoint)

    def test_windows_branch_uses_msvcrt_locking_with_mocks(self) -> None:
        paths = instance_paths("ws-win", state_root=self.state, create=True)
        calls = []
        held = {"value": False}

        def locking(descriptor, mode, size):
            calls.append((mode, size))
            if mode == 2 and held["value"]:
                raise OSError(errno.EACCES, "synthetic locked region")
            held["value"] = mode == 2

        fake = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=locking)
        with mock.patch.dict(sys.modules, {"msvcrt": fake}):
            lock = try_acquire(paths, os_name="nt")
            self.assertIsNotNone(lock)
            self.assertIsNone(try_acquire(paths, os_name="nt"))
            lock.release()
        self.assertEqual(calls, [(2, 1), (2, 1), (0, 1)])
        base = self.root / "LocalAppData"
        self.assertEqual(default_state_root("nt", {"LOCALAPPDATA": str(base)}), base / "AIK" / "supervisor")
        with self.assertRaises(UnsafeStoragePath):
            default_state_root("nt", {})
        endpoint = default_endpoint(paths, "ws-win", "director", "0" * 32, os_name="nt")
        self.assertEqual(endpoint.kind, "named-pipe")
        self.assertTrue(endpoint.address.startswith("\\\\.\\pipe\\aik-director-ws-win-"))

    # --- Regression tests for the independent review (F1, F2, F5, F6, F9) ---

    def test_without_store_shutdown_and_startup_report_unknown_not_empty(self) -> None:
        # F1: no store means nothing was scanned; this must not read as "none".
        owner = self.own()
        self.assertFalse(owner.observation_scan_complete)
        self.assertEqual(owner.observation_targets, ())
        report = owner.shutdown()
        self.assertEqual((report.in_flight_lease_ids, report.scan_complete), ((), False))
        store, _ = self.seed_store()
        with_store = self.own(store=store)
        self.assertTrue(with_store.observation_scan_complete)
        self.assertEqual([entry.value.id for entry in with_store.observation_targets], ["lease-1"])
        self.assertTrue(with_store.shutdown().scan_complete)

    def test_shutdown_withdraws_endpoint_before_scan_so_relaunch_cannot_attach_to_dying_owner(self) -> None:
        # F2: during the shutdown scan the endpoint must already be gone.
        store, _ = self.seed_store()
        owner = self.own(store=store)
        observed = {}
        real_scan = supervisor_module._scan_in_flight

        def probe(scan_store):
            observed["endpoint_exists"] = owner.paths.endpoint.exists()
            try:
                result = launch("ws-alpha", state_root=self.state, attach_timeout=0)
            except SupervisorUnavailable:
                observed["relaunch"] = "unavailable"
            else:
                observed["relaunch"] = result.role
                if isinstance(result, Supervisor):
                    result.shutdown()
            return real_scan(scan_store)

        with mock.patch.object(supervisor_module, "_scan_in_flight", side_effect=probe):
            report = owner.shutdown()
        self.assertEqual(observed, {"endpoint_exists": False, "relaunch": "unavailable"})
        self.assertEqual((report.in_flight_lease_ids, report.scan_complete), (("lease-1",), True))
        relaunched = self.own(store=store)
        self.assertNotEqual(relaunched.instance_id, owner.instance_id)
        self.assertFalse(relaunched.stale_endpoint_replaced)

    def test_deeply_nested_or_recursive_endpoint_is_invalid_and_replaced_by_new_owner(self) -> None:
        # F5: parser recursion failures are malformed records, not launch failures.
        paths = instance_paths("ws-alpha", state_root=self.state, create=True)
        paths.endpoint.write_text("[" * 4000, encoding="ascii")
        with self.assertRaises(InvalidEndpoint):
            read_endpoint(paths.endpoint)
        with mock.patch.object(instance_module.json, "loads", side_effect=RecursionError("synthetic depth")):
            with self.assertRaises(InvalidEndpoint):
                read_endpoint(paths.endpoint)
            owner = launch("ws-alpha", state_root=self.state, attach_timeout=0)
        self.addCleanup(owner.shutdown)
        self.assertIsInstance(owner, Supervisor)
        self.assertTrue(owner.stale_endpoint_replaced)
        self.assertIsNone(owner.previous_instance_id)
        self.assertEqual(read_endpoint(paths.endpoint).instance_id, owner.instance_id)

    def test_workspace_ids_differing_only_in_case_share_one_owner(self) -> None:
        # F6: identical semantics on case-insensitive and case-sensitive filesystems.
        owner = launch("WS-alpha", state_root=self.state, attach_timeout=0)
        self.addCleanup(owner.shutdown)
        self.assertIsInstance(owner, Supervisor)
        second = launch("ws-alpha", state_root=self.state, attach_timeout=0)
        if isinstance(second, Supervisor):
            self.addCleanup(second.shutdown)
        self.assertIsInstance(second, Attachment)
        self.assertEqual(second.record.instance_id, owner.instance_id)
        self.assertEqual(instance_paths("WS-alpha", state_root=self.state),
                         instance_paths("ws-alpha", state_root=self.state))

    def test_shutdown_scan_failure_reports_unknown_and_still_releases_lock(self) -> None:
        # F9: scan_complete=False path.
        store, active = self.seed_store()
        owner = self.own(store=store)
        with mock.patch.object(RuntimeStore, "transaction", side_effect=StorageError("synthetic scan failure")):
            report = owner.shutdown()
        self.assertEqual((report.in_flight_lease_ids, report.scan_complete), ((), False))
        self.assertEqual(owner.lifecycle, "STOPPED")
        self.assertFalse(owner.paths.endpoint.exists())
        self.assertEqual(store.get(Lease, "lease-1"), Versioned(active, 1))
        self.own(store=store)

    def test_startup_scan_failure_fails_closed_and_releases_the_lock(self) -> None:
        # F9: no owner, no endpoint, and the next launch can become owner.
        store, _ = self.seed_store()
        with mock.patch.object(RuntimeStore, "transaction", side_effect=StorageError("synthetic scan failure")):
            with self.assertRaises(SupervisorError):
                launch("ws-alpha", state_root=self.state, store=store, attach_timeout=0)
        self.assertFalse(instance_paths("ws-alpha", state_root=self.state).endpoint.exists())
        owner = self.own(store=store)
        self.assertEqual([entry.value.id for entry in owner.observation_targets], ["lease-1"])

    def test_closed_window_does_not_stop_placement_admission(self) -> None:
        # F9: window and lifecycle axes are independent.
        store, _ = self.seed_store()
        owner = self.own(store=store)
        owner.close_window()
        self.assertEqual(owner.admit_placement(self.new_placement(1)).revision, 1)
        owner.pause()
        with self.assertRaises(PlacementRejected):
            owner.admit_placement(self.new_placement(2))
        self.assertEqual((owner.lifecycle, owner.window), ("PAUSED", "WINDOW_CLOSED"))

    def test_window_reopened_from_separate_process_attaches_to_owner(self) -> None:
        # F9: a second GUI process after the window closed attaches, not owns.
        owner = self.own()
        owner.close_window()
        go = self.root / "go"
        go.write_text("go", encoding="utf-8")
        process = self.spawn(go, self.root / "stop")
        result = self.read_result(process)
        process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(result, {"role": "attached", "instance_id": owner.instance_id})
        self.assertEqual(owner.lifecycle, "RUNNING")

    def test_killed_owner_with_store_leaves_active_lease_as_restart_observation_target(self) -> None:
        # F9: SIGKILL (POSIX) followed by restart observes the in-flight lease.
        store, active = self.seed_store()
        process = subprocess.Popen(
            [sys.executable, "-c", STORE_CHILD, str(ROOT / "src"), str(self.state), str(store.path)],
            cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        )
        self.addCleanup(lambda: process.poll() is None and (process.kill(), process.communicate(timeout=5)))
        old = self.read_result(process)
        self.assertEqual((old["role"], old["targets"]), ("owner", ["lease-1"]))
        process.kill()
        process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)
        restarted = self.own(store=store)
        self.assertTrue(restarted.stale_endpoint_replaced)
        self.assertEqual(restarted.previous_instance_id, old["instance_id"])
        self.assertTrue(restarted.observation_scan_complete)
        self.assertEqual([entry.value.id for entry in restarted.observation_targets], ["lease-1"])
        self.assertEqual(store.get(Lease, "lease-1"), Versioned(active, 1))


if __name__ == "__main__":
    unittest.main()
