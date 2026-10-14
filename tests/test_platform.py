"""AIK-091: the OS port layer and its Linux adapters (Secret Service vault, logind session lock).

Windows adapters are exercised through injected OS names and fakes here; the
real Windows calls run in the Windows CI job. The Secret Service backend runs
against a fake ``secret-tool``; a real keyring is exercised only where the CI
job provides one (``AIK_SECRET_SERVICE_LIVE=1``).
"""
from __future__ import annotations

import ast
import errno
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import aik_platform  # noqa: E402
from aik_platform import PlatformUnavailable, fs, locks, paths, process, session  # noqa: E402
from aik_vault import (  # noqa: E402
    CredentialNotFound, SecretServiceVaultBackend, VaultCorrupt, VaultUnavailable, open_user_vault,
)
from aik_vault.secret_service import namespace_for  # noqa: E402

SECRET = "synthetic-secret-éé-value"


class FakeKeyring:
    """Mimics ``secret-tool`` exit codes; records argv and stdin."""

    def __init__(self):
        self.items: dict[tuple, bytes] = {}
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.unavailable = False

    def __call__(self, argv, stdin, timeout):
        self.calls.append((list(argv), stdin))
        if self.unavailable:
            return 1, b"", False  # e.g. no D-Bus session: exit 1 with a message on stderr
        operation, attributes = argv[1], tuple(item for item in argv[2:] if not item.startswith("--"))
        if operation == "store":
            self.items[attributes] = stdin
            return 0, b"", True
        if operation == "lookup":
            value = self.items.get(attributes)
            return (0, value, True) if value is not None else (1, b"", True)
        if operation == "clear":
            self.items.pop(attributes, None)
            return 0, b"", True
        raise AssertionError(operation)


class PlatformPortTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="aik-platform-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_family_names_the_current_platform(self):
        self.assertIn(aik_platform.family(), ("windows", "linux", "macos", sys.platform))
        self.assertEqual(aik_platform.is_windows(), os.name == "nt")
        self.assertTrue(aik_platform.is_windows("nt"))
        self.assertFalse(aik_platform.is_windows("posix"))

    def test_sync_helpers_flush_files_and_directories(self):
        target = self.root / "file.bin"
        target.write_bytes(b"data")
        fs.sync_file(target)
        fs.sync_directory(self.root)
        self.assertEqual(target.read_bytes(), b"data")

    @unittest.skipIf(os.name == "nt", "POSIX mode bits")
    def test_restrict_to_owner_sets_posix_modes(self):
        target = self.root / "secret.txt"
        target.write_text("x")
        fs.restrict_to_owner(target)
        fs.restrict_to_owner(self.root, directory=True)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)

    def test_link_and_reparse_point_detection(self):
        regular = self.root / "regular"
        regular.write_text("x")
        self.assertFalse(fs.is_link_or_reparse_point(regular.lstat()))
        fake = mock.Mock(st_mode=stat.S_IFDIR | 0o700, st_file_attributes=0x400)
        self.assertTrue(fs.is_link_or_reparse_point(fake))
        try:
            (self.root / "link").symlink_to(regular)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlinks unavailable: {error}")
        self.assertTrue(fs.is_link_or_reparse_point((self.root / "link").lstat()))

    def test_lock_is_exclusive_until_released(self):
        lockfile = self.root / "x.lock"
        first = os.open(lockfile, os.O_RDWR | os.O_CREAT, 0o600)
        second = os.open(lockfile, os.O_RDWR)
        self.addCleanup(os.close, first)
        self.addCleanup(os.close, second)
        acquire_first, release_first = locks.lock_ops(first)
        acquire_second, release_second = locks.lock_ops(second)
        acquire_first()
        with self.assertRaises(OSError) as caught:
            acquire_second()
        self.assertIn(caught.exception.errno, (errno.EACCES, errno.EAGAIN, errno.EDEADLK))
        release_first()
        acquire_second()
        release_second()

    def test_state_paths_follow_the_platform_layout(self):
        windows = {"LOCALAPPDATA": str(self.root / "local")}
        self.assertEqual(paths.component_file("runtime", "runtime.sqlite3", "nt", windows),
                         self.root / "local" / "AIK" / "runtime" / "runtime.sqlite3")
        self.assertEqual(paths.component_dir("vault", "nt", windows), self.root / "local" / "AIK" / "vault")
        posix = {"XDG_STATE_HOME": str(self.root / "state")}
        self.assertEqual(paths.component_file("runtime", "runtime.sqlite3", "posix", posix),
                         self.root / "state" / "aik" / "runtime.sqlite3")
        self.assertEqual(paths.component_dir("supervisor", "posix", posix), self.root / "state" / "aik" / "supervisor")
        self.assertEqual(paths.state_root("posix", {}), Path.home() / ".local" / "state" / "aik")
        with self.assertRaises(PlatformUnavailable) as caught:
            paths.state_root("nt", {})
        self.assertEqual(caught.exception.code, "LOCALAPPDATA_REQUIRED")

    @unittest.skipIf(os.name == "nt", "POSIX process groups")
    def test_kill_group_stops_the_command_and_its_children(self):
        command = [sys.executable, "-c", "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); time.sleep(60)"]
        child = subprocess.Popen(command, **process.popen_options())
        self.addCleanup(lambda: child.poll() is None and child.kill())
        time.sleep(0.3)
        self.assertTrue(process.kill_group(child))
        self.assertEqual(child.wait(timeout=10), -9)
        self.assertEqual(process.no_window_flags(), 0)
        self.assertEqual(process.new_console_flags(), 0)

    def test_process_cmdline_observes_only_where_supported(self):
        own = process.process_cmdline(os.getpid())
        if sys.platform.startswith("linux"):
            self.assertIn(os.fsencode(Path(sys.executable).name).split(b".")[0][:6], own)
        else:
            self.assertIsNone(own)
        self.assertIsNone(process.process_cmdline(2 ** 22 + 12345))


