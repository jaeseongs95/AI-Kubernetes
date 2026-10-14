"""AIK-045 desktop engine protocol against disposable directories and synthetic events only.

No credential, provider, VM or AGS process is used. The real engine process is
started once over stdio to show the framing works end to end.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_desktop_engine import COMMANDS, ENGINE_VERSION, PROTOCOL_VERSION, Engine, EngineConfig, serve  # noqa: E402
from aik_desktop_engine.server import MAX_FRAME  # noqa: E402
from aik_readmodel import ReadModel, SourceEvent  # noqa: E402

NONCE = "ab" * 32
SCOPE = "os-user-synthetic-1"


def request(command, *, capability=NONCE, **override):
    value = {"protocol_version": PROTOCOL_VERSION, "request_id": str(uuid.uuid4()), "command": command,
             "payload": {}, "capability": capability}
    value.update(override)
    return value


def dispatch_event(cursor, state):
    refs = {"request_id": "request-1", "lease_id": "lease-1", "vm_attempt_id": "vm-attempt-1"}
    return SourceEvent("dispatch", cursor, f"dispatch-{cursor}", SCOPE, state, refs, {"state": state})


def frames(*values):
    return io.BytesIO(b"".join(json.dumps(value).encode() + b"\n" for value in values))


def lines(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


class EngineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.environ = {"XDG_STATE_HOME": str(self.root / "state"), "LOCALAPPDATA": str(self.root / "local")}

    def engine(self, config=None):
        return Engine(NONCE, config, environ=self.environ)

    def test_unconfigured_engine_reports_what_it_observed(self):
        engine = self.engine()
        status = engine.dispatch(request("system.status"))
        self.assertTrue(status["ok"])
        self.assertEqual(status["result"], {"state": "DEGRADED", "director_state": "NOT_CONFIGURED"})
        info = engine.dispatch(request("engine.info"))["result"]
        self.assertEqual(info["engine_version"], ENGINE_VERSION)
        self.assertEqual((info["state_root"], info["readmodel"], info["director_state"]),
                         ("ABSENT", "NOT_CONFIGURED", "NOT_CONFIGURED"))
        self.assertEqual(engine.dispatch(request("readmodel.sources"))["result"],
                         {"readmodel": "NOT_CONFIGURED", "authoritative": False, "sources": []})
        self.assertFalse((self.root / "state").exists() or (self.root / "local").exists(), "status must not create state")

    def test_state_root_and_absent_readmodel(self):
        for directory in ("state/aik", "local/AIK"):
            (self.root / directory).mkdir(parents=True)
        path = self.root / "readmodel.sqlite3"
        engine = self.engine(EngineConfig(path, SCOPE))
        info = engine.dispatch(request("engine.info"))["result"]
        self.assertEqual((info["state_root"], info["readmodel"]), ("WRITABLE", "ABSENT"))
        self.assertFalse(path.exists(), "the engine must not create the read model")

    def test_readmodel_sources_report_cursor_gaps_as_stale(self):
        path = self.root / "readmodel.sqlite3"
        model = ReadModel(path, user_scope=SCOPE)
        model.apply([dispatch_event(1, "reserved"), dispatch_event(3, "started")])
        result = self.engine(EngineConfig(path, SCOPE)).dispatch(request("readmodel.sources"))["result"]
        self.assertEqual(result["readmodel"], "READY")
        self.assertFalse(result["authoritative"])
        dispatch = [source for source in result["sources"] if source["source"] == "dispatch"]
        self.assertEqual(dispatch, [{"source": "dispatch", "applied_cursor": 1, "max_seen_cursor": 3,
                                     "open_gaps": 1, "conflicts": 0, "stale": True}])
        model.apply([dispatch_event(2, "reserved")])
        result = self.engine(EngineConfig(path, SCOPE)).dispatch(request("readmodel.sources"))["result"]
        self.assertFalse([source for source in result["sources"] if source["source"] == "dispatch"][0]["stale"])

    def test_scope_mismatch_and_foreign_database_are_named(self):
        path = self.root / "readmodel.sqlite3"
        ReadModel(path, user_scope=SCOPE)
        mismatch = self.engine(EngineConfig(path, "os-user-synthetic-2")).dispatch(request("readmodel.sources"))
        self.assertEqual(mismatch["result"]["readmodel"], "SCOPE_MISMATCH")
        foreign = self.root / "foreign.sqlite3"
        foreign.write_bytes(b"not a database" * 100)
        self.assertEqual(self.engine(EngineConfig(foreign, SCOPE)).dispatch(request("readmodel.sources"))
                         ["result"]["readmodel"], "INVALID")

    def test_requests_are_validated_before_any_handler(self):
        engine = self.engine()
        cases = [
            (request("system.status", capability="cd" * 32), "UNAUTHORIZED"),
            (request("system.status", protocol_version=2), "INCOMPATIBLE_PROTOCOL"),
            (request("readmodel.write"), "UNSUPPORTED_COMMAND"),
            (request("system.status", payload={"path": "/"}), "INVALID_REQUEST"),
            (request("system.status", request_id="not-a-uuid"), "INVALID_REQUEST"),
            ({**request("system.status"), "extra": 1}, "INVALID_REQUEST"),
        ]
        for value, code in cases:
            with self.subTest(code=code):
                answer = engine.dispatch(value)
                self.assertFalse(answer["ok"])
                self.assertEqual(answer["error"], {"code": code})

    def test_config_requires_absolute_path_with_scope(self):
        with self.assertRaises(ValueError):
            EngineConfig(Path("relative.sqlite3"), SCOPE)
        with self.assertRaises(ValueError):
            EngineConfig(self.root / "a.sqlite3", None)


class FramingTests(unittest.TestCase):
    def run_serve(self, *values, raw=None):
        stdin = io.BytesIO(raw) if raw is not None else frames(*values)
        stdout = io.BytesIO()
        code = serve(stdin, stdout, environ={"XDG_STATE_HOME": "/nonexistent-aik-test", "LOCALAPPDATA": "C:\\none"})
        return code, lines(stdout)

    def test_session_answers_each_request_on_one_line(self):
        code, answers = self.run_serve({"kind": "bootstrap", "protocol_version": 1, "nonce": NONCE},
                                       *(request(command) for command in sorted(COMMANDS)))
        self.assertEqual(code, 0)
        self.assertEqual(answers[0]["kind"], "ready")
        self.assertEqual(answers[0]["engine_version"], ENGINE_VERSION)
        self.assertEqual([answer["command"] for answer in answers[1:]], sorted(COMMANDS))
        self.assertTrue(all(answer["ok"] for answer in answers[1:]))

    def test_version_mismatch_is_announced_then_the_engine_exits(self):
        code, answers = self.run_serve({"kind": "bootstrap", "protocol_version": 2, "nonce": NONCE},
                                       request("system.status"))
        self.assertEqual(code, 3)
        self.assertEqual(answers, [{"kind": "incompatible", "supported_versions": [1]}])

    def test_malformed_bootstrap_or_frame_ends_the_session(self):
        self.assertEqual(self.run_serve({"kind": "bootstrap", "protocol_version": 1, "nonce": "short"}), (2, []))
        code, answers = self.run_serve(raw=json.dumps({"kind": "bootstrap", "protocol_version": 1,
                                                       "nonce": NONCE}).encode() + b"\n{not json\n")
        self.assertEqual((code, len(answers)), (2, 1))
        oversized = json.dumps({"kind": "bootstrap", "protocol_version": 1, "nonce": NONCE}).encode() + b"\n" \
            + b" " * (MAX_FRAME + 10) + b"\n"
        self.assertEqual(self.run_serve(raw=oversized)[0], 2)
        duplicate = b'{"kind":"bootstrap","kind":"bootstrap","protocol_version":1,"nonce":"' + NONCE.encode() + b'"}\n'
        self.assertEqual(self.run_serve(raw=duplicate), (2, []))

    def test_real_process_over_stdio(self):
        process = subprocess.run(
            [sys.executable, "-I", str(ROOT / "apps" / "desktop" / "engine" / "aik_engine_entry.py")],
            input=frames({"kind": "bootstrap", "protocol_version": 1, "nonce": NONCE},
                         request("system.status")).getvalue(),
            capture_output=True, timeout=30, check=False)
        self.assertEqual(process.returncode, 0)
        answers = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(answers[0]["kind"], "ready")
        self.assertTrue(answers[1]["ok"])
        self.assertEqual(process.stderr, b"")


if __name__ == "__main__":
    unittest.main()
