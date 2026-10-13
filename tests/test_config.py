"""Exercise AIK settings against disposable SQLite files outside the repository.

Only synthetic IDs, roots and markers are used. No provider, credential or
network is touched. Linux results are development verification, not Windows
support evidence.
"""
from __future__ import annotations

from contextlib import closing
import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_config import (  # noqa: E402
    AgentProfile, ConnectionProfile, CrossWorkspaceReference, ExecutionTarget, InvalidDatabase,
    InvalidReference, InvalidSetting, InvalidSettingsDocument, NoActiveWorkspace, PermissionDefaults,
    RevisionConflict, Revisioned, SettingsStore, StorageBusy, UnsafeStoragePath, Workspace,
    WorkspaceBinding, WorkspaceBindingMismatch, WorkspaceBindingRequired, WorkspaceLimits,
    default_settings_path,
)
from aik_runtime import RuntimeStore  # noqa: E402
from aik_runtime import paths as runtime_paths  # noqa: E402


SECRET_MARKER = "SYNTHETIC_SECRET_MUST_NOT_BE_STORED"
ALPHA_ROOT = "/synthetic/roots/alpha"


class FakeAuthority:
    """Stands in for the unimplemented AIK-084 binding authority."""

    def __init__(self, bindings=None, fail: bool = False):
        self.bindings = dict(bindings or {})
        self.fail = fail

    def binding_for(self, workspace_id):
        if self.fail:
            raise RuntimeError("synthetic authority outage")
        return self.bindings.get(workspace_id)


