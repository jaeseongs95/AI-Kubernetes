"""AIK-087: workspaces.* engine commands over the AIK-084 manager (synthetic folders, fake VM port)."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_config import SettingsStore, Workspace  # noqa: E402
from aik_config.models import ConnectionProfile  # noqa: E402
from aik_desktop_engine.server import WORKSPACE_COMMANDS, Engine  # noqa: E402
from aik_desktop_engine.workspaces import (  # noqa: E402
    WORKSPACE_CODES, WorkspaceCommandError, WorkspaceService,
)
from aik_scope import ConnectionScope, ScopeStore  # noqa: E402
from aik_workspace import ProjectInfo, VmProjectUnavailable  # noqa: E402

NONCE = "a" * 64


class FakeProjects:
    """A VM that records the root it was given, like the upstream ProjectMap root."""

    def __init__(self) -> None:
        self.projects: dict[str, str] = {}
        self.fail_lookup = False

    def create_project(self, workspace_id: str, canonical_root: str, name: str) -> str:
        project_id = "project_%032x" % (len(self.projects) + 1)
        self.projects[project_id] = canonical_root
        return project_id

    def lookup_project(self, project_id: str):
        if self.fail_lookup:
            raise VmProjectUnavailable("unreachable")
        root = self.projects.get(project_id)
        return None if root is None else ProjectInfo(project_id, root)


def _snapshot(path: Path) -> set:
    return {str(item.relative_to(path)) for item in path.rglob("*")}


class WorkspaceServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="aik-087-"))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.state = self.temp / "state"
        self.state.mkdir()
        self.user = self.temp / "사용자 폴더"
        self.user.mkdir()
        self.vm = FakeProjects()

    def service(self, *, vm: bool = True) -> WorkspaceService:
        counter = iter(range(1, 100))
        return WorkspaceService(self.state / "config" / "settings.sqlite3", self.state / "ws" / "workspaces.json",
                                projects=self.vm if vm else None, new_id=lambda: "ws-%04d" % next(counter))

    def code(self, call) -> str:
        with self.assertRaises(WorkspaceCommandError) as caught:
            call()
        self.assertIn(caught.exception.code, WORKSPACE_CODES)
        return caught.exception.code

    def test_reads_create_nothing(self):
        before = _snapshot(self.temp)
        service = self.service()
        self.assertEqual(service.list(), {"vm": "CONFIGURED", "registry": "ABSENT", "active": None, "workspaces": []})
        self.assertEqual(self.code(lambda: service.scope("ws-0001")), "UNKNOWN_WORKSPACE")
        self.assertEqual(_snapshot(self.temp), before)

    def test_without_a_vm_registration_fails_closed_and_leaves_nothing(self):
        existing = self.user / "기존 프로젝트"
        existing.mkdir()
        service = self.service(vm=False)
        self.assertEqual(service.list()["vm"], "NOT_CONFIGURED")
        self.assertEqual(self.code(lambda: service.open("기존 프로젝트", str(existing))), "VM_NOT_CONFIGURED")
        self.assertEqual(self.code(lambda: service.create("새 프로젝트", str(self.user), "새 폴더")), "VM_NOT_CONFIGURED")
        self.assertFalse((self.user / "새 폴더").exists())  # the created folder is removed again
        self.assertFalse((self.state / "ws" / "workspaces.json").exists())
        self.assertEqual(self.service(vm=False).list()["workspaces"], [])

    def test_open_create_select_and_restore_after_restart(self):
        existing = self.user / "기존 프로젝트 A"
        existing.mkdir()
        (existing / "README.md").write_text("keep", encoding="utf-8")
        service = self.service()
        opened = service.open("기존 프로젝트 A", str(existing))
        self.assertEqual((opened["workspace_id"], opened["status"], opened["required_action"], opened["active"]),
                         ("ws-0001", "ready", "none", False))
        self.assertEqual(Path(opened["root"]), existing.resolve())
        self.assertEqual((existing / "README.md").read_text(encoding="utf-8"), "keep")  # contents untouched
        created = service.create("새 프로젝트 B", str(self.user), "새 프로젝트 B")
        self.assertTrue((self.user / "새 프로젝트 B").is_dir())
        self.assertEqual(Path(created["root"]), (self.user / "새 프로젝트 B").resolve())
        self.assertEqual(sorted(self.vm.projects.values()), sorted([opened["root"], created["root"]]))

        active = service.select("ws-0001")
        self.assertEqual((active["workspace_id"], active["status"], active["selection_revision"]), ("ws-0001", "ready", 1))
        self.assertEqual(Path(active["cwd"]), existing.resolve())
        switched = service.select("ws-0002")
        self.assertEqual((switched["workspace_id"], switched["selection_revision"]), ("ws-0002", 2))
        self.assertEqual(Path(switched["cwd"]), (self.user / "새 프로젝트 B").resolve())

        restarted = self.service().list()
        self.assertEqual(restarted["active"]["workspace_id"], "ws-0002")
        self.assertEqual(Path(restarted["active"]["cwd"]), (self.user / "새 프로젝트 B").resolve())
        self.assertEqual([(w["workspace_id"], w["active"]) for w in restarted["workspaces"]],
                         [("ws-0001", False), ("ws-0002", True)])

    def test_moved_folder_is_reported_with_a_reselect_path_and_rebinds(self):
        folder = self.user / "옮길 폴더"
        folder.mkdir()
        service = self.service()
        record = service.open("옮길 폴더", str(folder))
        service.select(record["workspace_id"])
        moved = self.user / "옮긴 폴더"
        folder.rename(moved)
        listed = self.service().list()
        self.assertEqual((listed["active"]["status"], listed["active"]["required_action"], listed["active"]["cwd"]),
                         ("root_missing", "reselect_folder", None))
        self.assertEqual(self.code(lambda: service.select(record["workspace_id"])), "WORKSPACE_ROOT_MISSING")
        # A stale revision is refused; the current one re-binds the same workspace.
        self.assertEqual(self.code(lambda: service.rebind_root(record["workspace_id"], str(moved), 9)),
                         "REVISION_CONFLICT")
        rebound = service.rebind_root(record["workspace_id"], str(moved), record["revision"])
        # The VM still reports the old root, so execution stays blocked until the project matches.
        self.assertEqual((rebound["status"], rebound["required_action"], rebound["revision"]),
                         ("vm_project_mismatch", "rebind_vm_project", 2))
        self.vm.projects = {key: str(moved.resolve()) for key in self.vm.projects}
        self.assertEqual(Path(service.select(record["workspace_id"])["cwd"]), moved.resolve())

    def test_unreachable_vm_and_archive_restore(self):
        folder = self.user / "보관 폴더"
        folder.mkdir()
        service = self.service()
        record = service.open("보관 폴더", str(folder))
        self.vm.fail_lookup = True
        self.assertEqual(service.list()["workspaces"][0]["status"], "vm_project_unknown")
        self.assertEqual(self.code(lambda: service.select(record["workspace_id"])), "WORKSPACE_VM_PROJECT_UNKNOWN")
        self.vm.fail_lookup = False
        archived = service.archive(record["workspace_id"], record["revision"])
        self.assertEqual((archived["state"], archived["status"], archived["required_action"]),
                         ("archived", "archived", "restore_workspace"))
        self.assertEqual(self.code(lambda: service.select(record["workspace_id"])), "WORKSPACE_ARCHIVED")
        restored = service.restore(record["workspace_id"], archived["revision"])
        self.assertEqual((restored["state"], restored["status"]), ("active", "ready"))

    def test_invalid_input_names_and_roots(self):
        folder = self.user / "A"
        folder.mkdir()
        service = self.service()
        self.assertEqual(self.code(lambda: service.open(" 앞 공백", str(folder))), "INVALID_NAME")
        self.assertEqual(self.code(lambda: service.open("이름", "relative/path")), "ROOT_INVALID")
        self.assertEqual(self.code(lambda: service.open("이름", str(self.user / "없는 폴더"))), "ROOT_UNAVAILABLE")
        self.assertEqual(self.code(lambda: service.create("이름", str(self.user), "a/b")), "INVALID_FOLDER_NAME")
        self.assertEqual(self.code(lambda: service.create("이름", str(self.user), "A")), "ROOT_INVALID")
        service.open("A", str(folder))
        nested = folder / "안쪽"
        nested.mkdir()
        self.assertEqual(self.code(lambda: service.open("안쪽", str(nested))), "ROOT_CONFLICT")
        self.assertEqual(self.code(lambda: service.select("ws-9999")), "UNKNOWN_WORKSPACE")
        self.assertEqual(self.code(lambda: service.archive("ws-0001", 0)), "INVALID_INPUT")

    def test_scope_reports_each_connection_without_creating_the_scope_file(self):
        folder = self.user / "범위 폴더"
        folder.mkdir()
        service = self.service()
        record = service.open("범위 폴더", str(folder))
        settings = SettingsStore(self.state / "config" / "settings.sqlite3")
        settings.save(Workspace("local", "로컬"))
        settings.save(ConnectionProfile("conn-a", "local", "openai"))
        settings.save(ConnectionProfile("conn-b", "local", "anthropic"))
        scope_file = self.state / "config" / "scopes.sqlite3"
        absent = service.scope(record["workspace_id"])
        self.assertEqual(absent["scope"], "ABSENT")
        self.assertEqual([(row["connection_id"], row["allowed"], row["reason"]) for row in absent["connections"]],
                         [("conn-a", False, "NO_SCOPE"), ("conn-b", False, "NO_SCOPE")])
        self.assertFalse(scope_file.exists())
        store = ScopeStore(settings)
        store.save(ConnectionScope("local", shared_connection_ids=("conn-a",)))
        store.save(ConnectionScope(record["workspace_id"], allowed_connection_ids=("conn-a", "conn-b")))
        ready = service.scope(record["workspace_id"])
        self.assertEqual([(row["connection_id"], row["allowed"], row["reason"]) for row in ready["connections"]],
                         [("conn-a", True, None), ("conn-b", False, "NOT_SHARED")])


class WorkspaceCommandTests(unittest.TestCase):
    def request(self, command: str, payload: dict) -> dict:
        return {"protocol_version": 1, "request_id": "0b3f5c1e-8a2d-4c7e-9f10-2a3b4c5d6e7f", "command": command,
                "payload": payload, "capability": NONCE}

    def test_payload_keys_are_exact_and_unconfigured_engine_says_so(self):
        engine = Engine(NONCE)
        for command, keys in WORKSPACE_COMMANDS.items():
            payload = {key: ("x" if key != "expected_revision" else 1) for key in keys}
            answer = engine.dispatch(self.request(command, dict(payload)))
            self.assertEqual(answer["error"], {"code": "WORKSPACES_NOT_CONFIGURED"}, command)
            answer = engine.dispatch(self.request(command, {**payload, "extra": 1}))
            self.assertEqual(answer["error"], {"code": "INVALID_REQUEST"}, command)

    def test_service_codes_cross_the_pipe(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            folder = state / "프로젝트"
            folder.mkdir()
            service = WorkspaceService(state / "c" / "settings.sqlite3", state / "w" / "workspaces.json")
            engine = Engine(NONCE, workspaces=service)
            listed = engine.dispatch(self.request("workspaces.list", {}))
            self.assertEqual(listed["result"]["vm"], "NOT_CONFIGURED")
            answer = engine.dispatch(self.request("workspaces.open", {"name": "프로젝트", "root": str(folder)}))
            self.assertEqual(answer, {**answer, "ok": False, "error": {"code": "VM_NOT_CONFIGURED"}})
            self.assertNotIn(str(folder), str(answer))


if __name__ == "__main__":
    unittest.main()