class SessionLockTests(unittest.TestCase):
    def logind(self, stdout=b"no\n", returncode=0, environ=None, which="/usr/bin/loginctl", error=None):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            if error is not None:
                raise error
            return subprocess.CompletedProcess(argv, returncode, stdout, b"")

        env = {"XDG_SESSION_ID": "c2"} if environ is None else environ
        with mock.patch.object(session.sys, "platform", "linux"), mock.patch.object(session, "is_windows", return_value=False):
            result = session.session_locked(environ=env, run=run, which=lambda name: which)
        return result, calls

    def test_logind_locked_hint_is_read_without_a_shell(self):
        self.assertEqual(self.logind(b"yes\n")[0], True)
        result, calls = self.logind(b"no\n")
        self.assertIs(result, False)
        argv, kwargs = calls[0]
        self.assertEqual(argv, ["/usr/bin/loginctl", "show-session", "c2", "--property=LockedHint", "--value"])
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["timeout"], 2.0)

    def test_unanswerable_session_is_unavailable(self):
        cases = (
            {"stdout": b"maybe"}, {"returncode": 1}, {"environ": {}}, {"environ": {"XDG_SESSION_ID": "c2;rm"}},
            {"which": None}, {"which": "loginctl"}, {"error": subprocess.TimeoutExpired("loginctl", 2)},
            {"error": OSError("missing")},
        )
        for case in cases:
            with self.subTest(case=case), self.assertRaises(PlatformUnavailable):
                self.logind(**case)

    def test_other_platforms_are_unavailable(self):
        with mock.patch.object(session.sys, "platform", "darwin"), \
                mock.patch.object(session, "is_windows", return_value=False), \
                self.assertRaises(PlatformUnavailable):
            session.session_locked()

    def test_auth_lifecycle_signal_maps_unavailable_to_its_error(self):
        from aik_auth_lifecycle import AuthLifecycleError, os_session_locked
        with mock.patch("aik_auth_lifecycle.signals.session_locked",
                        side_effect=PlatformUnavailable("SESSION_SIGNAL_UNAVAILABLE")), \
                self.assertRaises(AuthLifecycleError) as caught:
            os_session_locked()
        self.assertEqual(str(caught.exception), "SESSION_SIGNAL_UNAVAILABLE")


