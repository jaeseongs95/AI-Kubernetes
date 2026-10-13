"""Exercise AIK settings against disposable SQLite files outside the repository.

Only synthetic IDs, roots and markers are used. No provider, credential or
network is touched. Linux results are development verification, not Windows
support evidence.
"""
from __future__ import annotations

from contextlib import closing
import copy
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import shutil
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
from aik_config import storage as config_storage  # noqa: E402
from aik_runtime import RuntimeStore  # noqa: E402
from aik_runtime import paths as runtime_paths  # noqa: E402


SECRET_MARKER = "SYNTHETIC_SECRET_MUST_NOT_BE_STORED"
ALPHA_ROOT = "/synthetic/roots/alpha"
JOURNAL_MAGIC = b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7"


class FakeAuthority:
    """Stands in for the unimplemented AIK-084 binding authority."""

    def __init__(self, bindings=None, fail: bool = False):
        self.bindings = dict(bindings or {})
        self.fail = fail

    def binding_for(self, workspace_id):
        if self.fail:
            raise RuntimeError("synthetic authority outage")
        return self.bindings.get(workspace_id)


ALPHA_BINDING = WorkspaceBinding("ws-alpha", ALPHA_ROOT, "vm-project-alpha")


def seed(store: SettingsStore) -> None:
    # Binding a workspace requires AIK-084 confirmation (fail-closed), so the
    # seed writes through a store with a synthetic authority for ws-alpha.
    writer = SettingsStore(store.path, timeout=store.timeout,
                           binding_authority=FakeAuthority({"ws-alpha": ALPHA_BINDING}))
    with writer.transaction() as tx:
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

    # ------------------------------------------------- review regression tests

    def test_binding_without_authority_is_rejected_and_not_stored(self) -> None:
        # F1 / F6(e): an unconfirmed root must not become a frozen binding.
        store = SettingsStore(self.database)
        with self.assertRaises(WorkspaceBindingRequired):
            store.save(Workspace("ws-new", "New", "/synthetic/roots/typo", "vm-project-new"))
        self.assertIsNone(store.get(Workspace, "ws-new"))
        plain = Workspace("ws-plain", "Plain")
        store.save(plain)
        with self.assertRaises(WorkspaceBindingRequired):
            store.save(Workspace("ws-plain", "Plain", "/synthetic/roots/typo", "vm-project-plain"),
                       expected_revision=1)
        self.assertEqual(store.get(Workspace, "ws-plain"), Revisioned(plain, 1))
        # The real AIK-084 binding can still be applied afterwards.
        authority = FakeAuthority({"ws-plain": WorkspaceBinding("ws-plain", "/synthetic/roots/real", "vm-project-plain")})
        confirmed = Workspace("ws-plain", "Plain", "/synthetic/roots/real", "vm-project-plain")
        self.assertEqual(SettingsStore(self.database, binding_authority=authority).save(confirmed, expected_revision=1),
                         Revisioned(confirmed, 2))
        self.assertNotIn(b"/synthetic/roots/typo", self.database.read_bytes())

    def test_rebind_workspace_applies_only_the_authority_binding(self) -> None:
        # F2: a stale binding has a recovery path that only AIK-084 drives.
        store = SettingsStore(self.database)
        seed(store)
        original = store.get(Workspace, "ws-alpha")
        store.select_workspace("ws-alpha")
        authority = FakeAuthority({"ws-alpha": WorkspaceBinding("ws-alpha", "/synthetic/roots/rebound",
                                                                "vm-project-alpha-2")})
        bound = SettingsStore(self.database, binding_authority=authority)
        with self.assertRaises(WorkspaceBindingMismatch):
            bound.resolve_execution_target()

        with self.assertRaises(WorkspaceBindingRequired):
            store.rebind_workspace("ws-alpha", expected_revision=1)
        with self.assertRaises(WorkspaceBindingRequired):
            SettingsStore(self.database, binding_authority=FakeAuthority()).rebind_workspace("ws-alpha", 1)
        with self.assertRaises(WorkspaceBindingRequired):
            SettingsStore(self.database, binding_authority=FakeAuthority(fail=True)).rebind_workspace("ws-alpha", 1)
        foreign_owner = FakeAuthority({"ws-alpha": WorkspaceBinding("ws-beta", "/synthetic/roots/x", "vm-x")})
        with self.assertRaises(WorkspaceBindingMismatch):
            SettingsStore(self.database, binding_authority=foreign_owner).rebind_workspace("ws-alpha", 1)
        with self.assertRaises(RevisionConflict):
            bound.rebind_workspace("ws-alpha", expected_revision=2)
        with self.assertRaises(InvalidReference):
            bound.rebind_workspace("ws-missing", expected_revision=1)
        self.assertEqual(store.get(Workspace, "ws-alpha"), original)

        rebound = Workspace("ws-alpha", "알파 작업공간", "/synthetic/roots/rebound", "vm-project-alpha-2")
        self.assertEqual(bound.rebind_workspace("ws-alpha", expected_revision=1), Revisioned(rebound, 2))
        self.assertEqual(bound.resolve_execution_target(),
                         ExecutionTarget("ws-alpha", "/synthetic/roots/rebound", "vm-project-alpha-2"))
        # An unbound (for example imported) workspace is bound the same way.
        authority.bindings["ws-beta"] = WorkspaceBinding("ws-beta", "/synthetic/roots/beta", "vm-project-beta")
        self.assertTrue(bound.rebind_workspace("ws-beta", expected_revision=1).value.bound)
        self.assert_integrity()

    def test_foreign_hot_journal_is_checked_on_a_private_copy_and_source_preserved(self) -> None:
        # F4: a hot journal beside an AIKC-headed file must not be replayed into it.
        SettingsStore(self.database)
        journal = self.database.with_name(self.database.name + "-journal")
        foreign = self.root / "foreign-writer.sqlite3"
        foreign_journal = foreign.with_name(foreign.name + "-journal")
        with closing(sqlite3.connect(foreign, isolation_level=None)) as connection:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("CREATE TABLE payload (value TEXT)")
            connection.execute("PRAGMA cache_size=4")
            connection.execute("PRAGMA cache_spill=ON")
            connection.execute("BEGIN IMMEDIATE")
            for number in range(512):
                connection.execute("INSERT INTO payload VALUES (?)", (f"{number:04d}-" + "x" * 200,))
            self.assertTrue(foreign_journal.is_file(), "The harness did not produce a rollback journal.")
            self.assertEqual(foreign_journal.read_bytes()[:8], JOURNAL_MAGIC)
            shutil.copyfile(foreign_journal, journal)
            connection.rollback()
        before, journal_before = self.database.read_bytes(), journal.read_bytes()
        with self.assertRaises(InvalidDatabase):
            SettingsStore(self.database)
        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual(journal.read_bytes(), journal_before)
        self.assertEqual([p.name for p in self.database.parent.iterdir() if p.name.startswith(".aik-settings-")], [])

    def test_genuine_hot_journal_still_recovers_after_private_check(self) -> None:
        # F4 guard: the private check must not block legitimate crash recovery.
        store = SettingsStore(self.database)
        store.save(Workspace("ws-committed", "Committed"))
        crashed = self.root / "crashed" / "settings.sqlite3"
        crashed.parent.mkdir()
        journal = self.database.with_name(self.database.name + "-journal")
        with closing(sqlite3.connect(self.database, isolation_level=None)) as connection:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA cache_size=4")
            connection.execute("PRAGMA cache_spill=ON")
            connection.execute("BEGIN IMMEDIATE")
            for number in range(512):
                connection.execute(
                    "INSERT INTO workspaces (id, name, canonical_root, vm_project_id, revision) "
                    "VALUES (?, ?, NULL, NULL, 1)", (f"ws-uncommitted-{number:04d}", "x" * 120))
            self.assertTrue(journal.is_file())
            shutil.copyfile(self.database, crashed)
            shutil.copyfile(journal, crashed.with_name(crashed.name + "-journal"))
            connection.rollback()
        recovered = SettingsStore(crashed)
        self.assertEqual(recovered.list(Workspace), (Revisioned(Workspace("ws-committed", "Committed"), 1),))
        self.assertFalse(crashed.with_name(crashed.name + "-journal").exists())
        self.assert_integrity(crashed)

    def test_import_record_limit_is_enforced_before_records_are_constructed(self) -> None:
        # F5: the limit must bound work, not be checked after building records.
        target = SettingsStore(self.database)
        document = {
            "format": "aik.settings-export", "format_version": 1, "scope": "settings",
            "contains_secret_values": False, "excluded": [],
            "workspaces": [{
                "id": "ws-bulk", "name": "Bulk", "requires_binding": True,
                "connections": [
                    {"id": f"conn-{number:05d}", "provider": "synthetic", "auth_mode": "none",
                     "requires_authentication": False}
                    for number in range(config_storage.MAX_IMPORT_RECORDS)
                ],
                "profiles": [], "limits": None, "permissions": None,
            }],
        }
        with mock.patch.object(config_storage, "ConnectionProfile", wraps=ConnectionProfile) as constructor:
            with self.assertRaisesRegex(InvalidSettingsDocument, "import record limit"):
                target.import_settings(document)
        self.assertEqual(constructor.call_count, 0)
        self.assertEqual(target.list(Workspace), ())

    def test_selected_unreferenced_workspace_cannot_be_deleted(self) -> None:
        # F6(a): the selection alone blocks deletion.
        store = SettingsStore(self.database)
        store.save(Workspace("ws-lonely", "Lonely"))
        store.save(Workspace("ws-other", "Other"))
        store.select_workspace("ws-lonely")
        with self.assertRaises(InvalidReference):
            store.delete(Workspace, "ws-lonely", expected_revision=1)
        self.assertIsNotNone(store.get(Workspace, "ws-lonely"))
        store.select_workspace("ws-other", expected_revision=1)
        store.delete(Workspace, "ws-lonely", expected_revision=1)
        self.assertIsNone(store.get(Workspace, "ws-lonely"))

    def test_bound_workspace_cannot_be_unbound_and_subclasses_are_rejected(self) -> None:
        # F6(b)(c)
        store = SettingsStore(self.database)
        seed(store)
        with self.assertRaises(InvalidSetting):
            store.save(Workspace("ws-alpha", "알파 작업공간"), expected_revision=1)
        self.assertTrue(store.get(Workspace, "ws-alpha").value.bound)

        @dataclass(frozen=True)
        class ExtendedWorkspace(Workspace):
            note: str = SECRET_MARKER

        with self.assertRaises(InvalidSetting):
            store.save(ExtendedWorkspace("ws-extended", "Extended"))
        self.assertIsNone(store.get(Workspace, "ws-extended"))
        self.assertNotIn(SECRET_MARKER.encode(), self.database.read_bytes())

    def test_import_rejects_malformed_documents_atomically(self) -> None:
        # F6(d)
        source = SettingsStore(self.database)
        seed(source)
        document = source.export_settings()
        target = SettingsStore(self.root / "import target" / "settings.sqlite3")

        def variant(change):
            changed = copy.deepcopy(document)
            change(changed)
            return changed

        cases = {
            "absent-connection": (InvalidReference, variant(lambda d: d["workspaces"][0]["profiles"].append(
                {"id": "worker-absent", "role": "worker", "connection_id": "conn-absent", "model": "m",
                 "effort": None}))),
            "duplicate-workspace": (InvalidSettingsDocument,
                                    variant(lambda d: d["workspaces"][1].__setitem__("id", "ws-alpha"))),
            "duplicate-profile": (InvalidSettingsDocument, variant(
                lambda d: d["workspaces"][0]["profiles"].append(copy.deepcopy(d["workspaces"][0]["profiles"][0])))),
            "duplicate-connection": (InvalidSettingsDocument, variant(
                lambda d: d["workspaces"][1]["connections"].append(
                    copy.deepcopy(d["workspaces"][0]["connections"][0])))),
            "format": (InvalidSettingsDocument, variant(lambda d: d.__setitem__("format", "other.export"))),
            "format-version": (InvalidSettingsDocument, variant(lambda d: d.__setitem__("format_version", 2))),
            "format-version-bool": (InvalidSettingsDocument,
                                    variant(lambda d: d.__setitem__("format_version", True))),
            "scope": (InvalidSettingsDocument, variant(lambda d: d.__setitem__("scope", "backup"))),
        }
        for name, (expected, candidate) in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(expected):
                    target.import_settings(candidate)
                self.assertEqual(target.list(Workspace), ())
                self.assertEqual(target.list(ConnectionProfile), ())
                self.assertEqual(target.list(AgentProfile), ())


if __name__ == "__main__":
    unittest.main()
