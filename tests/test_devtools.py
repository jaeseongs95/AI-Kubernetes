"""AIK-085 development tools against real temporary files and real child processes.

Authority and VM effect authority are synthetic fakes that return the real
aik_authority types; the workspace is a real aik_workspace binding with a fake
VM project port. No provider, credential, network or VM installation is used.
Linux success is development verification, not Windows support evidence.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_authority import (  # noqa: E402
    STATUS_AUTHORIZED, AuthorityCode, AuthorityDenied, EffectKind, EffectOutcomeUnknown, EffectReceipt,
    InvalidPolicy, ToolCallAuthorization, ToolPolicy, ToolSession, canonical_arguments,
)
from aik_authority.models import target_covers  # noqa: E402
from aik_workspace import ProjectInfo, WorkspaceManager, WorkspaceRegistry, sha256_digest  # noqa: E402
from aik_devtools import (  # noqa: E402
    EFFECT_KINDS, TOOL_CREATE_FILE, TOOL_READ_FILE, TOOL_RUN_VERIFICATION, TOOL_UPDATE_FILE, DevToolCode,
    DevToolError, DevToolPolicy, DevTools, EffectUnknown, InvalidDevToolPolicy, Invocation,
    VerificationCommand, files_scope_target, tool_declarations, verification_scope_target,
)
from aik_devtools import tools as devtools_module  # noqa: E402
from aik_runtime import paths as runtime_paths, storage as runtime_storage  # noqa: E402


WORKSPACE = "ws-dev"
INPUT_REVISION = "plan-rev-1"
PYTHON = os.path.abspath(sys.executable)
ORACLE = "hello 세계\n".encode("utf-8")
UNIT = (
    "import os, pathlib, sys\n"
    "with open(sys.argv[1], 'a', encoding='utf-8') as handle:\n"
    "    handle.write('run\\n')\n"
    "print(os.getcwd())\n"
    "app = pathlib.Path('src/app.txt')\n"
    "actual = app.read_bytes() if app.exists() else b''\n"
    "sys.exit(0 if actual == pathlib.Path('tests/expected.txt').read_bytes() else 1)\n"
)


class SimulatedCrash(BaseException):
    """Stands in for process death between an effect and its receipt."""


class FakeProjects:
    def __init__(self):
        self.roots = {}

    def create_project(self, workspace_id, canonical_root, name):
        self.roots["vm-project-1"] = canonical_root
        return "vm-project-1"

    def lookup_project(self, project_id):
        return ProjectInfo(project_id, self.roots.get(project_id))


class FakeAuthority:
    """Mimics the AuthorityGateway contract (scope, revisions, intent, receipts, replay rules).

    It does not check lease or expiry; that part of the real gateway is NOT_RUN.
    """

    def __init__(self, *, tools=None, targets=None):
        self.kinds = {declaration.tool_id: declaration.effect_kind for declaration in tool_declarations()}
        self.tools = frozenset(self.kinds if tools is None else tools)
        self.targets = tuple(targets or (files_scope_target(WORKSPACE), verification_scope_target(WORKSPACE)))
        self.requests, self.intents, self.receipts = [], [], []
        self.decisions, self.effects = {}, {}
        self.intent_failure = False

    def request(self, session, call):
        self.requests.append(call.tool_call_id)
        if call.tool_id not in self.tools:
            raise AuthorityDenied(AuthorityCode.TOOL_NOT_IN_AUTHORIZATION, subject="tool_id")
        if not any(target_covers(target, call.target) for target in self.targets):
            raise AuthorityDenied(AuthorityCode.TARGET_OUT_OF_SCOPE, subject="target")
        if call.input_revision != INPUT_REVISION:
            raise AuthorityDenied(AuthorityCode.STALE_INPUT_REVISION, subject="input_revision")
        if call.authorization_revision != 1:
            raise AuthorityDenied(AuthorityCode.STALE_AUTHORIZATION_REVISION, subject="authorization_revision")
        kind = self.kinds[call.tool_id]
        payload = json.dumps({"tool": call.tool_id, "target": call.target, "effect": call.effect_id,
                              "arguments": canonical_arguments(call.arguments)}, sort_keys=True)
        digest = "sha256:" + hashlib.sha256(payload.encode()).hexdigest()
        if call.effect_id is not None and self.effects.get(call.effect_id) == "outcome_unknown":
            raise AuthorityDenied(AuthorityCode.RECONCILIATION_REQUIRED, subject="effect_id")
        previous = self.decisions.get(call.tool_call_id)
        if previous is not None:
            if previous.request_digest != digest or previous.session_id != session.session_id:
                raise AuthorityDenied(AuthorityCode.TOOL_CALL_CONFLICT, subject="tool_call_id")
            if kind is EffectKind.NON_IDEMPOTENT:
                raise AuthorityDenied(AuthorityCode.EFFECT_ALREADY_AUTHORIZED, subject="effect_id")
            return previous
        if kind is not EffectKind.READ_ONLY:
            if self.intent_failure:
                raise EffectOutcomeUnknown(AuthorityCode.INTENT_UNKNOWN, subject="record_intent")
            self.intents.append((call.effect_id, call.tool_call_id, digest))
            self.effects[call.effect_id] = "intent_recorded"
        decision = ToolCallAuthorization(
            STATUS_AUTHORIZED, call.tool_call_id, call.tool_id, kind, call.effect_id, digest, session.session_id,
            session.role, call.workspace_id, "vm-project-1", "vm-task-1", "vm-attempt-1", "vm-auth-1", 1,
            "lease-1", 1, 1, "sha256:" + "0" * 64, "2030-01-01T00:00:00Z",
        )
        self.decisions[call.tool_call_id] = decision
        return decision

    def forward_receipt(self, session, tool_call_id, status, *, receipt_ref=None):
        decision = self.decisions.get(tool_call_id)
        if decision is None or decision.session_id != session.session_id:
            raise AuthorityDenied(AuthorityCode.TOOL_CALL_UNKNOWN, subject="tool_call_id")
        self.receipts.append((tool_call_id, status, receipt_ref))
        self.effects[decision.effect_id] = "outcome_unknown" if status == "unknown" else "receipt_forwarded"
        return EffectReceipt(decision.effect_id, tool_call_id, status, "recorded", status == "unknown")

    def effect_state(self, effect_id):
        return self.effects.get(effect_id)


class TamperedAuthority(FakeAuthority):
    """Returns an otherwise valid decision with one field replaced."""

    def __init__(self, **change):
        super().__init__()
        self.change = change

    def request(self, session, call):
        return replace(super().request(session, call), **self.change)


class LosingReceipts(FakeAuthority):
    """The receipt channel fails after the effect."""

    def forward_receipt(self, session, tool_call_id, status, *, receipt_ref=None):
        raise RuntimeError("synthetic receipt channel failure")


class DevToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="aik-devtools-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        folder = self.base / "작업 공간" / "프로젝트 A"
        folder.mkdir(parents=True)
        self.manager = WorkspaceManager(WorkspaceRegistry(self.base / "state" / "workspaces.json"), FakeProjects())
        self.manager.open_workspace(WORKSPACE, "개발 작업", folder)
        self.root = Path(self.manager.execution_context(WORKSPACE).canonical_root)
        (self.root / "tests").mkdir()
        (self.root / "tests" / "expected.txt").write_bytes(ORACLE)
        self.counter = self.base / "run-counter.txt"
        self.session = ToolSession("session-worker-1", "worker", WORKSPACE)
        self.authority = FakeAuthority()

    def policy(self) -> DevToolPolicy:
        return DevToolPolicy(WORKSPACE, (
            VerificationCommand("unit", (PYTHON, "-c", UNIT, str(self.counter)), timeout_seconds=30,
                                max_output_bytes=4096,
                                oracle_files=(("tests/expected.txt", sha256_digest(ORACLE)),)),
            VerificationCommand("fail", (PYTHON, "-c", "import sys; sys.exit(3)"), timeout_seconds=30),
            VerificationCommand("noisy", (PYTHON, "-c", "import sys; sys.stdout.write('x' * 10000); "
                                          "sys.stderr.write('e' * 10000)"),
                                timeout_seconds=30, max_output_bytes=100),
            VerificationCommand("slow", (PYTHON, "-c", "import time; time.sleep(30)"), timeout_seconds=0.5),
        ), analysis_only_paths=("fixtures",))

    def tools(self, authority=None) -> DevTools:
        return DevTools(authority=authority or self.authority, workspaces=self.manager, policy=self.policy())

    @staticmethod
    def inv(number: int, *, effect: bool = True, revision: str = INPUT_REVISION, cancel=None) -> Invocation:
        return Invocation(f"call-{number}", revision, 1, f"effect-{number}" if effect else None, cancel)

    def runs(self) -> int:
        return self.counter.read_text(encoding="utf-8").count("run") if self.counter.exists() else 0

    def assert_code(self, context, code: DevToolCode) -> DevToolError:
        self.assertEqual(context.exception.code, code)
        return context.exception

    # ----------------------------------------------------------------- tests
    def test_read_create_update_and_independent_verification_in_korean_space_root(self) -> None:
        tools = self.tools()
        created = tools.create_file(self.session, self.inv(1), "src/app.txt", b"draft\n")
        self.assertEqual((created.status, created.digest), ("created", sha256_digest(b"draft\n")))
        self.assertEqual((self.root / "src" / "app.txt").read_bytes(), b"draft\n")
        self.assertTrue(created.receipt.forwarded)
        read = tools.read_file(self.session, self.inv(2, effect=False), "src/app.txt")
        self.assertEqual((read.content, read.digest), (b"draft\n", created.digest))

        failing = tools.run_verification(self.session, self.inv(3), "unit")
        self.assertEqual((failing.status, failing.exit_code, failing.passed), ("completed", 1, False))
        updated = tools.update_file(self.session, self.inv(4), "src/app.txt", ORACLE, expected_digest=created.digest)
        self.assertEqual((updated.status, updated.previous_digest), ("updated", created.digest))
        self.assertEqual((self.root / "src" / "app.txt").read_bytes(), ORACLE)
        passing = tools.run_verification(self.session, self.inv(5), "unit")
        self.assertEqual((passing.exit_code, passing.passed, passing.oracle_ok), (0, True, True))
        self.assertEqual(passing.stdout.strip(), str(self.root))
        self.assertEqual(self.runs(), 2)

        self.assertEqual([status for _, status, _ in self.authority.receipts], ["succeeded"] * 4)
        self.assertEqual([intent[0] for intent in self.authority.intents],
                         ["effect-1", "effect-3", "effect-4", "effect-5"])
        self.assertTrue(tools.verify_result(failing))
        self.assertFalse(tools.verify_result(replace(failing, exit_code=0, passed=True)))

    def test_update_requires_current_digest_and_stale_input_changes_nothing(self) -> None:
        tools = self.tools()
        tools.create_file(self.session, self.inv(1), "src/app.txt", b"v1")
        with self.assertRaises(DevToolError) as context:
            tools.update_file(self.session, self.inv(2), "src/app.txt", b"v2", expected_digest=None)
        self.assert_code(context, DevToolCode.INVALID_REQUEST)
        stale = sha256_digest(b"something else")
        with self.assertRaises(DevToolError) as context:
            tools.update_file(self.session, self.inv(3), "src/app.txt", b"v2", expected_digest=stale)
        self.assert_code(context, DevToolCode.STALE_INPUT)
        self.assertEqual((self.root / "src" / "app.txt").read_bytes(), b"v1")
        self.assertEqual(self.authority.receipts[-1][:2], ("call-3", "failed"))
        receipts = len(self.authority.receipts)
        with self.assertRaises(DevToolError) as context:
            tools.update_file(self.session, self.inv(3), "src/app.txt", b"v2", expected_digest=stale)
        self.assert_code(context, DevToolCode.STALE_INPUT)
        self.assertEqual(len(self.authority.receipts), receipts)
        with self.assertRaises(DevToolError) as context:
            tools.read_file(self.session, self.inv(4, effect=False), "src/app.txt", expected_digest=stale)
        self.assert_code(context, DevToolCode.STALE_INPUT)

    def test_paths_outside_root_and_links_are_refused_before_any_write(self) -> None:
        tools = self.tools()
        for number, path in enumerate(("../escape.txt", "/etc/passwd", "a\\b.txt", ".git/config",
                                       "src/../../x.txt", "", "C:/x.txt"), start=1):
            with self.subTest(path=path):
                with self.assertRaises(DevToolError) as context:
                    tools.create_file(self.session, self.inv(number), path, b"x")
                self.assert_code(context, DevToolCode.PATH_REJECTED)
        self.assertEqual(self.authority.requests, [])
        self.assertFalse((self.base / "작업 공간" / "escape.txt").exists())

        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_bytes(b"outside bytes")
        try:
            (self.root / "link").symlink_to(outside, target_is_directory=True)
            (self.root / "alias.txt").symlink_to(outside / "secret.txt")
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"Symbolic links unavailable: {error}")
        with self.assertRaises(DevToolError) as context:
            tools.create_file(self.session, self.inv(20), "link/evil.txt", b"x")
        self.assert_code(context, DevToolCode.PATH_UNSAFE)
        self.assertFalse((outside / "evil.txt").exists())
        with self.assertRaises(DevToolError) as context:
            tools.read_file(self.session, self.inv(21, effect=False), "alias.txt")
        self.assert_code(context, DevToolCode.PATH_UNSAFE)
        with self.assertRaises(DevToolError) as context:
            tools.update_file(self.session, self.inv(22), "alias.txt", b"overwrite",
                              expected_digest=sha256_digest(b"outside bytes"))
        self.assert_code(context, DevToolCode.PATH_UNSAFE)
        self.assertEqual((outside / "secret.txt").read_bytes(), b"outside bytes")

    def test_authorization_scope_and_stale_input_are_checked_before_any_effect(self) -> None:
        denied = self.tools(FakeAuthority(tools=(TOOL_READ_FILE,)))
        with self.assertRaises(DevToolError) as context:
            denied.create_file(self.session, self.inv(1), "src/new.txt", b"x")
        error = self.assert_code(context, DevToolCode.AUTHORIZATION_DENIED)
        self.assertEqual(error.authority_code, "TOOL_NOT_IN_AUTHORIZATION")
        with self.assertRaises(DevToolError):
            denied.run_verification(self.session, self.inv(2), "unit")
        self.assertFalse((self.root / "src" / "new.txt").exists())
        self.assertEqual(self.runs(), 0)

        scoped = self.tools(FakeAuthority(targets=(files_scope_target(WORKSPACE, "docs"),)))
        with self.assertRaises(DevToolError) as context:
            scoped.create_file(self.session, self.inv(3), "src/new.txt", b"x")
        error = self.assert_code(context, DevToolCode.AUTHORIZATION_DENIED)
        self.assertEqual(error.authority_code, "TARGET_OUT_OF_SCOPE")
        self.assertTrue(error.escalation)
        created = scoped.create_file(self.session, self.inv(4), "docs/한글 문서.md", "내용".encode())
        self.assertEqual(created.status, "created")
        self.assertEqual((self.root / "docs" / "한글 문서.md").read_text(encoding="utf-8"), "내용")

        tools = self.tools()
        with self.assertRaises(DevToolError) as context:
            tools.create_file(self.session, self.inv(5, revision="plan-rev-0"), "src/stale.txt", b"x")
        self.assertEqual(self.assert_code(context, DevToolCode.AUTHORIZATION_DENIED).authority_code,
                         "STALE_INPUT_REVISION")
        self.assertFalse((self.root / "src" / "stale.txt").exists())
        other = ToolSession("session-other", "worker", "ws-other")
        with self.assertRaises(DevToolError) as context:
            tools.read_file(other, self.inv(6, effect=False), "tests/expected.txt")
        self.assert_code(context, DevToolCode.WORKSPACE_MISMATCH)

    def test_result_resend_returns_stored_result_without_rerunning(self) -> None:
        tools = self.tools()
        first = tools.run_verification(self.session, self.inv(1), "unit")
        requests = len(self.authority.requests)
        again = tools.run_verification(self.session, self.inv(1), "unit")
        self.assertTrue(again.replayed)
        self.assertEqual(replace(again, replayed=False), first)
        self.assertEqual(self.runs(), 1)
        self.assertEqual(len(self.authority.requests), requests)
        self.assertTrue(tools.verify_result(again))

        created = tools.create_file(self.session, self.inv(2), "src/app.txt", b"a")
        resent = tools.create_file(self.session, self.inv(2), "src/app.txt", b"a")
        self.assertEqual(replace(resent, replayed=False), created)
        with self.assertRaises(DevToolError) as context:
            tools.run_verification(self.session, self.inv(1), "fail")
        self.assert_code(context, DevToolCode.CALL_CONFLICT)
        with self.assertRaises(DevToolError) as context:
            tools.run_verification(ToolSession("session-worker-2", "worker", WORKSPACE), self.inv(1), "unit")
        self.assert_code(context, DevToolCode.CALL_CONFLICT)
        self.assertEqual(self.runs(), 1)

    def test_interruption_after_effect_before_receipt_is_observed_or_unknown(self) -> None:
        tools = self.tools()
        created = tools.create_file(self.session, self.inv(1), "src/app.txt", b"v1")
        with mock.patch.object(DevTools, "_after_effect", side_effect=SimulatedCrash):
            with self.assertRaises(SimulatedCrash):
                tools.update_file(self.session, self.inv(2), "src/app.txt", b"v2", expected_digest=created.digest)
        target = self.root / "src" / "app.txt"
        self.assertEqual(target.read_bytes(), b"v2")
        self.assertNotIn("call-2", [receipt[0] for receipt in self.authority.receipts])
        inode = target.stat().st_ino

        restarted = self.tools()
        observed = restarted.update_file(self.session, self.inv(2), "src/app.txt", b"v2",
                                         expected_digest=created.digest)
        self.assertEqual(observed.status, "already_applied")
        self.assertEqual(target.stat().st_ino, inode)
        self.assertEqual(self.authority.receipts[-1][:2], ("call-2", "succeeded"))

        with mock.patch.object(DevTools, "_after_effect", side_effect=SimulatedCrash):
            with self.assertRaises(SimulatedCrash):
                tools.run_verification(self.session, self.inv(3), "unit")
        self.assertEqual(self.runs(), 1)
        with self.assertRaises(EffectUnknown):
            tools.run_verification(self.session, self.inv(3), "unit")
        with self.assertRaises(EffectUnknown) as context:
            self.tools().run_verification(self.session, self.inv(3), "unit")
        self.assertEqual(context.exception.authority_code, "EFFECT_ALREADY_AUTHORIZED")
        self.assertEqual(self.authority.receipts[-1][:2], ("call-3", "unknown"))
        with self.assertRaises(EffectUnknown) as context:
            self.tools().run_verification(self.session, self.inv(3), "unit")
        self.assertEqual(context.exception.code, DevToolCode.RECONCILIATION_REQUIRED)
        self.assertEqual(self.runs(), 1)

    def test_verification_reports_exit_code_truncation_and_timeout(self) -> None:
        tools = self.tools()
        failed = tools.run_verification(self.session, self.inv(1), "fail")
        self.assertEqual((failed.status, failed.exit_code, failed.passed), ("completed", 3, False))
        noisy = tools.run_verification(self.session, self.inv(2), "noisy")
        self.assertEqual((len(noisy.stdout), noisy.stdout_bytes, noisy.stdout_truncated), (100, 10000, True))
        self.assertTrue(noisy.stderr_truncated and noisy.output_limit_exceeded)
        self.assertEqual(noisy.exit_code, 0)
        started = time.monotonic()
        slow = tools.run_verification(self.session, self.inv(3), "slow")
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual((slow.status, slow.timed_out, slow.exit_code, slow.passed), ("timed_out", True, None, False))
        self.assertEqual(self.authority.receipts[-1][:2], ("call-3", "failed"))
        requests = len(self.authority.requests)
        with self.assertRaises(DevToolError) as context:
            tools.run_verification(self.session, self.inv(4), "rm-rf")
        self.assert_code(context, DevToolCode.COMMAND_NOT_ALLOWED)
        self.assertEqual(len(self.authority.requests), requests)

    def test_policy_rejects_shells_and_worker_cannot_change_the_oracle(self) -> None:
        with self.assertRaises(InvalidDevToolPolicy) as context:
            VerificationCommand("shell", ("/bin/sh", "-c", "true"))
        self.assertEqual(context.exception.code, DevToolCode.UNSUPPORTED_CAPABILITY)
        for argv in (("python3", "-c", "pass"), [PYTHON, "-c", "pass"], ("C:\\x\\run.bat",)):
            with self.subTest(argv=argv), self.assertRaises(InvalidDevToolPolicy):
                VerificationCommand("bad", argv)
        self.assertEqual(list(inspect.signature(DevTools.run_verification).parameters),
                         ["self", "session", "invocation", "command_id"])

        tools = self.tools()
        self.assertIn("shell", tools.capabilities()["unsupported"])
        for number, path in ((1, "tests/expected.txt"), (2, "fixtures/new.txt"), (3, "TESTS/expected.txt")):
            with self.subTest(path=path), self.assertRaises(DevToolError) as context:
                tools.create_file(self.session, self.inv(number), path, b"pass")
            self.assert_code(context, DevToolCode.PATH_PROTECTED)
        with self.assertRaises(DevToolError) as context:
            tools.update_file(self.session, self.inv(4), "tests/expected.txt", b"x",
                              expected_digest=sha256_digest(ORACLE))
        self.assert_code(context, DevToolCode.PATH_PROTECTED)
        self.assertEqual(self.authority.requests, [])
        self.assertEqual(tools.read_file(self.session, self.inv(5, effect=False), "tests/expected.txt").content, ORACLE)

        # A native worker could edit the oracle outside these tools: the pinned
        # digest then blocks the run instead of producing a self-made success.
        (self.root / "src").mkdir()
        (self.root / "src" / "app.txt").write_bytes(b"cheat")
        (self.root / "tests" / "expected.txt").write_bytes(b"cheat")
        result = tools.run_verification(self.session, self.inv(6), "unit")
        self.assertEqual((result.status, result.passed, result.oracle_ok, result.exit_code),
                         ("oracle_mismatch", False, False, None))
        self.assertEqual(self.runs(), 0)
        self.assertEqual(self.authority.receipts[-1][:2], ("call-6", "failed"))

    def test_effect_classification_matches_gateway_declarations(self) -> None:
        self.assertEqual(dict(EFFECT_KINDS), {
            TOOL_READ_FILE: EffectKind.READ_ONLY, TOOL_CREATE_FILE: EffectKind.IDEMPOTENT,
            TOOL_UPDATE_FILE: EffectKind.IDEMPOTENT, TOOL_RUN_VERIFICATION: EffectKind.NON_IDEMPOTENT,
        })
        everything = tuple(EFFECT_KINDS)
        policy = ToolPolicy(WORKSPACE, tool_declarations(), director_tools=everything, worker_tools=everything,
                            reviewer_tools=(TOOL_READ_FILE,))
        self.assertEqual(policy.allowed("worker"), frozenset(everything))
        with self.assertRaises(InvalidPolicy):
            ToolPolicy(WORKSPACE, tool_declarations(), reviewer_tools=(TOOL_UPDATE_FILE,))

    def test_intent_unknown_cancel_and_unavailable_workspace_prevent_effects(self) -> None:
        tools = self.tools()
        self.authority.intent_failure = True
        with self.assertRaises(EffectUnknown):
            tools.create_file(self.session, self.inv(1), "src/a.txt", b"x")
        self.assertFalse((self.root / "src" / "a.txt").exists())
        self.authority.intent_failure = False

        event = threading.Event()
        event.set()
        with self.assertRaises(DevToolError) as context:
            tools.create_file(self.session, self.inv(2, cancel=event), "src/b.txt", b"x")
        self.assert_code(context, DevToolCode.CANCELLED)
        self.assertFalse((self.root / "src" / "b.txt").exists())
        self.assertEqual(self.authority.receipts[-1][:2], ("call-2", "cancelled"))

        self.root.rename(self.root.with_name("moved"))
        requests = len(self.authority.requests)
        with self.assertRaises(DevToolError) as context:
            tools.create_file(self.session, self.inv(3), "src/c.txt", b"x")
        self.assertEqual(self.assert_code(context, DevToolCode.WORKSPACE_UNAVAILABLE).subject, "root_missing")
        self.assertEqual(len(self.authority.requests), requests)

    # ------------------------------------------------- review regressions (F1)
    def test_worker_shadow_module_in_root_cannot_turn_verification_into_success(self) -> None:
        tools = self.tools()
        tools.create_file(self.session, self.inv(1), "pathlib.py", b"import sys\nsys.exit(0)\n")
        result = tools.run_verification(self.session, self.inv(2), "unit")
        self.assertTrue(result.oracle_ok)
        # The real pathlib is used: src/app.txt is missing, so the judge fails.
        self.assertEqual((result.status, result.exit_code, result.passed), ("completed", 1, False))
        self.assertEqual(self.runs(), 1)

    def test_executable_inside_workspace_is_refused_before_running(self) -> None:
        executable = str(self.root / "venv" / "bin" / "python")
        policy = DevToolPolicy(WORKSPACE, (VerificationCommand("local", (executable, "-c", "pass"),
                                                               timeout_seconds=30),))
        tools = DevTools(authority=self.authority, workspaces=self.manager, policy=policy)
        with self.assertRaises(DevToolError) as context:
            tools.run_verification(self.session, self.inv(1), "local")
        self.assertEqual(self.assert_code(context, DevToolCode.COMMAND_NOT_ALLOWED).subject, "argv")
        self.assertEqual(self.authority.receipts[-1][:2], ("call-1", "failed"))

    def test_command_protected_paths_cannot_be_written_by_worker_tools(self) -> None:
        command = VerificationCommand("judge", (PYTHON, "-c", "pass"),
                                      protected_paths=("conftest.py", "tests/support"))
        tools = DevTools(authority=self.authority, workspaces=self.manager,
                         policy=DevToolPolicy(WORKSPACE, (command,)))
        for number, path in ((1, "conftest.py"), (2, "tests/support/helper.py"), (3, "CONFTEST.py")):
            with self.subTest(path=path), self.assertRaises(DevToolError) as context:
                tools.create_file(self.session, self.inv(number), path, b"pass")
            self.assert_code(context, DevToolCode.PATH_PROTECTED)
        self.assertEqual(self.authority.requests, [])
        self.assertIn("conftest.py", tools.capabilities()["analysis_only_paths"])
        self.assertNotEqual(command.definition_digest(),
                            VerificationCommand("judge", (PYTHON, "-c", "pass")).definition_digest())
        with self.assertRaises(InvalidDevToolPolicy):
            VerificationCommand("judge", (PYTHON,), protected_paths=("../outside",))

    # ------------------------------------------------- review regressions (F2)
    def test_resent_update_after_interruption_and_later_change_reports_unknown_not_failed(self) -> None:
        tools = self.tools()
        created = tools.create_file(self.session, self.inv(1), "src/app.txt", b"v1")
        with mock.patch.object(DevTools, "_after_effect", side_effect=SimulatedCrash):
            with self.assertRaises(SimulatedCrash):
                tools.update_file(self.session, self.inv(2), "src/app.txt", b"v2", expected_digest=created.digest)
        tools.update_file(self.session, self.inv(3), "src/app.txt", b"v3", expected_digest=sha256_digest(b"v2"))
        with self.assertRaises(EffectUnknown) as context:
            tools.update_file(self.session, self.inv(2), "src/app.txt", b"v2", expected_digest=created.digest)
        self.assertEqual(context.exception.code, DevToolCode.OUTCOME_UNKNOWN)
        self.assertEqual(self.authority.receipts[-1][:2], ("call-2", "unknown"))
        self.assertNotIn(("call-2", "failed"), [receipt[:2] for receipt in self.authority.receipts])
        self.assertEqual((self.root / "src" / "app.txt").read_bytes(), b"v3")

    def test_restarted_create_resend_after_later_change_reports_unknown(self) -> None:
        with mock.patch.object(DevTools, "_after_effect", side_effect=SimulatedCrash):
            with self.assertRaises(SimulatedCrash):
                self.tools().create_file(self.session, self.inv(1), "src/app.txt", b"a")
        self.tools().update_file(self.session, self.inv(2), "src/app.txt", b"b", expected_digest=sha256_digest(b"a"))
        with self.assertRaises(EffectUnknown):
            self.tools().create_file(self.session, self.inv(1), "src/app.txt", b"a")
        self.assertEqual(self.authority.receipts[-1][:2], ("call-1", "unknown"))
        self.assertNotIn(("call-1", "failed"), [receipt[:2] for receipt in self.authority.receipts])

    def test_resumed_call_cancelled_after_possible_effect_reports_unknown(self) -> None:
        tools = self.tools()
        with mock.patch.object(DevTools, "_after_effect", side_effect=SimulatedCrash):
            with self.assertRaises(SimulatedCrash):
                tools.create_file(self.session, self.inv(1), "src/app.txt", b"a")
        event = threading.Event()
        event.set()
        with self.assertRaises(EffectUnknown):
            tools.create_file(self.session, self.inv(1, cancel=event), "src/app.txt", b"a")
        self.assertEqual(self.authority.receipts[-1][:2], ("call-1", "unknown"))

    # ------------------------------------------------- review regressions (F3)
    def test_death_between_link_and_unlink_is_cleaned_and_observed_as_applied(self) -> None:
        real_link = os.link

        def link_then_crash(source, destination, *args, **kwargs):
            real_link(source, destination)
            raise SimulatedCrash

        with mock.patch.object(os, "link", new=link_then_crash), \
                mock.patch.object(Path, "unlink", new=lambda self, missing_ok=False: None):
            with self.assertRaises(SimulatedCrash):
                self.tools().create_file(self.session, self.inv(1), "src/app.txt", b"a")
        target = self.root / "src" / "app.txt"
        self.assertEqual(target.stat().st_nlink, 2, "The fault harness did not leave a temporary twin.")

        observed = self.tools().create_file(self.session, self.inv(1), "src/app.txt", b"a")
        self.assertEqual(observed.status, "already_applied")
        self.assertEqual(target.stat().st_nlink, 1)
        self.assertEqual([name for name in os.listdir(target.parent) if name.startswith(".aik-devtools-")], [])
        self.assertEqual(self.authority.receipts[-1][:2], ("call-1", "succeeded"))

    # ------------------------------------------------- review regressions (F4)
    def test_read_resend_rereads_bytes_and_reports_stale_after_change(self) -> None:
        tools = self.tools()
        (self.root / "src").mkdir()
        target = self.root / "src" / "app.txt"
        target.write_bytes(b"one")
        first = tools.read_file(self.session, self.inv(1, effect=False), "src/app.txt")
        requests = len(self.authority.requests)
        again = tools.read_file(self.session, self.inv(1, effect=False), "src/app.txt")
        self.assertTrue(again.replayed)
        self.assertEqual(replace(again, replayed=False), first)
        self.assertGreater(len(self.authority.requests), requests)
        self.assertTrue(tools.verify_result(again))
        target.write_bytes(b"two")
        with self.assertRaises(DevToolError) as context:
            tools.read_file(self.session, self.inv(1, effect=False), "src/app.txt")
        self.assert_code(context, DevToolCode.STALE_INPUT)

    def test_stored_results_are_bounded_and_evicted_effects_are_never_rerun(self) -> None:
        tools = DevTools(authority=self.authority, workspaces=self.manager, policy=self.policy(), max_entries=2)
        (self.root / "src").mkdir()
        (self.root / "src" / "app.txt").write_bytes(b"data")
        reads = [tools.read_file(self.session, self.inv(number, effect=False), "src/app.txt")
                 for number in range(1, 5)]
        self.assertFalse(tools.verify_result(reads[0]))
        self.assertFalse(tools.verify_result(reads[1]))
        self.assertTrue(tools.verify_result(reads[3]))
        self.assertEqual(len(tools._entries), 2)

        tools.run_verification(self.session, self.inv(10), "unit")
        tools.run_verification(self.session, self.inv(11), "fail")
        tools.run_verification(self.session, self.inv(12), "fail")
        with self.assertRaises(EffectUnknown):
            tools.run_verification(self.session, self.inv(10), "unit")
        self.assertEqual(self.runs(), 1)

        small = DevTools(authority=self.authority, workspaces=self.manager, policy=self.policy(),
                         max_stored_bytes=150)
        noisy = small.run_verification(self.session, self.inv(20), "noisy")
        self.assertFalse(small.verify_result(noisy))
        self.assertEqual(small._stored_bytes, 0)
        with self.assertRaises(InvalidDevToolPolicy):
            DevTools(authority=self.authority, workspaces=self.manager, policy=self.policy(), max_entries=0)

    # ------------------------------------------------- review regressions (F5)
    @unittest.skipIf(os.name == "nt", "Process group kill after normal exit is POSIX-only (Windows NOT_RUN).")
    def test_background_descendant_is_stopped_after_normal_exit(self) -> None:
        marker = self.base / "late-descendant.txt"
        grandchild = "import pathlib, sys, time; time.sleep(1.0); pathlib.Path(sys.argv[1]).write_text('late')"
        spawn = "import subprocess, sys; subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]])"
        policy = DevToolPolicy(WORKSPACE, (VerificationCommand(
            "spawn", (PYTHON, "-c", spawn, grandchild, str(marker)), timeout_seconds=30),))
        tools = DevTools(authority=self.authority, workspaces=self.manager, policy=policy)
        result = tools.run_verification(self.session, self.inv(1), "spawn")
        self.assertEqual((result.status, result.exit_code), ("completed", 0))
        time.sleep(2.0)
        self.assertFalse(marker.exists(), "A descendant kept running after the receipt.")

    # ------------------------------------------------- review regressions (F7)
    def test_effect_id_reused_by_another_tool_call_is_refused_locally(self) -> None:
        tools = self.tools()
        tools.run_verification(self.session, Invocation("call-a", INPUT_REVISION, 1, "effect-shared"), "fail")
        requests = len(self.authority.requests)
        with self.assertRaises(DevToolError) as context:
            tools.run_verification(self.session, Invocation("call-b", INPUT_REVISION, 1, "effect-shared"), "unit")
        self.assertEqual(self.assert_code(context, DevToolCode.CALL_CONFLICT).subject, "effect_id")
        self.assertEqual(len(self.authority.requests), requests)
        self.assertEqual(self.runs(), 0)

    # ------------------------------------------------- review coverage (F8)
    def test_script_extension_with_native_absolute_path_is_unsupported(self) -> None:
        executable = "C:\\x\\run.bat" if os.name == "nt" else "/opt/x/run.bat"
        with self.assertRaises(InvalidDevToolPolicy) as context:
            VerificationCommand("script", (executable,))
        self.assertEqual(context.exception.code, DevToolCode.UNSUPPORTED_CAPABILITY)

    def test_tampered_decision_is_invalid_and_nothing_runs(self) -> None:
        with self.assertRaises(DevToolError) as context:
            self.tools(TamperedAuthority(effect_kind=EffectKind.IDEMPOTENT)).run_verification(
                self.session, self.inv(1), "unit")
        self.assert_code(context, DevToolCode.AUTHORIZATION_INVALID)
        self.assertEqual(self.runs(), 0)
        with self.assertRaises(DevToolError) as context:
            self.tools(TamperedAuthority(session_id="session-other")).create_file(
                self.session, self.inv(2), "src/new.txt", b"x")
        self.assert_code(context, DevToolCode.AUTHORIZATION_INVALID)
        self.assertFalse((self.root / "src" / "new.txt").exists())

    def test_receipt_forwarding_failure_keeps_result_and_marks_reconciliation(self) -> None:
        created = self.tools(LosingReceipts()).create_file(self.session, self.inv(1), "src/app.txt", b"a")
        self.assertEqual(created.status, "created")
        self.assertEqual((created.receipt.forwarded, created.receipt.reconciliation_required,
                          created.receipt.error_code), (False, True, "RECEIPT_UNKNOWN"))
        self.assertEqual((self.root / "src" / "app.txt").read_bytes(), b"a")

    def test_oversized_content_and_file_are_too_large(self) -> None:
        tools = self.tools()
        with self.assertRaises(DevToolError) as context:
            tools.create_file(self.session, self.inv(1), "src/big.txt", b"x" * (4 * 1024 * 1024 + 1))
        self.assert_code(context, DevToolCode.TOO_LARGE)
        self.assertEqual(self.authority.requests, [])
        small = DevTools(authority=self.authority, workspaces=self.manager,
                         policy=DevToolPolicy(WORKSPACE, max_file_bytes=8))
        (self.root / "src").mkdir()
        (self.root / "src" / "big.txt").write_bytes(b"y" * 16)
        with self.assertRaises(DevToolError) as context:
            small.read_file(self.session, self.inv(2, effect=False), "src/big.txt")
        self.assert_code(context, DevToolCode.TOO_LARGE)

    def test_cancel_during_run_stops_the_process(self) -> None:
        policy = DevToolPolicy(WORKSPACE, (VerificationCommand(
            "long", (PYTHON, "-c", "import time; time.sleep(30)"), timeout_seconds=30),))
        tools = DevTools(authority=self.authority, workspaces=self.manager, policy=policy)
        event = threading.Event()
        timer = threading.Timer(0.5, event.set)
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        result = tools.run_verification(self.session, self.inv(1, cancel=event), "long")
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual((result.status, result.exit_code, result.passed, result.timed_out),
                         ("cancelled", None, False, False))
        self.assertEqual(self.authority.receipts[-1][:2], ("call-1", "cancelled"))

    def test_update_through_hardlinked_file_is_refused(self) -> None:
        (self.root / "src").mkdir()
        inside = self.root / "src" / "data.txt"
        inside.write_bytes(b"orig")
        outside = self.base / "outside-link.txt"
        try:
            outside.hardlink_to(inside)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"Hard links unavailable: {error}")
        with self.assertRaises(DevToolError) as context:
            self.tools().update_file(self.session, self.inv(1), "src/data.txt", b"new",
                                     expected_digest=sha256_digest(b"orig"))
        self.assert_code(context, DevToolCode.PATH_UNSAFE)
        self.assertEqual((inside.read_bytes(), outside.read_bytes()), (b"orig", b"orig"))

    # ------------------------------------------------- review regressions (F10)
    def test_devtools_does_not_bind_private_runtime_helpers(self) -> None:
        for private in (runtime_paths._inspect, runtime_storage._sync_directory):
            self.assertFalse(any(value is private for value in vars(devtools_module).values()))


if __name__ == "__main__":
    unittest.main()