class SecretServiceVaultTests(unittest.TestCase):
    def vault(self, keyring=None, namespace="test"):
        self.keyring = keyring or FakeKeyring()
        return SecretServiceVaultBackend(namespace, _runner=self.keyring, _which=lambda name: "/usr/bin/secret-tool")

    def test_round_trip_keeps_the_secret_out_of_argv(self):
        vault = self.vault()
        ref = vault.put(SECRET)
        self.assertRegex(ref, r"secret://aik-vault/[0-9a-f]{32}\Z")
        self.assertEqual(vault.resolve(ref), SECRET)
        vault.replace(ref, SECRET + "-2")
        self.assertEqual(vault.resolve(ref), SECRET + "-2")
        vault.delete(ref)
        with self.assertRaises(CredentialNotFound):
            vault.resolve(ref)
        for argv, stdin in self.keyring.calls:
            self.assertFalse(any(SECRET in item for item in argv))
            self.assertEqual(argv[0], "/usr/bin/secret-tool")
        stores = [stdin for argv, stdin in self.keyring.calls if argv[1] == "store"]
        self.assertEqual(stores, [SECRET.encode(), (SECRET + "-2").encode()])

    def test_missing_reference_and_namespaces_are_separate(self):
        keyring = FakeKeyring()
        first, second = self.vault(keyring, "a"), self.vault(keyring, "b")
        ref = first.put(SECRET)
        for call in (second.resolve, second.delete, lambda value: second.replace(value, "x")):
            with self.subTest(call=call), self.assertRaises(CredentialNotFound):
                call(ref)
        self.assertEqual(first.resolve(ref), SECRET)

    def test_unusable_keyring_is_unavailable_without_fallback(self):
        keyring = FakeKeyring()
        keyring.unavailable = True
        vault = self.vault(keyring)
        with self.assertRaises(VaultUnavailable):
            vault.put(SECRET)
        with self.assertRaises(VaultUnavailable):
            SecretServiceVaultBackend("test", _runner=keyring, _which=lambda name: None)

        def raising(argv, stdin, timeout):
            raise subprocess.TimeoutExpired(argv, timeout)

        with self.assertRaises(VaultUnavailable):
            self.vault(raising).resolve("secret://aik-vault/" + "0" * 32)

    def test_damaged_item_is_corrupt(self):
        keyring = FakeKeyring()
        vault = self.vault(keyring)
        ref = vault.put(SECRET)
        for attributes in keyring.items:
            keyring.items[attributes] = b"\xff\xfe"
        with self.assertRaises(VaultCorrupt):
            vault.resolve(ref)

    def test_open_user_vault_selects_by_platform(self):
        with mock.patch("aik_vault.user_vault.family", return_value="linux"), \
                mock.patch("aik_vault.secret_service.shutil.which", return_value="/usr/bin/secret-tool"):
            selected = open_user_vault(Path("/tmp/somewhere"))
        self.assertIsInstance(selected, SecretServiceVaultBackend)
        self.assertNotEqual(namespace_for(Path("/tmp/a")), namespace_for(Path("/tmp/b")))
        self.assertEqual(namespace_for(None), "default")
        with mock.patch("aik_vault.user_vault.family", return_value="macos"), self.assertRaises(VaultUnavailable):
            open_user_vault()

    @unittest.skipUnless(os.environ.get("AIK_SECRET_SERVICE_LIVE") == "1", "Needs an unlocked Secret Service keyring")
    def test_live_secret_service_round_trip(self):
        vault = SecretServiceVaultBackend("aik-live-test")
        ref = vault.put(SECRET)
        try:
            self.assertEqual(vault.resolve(ref), SECRET)
            vault.replace(ref, SECRET + "-live")
            self.assertEqual(SecretServiceVaultBackend("aik-live-test").resolve(ref), SECRET + "-live")
        finally:
            vault.delete(ref)
        with self.assertRaises(CredentialNotFound):
            vault.resolve(ref)


class PlatformBoundaryTests(unittest.TestCase):
    """PRN-OS: operating-system decisions live only in aik_platform (and named OS adapters)."""

    # A module that *is* one OS's adapter may call that OS's API directly.
    ADAPTERS = {"aik_vault/dpapi.py": {"WinDLL"}}

    def test_common_code_does_not_inspect_the_operating_system(self):
        findings = []
        for path in sorted((ROOT / "src").rglob("*.py")):
            relative = path.relative_to(ROOT / "src").as_posix()
            if relative.startswith("aik_platform/"):
                continue
            allowed = self.ADAPTERS.get(relative, set())
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Attribute):
                    if isinstance(node.value, ast.Name) and (node.value.id, node.attr) in (
                            ("os", "name"), ("sys", "platform"), ("platform", "system")):
                        findings.append(f"{relative}:{node.lineno} {node.value.id}.{node.attr}")
                    if node.attr in ("WinDLL", "windll") and node.attr not in allowed:
                        findings.append(f"{relative}:{node.lineno} {node.attr}")
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [alias.name for alias in node.names] + ([node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
                    for name in names:
                        if name in ("msvcrt", "fcntl", "winreg", "_winapi"):
                            findings.append(f"{relative}:{node.lineno} import {name}")
        self.assertEqual(findings, [])



class VendorBoundaryTests(unittest.TestCase):
    """PRN-VENDOR (AIK-092): common packages never import a vendor adapter or host bridge."""

    VENDOR_PACKAGES = ("aik_adapter_", "aik_host_")

    def test_common_packages_do_not_import_vendor_adapters(self):
        findings = []
        for path in sorted((ROOT / "src").rglob("*.py")):
            relative = path.relative_to(ROOT / "src").as_posix()
            if relative.startswith(self.VENDOR_PACKAGES):
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else (
                    [node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
                findings.extend(f"{relative}:{node.lineno} {module}" for module in modules
                                if module.startswith(self.VENDOR_PACKAGES))
        self.assertEqual(findings, [])

    def test_shared_transport_is_the_same_object_for_vendor_adapters(self):
        import aik_http
        from aik_adapter_anthropic import transport as anthropic_transport
        from aik_jev import client as jev_client
        self.assertIs(anthropic_transport.UrllibTransport, aik_http.UrllibTransport)
        self.assertIs(jev_client.UrllibTransport, aik_http.UrllibTransport)

if __name__ == "__main__":
    unittest.main()