def seed(store: SettingsStore) -> None:
    with store.transaction() as tx:
        tx.save(Workspace("ws-alpha", "알파 작업공간", ALPHA_ROOT, "vm-project-alpha"))
        tx.save(Workspace("ws-beta", "Beta"))
        tx.save(ConnectionProfile("conn-alpha", "ws-alpha", "synthetic", auth_mode="api_key"))
        tx.save(ConnectionProfile("conn-alpha-local", "ws-alpha", "synthetic-local"))
        tx.save(ConnectionProfile("conn-beta", "ws-beta", "synthetic", auth_mode="host_session"))
        tx.save(AgentProfile("director-alpha", "ws-alpha", "director", "conn-alpha", "synthetic-model", effort="high"))
        tx.save(AgentProfile("worker-alpha", "ws-alpha", "worker", "conn-alpha-local", "synthetic-small"))
        tx.save(WorkspaceLimits("ws-alpha", max_parallel_workers=3, budget_limit=5000,
                                budget_unit="usd_cents", budget_period="monthly"))
        tx.save(PermissionDefaults("ws-alpha", file_write="allow", shell_command="ask", network="deny"))


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aik-config-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "설정 공간" / "settings.sqlite3"

    def assert_integrity(self, path: Path | None = None) -> None:
        with closing(sqlite3.connect((path or self.database).as_uri() + "?mode=ro", uri=True)) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_settings_persist_across_restart_with_kinds_stored_separately(self) -> None:
        seed(SettingsStore(self.database))
        reopened = SettingsStore(self.database)
        alpha = reopened.workspace_settings("ws-alpha")
        self.assertEqual(alpha.workspace, Revisioned(Workspace("ws-alpha", "알파 작업공간", ALPHA_ROOT, "vm-project-alpha"), 1))
        self.assertEqual([c.value.id for c in alpha.connections], ["conn-alpha", "conn-alpha-local"])
        self.assertEqual([p.value.id for p in alpha.directors], ["director-alpha"])
        self.assertEqual([p.value.id for p in alpha.workers], ["worker-alpha"])
        self.assertEqual(alpha.limits, Revisioned(WorkspaceLimits("ws-alpha", 3, 5000, "usd_cents", "monthly"), 1))
        self.assertEqual(alpha.permissions.value.file_write, "allow")
        beta = reopened.workspace_settings("ws-beta")
        self.assertEqual(beta.limits, Revisioned(WorkspaceLimits("ws-beta"), 0))
        self.assertEqual(beta.permissions, Revisioned(PermissionDefaults("ws-beta"), 0))
        self.assertEqual(beta.permissions.value.network, "deny")
        self.assertEqual(reopened.save(beta.permissions.value, expected_revision=0).revision, 1)
        self.assertEqual(len(reopened.list(ConnectionProfile, workspace_id="ws-beta")), 1)
        self.assertIsNone(reopened.workspace_settings("ws-missing"))
        self.assert_integrity()

    def test_invalid_values_are_rejected_before_storage(self) -> None:
        for bad in ({"max_parallel_workers": 0}, {"max_parallel_workers": -1}, {"max_parallel_workers": 1.5},
                    {"max_parallel_workers": True}, {"max_parallel_workers": "2"}, {"budget_limit": 0},
                    {"budget_limit": -5}, {"budget_limit": 2.0}, {"budget_limit": False},
                    {"budget_unit": "euros"}, {"budget_period": "hourly"}):
            with self.subTest(limits=bad):
                with self.assertRaises(InvalidSetting):
                    WorkspaceLimits("ws", **bad)
        invalid = (
            lambda: AgentProfile("p", "ws", "reviewer", "c", "m"),
            lambda: ConnectionProfile("c", "ws", "p", auth_mode="none", needs_authentication=True),
            lambda: ConnectionProfile("c", "ws", "p", auth_mode="password"),
            lambda: Workspace("ws", "n", "relative/root", "vm"),
            lambda: Workspace("ws", "n", "/a/../b", "vm"),
            lambda: Workspace("ws", "n", "\\\\server\\share", "vm"),
            lambda: Workspace("ws", "n", "/root-only"),
            lambda: Workspace("ws", ""),
            lambda: Workspace("ws", "bad\x00name"),
            lambda: PermissionDefaults("ws", file_write="sometimes"),
            lambda: Workspace("bad id", "n"),
        )
        for number, factory in enumerate(invalid):
            with self.subTest(case=number):
                with self.assertRaises(InvalidSetting):
                    factory()
        store = SettingsStore(self.database)
        store.save(Workspace("ws-alpha", "Alpha"))
        forged = PermissionDefaults("ws-alpha")
        object.__setattr__(forged, "network", "always")
        with self.assertRaises(InvalidSetting):
            store.save(forged)
        with self.assertRaises(InvalidSetting):
            store.save({"workspace_id": "ws-alpha", "network": "allow"})
        with self.assertRaises(TypeError):
            ConnectionProfile("c", "ws", "p", api_key=SECRET_MARKER)
        self.assertIsNone(store.get(PermissionDefaults, "ws-alpha"))
        self.assertNotIn(SECRET_MARKER.encode(), self.database.read_bytes())

    def test_missing_and_cross_workspace_references_are_blocked(self) -> None:
        store = SettingsStore(self.database)
        seed(store)
        with self.assertRaises(CrossWorkspaceReference):
            store.save(AgentProfile("worker-beta", "ws-beta", "worker", "conn-alpha", "synthetic-model"))
        with self.assertRaises(InvalidReference):
            store.save(AgentProfile("worker-missing", "ws-alpha", "worker", "conn-missing", "synthetic-model"))
        with self.assertRaises(InvalidReference):
            store.save(ConnectionProfile("conn-orphan", "ws-missing", "synthetic"))
        with self.assertRaises(InvalidReference):
            store.save(WorkspaceLimits("ws-missing", max_parallel_workers=2))
        with self.assertRaises(InvalidSetting):
            store.save(AgentProfile("worker-alpha", "ws-beta", "worker", "conn-beta", "synthetic-small"),
                       expected_revision=1)
        for model, key in ((AgentProfile, "worker-beta"), (AgentProfile, "worker-missing"),
                           (ConnectionProfile, "conn-orphan"), (WorkspaceLimits, "ws-missing")):
            self.assertIsNone(store.get(model, key))
        self.assertEqual(store.get(AgentProfile, "worker-alpha").value.workspace_id, "ws-alpha")
        with self.assertRaises(InvalidReference):
            store.delete(Workspace, "ws-alpha", expected_revision=1)
        with self.assertRaises(InvalidReference):
            store.delete(ConnectionProfile, "conn-alpha", expected_revision=1)
        # The database also enforces the composite workspace reference.
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO agent_profiles (id, workspace_id, role, connection_id, model, effort, revision) "
                    "VALUES ('p-raw', 'ws-beta', 'worker', 'conn-alpha', 'm', NULL, 1)"
                )
            connection.rollback()
        self.assert_integrity()

    def test_revision_conflicts_between_two_stores_preserve_newer_values(self) -> None:
        first = SettingsStore(self.database)
        seed(first)
        second = SettingsStore(self.database)
        stale = second.get(WorkspaceLimits, "ws-alpha")
        updated = replace(stale.value, max_parallel_workers=5)
        self.assertEqual(first.save(updated, expected_revision=1), Revisioned(updated, 2))
        with self.assertRaises(RevisionConflict):
            second.save(replace(stale.value, max_parallel_workers=7), expected_revision=1)
        with self.assertRaises(RevisionConflict):
            second.delete(WorkspaceLimits, "ws-alpha", expected_revision=1)
        with self.assertRaises(RevisionConflict):
            second.save(Workspace("ws-alpha", "Duplicate"), expected_revision=0)
        self.assertEqual(second.get(WorkspaceLimits, "ws-alpha"), Revisioned(updated, 2))
        second.delete(WorkspaceLimits, "ws-alpha", expected_revision=2)
        self.assertEqual(first.workspace_settings("ws-alpha").limits.revision, 0)

    def test_exception_rolls_back_every_write_in_a_transaction(self) -> None:
        store = SettingsStore(self.database)
        seed(store)
        with self.assertRaisesRegex(RuntimeError, "synthetic interruption"):
            with store.transaction() as tx:
                tx.save(WorkspaceLimits("ws-alpha", max_parallel_workers=9), expected_revision=1)
                tx.save(Workspace("ws-uncommitted", "Uncommitted"))
                raise RuntimeError("synthetic interruption")
        self.assertEqual(store.get(WorkspaceLimits, "ws-alpha").value.max_parallel_workers, 3)
        self.assertIsNone(store.get(Workspace, "ws-uncommitted"))

    def test_busy_writer_times_out_without_writing(self) -> None:
        first = SettingsStore(self.database)
        second = SettingsStore(self.database, timeout=0.05)
        with first.transaction() as tx:
            tx.save(Workspace("ws-held", "Held"))
            with self.assertRaises(StorageBusy):
                second.save(Workspace("ws-waiting", "Waiting"))
        self.assertIsNone(second.get(Workspace, "ws-waiting"))

    def test_export_excludes_secrets_and_bindings_and_import_marks_authentication(self) -> None:
        store = SettingsStore(self.database)
        seed(store)
        exported = store.export_settings()
        text = json.dumps(exported, ensure_ascii=False)
        self.assertFalse(exported["contains_secret_values"])
        self.assertEqual(exported["scope"], "settings")
        for excluded in ("secret_values", "canonical_root_binding", "vm_project_binding", "revisions_and_history"):
            self.assertIn(excluded, exported["excluded"])
        self.assertNotIn(ALPHA_ROOT, text)
        self.assertNotIn("vm-project-alpha", text)
        self.assertNotIn(SECRET_MARKER, text)
        markers = {c["id"]: c["requires_authentication"] for w in exported["workspaces"] for c in w["connections"]}
        self.assertEqual(markers, {"conn-alpha": True, "conn-alpha-local": False, "conn-beta": True})

        target = SettingsStore(self.root / "other machine" / "settings.sqlite3")
        report = target.import_settings(json.loads(text))
        self.assertEqual(report.connections_requiring_authentication, ("conn-alpha", "conn-beta"))
        self.assertEqual(report.workspaces_requiring_binding, ("ws-alpha", "ws-beta"))
        self.assertTrue(target.get(ConnectionProfile, "conn-alpha").value.needs_authentication)
        self.assertFalse(target.get(ConnectionProfile, "conn-alpha-local").value.needs_authentication)
        self.assertFalse(target.get(Workspace, "ws-alpha").value.bound)
        self.assertEqual(target.get(WorkspaceLimits, "ws-alpha").value, WorkspaceLimits("ws-alpha", 3, 5000))
        self.assertEqual(target.get(AgentProfile, "director-alpha").value.effort, "high")
        self.assertIsNone(target.get(PermissionDefaults, "ws-beta"))
        self.assertIsNone(target.active_workspace())
        target.select_workspace("ws-alpha")
        with self.assertRaises(WorkspaceBindingRequired):
            target.resolve_execution_target()

    def test_import_rejects_secret_fields_cross_references_and_conflicts_atomically(self) -> None:
        source = SettingsStore(self.database)
        seed(source)
        document = source.export_settings()
        target = SettingsStore(self.root / "target" / "settings.sqlite3")

        smuggled = copy.deepcopy(document)
        smuggled["workspaces"][0]["connections"][0]["api_key"] = SECRET_MARKER
        with self.assertRaises(InvalidSettingsDocument) as rejected:
            target.import_settings(smuggled)
        self.assertNotIn(SECRET_MARKER, str(rejected.exception))

        claims_secrets = copy.deepcopy(document)
        claims_secrets["contains_secret_values"] = True
        with self.assertRaises(InvalidSettingsDocument):
            target.import_settings(claims_secrets)

        inconsistent = copy.deepcopy(document)
        inconsistent["workspaces"][0]["connections"][0]["requires_authentication"] = False
        with self.assertRaises(InvalidSettingsDocument):
            target.import_settings(inconsistent)

        cross = copy.deepcopy(document)
        cross["workspaces"][1]["profiles"].append(
            {"id": "worker-beta", "role": "worker", "connection_id": "conn-alpha", "model": "m", "effort": None})
        with self.assertRaises(CrossWorkspaceReference):
            target.import_settings(cross)

        bad_value = copy.deepcopy(document)
        bad_value["workspaces"][0]["limits"]["max_parallel_workers"] = 0
        with self.assertRaises(InvalidSetting):
            target.import_settings(bad_value)
        self.assertEqual(target.list(Workspace), ())

        target.save(Workspace("ws-beta", "Existing"))
        with self.assertRaises(RevisionConflict):
            target.import_settings(document)
        self.assertEqual(target.list(Workspace), (Revisioned(Workspace("ws-beta", "Existing"), 1),))
        self.assertIsNone(target.get(ConnectionProfile, "conn-alpha"))
        self.assertNotIn(SECRET_MARKER.encode(), target.path.read_bytes())

    def test_execution_target_follows_selection_and_requires_matching_binding(self) -> None:
        plain = SettingsStore(self.database)
        seed(plain)
        with self.assertRaises(NoActiveWorkspace):
            plain.resolve_execution_target()
        self.assertEqual(plain.select_workspace("ws-alpha"), Revisioned("ws-alpha", 1))
        with self.assertRaises(WorkspaceBindingRequired):
            plain.resolve_execution_target()

        authority = FakeAuthority({"ws-alpha": WorkspaceBinding("ws-alpha", ALPHA_ROOT, "vm-project-alpha")})
        bound = SettingsStore(self.database, binding_authority=authority)
        self.assertEqual(bound.resolve_execution_target(),
                         ExecutionTarget("ws-alpha", ALPHA_ROOT, "vm-project-alpha"))

        # Changing the selection changes the only execution root source: the
        # old alpha root is never returned for the newly selected workspace.
        bound.select_workspace("ws-beta", expected_revision=1)
        with self.assertRaises(WorkspaceBindingRequired):
            bound.resolve_execution_target()

        authority.bindings["ws-alpha"] = WorkspaceBinding("ws-alpha", "/synthetic/roots/other", "vm-project-alpha")
        with self.assertRaises(WorkspaceBindingMismatch):
            bound.select_workspace("ws-alpha", expected_revision=2)
        self.assertEqual(bound.active_workspace(), Revisioned("ws-beta", 2))
        plain.select_workspace("ws-alpha", expected_revision=2)
        with self.assertRaises(WorkspaceBindingMismatch):
            bound.resolve_execution_target()
        with self.assertRaises(WorkspaceBindingRequired):
            SettingsStore(self.database, binding_authority=FakeAuthority(fail=True)).resolve_execution_target()

        with self.assertRaises(InvalidSetting):
            plain.save(Workspace("ws-alpha", "알파 작업공간", "/synthetic/roots/moved", "vm-project-alpha"),
                       expected_revision=1)
        renamed = Workspace("ws-alpha", "Renamed", ALPHA_ROOT, "vm-project-alpha")
        self.assertEqual(plain.save(renamed, expected_revision=1), Revisioned(renamed, 2))
        # No authority record at all is a fail-closed "required" error ...
        with self.assertRaises(WorkspaceBindingRequired):
            bound.save(Workspace("ws-beta", "Beta", "/synthetic/roots/beta", "vm-project-beta"), expected_revision=1)
        # ... while an authority record with a different root is a mismatch.
        authority.bindings["ws-beta"] = WorkspaceBinding("ws-beta", "/synthetic/roots/authority-beta", "vm-project-beta")
        with self.assertRaises(WorkspaceBindingMismatch):
            bound.save(Workspace("ws-beta", "Beta", "/synthetic/roots/beta", "vm-project-beta"), expected_revision=1)
        self.assertFalse(plain.get(Workspace, "ws-beta").value.bound)
        with self.assertRaises(InvalidReference):
            plain.delete(Workspace, "ws-alpha", expected_revision=2)

    def test_settings_database_is_separate_from_runtime_storage(self) -> None:
        runtime = self.root / "runtime.sqlite3"
        RuntimeStore(runtime)
        before = runtime.read_bytes()
        with self.assertRaises(InvalidDatabase):
            SettingsStore(runtime)
        self.assertEqual(runtime.read_bytes(), before)
        SettingsStore(self.database)
        settings_before = self.database.read_bytes()
        with self.assertRaises(InvalidDatabase):
            RuntimeStore(self.database)
        self.assertEqual(self.database.read_bytes(), settings_before)

        windows_base, development_base = self.root / "windows-profile", self.root / "xdg-state"
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(windows_base), "XDG_STATE_HOME": str(development_base)}):
            self.assertNotEqual(default_settings_path(), runtime_paths.default_database_path())
            store = SettingsStore()
            store.save(Workspace("ws-default", "Default"))
        expected = (windows_base / "AIK/config/settings.sqlite3" if os.name == "nt"
                    else development_base / "aik/settings.sqlite3")
        self.assertTrue(expected.is_file())
        self.assert_integrity(expected)

    def test_unsafe_destinations_are_rejected_without_creating_files(self) -> None:
        project = self.root / "source-project"
        (project / ".git").mkdir(parents=True)
        for path in (project / "settings.sqlite3", project / "nested" / "settings.sqlite3",
                     self.root / "backlog.sqlite3", "relative-settings.sqlite3"):
            with self.subTest(destination=str(path)):
                with self.assertRaises(UnsafeStoragePath):
                    SettingsStore(path)
        self.assertFalse((project / "settings.sqlite3").exists())
        self.assertFalse((project / "nested").exists())
        self.assertFalse((self.root / "backlog.sqlite3").exists())

    def test_corrupt_foreign_and_wrong_schema_files_are_preserved(self) -> None:
        corrupt = self.root / "corrupt.sqlite3"
        corrupt.write_bytes(b"SQLite format 3\0SYNTHETIC DAMAGED DATABASE" * 12)
        empty = self.root / "empty.sqlite3"
        empty.write_bytes(b"")
        foreign = self.root / "foreign.sqlite3"
        with closing(sqlite3.connect(foreign)) as connection:
            connection.execute("CREATE TABLE unrelated (payload TEXT)")
            connection.commit()
        impostor = self.root / "impostor.sqlite3"
        with closing(sqlite3.connect(impostor)) as connection:
            connection.execute("PRAGMA application_id = 1095322435")  # 0x41494B43
            connection.execute("PRAGMA user_version = 1")
            connection.execute("CREATE TABLE unrelated (payload TEXT)")
            connection.commit()
        for path in (corrupt, empty, foreign, impostor):
            with self.subTest(source=path.name):
                before = path.read_bytes()
                with self.assertRaises(InvalidDatabase):
                    SettingsStore(path)
                self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
