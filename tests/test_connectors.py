"""AIK-046/047 vendor connectors over the existing adapters and host bridges.

Only synthetic fixtures are used. That means fake HTTP transports, the
test-only ``FakeVaultBackend`` and fake host runners that mimic assumed,
unverified CLI output. No network, no real key, no real CLI and no vendor is
contacted. A pass here is implementation conformance. It is not a vendor
observation or a live host round trip (NOT_RUN). Linux execution is
development verification, not Windows support evidence.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import secrets
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_config import SettingsStore, Workspace  # noqa: E402
from aik_connect import (  # noqa: E402
    CheckResult, CheckState, ConnectError, ConnectionService, HostLogin, KeyVerifier, LoginState, LoginStatus,
)
from aik_connection import ConnectionSpec, ConnectionStore, VaultCredentialRefChecker  # noqa: E402
from aik_vault import FakeVaultBackend, VaultAccessDenied  # noqa: E402

from aik_adapter_openai import HttpResponse as OpenAIResponse, OpenAIApiAdapter  # noqa: E402
from aik_adapter_openai.adapter import PROVIDER as OPENAI_PROVIDER  # noqa: E402
from aik_adapter_openai.connector import INFO as OPENAI_INFO, OpenAIKeyVerifier  # noqa: E402
from aik_adapter_openai.connector import create_connector as create_openai  # noqa: E402

from aik_adapter_anthropic import (  # noqa: E402
    MODELS_PATH, AnthropicAdapter, HttpResponse as AnthropicResponse, TransportNotSent,
)
from aik_adapter_anthropic.adapter import PROVIDER as ANTHROPIC_PROVIDER  # noqa: E402
from aik_adapter_anthropic.connector import INFO as ANTHROPIC_INFO, AnthropicKeyVerifier  # noqa: E402
from aik_adapter_anthropic.connector import create_connector as create_anthropic  # noqa: E402

from aik_host_claude import (  # noqa: E402
    PROVIDER as CLAUDE_PROVIDER, ClaudeHostBridge, CommandResult, CommandSpec, RunnerNotStarted,
    account_fingerprint,
)
from aik_host_claude.connector import ClaudeHostLogin  # noqa: E402

from aik_host_codex import CodexHostBridge, HostNotFound, RunnerError, RunResult, validate_invocation  # noqa: E402
from aik_host_codex.bridge import PROVIDER as CODEX_PROVIDER  # noqa: E402
from aik_host_codex.connector import CodexHostLogin  # noqa: E402

OPENAI_SECRET = "FAKE-openai-key-0000-SYNTHETIC-not-real-A"
OPENAI_ROTATED = "FAKE-openai-key-1111-SYNTHETIC-rotated-B"
ANTHROPIC_SECRET = "sk-ant-SYNTHETIC-FAKE-KEY-0001-abcdefghijklmnop"
ANTHROPIC_ROTATED = "sk-ant-SYNTHETIC-FAKE-KEY-0002-qrstuvwxyzabcd"
SECRET_MARKER = "SYNTHETIC_SECRET_MUST_NOT_LEAK_0001"
VENDOR_TEXT = "synthetic vendor message must not leak"
ACCOUNT_A = "acct-synthetic-a"
ACCOUNT_B = "acct-synthetic-b"
MODEL = "synthetic-model-a"
WORKSPACE = "local"
LEAKS = (OPENAI_SECRET, OPENAI_ROTATED, ANTHROPIC_SECRET, ANTHROPIC_ROTATED, SECRET_MARKER, VENDOR_TEXT,
         "secret://", "Incorrect API key", ACCOUNT_A, ACCOUNT_B, "/synthetic/")
CODE = re.compile(r"[A-Z][A-Z0-9_]{1,63}\Z")
FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
TRANSPORT_UNKNOWN = {"TRANSPORT_FAILED", "VENDOR_UNAVAILABLE"}


def make_error(cls) -> BaseException:
    """Build an exception regardless of whether it takes a message."""
    try:
        return cls()
    except TypeError:
        return cls("synthetic failure " + SECRET_MARKER)


class DenyingVault(FakeVaultBackend):
    """Test-only vault; ``deny`` makes every resolve fail with an access denial."""

    deny = False

    def resolve(self, *args, **kwargs):
        if self.deny:
            raise VaultAccessDenied()
        return super().resolve(*args, **kwargs)


class Registry:
    """A real ConnectionStore over synthetic files: the connectors require real ``Connection`` objects."""

    def __init__(self, root: Path, vault):
        settings = SettingsStore(root / "settings.sqlite3")
        settings.save(Workspace(WORKSPACE, "합성 작업공간"))
        self.store = ConnectionStore(settings, credential_checker=VaultCredentialRefChecker(vault),
                                     reference_sources=())
        self.entries: dict = {}

    def api(self, info, connection_id: str, reference: str):
        entry = self.store.register(ConnectionSpec(
            connection_id=connection_id, workspace_id=WORKSPACE, provider=info.provider,
            account_label="합성 " + connection_id, auth_kind="api_key", execution_kind="api",
            official_host=info.official_host, host_profile=info.host_profile,
            account_binding="key-" + secrets.token_hex(12), credential_ref=reference,
        ))
        self.entries[connection_id] = entry
        return entry.value

    def subscription(self, info, connection_id: str, account_binding: str):
        entry = self.store.register(ConnectionSpec(
            connection_id=connection_id, workspace_id=WORKSPACE, provider=info.provider,
            account_label="합성 " + connection_id, auth_kind="host_session", execution_kind="subscription",
            official_host=info.official_host, host_profile=info.host_profile, account_binding=account_binding,
        ))
        self.entries[connection_id] = entry
        return entry.value

    def rebind(self, connection_id: str):
        entry = self.entries[connection_id]
        entry = self.store.rebind(connection_id, entry.revision, account_binding="key-" + secrets.token_hex(12))
        self.entries[connection_id] = entry
        return entry.value


class Leaks:
    def assert_clean(self, *values) -> None:
        for value in values:
            text = repr(value) + str(value)
            if isinstance(value, (dict, list)):
                text += json.dumps(value, ensure_ascii=False)
            if isinstance(value, ConnectError):
                text += value.code
            for leak in LEAKS:
                self.assertNotIn(leak, text)
            code = getattr(value, "code", None)
            if code is not None:
                self.assertRegex(code, CODE)


# -- OpenAI fixtures (same shape as tests/test_adapter_openai.py) -------------------------------
class OpenAITransport:
    kind = "fixture"

    def __init__(self) -> None:
        self.script: dict = {}
        self.seen: list = []

    def add(self, method: str, path: str, *outcomes) -> None:
        self.script.setdefault((method, path), []).extend(outcomes)

    def send(self, request, *, timeout):
        self.seen.append((request.method, request.path, request.header("Authorization")))
        queue = self.script.get((request.method, request.path))
        if not queue:
            return OpenAIResponse(599, (), b"")
        outcome = queue.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def openai_resp(status: int, data) -> OpenAIResponse:
    return OpenAIResponse(status, (), json.dumps(data).encode("utf-8"))


OPENAI_MODELS = {"object": "list", "data": []}


# -- Anthropic fixtures (same shape as tests/test_adapter_anthropic.py) -------------------------
class AnthropicTransport:
    source = "synthetic_fixture"

    def __init__(self) -> None:
        self.routes: dict = {}
        self.requests: list = []

    def send(self, request):
        self.requests.append(request)
        route = self.routes.get((request.method, urlsplit(request.url).path))
        if isinstance(route, list):
            route = route.pop(0)
        if route is None:
            raise AssertionError("unexpected synthetic route")
        if isinstance(route, BaseException):
            raise route
        return route


def anthropic_json(status: int, payload) -> AnthropicResponse:
    return AnthropicResponse(status, (("content-type", "application/json"),), json.dumps(payload).encode("utf-8"))


def anthropic_error(status: int, error_type: str) -> AnthropicResponse:
    return anthropic_json(status, {"type": "error", "error": {
        "type": error_type, "message": f"{VENDOR_TEXT} echoing {ANTHROPIC_SECRET}"}})


ANTHROPIC_MODELS_OK = anthropic_json(200, {"data": [{"id": MODEL, "type": "model"}], "has_more": False})


# -- Claude host fixtures (same shape as tests/test_host_claude.py) -----------------------------
CLAUDE_VERSION_OK = CommandResult(0, b"2.1.0 (Claude Code)\n")


def claude_status(logged_in: bool = True, account: str = ACCOUNT_A) -> CommandResult:
    payload: dict = {"loggedIn": logged_in}
    if logged_in:
        payload["authMethod"] = "claude.ai"
        payload["accountId"] = account
    return CommandResult(0, json.dumps(payload).encode("utf-8"))


class ClaudeRunner:
    """Synthetic host fixture. The last queued outcome per command is sticky; logout signs out."""

    source = "synthetic_fixture"

    def __init__(self) -> None:
        self.responses = {"version": [CLAUDE_VERSION_OK], "status": [claude_status()],
                          "logout": [CommandResult(0, b"")]}
        self.calls: list = []
        self.launches: list = []
        self.launch_error = None

    def set(self, key, *outcomes) -> None:
        self.responses[key] = list(outcomes)

    @staticmethod
    def key(argv) -> str:
        tail = tuple(argv[1:])
        if tail == ("--version",):
            return "version"
        if tail[:2] == ("auth", "status"):
            return "status"
        if tail[:2] == ("auth", "logout"):
            return "logout"
        raise AssertionError("unexpected synthetic command")

    def run(self, spec):
        assert type(spec) is CommandSpec
        self.calls.append(spec)
        key = self.key(spec.argv)
        queue = self.responses[key]
        outcome = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(outcome, type) and issubclass(outcome, BaseException):
            raise outcome()
        if isinstance(outcome, BaseException):
            raise outcome
        if key == "logout":
            self.set("status", claude_status(logged_in=False))
        return outcome

    def launch(self, spec):
        assert type(spec) is CommandSpec
        self.launches.append(spec)
        if self.launch_error is not None:
            raise self.launch_error

    def count(self, key: str) -> int:
        return sum(1 for spec in self.calls if self.key(spec.argv) == key)


# -- Codex host fixtures (same shape as tests/test_host_codex.py) -------------------------------
class CodexLoginHandle:
    def __init__(self, host: "CodexHost") -> None:
        self.host = host
        self.code = None
        self.terminated = False

    def poll(self):
        return self.code

    def terminate(self) -> None:
        self.terminated = True
        self.code = -15

    def complete(self, account: str) -> None:
        self.host.status = ("chatgpt", account)
        self.code = 0


class CodexHost:
    """Host protocol fixture: assumed (unverified) Codex CLI output formats."""

    def __init__(self) -> None:
        self.present = True
        self.version_output = b"codex-cli 0.40.0\n"
        self.status = ("chatgpt", ACCOUNT_A)
        self.handles: list = []

    def status_result(self) -> RunResult:
        kind = self.status[0]
        if kind == "chatgpt":
            account = self.status[1]
            text = "Logged in using ChatGPT\n" + (f"Account ID: {account}\n" if account else "")
            return RunResult(0, text.encode())
        if kind == "logged_out":
            return RunResult(1, b"Not logged in\n")
        return RunResult(1, b"Session expired. Please log in again.\n")


class CodexRunner:
    source = "synthetic_fixture"

    def __init__(self, host: CodexHost) -> None:
        self.host = host
        self.calls: list = []
        self.spawn_error = None

    def commands(self) -> list:
        return [tuple(argv[1:]) for argv, *_ in self.calls]

    def run(self, argv, *, env, timeout, max_output):
        validate_invocation(argv, env, timeout, max_output)
        self.calls.append((argv, dict(env), timeout, max_output))
        if not self.host.present:
            raise HostNotFound()
        args = tuple(argv[1:])
        if args == ("--version",):
            return RunResult(0, self.host.version_output)
        if args == ("login", "status"):
            return self.host.status_result()
        if args == ("logout",):
            self.host.status = ("logged_out", None)
            return RunResult(0, b"synthetic logout\n")
        raise AssertionError("unexpected host command")

    def spawn(self, argv, *, env):
        validate_invocation(argv, env)
        self.calls.append((argv, dict(env), None, None))
        if not self.host.present:
            raise HostNotFound()
        if self.spawn_error is not None:
            raise self.spawn_error
        handle = CodexLoginHandle(self.host)
        self.host.handles.append(handle)
        return handle


class TempCase(Leaks, unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="aik-connectors-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)


# ====================================================================================================
class OpenAIVerifierTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.vault = DenyingVault(self.root / "vault", key=bytes(range(32)))
        self.transport = OpenAITransport()
        self.adapter = OpenAIApiAdapter(transport=self.transport, vault=self.vault, clock=lambda: 1000.0)
        self.verifier = OpenAIKeyVerifier(self.adapter)
        self.registry = Registry(self.root, self.vault)

    def connection(self, connection_id: str, secret: str = OPENAI_SECRET):
        return self.registry.api(OPENAI_INFO, connection_id, self.vault.put(secret))

    def vault_files(self) -> list:
        directory = self.root / "vault"
        return sorted(path.name for path in directory.iterdir()) if directory.exists() else []

    def test_port_and_info(self) -> None:
        self.assertIsInstance(self.verifier, KeyVerifier)
        info = self.verifier.info
        self.assertEqual((info.provider, info.execution_kind, info.method, info.display_name, info.official_host),
                         (OPENAI_PROVIDER, "api", "api_key", "OpenAI API", "openai-api"))
        wired = create_openai(vault=self.vault, transport=self.transport)
        self.assertIsInstance(wired, OpenAIKeyVerifier)
        self.assertIsInstance(wired, KeyVerifier)
        self.assertEqual(repr(wired), "OpenAIKeyVerifier()")
        with self.assertRaises(TypeError):
            OpenAIKeyVerifier(object())

    def test_accepts_checks_the_format_only(self) -> None:
        before = self.vault_files()
        self.assertTrue(self.verifier.accepts(OPENAI_SECRET))
        for candidate in ("short", OPENAI_SECRET + "\r\nX-Injected: 1", "has space " + OPENAI_SECRET, 12345, "",
                          None):
            with self.subTest(kind=type(candidate).__name__):
                self.assertFalse(self.verifier.accepts(candidate))
        self.assertEqual(self.transport.seen, [])
        self.assertEqual(self.vault_files(), before)

    def test_200_is_verified_and_a_repeated_check_still_works(self) -> None:
        connection = self.connection("conn-openai-ok")
        self.transport.add("GET", "/models", openai_resp(200, OPENAI_MODELS), openai_resp(200, OPENAI_MODELS))
        first = self.verifier.verify(connection)
        self.assertEqual(first, CheckResult(CheckState.VERIFIED))
        self.assertEqual(self.transport.seen[-1][2], "Bearer " + OPENAI_SECRET)
        second = self.verifier.verify(connection)
        self.assertEqual(second, CheckResult(CheckState.VERIFIED))
        self.assertEqual(len(self.transport.seen), 2)
        self.assert_clean(first, second)

    def test_401_is_unauthenticated_with_a_fixed_code(self) -> None:
        connection = self.connection("conn-openai-401")
        self.transport.add("GET", "/models", openai_resp(401, {"error": {
            "code": "invalid_api_key",
            "message": "Incorrect API key provided: " + OPENAI_SECRET[:20] + " " + VENDOR_TEXT}}))
        result = self.verifier.verify(connection)
        self.assertIs(result.state, CheckState.UNAUTHENTICATED)
        self.assertEqual(result.code, "KEY_REJECTED")
        self.assertIsNone(result.account_binding)
        self.assert_clean(result)

    def test_vault_access_denial_is_locked_without_a_request(self) -> None:
        connection = self.connection("conn-openai-locked")
        self.vault.deny = True
        result = self.verifier.verify(connection)
        self.assertIs(result.state, CheckState.LOCKED)
        self.assertIn(result.code, {"VAULT_LOCKED", "ACCESS_DENIED"})
        self.assertEqual(self.transport.seen, [])
        self.assert_clean(result)

    def test_missing_vault_entry_is_unauthenticated_without_a_request(self) -> None:
        connection = self.connection("conn-openai-gone")
        self.vault.delete(connection.credential_ref)
        result = self.verifier.verify(connection)
        self.assertIs(result.state, CheckState.UNAUTHENTICATED)
        self.assertIn(result.code, {"CREDENTIAL_UNAVAILABLE", "KEY_REJECTED"})
        self.assertEqual(self.transport.seen, [])
        self.assert_clean(result)

    def test_network_failure_and_5xx_are_unknown(self) -> None:
        offline = self.connection("conn-openai-offline")
        self.transport.add("GET", "/models", RuntimeError("connection reset; Authorization: Bearer " + OPENAI_SECRET))
        result = self.verifier.verify(offline)
        self.assertIs(result.state, CheckState.UNKNOWN)
        self.assertIn(result.code, TRANSPORT_UNKNOWN)
        self.assert_clean(result)
        broken = self.connection("conn-openai-5xx")
        server_error = openai_resp(500, {"error": {"message": VENDOR_TEXT + " " + OPENAI_SECRET}})
        self.transport.add("GET", "/models", server_error, server_error, server_error)
        result = self.verifier.verify(broken)
        self.assertIs(result.state, CheckState.UNKNOWN)
        self.assertIn(result.code, TRANSPORT_UNKNOWN)
        self.assert_clean(result)

    def test_rotation_verifies_the_new_binding_and_refuses_the_old_one(self) -> None:
        old = self.connection("conn-openai-rotate")
        self.transport.add("GET", "/models", openai_resp(200, OPENAI_MODELS))
        self.assertIs(self.verifier.verify(old).state, CheckState.VERIFIED)
        rotated = self.registry.rebind(old.connection_id)
        self.vault.replace(old.credential_ref, OPENAI_ROTATED)
        self.assertEqual(rotated.credential_ref, old.credential_ref)
        self.assertEqual(rotated.binding_revision, old.binding_revision + 1)
        self.transport.add("GET", "/models", openai_resp(200, OPENAI_MODELS))
        result = self.verifier.verify(rotated)
        self.assertEqual(result, CheckResult(CheckState.VERIFIED))
        self.assertEqual(self.transport.seen[-1][2], "Bearer " + OPENAI_ROTATED)
        sent = len(self.transport.seen)
        stale = self.verifier.verify(old)
        self.assertEqual(stale, CheckResult(CheckState.UNKNOWN, code="STALE_BINDING"))
        self.assertEqual(len(self.transport.seen), sent)
        self.assert_clean(result, stale)

    def test_mismatch_and_adapter_faults_never_raise(self) -> None:
        self.assertEqual(self.verifier.verify(object()), CheckResult(CheckState.UNKNOWN, code="CONNECTION_MISMATCH"))
        foreign = self.registry.api(ANTHROPIC_INFO, "conn-foreign", self.vault.put(ANTHROPIC_SECRET))
        self.assertEqual(self.verifier.verify(foreign).code, "CONNECTION_MISMATCH")
        connection = self.connection("conn-openai-boom")
        with mock.patch.object(self.adapter, "verify_key", side_effect=RuntimeError(OPENAI_SECRET)):
            result = self.verifier.verify(connection)
        self.assertIs(result.state, CheckState.UNKNOWN)
        self.assert_clean(result)


# ====================================================================================================
class AnthropicVerifierTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.vault = DenyingVault(self.root / "vault", key=bytes(range(32)))
        self.transport = AnthropicTransport()
        self.adapter = AnthropicAdapter(self.vault, self.transport)
        self.verifier = AnthropicKeyVerifier(self.adapter)
        self.registry = Registry(self.root, self.vault)

    def connection(self, connection_id: str, secret: str = ANTHROPIC_SECRET):
        return self.registry.api(ANTHROPIC_INFO, connection_id, self.vault.put(secret))

    def test_port_and_info(self) -> None:
        self.assertIsInstance(self.verifier, KeyVerifier)
        info = self.verifier.info
        self.assertEqual((info.provider, info.execution_kind, info.method, info.display_name, info.official_host),
                         (ANTHROPIC_PROVIDER, "api", "api_key", "Anthropic API", "anthropic-api"))
        wired = create_anthropic(vault=self.vault, transport=self.transport)
        self.assertIsInstance(wired, AnthropicKeyVerifier)
        self.assertIsInstance(wired, KeyVerifier)
        self.assertEqual(repr(wired), "AnthropicKeyVerifier()")
        with self.assertRaises(TypeError):
            AnthropicKeyVerifier(object())

    def test_accepts_checks_the_format_only(self) -> None:
        self.assertTrue(self.verifier.accepts(ANTHROPIC_SECRET))
        for candidate in ("", 12345, None, ANTHROPIC_SECRET + "\r\nX-Injected: 1"):
            with self.subTest(kind=type(candidate).__name__):
                self.assertFalse(self.verifier.accepts(candidate))
        self.assertEqual(self.transport.requests, [])
        self.assertFalse((self.root / "vault").exists() and any((self.root / "vault").glob("*.aikv")))

    def test_200_is_verified_and_a_repeated_check_still_works(self) -> None:
        connection = self.connection("conn-anthropic-ok")
        self.transport.routes[("GET", MODELS_PATH)] = ANTHROPIC_MODELS_OK
        first = self.verifier.verify(connection)
        self.assertEqual(first, CheckResult(CheckState.VERIFIED))
        self.assertEqual(self.transport.requests[-1].header("x-api-key"), ANTHROPIC_SECRET)
        self.assertEqual(self.verifier.verify(connection), CheckResult(CheckState.VERIFIED))
        self.assert_clean(first)

    def test_401_is_unauthenticated_with_a_fixed_code(self) -> None:
        connection = self.connection("conn-anthropic-401")
        self.transport.routes[("GET", MODELS_PATH)] = anthropic_error(401, "authentication_error")
        result = self.verifier.verify(connection)
        self.assertIs(result.state, CheckState.UNAUTHENTICATED)
        self.assertEqual(result.code, "KEY_REJECTED")
        self.assert_clean(result)

    def test_vault_access_denial_is_locked_without_a_request(self) -> None:
        connection = self.connection("conn-anthropic-locked")
        self.vault.deny = True
        result = self.verifier.verify(connection)
        self.assertEqual(result, CheckResult(CheckState.LOCKED, code="VAULT_LOCKED"))
        self.assertEqual(self.transport.requests, [])
        self.assert_clean(result)

    def test_missing_vault_entry_is_unauthenticated_without_a_request(self) -> None:
        connection = self.connection("conn-anthropic-gone")
        self.vault.delete(connection.credential_ref)
        result = self.verifier.verify(connection)
        self.assertEqual(result, CheckResult(CheckState.UNAUTHENTICATED, code="CREDENTIAL_UNAVAILABLE"))
        self.assertEqual(self.transport.requests, [])

    def test_network_failure_and_5xx_are_unknown(self) -> None:
        offline = self.connection("conn-anthropic-offline")
        self.transport.routes[("GET", MODELS_PATH)] = make_error(TransportNotSent)
        result = self.verifier.verify(offline)
        self.assertIs(result.state, CheckState.UNKNOWN)
        self.assertIn(result.code, TRANSPORT_UNKNOWN)
        self.assert_clean(result)
        broken = self.connection("conn-anthropic-5xx")
        self.transport.routes[("GET", MODELS_PATH)] = anthropic_error(500, "api_error")
        result = self.verifier.verify(broken)
        self.assertIs(result.state, CheckState.UNKNOWN)
        self.assertIn(result.code, TRANSPORT_UNKNOWN)
        self.assert_clean(result)

    def test_rotation_verifies_the_new_binding_and_refuses_the_old_one(self) -> None:
        old = self.connection("conn-anthropic-rotate")
        self.transport.routes[("GET", MODELS_PATH)] = ANTHROPIC_MODELS_OK
        self.assertIs(self.verifier.verify(old).state, CheckState.VERIFIED)
        rotated = self.registry.rebind(old.connection_id)
        self.vault.replace(old.credential_ref, ANTHROPIC_ROTATED)
        self.assertEqual(rotated.credential_ref, old.credential_ref)
        self.assertEqual(rotated.binding_revision, old.binding_revision + 1)
        result = self.verifier.verify(rotated)
        self.assertEqual(result, CheckResult(CheckState.VERIFIED))
        self.assertEqual(self.transport.requests[-1].header("x-api-key"), ANTHROPIC_ROTATED)
        sent = len(self.transport.requests)
        stale = self.verifier.verify(old)
        self.assertEqual(stale, CheckResult(CheckState.UNKNOWN, code="STALE_BINDING"))
        self.assertEqual(len(self.transport.requests), sent)
        self.assert_clean(result, stale)

    def test_mismatch_and_adapter_faults_never_raise(self) -> None:
        self.assertEqual(self.verifier.verify(object()), CheckResult(CheckState.UNKNOWN, code="CONNECTION_MISMATCH"))
        foreign = self.registry.api(OPENAI_INFO, "conn-foreign", self.vault.put(OPENAI_SECRET))
        self.assertEqual(self.verifier.verify(foreign).code, "CONNECTION_MISMATCH")
        connection = self.connection("conn-anthropic-boom")
        with mock.patch.object(self.adapter, "auth_status", side_effect=RuntimeError(ANTHROPIC_SECRET)):
            result = self.verifier.verify(connection)
        self.assertEqual(result, CheckResult(CheckState.UNKNOWN, code="VERIFY_FAILED"))
        self.assert_clean(result)


# ====================================================================================================
class ClaudeHostLoginTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.now = [100.0]
        self.registry = Registry(self.root, FakeVaultBackend(self.root / "vault", key=bytes(range(32))))
        self.runner, self.login = self.make()

    def make(self):
        runner = ClaudeRunner()
        bridge = ClaudeHostBridge(runner, catalog=("synthetic-model",),
                                  environ={"PATH": "/synthetic/bin", "HOME": "/synthetic/home"})
        return runner, ClaudeHostLogin(bridge, clock=lambda: self.now[0], login_timeout=30.0)

    def test_port_and_info(self) -> None:
        self.assertIsInstance(self.login, HostLogin)
        info = self.login.info
        self.assertEqual((info.provider, info.execution_kind, info.method, info.display_name, info.official_host),
                         (CLAUDE_PROVIDER, "subscription", "host_login", "Claude 구독(Claude Code)", "claude-code"))
        self.assertEqual(repr(self.login), "ClaudeHostLogin()")
        with self.assertRaises(TypeError):
            ClaudeHostLogin(object())
        with self.assertRaises(ValueError):
            ClaudeHostLogin(self.login._bridge, login_timeout=0)

    def test_login_connect_check_logout_flow(self) -> None:
        self.assertEqual(self.login.login_status(), LoginStatus(LoginState.IDLE, False))
        self.runner.set("status", claude_status(logged_in=False))
        started = self.login.start_login("conn-claude-1")
        self.assertEqual(started, LoginStatus(LoginState.PENDING, False))
        self.assertEqual(len(self.runner.launches), 1)
        self.assertEqual(self.login.login_status(), LoginStatus(LoginState.PENDING, False))
        self.assertFalse(self.login.cancel_login())  # the bridge cannot stop a launched login
        self.assertEqual(self.login.login_status().state, LoginState.PENDING)
        self.runner.set("status", claude_status())
        self.assertEqual(self.login.login_status(), LoginStatus(LoginState.SIGNED_IN, False))
        connected = self.login.connect("conn-claude-1")
        fingerprint = account_fingerprint(ACCOUNT_A)
        self.assertEqual(connected, CheckResult(CheckState.VERIFIED, account_binding=fingerprint))
        connection = self.registry.subscription(self.login.info, "conn-claude-1", fingerprint)
        self.assertEqual(self.login.check(connection), CheckResult(CheckState.VERIFIED, account_binding=fingerprint))
        self.assertIsNone(self.login.logout(connection))
        self.assertEqual(self.runner.count("logout"), 1)
        after = self.login.check(connection)
        self.assertEqual(after, CheckResult(CheckState.UNAUTHENTICATED, code="NOT_SIGNED_IN"))
        self.assert_clean(started, connected, after)

    def test_already_signed_in_launches_nothing(self) -> None:
        self.assertEqual(self.login.start_login("conn-claude-2"), LoginStatus(LoginState.SIGNED_IN, False))
        self.assertEqual(self.runner.launches, [])

    def test_login_timeout_is_failed(self) -> None:
        self.runner.set("status", claude_status(logged_in=False))
        self.login.start_login("conn-claude-3")
        self.now[0] += 31.0
        self.assertEqual(self.login.login_status(), LoginStatus(LoginState.FAILED, False, "LOGIN_TIMEOUT"))
        self.assertEqual(self.login.login_status().code, "LOGIN_TIMEOUT")

    def test_absent_host_and_launch_failure_are_failed_with_fixed_codes(self) -> None:
        runner, login = self.make()
        runner.set("version", RunnerNotStarted)
        runner.set("status", claude_status(logged_in=False))
        started = login.start_login("conn-claude-4")
        self.assertIsNot(started.state, LoginState.SIGNED_IN)
        self.assertEqual(login.login_status(), LoginStatus(LoginState.FAILED, False, "HOST_ABSENT"))
        self.assertEqual(login.connect("conn-claude-4"), CheckResult(CheckState.UNKNOWN, code="HOST_ABSENT"))
        runner, login = self.make()
        runner.set("status", claude_status(logged_in=False))
        runner.launch_error = RunnerNotStarted()
        failed = login.start_login("conn-claude-5")
        self.assertIs(failed.state, LoginState.FAILED)
        self.assertRegex(failed.code, CODE)
        self.assertEqual(login.login_status(), failed)
        self.assert_clean(started, failed)

    def test_bridge_faults_never_raise(self) -> None:
        self.runner.set("status", claude_status(logged_in=False))
        self.login.start_login("conn-claude-6")
        with mock.patch.object(self.login._bridge, "probe", side_effect=RuntimeError(SECRET_MARKER)):
            status = self.login.login_status()
        self.assertEqual(status, LoginStatus(LoginState.FAILED, False, "HOST_STATE_UNKNOWN"))
        self.assertEqual(self.login.check(object()), CheckResult(CheckState.UNKNOWN, code="CONNECTION_MISMATCH"))
        with self.assertRaises(ConnectError) as raised:
            self.login.logout(object())
        self.assertEqual(raised.exception.code, "CONNECTION_MISMATCH")
        self.assert_clean(status, raised.exception)

    def test_restart_binds_only_the_stored_account(self) -> None:
        fingerprint = account_fingerprint(ACCOUNT_A)
        connection = self.registry.subscription(self.login.info, "conn-claude-7", fingerprint)
        _, fresh = self.make()
        self.assertEqual(fresh.check(connection), CheckResult(CheckState.VERIFIED, account_binding=fingerprint))
        runner, other = self.make()
        runner.set("status", claude_status(account=ACCOUNT_B))
        self.assertEqual(other.check(connection), CheckResult(CheckState.ACCOUNT_CHANGED, code="ACCOUNT_CHANGED"))
        legacy = self.registry.subscription(self.login.info, "conn-claude-8", "acct-legacy")
        _, fresh = self.make()
        self.assertEqual(fresh.check(legacy), CheckResult(CheckState.UNKNOWN, code="BINDING_UNSUPPORTED"))

    def test_logout_never_signs_out_another_account(self) -> None:
        connection = self.registry.subscription(self.login.info, "conn-claude-9", account_fingerprint(ACCOUNT_A))
        self.runner.set("status", claude_status(account=ACCOUNT_B))
        with self.assertRaises(ConnectError) as raised:
            self.login.logout(connection)
        self.assertEqual(raised.exception.code, "HOST_ACCOUNT_CHANGED")
        self.assertEqual(self.runner.count("logout"), 0)
        self.runner.set("status", claude_status(logged_in=False))
        self.assertIsNone(self.login.logout(connection))  # already signed out: nothing is sent to the host
        self.assertEqual(self.runner.count("logout"), 0)


# ====================================================================================================
class CodexHostLoginTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.now = [1000.0]
        self.registry = Registry(self.root, FakeVaultBackend(self.root / "vault", key=bytes(range(32))))
        self.host, self.runner, self.login = self.make()

    def make(self):
        host = CodexHost()
        runner = CodexRunner(host)
        bridge = CodexHostBridge(runner, environment={"PATH": "/synthetic/bin", "HOME": str(self.root),
                                                      "LANG": "C.UTF-8"}, clock=lambda: self.now[0])
        return host, runner, CodexHostLogin(bridge)

    def test_port_and_info(self) -> None:
        self.assertIsInstance(self.login, HostLogin)
        info = self.login.info
        self.assertEqual((info.provider, info.execution_kind, info.method, info.display_name, info.official_host,
                          info.host_profile),
                         (CODEX_PROVIDER, "subscription", "host_login", "ChatGPT 구독(Codex)", "codex-cli", "default"))
        self.assertEqual(repr(self.login), "CodexHostLogin()")
        with self.assertRaises(TypeError):
            CodexHostLogin(object())

    def test_login_connect_check_logout_flow(self) -> None:
        self.host.status = ("logged_out", None)
        self.assertEqual(self.login.login_status(), LoginStatus(LoginState.IDLE, True))
        started = self.login.start_login("conn-codex-1")
        self.assertEqual(started, LoginStatus(LoginState.PENDING, True))
        self.assertEqual(len(self.host.handles), 1)
        self.assertEqual(self.login.login_status(), LoginStatus(LoginState.PENDING, True))
        self.host.handles[0].complete(ACCOUNT_A)
        self.assertEqual(self.login.login_status(), LoginStatus(LoginState.SIGNED_IN, True))
        connected = self.login.connect("conn-codex-1")
        self.assertIs(connected.state, CheckState.VERIFIED)
        self.assertRegex(connected.account_binding, FINGERPRINT)
        connection = self.registry.subscription(self.login.info, "conn-codex-1", connected.account_binding)
        self.assertEqual(self.login.check(connection),
                         CheckResult(CheckState.VERIFIED, account_binding=connected.account_binding))
        self.assertIsNone(self.login.logout(connection))
        self.assertIn(("logout",), self.runner.commands())
        self.assertEqual(self.host.status, ("logged_out", None))
        after = self.login.check(connection)
        self.assertIsNot(after.state, CheckState.VERIFIED)
        self.assert_clean(started, connected, after)

    def test_cancel_stops_a_running_login(self) -> None:
        self.assertFalse(self.login.cancel_login())  # nothing was started
        self.host.status = ("logged_out", None)
        self.assertEqual(self.login.start_login("conn-codex-2").state, LoginState.PENDING)
        self.assertTrue(self.login.cancel_login())
        self.assertTrue(self.host.handles[0].terminated)
        self.assertEqual(self.login.login_status(), LoginStatus(LoginState.CANCELLED, True))

    def test_absent_host_and_spawn_failure_are_failed_with_fixed_codes(self) -> None:
        self.host.present = False
        self.assertEqual(self.login.start_login("conn-codex-3"), LoginStatus(LoginState.FAILED, True, "HOST_ABSENT"))
        self.assertEqual(self.login.login_status(), LoginStatus(LoginState.FAILED, True, "HOST_ABSENT"))
        self.assertEqual(self.login.connect("conn-codex-3"), CheckResult(CheckState.UNKNOWN, code="HOST_ABSENT"))
        stored = self.registry.subscription(self.login.info, "conn-codex-4", "0" * 64)
        self.assertEqual(self.login.check(stored), CheckResult(CheckState.UNKNOWN, code="HOST_ABSENT"))
        host, runner, login = self.make()
        host.status = ("logged_out", None)
        runner.spawn_error = make_error(RunnerError)
        failed = login.start_login("conn-codex-5")
        self.assertIs(failed.state, LoginState.FAILED)
        self.assertRegex(failed.code, CODE)
        self.assertEqual(login.login_status(), failed)
        self.assert_clean(failed)

    def test_bridge_faults_never_raise(self) -> None:
        self.host.status = ("logged_out", None)
        self.login.start_login("conn-codex-6")
        with mock.patch.object(self.login._bridge, "probe", side_effect=RuntimeError(SECRET_MARKER)):
            status = self.login.login_status()
        self.assertEqual(status, LoginStatus(LoginState.FAILED, True, "HOST_STATE_UNKNOWN"))
        self.assertEqual(self.login.check(object()), CheckResult(CheckState.UNKNOWN, code="CONNECTION_MISMATCH"))
        with self.assertRaises(ConnectError) as raised:
            self.login.logout(object())
        self.assertEqual(raised.exception.code, "CONNECTION_MISMATCH")
        self.assert_clean(status, raised.exception)

    def test_logout_never_signs_out_another_account(self) -> None:
        connected = self.login.connect("conn-codex-7")
        self.assertIs(connected.state, CheckState.VERIFIED)
        connection = self.registry.subscription(self.login.info, "conn-codex-7", connected.account_binding)
        self.host.status = ("chatgpt", ACCOUNT_B)
        with self.assertRaises(ConnectError) as raised:
            self.login.logout(connection)
        self.assertRegex(raised.exception.code, CODE)
        self.assertNotIn(("logout",), self.runner.commands())
        self.assertEqual(self.host.status, ("chatgpt", ACCOUNT_B))
        self.assert_clean(raised.exception)


# ====================================================================================================
class ServiceWithAnthropicVerifierTests(TempCase):
    """ConnectionService + the real Anthropic verifier + a synthetic transport + the test-only vault."""

    def setUp(self) -> None:
        super().setUp()
        self.vault = FakeVaultBackend(self.root / "vault", key=bytes(range(32)))
        self.transport = AnthropicTransport()
        self.verifier = create_anthropic(vault=self.vault, transport=self.transport)
        self.service = ConnectionService(self.root / "config" / "settings.sqlite3", (self.verifier,),
                                         vault_factory=lambda: self.vault)

    def files_without_secret(self) -> None:
        for path in self.root.rglob("*.sqlite3"):
            data = path.read_bytes()
            for secret in (ANTHROPIC_SECRET, ANTHROPIC_ROTATED):
                self.assertNotIn(secret.encode("ascii"), data)

    def test_add_rotate_and_check(self) -> None:
        self.transport.routes[("GET", MODELS_PATH)] = ANTHROPIC_MODELS_OK
        added = self.service.add_api_key(ANTHROPIC_PROVIDER, "합성 Anthropic 키", ANTHROPIC_SECRET)
        connection = added["connection"]
        self.assertEqual(added["check"], {"state": "verified", "code": None})
        self.assertEqual((connection["state"], connection["method"], connection["display_name"]),
                         ("verified", "api_key", "Anthropic API"))
        rotated = self.service.rotate_key(connection["connection_id"], connection["revision"], ANTHROPIC_ROTATED)
        self.assertEqual(rotated["check"], {"state": "verified", "code": None})
        self.assertEqual(rotated["connection"]["state"], "verified")
        self.assertEqual(rotated["connection"]["binding_revision"], connection["binding_revision"] + 1)
        self.assertEqual(self.transport.requests[-1].header("x-api-key"), ANTHROPIC_ROTATED)
        again = self.service.check(connection["connection_id"])
        self.assertEqual(again["check"], {"state": "verified", "code": None})
        self.assert_clean(added, rotated, again, self.service.list())
        self.files_without_secret()

    def test_a_key_the_vendor_rejects_is_not_kept(self) -> None:
        self.transport.routes[("GET", MODELS_PATH)] = anthropic_error(401, "authentication_error")
        with self.assertRaises(ConnectError) as raised:
            self.service.add_api_key(ANTHROPIC_PROVIDER, "틀린 키", ANTHROPIC_SECRET)
        self.assertEqual(raised.exception.code, "KEY_REJECTED")
        self.assertEqual(self.service.list()["connections"], [])
        self.assertEqual(list((self.root / "vault").glob("*.aikv")), [])
        self.assert_clean(raised.exception)
        self.files_without_secret()


if __name__ == "__main__":
    unittest.main()
