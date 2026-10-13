"""AIK-084 workspace lifecycle tests with temporary folders and a fake VM port only.

No provider, credential, network or VM/AGS installation is used. Passing on
Linux is development verification, not evidence of Windows support.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_config import SettingsStore, Workspace, WorkspaceBindingError  # noqa: E402
from aik_provider.contracts import AuthKind  # noqa: E402
from aik_runtime.errors import InvalidDatabase, InvalidRecord, RevisionConflict  # noqa: E402
from aik_vm_bridge import PINNED_SURFACE_REVISION, Command, TaskExecutionBinding, VmBridge  # noqa: E402
from aik_workspace import (  # noqa: E402
    BridgeProjectPort, InputBindingMismatch, InputCondition, InputError, InputNotCurrent,
    InputTampered, InvalidInputPath, InvalidWorkspaceRoot, RootConflict, VmProjectUnavailable,
    WorkspaceManager, WorkspaceRecord, WorkspaceRegistry, WorkspaceStatus, WorkspaceUnavailable,
    canonical_root,
)


SURFACE = {
    "format": "aik-upstream-surface-v1",
    "revision": PINNED_SURFACE_REVISION,
    "classes": [{"name": "EngineApplication", "methods": [{"name": "status", "signature": "self, project_id: str"}]}],
}
CONSUMER = (
    "import hashlib, sys\n"
    "data = open(sys.argv[1], 'rb').read()\n"
    "print('sha256:' + hashlib.sha256(data).hexdigest())\n"
)


class FakeVmError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class FakeVmApplication:
    """Synthetic VM façade: status() reports the ProjectMap root; nothing real is called."""

    def __init__(self) -> None:
        self.projects: dict[str, str] = {}
        self.created = 0
        self.fail = False
        self.omit_root = False
        self.reported_project_id = None

    def handshake(self):
        return {"surface_revision": PINNED_SURFACE_REVISION, "engine_schema_revision": 4,
                "methods": {"status": "self, project_id: str"}}

    def status(self, project_id):
        if self.fail:
            raise RuntimeError("synthetic transport failure")
        if project_id not in self.projects:
            raise FakeVmError("PROJECT_NOT_FOUND")
        payload = {"project_id": self.reported_project_id or project_id}
        if not self.omit_root:
            payload["project_root"] = self.projects[project_id]
        return payload

    def create_project(self, workspace_id, canonical_root, name):
        self.created += 1
        project_id = f"vm-project-{self.created}"
        self.projects[project_id] = canonical_root
        return project_id


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def task_binding(manifest, **overrides) -> TaskExecutionBinding:
    values = dict(
        vm_instance_id="vm-instance-1", vm_project_id=manifest.vm_project_id, vm_goal_revision_id="goal-rev-1",
        vm_plan_revision_id="plan-rev-1", vm_task_id="vm-task-consumer", vm_attempt_id="vm-attempt-1",
        vm_execution_spec_digest="sha256:" + "0" * 64, vm_runtime_intent_id="intent-1",
        vm_provider_call_id="call-1", cluster_id="cluster-1", route_id="route-1", route_revision="route-rev-1",
        connection_id="connection-1", connection_revision="connection-rev-1", provider="synthetic",
        auth_kind=next(iter(AuthKind)).value, provider_account_fingerprint="account-fp-1", pool_id="pool-1",
        placement_id="placement-1", lease_id="lease-1", lease_fencing_token=1,
        support_matrix_digest="sha256:" + "1" * 64, input_manifest_digest=manifest.digest(),
        workspace_root_ref=manifest.workspace_root_ref,
    )
    values.update(overrides)
    return TaskExecutionBinding(**values)


class WorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="aik-workspace-test-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.state = self.tmp / "상태 저장"
        self.base = self.tmp / "작업 공간"
        self.base.mkdir()
        self.vm = FakeVmApplication()

    def build(self, host_check=None, *, creator=True, registry_name="workspaces.json"):
        bridge = VmBridge(self.vm, SURFACE, commands=[Command.STATUS])
        port = BridgeProjectPort(bridge, create_project=self.vm.create_project if creator else None)
        manager = WorkspaceManager(WorkspaceRegistry(self.state / registry_name), port, host_check=host_check)
        settings = SettingsStore(self.state / "settings.sqlite3", binding_authority=manager)
        manager.connect_settings(settings)
        return manager, settings

    @staticmethod
    def run_in(context, args):
        environment = dict(os.environ, PYTHONIOENCODING="utf-8")
        result = subprocess.run([sys.executable, "-c", *args], **context.subprocess_kwargs(),
                                capture_output=True, text=True, encoding="utf-8", env=environment,
                                timeout=30, check=True)
        return result.stdout.strip()

    # ---------------------------------------------------------------- lifecycle
    def test_create_new_hangul_space_folder_binds_root_project_settings_and_real_cwd(self) -> None:
        manager, settings = self.build()
        requested = self.base / "새 프로젝트 A"
        record = manager.create_workspace("ws-a", "프로젝트 A", requested)
        self.assertTrue(requested.is_dir())
        self.assertEqual(record.canonical_root, str(requested.resolve()))
        self.assertEqual(self.vm.projects[record.vm_project_id], record.canonical_root)
        stored = settings.get(Workspace, "ws-a").value
        self.assertEqual((stored.canonical_root, stored.vm_project_id), (record.canonical_root, record.vm_project_id))

        context = manager.select("ws-a")
        self.assertEqual(context.canonical_root, record.canonical_root)
        self.assertEqual(settings.resolve_execution_target().canonical_root, record.canonical_root)
        environment = dict(os.environ, PYTHONIOENCODING="utf-8")
        result = subprocess.run([sys.executable, "-c", "import os; print(os.getcwd())"],
                                **context.subprocess_kwargs(), capture_output=True, text=True,
                                encoding="utf-8", env=environment, timeout=30, check=True)
        self.assertTrue(os.path.samefile(result.stdout.strip(), record.canonical_root))

    def test_open_and_switch_revalidate_root_and_vm_project_and_gui_only_selection_fails(self) -> None:
        manager, settings = self.build()
        existing = self.base / "기존 폴더"
        existing.mkdir()
        (existing / "README.txt").write_bytes(b"synthetic user content")
        manager.open_workspace("ws-a", "기존", existing)
        self.assertEqual((existing / "README.txt").read_bytes(), b"synthetic user content")
        manager.create_workspace("ws-b", "두번째", self.base / "두번째 폴더")
        self.assertEqual(manager.select("ws-a").workspace_id, "ws-a")
        self.assertEqual(manager.select("ws-b").workspace_id, "ws-b")
        self.assertEqual(manager.current_execution().workspace_id, "ws-b")

        del self.vm.projects[manager.get("ws-a").vm_project_id]
        with self.assertRaises(WorkspaceUnavailable) as failure:
            manager.select("ws-a")
        self.assertEqual(failure.exception.status, WorkspaceStatus.VM_PROJECT_MISSING)
        self.assertEqual(failure.exception.action, "rebind_vm_project")
        active = settings.active_workspace()
        with self.assertRaises(WorkspaceBindingError):
            settings.select_workspace("ws-a", active.revision)  # GUI selection alone is refused.
        self.assertEqual(settings.active_workspace().value, "ws-b")
        self.assertEqual(manager.current_execution().workspace_id, "ws-b")

    def test_relative_traversal_network_git_internal_and_ledger_roots_are_rejected(self) -> None:
        manager, _ = self.build()
        project = self.tmp / "user repo"
        (project / ".git" / "objects").mkdir(parents=True)
        (project / ".git" / "refs").mkdir()
        (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        bare = self.tmp / "bare.repo"
        (bare / "objects").mkdir(parents=True)
        (bare / "refs").mkdir()
        (bare / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        ledger = self.tmp / "개발 저장소"
        (ledger / "planning").mkdir(parents=True)
        (ledger / "planning" / "backlog.sqlite3").write_bytes(b"synthetic ledger")
        (ledger / "sub").mkdir()
        a_file = self.tmp / "file.txt"
        a_file.write_bytes(b"x")
        rejected = ("relative/project", str(self.base) + "/../작업 공간", "//server/share/project",
                    project / ".git", project / ".git" / "objects", bare, bare / "objects", ledger, ledger / "sub",
                    ROOT, ROOT / "src", self.tmp / "missing", a_file, Path(self.base.anchor))
        for path in rejected:
            with self.subTest(path=str(path)):
                with self.assertRaises(InvalidWorkspaceRoot):
                    canonical_root(path)
        with self.assertRaises(InvalidWorkspaceRoot):
            manager.open_workspace("ws-ledger", "원장", ROOT)
        self.assertEqual(manager.list_workspaces(), ())
        self.assertEqual(self.vm.created, 0)
        # A user's own Git working tree is an acceptable workspace root.
        self.assertEqual(manager.open_workspace("ws-repo", "저장소", project).canonical_root, str(project.resolve()))

    def test_symlinked_root_and_symlinked_parent_are_rejected(self) -> None:
        target = self.base / "실제 폴더"
        (target / "proj").mkdir(parents=True)
        link, parent_link = self.tmp / "link-root", self.tmp / "link-parent"
        try:
            link.symlink_to(target, target_is_directory=True)
            parent_link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"Symbolic links unavailable: {error}")
        for path in (link, parent_link / "proj"):
            with self.subTest(path=path.name):
                with self.assertRaises(InvalidWorkspaceRoot):
                    canonical_root(path)

    def test_duplicate_nested_parent_and_case_alias_roots_are_rejected_before_vm_effects(self) -> None:
        manager, _ = self.build()
        shared = self.base / "공유 Root"
        (shared / "하위").mkdir(parents=True)
        manager.open_workspace("ws-a", "공유", shared)
        created = self.vm.created
        for path in (shared, shared / "하위", self.base):
            with self.subTest(path=path.name):
                with self.assertRaises(RootConflict):
                    manager.open_workspace("ws-b", "충돌", path)
        alias = shared.parent / "공유 ROOT"
        if alias.exists():  # Case-insensitive filesystem only.
            with self.assertRaises(RootConflict):
                manager.open_workspace("ws-c", "대소문자", alias)
        self.assertEqual(self.vm.created, created)
        self.assertEqual([record.id for record in manager.list_workspaces()], ["ws-a"])

    def test_restart_restores_binding_and_selection(self) -> None:
        manager, _ = self.build()
        record = manager.create_workspace("ws-a", "프로젝트 A", self.base / "재시작 프로젝트")
        manager.select("ws-a")
        del manager
        restarted, settings = self.build()
        self.assertEqual(restarted.status("ws-a"), WorkspaceStatus.READY)
        self.assertEqual(settings.active_workspace().value, "ws-a")
        context = restarted.current_execution()
        self.assertEqual((context.canonical_root, context.vm_project_id),
                         (record.canonical_root, record.vm_project_id))

    def test_moved_folder_requires_reselection_and_explicit_root_and_project_rebind(self) -> None:
        manager, settings = self.build()
        old = self.base / "이동 전"
        record = manager.create_workspace("ws-a", "이동", old)
        manager.select("ws-a")
        new = self.base / "이동 후"
        old.rename(new)
        self.assertEqual(manager.status("ws-a"), WorkspaceStatus.ROOT_MISSING)
        with self.assertRaises(WorkspaceUnavailable) as failure:
            manager.execution_context("ws-a")
        self.assertEqual(failure.exception.action, "reselect_folder")
        self.assertIsNone(manager.binding_for("ws-a"))
        with self.assertRaises(WorkspaceBindingError):
            settings.resolve_execution_target()
        with self.assertRaises(WorkspaceBindingError):
            manager.current_execution()

        moved = manager.rebind_root("ws-a", new, record.revision)
        self.assertEqual(moved.revision, 2)
        self.assertEqual(manager.status("ws-a"), WorkspaceStatus.VM_PROJECT_MISMATCH)
        with self.assertRaises(WorkspaceBindingError):
            manager.current_execution()
        rebound = manager.rebind_project("ws-a", moved.revision)
        self.assertEqual(manager.status("ws-a"), WorkspaceStatus.READY)
        context = manager.current_execution()
        self.assertEqual(context.canonical_root, str(new.resolve()))
        self.assertEqual(context.vm_project_id, rebound.vm_project_id)
        self.assertNotEqual(rebound.vm_project_id, record.vm_project_id)
        self.assertIn(record.vm_project_id, self.vm.projects)  # The old VM project is not deleted.

    def test_deleted_then_replaced_folder_requires_confirmed_rebind(self) -> None:
        manager, _ = self.build()
        root = self.base / "교체 대상"
        record = manager.create_workspace("ws-a", "교체", root)
        replacement = self.base / "교체용"
        replacement.mkdir()  # Allocated while the original exists, so its identity differs.
        shutil.rmtree(root)
        self.assertEqual(manager.status("ws-a"), WorkspaceStatus.ROOT_MISSING)
        replacement.rename(root)
        self.assertEqual(manager.status("ws-a"), WorkspaceStatus.ROOT_REPLACED)
        with self.assertRaises(WorkspaceUnavailable) as failure:
            manager.execution_context("ws-a")
        self.assertEqual(failure.exception.action, "confirm_rebind_root")
        manager.rebind_root("ws-a", root, record.revision)
        self.assertEqual(manager.status("ws-a"), WorkspaceStatus.READY)

    def test_access_loss_is_reported_and_blocks_binding(self) -> None:
        manager, _ = self.build()
        record = manager.create_workspace("ws-a", "권한", self.base / "권한 폴더")
        with mock.patch("aik_workspace.roots.probe_access", side_effect=PermissionError(13, "synthetic")):
            self.assertEqual(manager.status("ws-a"), WorkspaceStatus.ACCESS_DENIED)
            self.assertIsNone(manager.binding_for("ws-a"))
        self.assertEqual(manager.status("ws-a"), WorkspaceStatus.READY)
        if os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0):
            self.skipTest("Real permission removal is not observable here (Windows ACL / root user).")
        root = Path(record.canonical_root)
        os.chmod(root, 0)
        self.addCleanup(os.chmod, root, 0o700)
        self.assertEqual(manager.status("ws-a"), WorkspaceStatus.ACCESS_DENIED)

    def test_unclear_vm_query_is_unknown_and_never_creates_a_replacement_project(self) -> None:
        manager, _ = self.build()
        record = manager.create_workspace("ws-a", "불명확", self.base / "불명확 폴더")
        self.vm.fail = True
        self.assertEqual(manager.status("ws-a"), WorkspaceStatus.VM_PROJECT_UNKNOWN)
        self.assertIsNone(manager.binding_for("ws-a"))
        created = self.vm.created
        with self.assertRaises(WorkspaceUnavailable):
            manager.rebind_project("ws-a", record.revision)
        self.assertEqual(self.vm.created, created)

    def test_vm_status_without_project_root_or_with_other_project_id_is_unknown(self) -> None:
        # F1: an unreported ProjectMap root must not be treated as a confirmed binding.
        manager, _ = self.build()
        old = self.base / "루트 미보고"
        record = manager.create_workspace("ws-a", "미보고", old)
        self.vm.omit_root = True
        self.assertEqual(manager.status("ws-a"), WorkspaceStatus.VM_PROJECT_UNKNOWN)
        self.assertIsNone(manager.binding_for("ws-a"))
        new = self.base / "루트 미보고 이동"
        old.rename(new)
        moved = manager.rebind_root("ws-a", new, record.revision)
        self.assertEqual(manager.status("ws-a"), WorkspaceStatus.VM_PROJECT_UNKNOWN)
        created = self.vm.created
        with self.assertRaises(WorkspaceUnavailable) as failure:
            manager.rebind_project("ws-a", moved.revision)
        self.assertEqual(failure.exception.action, "retry_vm_query")
        self.assertEqual(self.vm.created, created)

        self.vm.omit_root = False
        self.vm.reported_project_id = "vm-project-other"
        self.assertEqual(manager.status("ws-a"), WorkspaceStatus.VM_PROJECT_UNKNOWN)
        self.assertIsNone(manager.binding_for("ws-a"))

    def test_unsupported_host_cwd_binding_is_not_executable(self) -> None:
        flag = {"ok": False}
        manager, _ = self.build(host_check=lambda root: flag["ok"])
        folder = self.base / "호스트"
        folder.mkdir()
        with self.assertRaises(WorkspaceUnavailable) as failure:
            manager.open_workspace("ws-a", "호스트", folder)
        self.assertEqual(failure.exception.status, WorkspaceStatus.UNSUPPORTED_HOST)
        self.assertEqual((manager.list_workspaces(), self.vm.created), ((), 0))
        flag["ok"] = True
        manager.open_workspace("ws-a", "호스트", folder)
        flag["ok"] = False
        self.assertEqual(manager.status("ws-a"), WorkspaceStatus.UNSUPPORTED_HOST)
        self.assertIsNone(manager.binding_for("ws-a"))
        with self.assertRaises(WorkspaceUnavailable) as failure:
            manager.execution_context("ws-a")
        self.assertEqual(failure.exception.action, "not_executable")

    def test_create_workspace_rejects_git_ledger_and_unsupported_parents_before_mkdir(self) -> None:
        # F5: nothing may be created inside Git internals, ledgers or on an unsupported host.
        flag = {"ok": True}
        manager, _ = self.build(host_check=lambda root: flag["ok"])
        bare = self.tmp / "bare.repo"
        (bare / "objects").mkdir(parents=True)
        (bare / "refs").mkdir()
        (bare / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        ledger = self.tmp / "개발 저장소"
        (ledger / "planning").mkdir(parents=True)
        (ledger / "planning" / "backlog.sqlite3").write_bytes(b"synthetic ledger")
        (ledger / "sub").mkdir()
        targets = [bare / "objects" / "새 폴더", ledger / "sub" / "새 폴더"]
        unsupported = self.base / "미지원 호스트"
        with mock.patch("aik_workspace.manager.os.mkdir", wraps=os.mkdir) as spy:
            for target in targets:
                with self.subTest(target=str(target)):
                    with self.assertRaises(InvalidWorkspaceRoot):
                        manager.create_workspace("ws-x", "거절", target)
                    self.assertFalse(target.exists())
            flag["ok"] = False
            with self.assertRaises(WorkspaceUnavailable) as failure:
                manager.create_workspace("ws-x", "거절", unsupported)
            self.assertEqual(failure.exception.status, WorkspaceStatus.UNSUPPORTED_HOST)
            created_paths = [Path(call.args[0]) for call in spy.call_args_list if call.args]
        for target in (*targets, unsupported):
            self.assertNotIn(target, created_paths)
        self.assertFalse(unsupported.exists())
        self.assertEqual((manager.list_workspaces(), self.vm.created), ((), 0))

    def test_create_workspace_on_existing_folder_is_rejected_without_effects(self) -> None:
        manager, _ = self.build()
        existing = self.base / "이미 있음"
        existing.mkdir()
        (existing / "keep.txt").write_bytes(b"synthetic keep")
        with self.assertRaises(InvalidWorkspaceRoot):
            manager.create_workspace("ws-a", "기존", existing)
        self.assertEqual((existing / "keep.txt").read_bytes(), b"synthetic keep")
        self.assertEqual((manager.list_workspaces(), self.vm.created), ((), 0))

    def test_out_of_range_root_identity_is_rejected_before_vm_project_creation(self) -> None:
        # F6: an identity the record cannot hold must fail before any VM effect.
        manager, _ = self.build()
        folder = self.base / "범위 초과"
        folder.mkdir()
        with mock.patch("aik_workspace.roots.root_identity", return_value=(1, 2**64)):
            with self.assertRaises(WorkspaceUnavailable) as failure:
                manager.open_workspace("ws-a", "범위", folder)
        self.assertEqual(failure.exception.status, WorkspaceStatus.ROOT_UNSAFE)
        self.assertEqual((manager.list_workspaces(), self.vm.created), ((), 0))

    def test_zero_inode_identity_is_not_trusted(self) -> None:
        # F7: without an inode a replacement is undetectable; fail closed.
        manager, _ = self.build()
        record = manager.create_workspace("ws-a", "inode", self.base / "inode 확인")
        with mock.patch("aik_workspace.roots.root_identity", return_value=(record.root_device, 0)):
            self.assertEqual(manager.status("ws-a"), WorkspaceStatus.ROOT_REPLACED)
            self.assertIsNone(manager.binding_for("ws-a"))
            folder = self.base / "inode 0"
            folder.mkdir()
            created = self.vm.created
            with self.assertRaises(WorkspaceUnavailable) as failure:
                manager.open_workspace("ws-z", "inode 0", folder)
            self.assertEqual(failure.exception.status, WorkspaceStatus.ROOT_UNSAFE)
            self.assertEqual(self.vm.created, created)
        self.assertEqual(manager.status("ws-a"), WorkspaceStatus.READY)

    def test_settings_failure_rolls_back_registry_and_new_folder(self) -> None:
        manager, settings = self.build()
        target = self.base / "설정 실패"
        with mock.patch.object(SettingsStore, "transaction", side_effect=RuntimeError("synthetic settings failure")):
            with self.assertRaises(RuntimeError):
                manager.create_workspace("ws-a", "설정", target)
        self.assertEqual(manager.list_workspaces(), ())
        self.assertFalse(target.exists())
        self.assertIsNone(settings.get(Workspace, "ws-a"))
        self.assertEqual(self.vm.created, 1)  # Documented limit: the VM project is left behind.

    def test_concurrent_registration_loses_without_overwriting_and_leaves_orphan_vm_project(self) -> None:
        # F10/F12: a competing registration between VM creation and insert wins; nothing is overwritten.
        rival_root = self.base / "경쟁 폴더"
        rival_root.mkdir()
        rival_info = os.stat(rival_root)
        original_create = self.vm.create_project

        def racing_create(workspace_id, canonical_root, name):
            project_id = original_create(workspace_id, canonical_root, name)
            rival = WorkspaceRecord(workspace_id, "경쟁", str(rival_root.resolve()), rival_info.st_dev,
                                    rival_info.st_ino, "vm-project-rival")
            WorkspaceRegistry(self.state / "workspaces.json").update(
                lambda state: state.workspaces.__setitem__(workspace_id, rival))
            return project_id

        self.vm.create_project = racing_create
        manager, _ = self.build()
        target = self.base / "동시 등록"
        target.mkdir()
        with self.assertRaises(RevisionConflict):
            manager.open_workspace("ws-a", "동시", target)
        self.assertEqual(manager.get("ws-a").vm_project_id, "vm-project-rival")
        self.assertEqual(self.vm.created, 1)
        self.assertIn("vm-project-1", self.vm.projects)  # Orphan VM project (documented limit).

    def test_archive_hides_binding_and_restore_rechecks_conflicts(self) -> None:
        manager, _ = self.build()
        root = self.base / "보관"
        record = manager.create_workspace("ws-a", "보관", root)
        archived = manager.archive_workspace("ws-a", record.revision)
        self.assertEqual(manager.status("ws-a"), WorkspaceStatus.ARCHIVED)
        self.assertIsNone(manager.binding_for("ws-a"))
        with self.assertRaises(WorkspaceUnavailable):
            manager.select("ws-a")
        manager.open_workspace("ws-c", "대체", root)
        with self.assertRaises(RootConflict):
            manager.restore_workspace("ws-a", archived.revision)

    def test_missing_creation_seam_fails_closed_and_removes_the_new_folder(self) -> None:
        manager, _ = self.build(creator=False, registry_name="no-creator.json")
        target = self.base / "생성 실패"
        with self.assertRaises(VmProjectUnavailable):
            manager.create_workspace("ws-a", "실패", target)
        self.assertFalse(target.exists())
        self.assertEqual(manager.list_workspaces(), ())

    def test_corrupt_registry_is_rejected_and_preserved(self) -> None:
        self.state.mkdir()
        broken = self.state / "broken.json"
        broken.write_bytes(b"{synthetic damaged registry")
        before = broken.read_bytes()
        with self.assertRaises(InvalidDatabase):
            WorkspaceRegistry(broken)
        self.assertEqual(broken.read_bytes(), before)

    # ---------------------------------------------------------------- inputs
    def test_follow_up_task_consumes_actual_producer_artifact_with_same_bytes_and_digest(self) -> None:
        manager, _ = self.build()
        record = manager.create_workspace("ws-a", "산출물", self.base / "산출물 프로젝트")
        artifact = Path(record.canonical_root) / "산출물" / "결과 데이터.json"
        artifact.parent.mkdir()
        produced = '{"합성": "생산 결과"}'.encode("utf-8")
        artifact.write_bytes(produced)
        bound = manager.bind_input("ws-a", "산출물/결과 데이터.json", producer_task_id="vm-task-producer",
                                   expected_digest=sha(produced))
        self.assertEqual((bound.digest, bound.size, bound.revision), (sha(produced), len(produced), 1))
        manifest = manager.prepare_inputs("ws-a")
        binding = task_binding(manifest)  # The manifest digest and root ref fit the VM binding schema.
        manager.verify_task_binding(binding, manifest)
        consumed = manager.read_input(manifest, "산출물/결과 데이터.json")
        self.assertEqual(consumed, produced)
        self.assertEqual(manifest.entries[0].producer_task_id, "vm-task-producer")

    def test_producer_artifact_requires_declared_digest(self) -> None:
        # F2: a predecessor artifact cannot be bound to whatever bytes are on disk now.
        manager, _ = self.build()
        record = manager.create_workspace("ws-a", "선행", self.base / "선행 산출물")
        out = Path(record.canonical_root) / "out.json"
        out.write_bytes(b"synthetic possibly altered artifact")
        with self.assertRaises(InvalidRecord):
            manager.bind_input("ws-a", "out.json", producer_task_id="vm-task-producer")
        self.assertEqual(manager.input_states("ws-a"), ())
        manager.bind_input("ws-a", "out.json")  # A plain user input needs no producer digest.
        out.write_bytes(b"synthetic changed artifact")
        with self.assertRaises(InvalidRecord):
            manager.accept_input_change("ws-a", "out.json", 1, producer_task_id="vm-task-producer")
        state = manager.input_states("ws-a")[0]
        self.assertEqual((state.condition, state.recorded_digest),
                         (InputCondition.STALE, sha(b"synthetic possibly altered artifact")))

    def test_wrong_declared_digest_is_not_recorded(self) -> None:
        manager, _ = self.build()
        record = manager.create_workspace("ws-a", "변조", self.base / "변조 프로젝트")
        (Path(record.canonical_root) / "out.bin").write_bytes(b"synthetic bytes")
        with self.assertRaises(InputTampered):
            manager.bind_input("ws-a", "out.bin", expected_digest=sha(b"other bytes"))
        self.assertEqual(manager.input_states("ws-a"), ())

    def test_stale_tampered_and_missing_inputs_are_blocked_before_effect(self) -> None:
        manager, _ = self.build()
        record = manager.create_workspace("ws-a", "입력", self.base / "입력 프로젝트")
        root = Path(record.canonical_root)
        (root / "입력").mkdir()
        (root / "입력" / "요구사항.txt").write_bytes(b"requirement v1")
        (root / "입력" / "설정 값.txt").write_bytes(b"setting v1")
        manager.bind_input("ws-a", "입력/요구사항.txt")
        manager.bind_input("ws-a", "입력/설정 값.txt")
        manifest = manager.prepare_inputs("ws-a")
        binding = task_binding(manifest)
        effect = mock.Mock()

        def guarded_effect():
            manager.verify_task_binding(binding, manifest)
            effect()

        (root / "입력" / "요구사항.txt").write_bytes(b"requirement v2 (unaccepted)")
        states = {state.path: state.condition for state in manager.input_states("ws-a")}
        self.assertEqual(states, {"입력/요구사항.txt": InputCondition.STALE, "입력/설정 값.txt": InputCondition.CURRENT})
        with self.assertRaises(InputNotCurrent) as failure:
            manager.prepare_inputs("ws-a")
        self.assertIn(("입력/요구사항.txt", "stale"), failure.exception.details)
        with self.assertRaises(InputTampered):
            manager.read_input(manifest, "입력/요구사항.txt")
        with self.assertRaises(InputError):
            guarded_effect()

        (root / "입력" / "설정 값.txt").unlink()
        with self.assertRaises(InputNotCurrent) as failure:
            manager.read_input(manifest, "입력/설정 값.txt")
        self.assertEqual(failure.exception.details, (("입력/설정 값.txt", "missing"),))
        with self.assertRaises(InputNotCurrent):
            manager.prepare_inputs("ws-a", ["입력/설정 값.txt", "unbound.txt"])
        effect.assert_not_called()

    def test_post_effect_check_detects_bytes_changed_after_verification_in_real_subprocess(self) -> None:
        # F3: a native consumer reads through cwd; changes after verification must surface afterwards.
        manager, _ = self.build()
        record = manager.create_workspace("ws-a", "사후", self.base / "사후 검사")
        root = Path(record.canonical_root)
        (root / "입력").mkdir()
        target = root / "입력" / "요구사항.txt"
        target.write_bytes(b"requirement v1")
        manager.bind_input("ws-a", "입력/요구사항.txt")
        manifest = manager.prepare_inputs("ws-a")
        context = manager.verify_task_binding(task_binding(manifest), manifest)
        bound_digest = manifest.entries[0].digest
        self.assertEqual(self.run_in(context, [CONSUMER, "입력/요구사항.txt"]), bound_digest)
        manager.confirm_inputs_after_effect(manifest)

        target.write_bytes(b"requirement v2 (changed after verification)")
        self.assertNotEqual(self.run_in(context, [CONSUMER, "입력/요구사항.txt"]), bound_digest)
        with self.assertRaises(InputNotCurrent) as failure:
            manager.confirm_inputs_after_effect(manifest)
        self.assertEqual(failure.exception.details, (("입력/요구사항.txt", "stale"),))

    def test_verified_task_binding_returns_its_own_context_across_workspace_switch(self) -> None:
        # F4: the launch cwd comes from the verified manifest, not from the later selection.
        manager, _ = self.build()
        a = manager.create_workspace("ws-a", "A", self.base / "작업 A")
        b = manager.create_workspace("ws-b", "B", self.base / "작업 B")
        (Path(a.canonical_root) / "spec.md").write_bytes(b"synthetic spec")
        manager.bind_input("ws-a", "spec.md")
        manager.select("ws-a")
        manifest = manager.prepare_inputs("ws-a")
        context = manager.verify_task_binding(task_binding(manifest), manifest)
        manager.select("ws-b")
        self.assertEqual(manager.current_execution().canonical_root, b.canonical_root)
        self.assertEqual((context.workspace_id, context.canonical_root, context.vm_project_id),
                         ("ws-a", a.canonical_root, a.vm_project_id))
        self.assertTrue(os.path.samefile(self.run_in(context, ["import os; print(os.getcwd())"]),
                                         a.canonical_root))

    def test_task_binding_with_other_vm_project_id_is_rejected(self) -> None:
        manager, _ = self.build()
        record = manager.create_workspace("ws-a", "VM", self.base / "VM 불일치")
        (Path(record.canonical_root) / "spec.md").write_bytes(b"synthetic spec")
        manager.bind_input("ws-a", "spec.md")
        manifest = manager.prepare_inputs("ws-a")
        with self.assertRaises(InputBindingMismatch):
            manager.verify_task_binding(task_binding(manifest, vm_project_id="vm-project-other"), manifest)

    def test_accepted_change_bumps_revision_and_invalidates_older_manifest(self) -> None:
        manager, _ = self.build()
        record = manager.create_workspace("ws-a", "수정", self.base / "수정 프로젝트")
        path = Path(record.canonical_root) / "spec.md"
        path.write_bytes(b"v1")
        manager.bind_input("ws-a", "spec.md")
        first = manager.prepare_inputs("ws-a")
        path.write_bytes(b"v2")
        accepted = manager.accept_input_change("ws-a", "spec.md", 1)
        self.assertEqual((accepted.revision, accepted.digest), (2, sha(b"v2")))
        second = manager.prepare_inputs("ws-a")
        self.assertNotEqual(first.digest(), second.digest())
        with self.assertRaises(InputNotCurrent):
            manager.read_input(first, "spec.md")
        self.assertEqual(manager.read_input(second, "spec.md"), b"v2")

    def test_resolved_input_alias_case_or_short_name_is_unsafe(self) -> None:
        # F8: simulate Windows resolve() expanding a case variant / 8.3 name to another component.
        manager, _ = self.build()
        record = manager.create_workspace("ws-a", "별칭", self.base / "별칭 입력")
        root = Path(record.canonical_root)
        (root / "spec.md").write_bytes(b"synthetic spec")
        (root / "GIT~1").mkdir()
        (root / "GIT~1" / "config").write_bytes(b"synthetic git config")
        aliases = {root / "spec.md": root / "Spec.md", root / "GIT~1" / "config": root / ".git" / "config"}
        real_resolve = Path.resolve

        def resolve_alias(path, strict=False):
            alias = aliases.get(path)
            return alias if alias is not None else real_resolve(path, strict=strict)

        with mock.patch.object(Path, "resolve", new=resolve_alias):
            for path in ("spec.md", "GIT~1/config"):
                with self.subTest(path=path):
                    with self.assertRaises(InputNotCurrent) as failure:
                        manager.bind_input("ws-a", path)
                    self.assertEqual(failure.exception.details[0][1], "unsafe")
        self.assertEqual(manager.input_states("ws-a"), ())
        self.assertEqual(manager.bind_input("ws-a", "spec.md").digest, sha(b"synthetic spec"))

    def test_escaping_linked_and_cross_workspace_inputs_are_rejected(self) -> None:
        manager, _ = self.build()
        a = Path(manager.create_workspace("ws-a", "A", self.base / "ws-a").canonical_root)
        b = Path(manager.create_workspace("ws-b", "B", self.base / "ws-b").canonical_root)
        (b / "private.txt").write_bytes(b"synthetic other workspace data")
        (a / "own.txt").write_bytes(b"own")
        for path in ("../ws-b/private.txt", str(b / "private.txt"), "a\\b.txt", ".git/config",
                     "C:/x.txt", "./own.txt", "dir//own.txt"):
            with self.subTest(path=path):
                with self.assertRaises(InvalidInputPath):
                    manager.bind_input("ws-a", path)
        try:
            (a / "link.txt").symlink_to(b / "private.txt")
            (a / "linkdir").symlink_to(b, target_is_directory=True)
        except (OSError, NotImplementedError):
            pass
        else:
            for path in ("link.txt", "linkdir/private.txt"):
                with self.subTest(link=path):
                    with self.assertRaises(InputNotCurrent) as failure:
                        manager.bind_input("ws-a", path)
                    self.assertEqual(failure.exception.details[0][1], "unsafe")
        try:
            os.link(b / "private.txt", a / "hardlink.txt")
        except (OSError, NotImplementedError):
            pass
        else:
            with self.assertRaises(InputNotCurrent):
                manager.bind_input("ws-a", "hardlink.txt")

        manager.bind_input("ws-a", "own.txt")
        manifest = manager.prepare_inputs("ws-a")
        with self.assertRaises(InputBindingMismatch):
            manager.read_input(replace(manifest, workspace_id="ws-b"), "own.txt")
        with self.assertRaises(InputBindingMismatch):
            manager.read_input(manifest, "private.txt")
        foreign = manager.prepare_inputs("ws-b")
        with self.assertRaises(InputBindingMismatch):
            manager.verify_task_binding(task_binding(foreign), manifest)


if __name__ == "__main__":
    unittest.main()
