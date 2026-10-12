#!/usr/bin/env python3
"""고정 AGS 설치물과 VM non-editable 설치의 공개 경계를 검사한다.

모델 호출, 계정 읽기, 사용자 원장 변경은 하지 않는다. 프로젝트 생성은 새
임시 디렉터리에만 수행한다. 성공한 metadata 검사를 runtime qualification으로
승격하지 않으며, 호환 불일치는 원인과 원시 출력 그대로 반환한다.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from typing import Any


DEFAULT_LOCK = Path(__file__).resolve().parents[1] / "contracts/upstream/baseline.json"


def strict_json(text: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=pairs)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_files(root: Path, files: list[dict[str, Any]]) -> dict[str, Any]:
    """파일 경로·존재·실제 bytes를 대조한다. 경로 탈출과 symlink를 거절한다."""
    root = root.resolve(strict=True)
    if not root.is_dir() or not files:
        raise ValueError("artifact root must be a directory and the file list cannot be empty")
    rows = []
    seen: set[str] = set()
    for expected in files:
        relative = expected["path"]
        if not isinstance(relative, str) or not isinstance(expected.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", expected["sha256"]):
            raise ValueError("artifact path and SHA-256 must be explicit strings")
        parts = PurePosixPath(relative)
        if (not relative or parts.is_absolute() or parts.as_posix() != relative
                or ".." in parts.parts or "\\" in relative or ":" in relative
                or relative in seen):
            raise ValueError(f"invalid or duplicate artifact path: {relative}")
        seen.add(relative)
        path = root.joinpath(*parts.parts)
        if not path.resolve().is_relative_to(root) or any(
            parent.is_symlink() for parent in (path, *path.parents) if parent != root and parent.is_relative_to(root)
        ):
            raise ValueError(f"artifact path escapes through a link: {relative}")
        if not path.is_file():
            rows.append({"path": relative, "status": "MISSING", "sha256": None})
            continue
        raw = path.read_bytes()
        digest = sha256(raw)
        rows.append({"path": relative, "status": "PASS" if digest == expected["sha256"] else "MISMATCH",
                     "sha256": digest, "bytes": len(raw)})
    return {"status": "PASS" if all(x["status"] == "PASS" for x in rows) else "BLOCKED", "files": rows}


def isolated_environment(state: Path) -> dict[str, str]:
    """인증·provider·기존 원장 설정을 자식에게 전달하지 않는 최소 실행 환경."""
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "LANG") if key in os.environ}
    env.update({
        "TEMP": str(state), "TMP": str(state), "TMPDIR": str(state),
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "AGENT_GOVERNANCE_DB_PATH": str(state / "workflows.sqlite3"),
        "AGENT_GOVERNANCE_CONTINUITY_DB_PATH": str(state / "continuity.sqlite3"),
        "AGENT_GOVERNANCE_TRUST_DB_PATH": str(state / "trust.sqlite3"),
        "AGENT_GOVERNANCE_SESSION_BOARD_DB_PATH": str(state / "session-board.sqlite3"),
        "AGENT_GOVERNANCE_SHARED_STATE_DIR": str(state / "shared"),
        "AGENT_GOVERNANCE_SESSION_MESSAGE_STATE_DIR": str(state / "messages"),
    })
    return env


def run_command(argv: list[str], cwd: Path, env: dict[str, str], timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = subprocess.run(argv, cwd=cwd, env=env, text=True, encoding="utf-8", errors="replace",
                                capture_output=True, timeout=timeout, check=False)
        return {"command": argv, "exit_code": result.returncode, "timed_out": False,
                "stdout": result.stdout, "stderr": result.stderr,
                "duration_seconds": round(time.monotonic() - started, 4)}
    except subprocess.TimeoutExpired as error:
        decode = lambda value: value.decode("utf-8", "replace") if isinstance(value, bytes) else (value or "")
        return {"command": argv, "exit_code": None, "timed_out": True,
                "stdout": decode(error.stdout), "stderr": decode(error.stderr),
                "duration_seconds": round(time.monotonic() - started, 4)}


def mcp_inventory(node: str, entrypoint: Path, cwd: Path, env: dict[str, str], timeout: float) -> dict[str, Any]:
    """initialize·tools/list와 VM이 요구하는 읽기 전용 fm/hello를 확인한다."""
    commands = [node, str(entrypoint)]
    incoming: queue.Queue[str | None] = queue.Queue()
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errors:
        process = subprocess.Popen(commands, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=errors, text=True,
                                   encoding="utf-8", errors="replace", bufsize=1)
        def read_lines() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                incoming.put(line)
            incoming.put(None)
        reader = threading.Thread(target=read_lines, daemon=True)
        reader.start()
        observed: list[Any] = []
        started = time.monotonic()
        deadline = started + timeout
        def send(value: dict[str, Any]) -> None:
            assert process.stdin is not None
            process.stdin.write(json.dumps(value) + "\n")
            process.stdin.flush()
        def receive(request_id: int) -> dict[str, Any]:
            while time.monotonic() < deadline:
                line = incoming.get(timeout=max(0.001, deadline - time.monotonic()))
                if line is None:
                    raise RuntimeError("MCP_SERVER_EXITED")
                if len(line) > 8_000_000:
                    raise ValueError("MCP response exceeds inventory limit")
                payload = strict_json(line)
                observed.append(payload)
                if payload.get("id") == request_id:
                    return payload
            raise TimeoutError("MCP inventory deadline")
        result: dict[str, Any] = {"command": commands, "methods": ["initialize", "tools/list", "fm/hello"]}
        try:
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "aik-upstream-inspector", "version": "1"}}})
            initialized = receive(1)
            if "error" in initialized or not isinstance(initialized.get("result"), dict):
                raise RuntimeError("MCP_INITIALIZE_REJECTED")
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            listed = receive(2)
            payload = listed.get("result", {})
            if "error" in listed or not isinstance(payload.get("tools"), list) or payload.get("nextCursor"):
                raise RuntimeError("MCP_TOOL_INVENTORY_INCOMPLETE")
            result.update({"status": "PASS", "initialize_response": initialized,
                           "tool_names": sorted(tool["name"] for tool in payload["tools"]),
                           "tools_list_sha256": sha256(json.dumps(listed, sort_keys=True, separators=(",", ":")).encode()),
                           "tool_count": len(payload["tools"])})
            send({"jsonrpc": "2.0", "id": 3, "method": "fm/hello", "params": {}})
            result["vm_a2_hello_response"] = receive(3)
        except (OSError, ValueError, RuntimeError, TimeoutError, queue.Empty) as error:
            result.update({"status": "BLOCKED", "error": f"{type(error).__name__}: {error}", "responses": observed})
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            reader.join(timeout=1)
            if process.stdin:
                process.stdin.close()
            if process.stdout:
                process.stdout.close()
            errors.seek(0)
            result["stderr"] = errors.read()
            result["duration_seconds"] = round(time.monotonic() - started, 4)
        return result


VM_METADATA_CODE = r'''
import importlib.metadata as m, inspect, json, pathlib
import flowmarshal, flowmarshal.engine
from flowmarshal.engine.application import EngineApplication, ApplicationAuthority
from flowmarshal.engine.runtime import CodexRuntimePort, owner_lock_platform_supported
from flowmarshal.engine.ledger import ENGINE_SCHEMA_REVISION, ENGINE_SCHEMA_ID, SQLITE_APPLICATION_ID
d=m.distribution("flowmarshal-engine")
print(json.dumps({"distribution":d.metadata["Name"], "version":d.version,
 "domain_schema_version":flowmarshal.engine.ENGINE_SCHEMA_VERSION,
 "sqlite_schema_revision":ENGINE_SCHEMA_REVISION, "sqlite_schema_id":ENGINE_SCHEMA_ID,
 "sqlite_application_id":SQLITE_APPLICATION_ID,
 "package_root":str(pathlib.Path(flowmarshal.__file__).parent),
 "direct_url":json.loads(d.read_text("direct_url.json") or "null"),
 "runtime_owner_platform_supported":owner_lock_platform_supported(),
 "entrypoints":{e.name:e.value for e in d.entry_points},
 "facade":{n:str(inspect.signature(getattr(EngineApplication,n))) for n in
 ["prepare","revise","authorize","run_once","observe","pause","cancel","status","replan","final_report","attempt_detail","usage_summary","recovery_status","model_binding_status"]},
 "runtime_port":{n:str(inspect.signature(getattr(CodexRuntimePort,n))) for n in
 ["verify_execution_policy","list_models","create_thread","start_turn","read","resume","interrupt","read_stored"]}},ensure_ascii=False))
'''


def inspect_upstream(ags_root: Path, vm_python: str, node: str, lock: dict[str, Any],
                     scratch_parent: Path | None = None, timeout: float = 25) -> dict[str, Any]:
    report: dict[str, Any] = {"format": "aik-upstream-inspection-v1",
        "observed_at": datetime.now(timezone.utc).isoformat(), "scope": "installed_public_contract",
        "qualification": "NOT_RUN", "provider_calls": 0, "checks": {}, "commands": {}}
    checks = report["checks"]
    checks["ags_bytes"] = verify_files(ags_root, lock["ags"]["files"])
    if checks["ags_bytes"]["status"] != "PASS":
        report.update({"status": "BLOCKED", "reason": "AGS_ARTIFACT_BYTES_MISMATCH"})
        return report
    with tempfile.TemporaryDirectory(prefix="aik-upstream-", dir=scratch_parent) as temporary:
        state = Path(temporary).resolve()
        env = isolated_environment(state)
        prefix = [vm_python, "-I"]
        meta = run_command([*prefix, "-c", VM_METADATA_CODE], state, env, timeout)
        report["commands"]["vm_metadata"] = meta
        if meta["exit_code"] != 0:
            report.update({"status": "BLOCKED", "reason": "VM_IMPORT_FAILED"})
            return report
        metadata = strict_json(meta["stdout"])
        report["vm_installed"] = metadata
        vm_root = Path(metadata["package_root"])
        checks["vm_bytes"] = verify_files(vm_root, lock["vm"]["files"])
        schema_matches = (metadata["sqlite_schema_revision"] == lock["vm"]["sqlite_schema_revision"]
                          and metadata["domain_schema_version"] == lock["vm"]["domain_schema_version"])
        checks["vm_schema"] = {"status": "PASS" if schema_matches else "BLOCKED",
                               "sqlite_schema_revision": metadata["sqlite_schema_revision"],
                               "domain_schema_version": metadata["domain_schema_version"]}
        direct_url = metadata.get("direct_url") or {}
        editable = direct_url.get("dir_info", {}).get("editable", False)
        wheel_hash = direct_url.get("archive_info", {}).get("hashes", {}).get("sha256")
        checks["vm_non_editable"] = {"status": "PASS" if not editable and wheel_hash else "BLOCKED", "editable": editable}
        checks["vm_wheel"] = {"status": "PASS" if wheel_hash == lock["vm"]["wheel_sha256"] else "BLOCKED",
                              "observed_sha256": wheel_hash, "expected_sha256": lock["vm"]["wheel_sha256"]}
        matches_distribution = (metadata["distribution"] == lock["vm"]["distribution"]
                                and metadata["version"] == lock["vm"]["version_label"])
        checks["vm_distribution"] = {"status": "PASS" if matches_distribution else "BLOCKED",
                                     "name": metadata["distribution"], "version": metadata["version"]}
        if any(checks[key]["status"] != "PASS" for key in ("vm_bytes", "vm_schema", "vm_non_editable", "vm_wheel", "vm_distribution")):
            report.update({"status": "BLOCKED", "reason": "VM_ARTIFACT_CONTRACT_MISMATCH"})
            return report
        console = [*prefix, "-m", "flowmarshal.engine.console_host"]
        report["commands"]["vm_help"] = run_command([*console, "--help"], state, env, timeout)
        project = state / "project"
        project.mkdir()
        (project / "README.txt").write_text("AIK installed-contract probe; no provider invocation.\n", encoding="utf-8")
        args = [*console, "--db", str(state / "engine.sqlite3"), "--artifacts", str(state / "artifacts")]
        created = run_command([*args, "project", "init", "--name", "AIK contract probe", "--root", str(project)], state, env, timeout)
        report["commands"]["vm_project_init"] = created
        if created["exit_code"] == 0:
            project_id = strict_json(created["stdout"])["project_id"]
            report["commands"]["vm_status"] = run_command([*args, "status", "--project-id", project_id], state, env, timeout)
            with sqlite3.connect(f"file:{(state / 'engine.sqlite3').as_posix()}?mode=ro", uri=True) as connection:
                revision = connection.execute("PRAGMA user_version").fetchone()[0]
                application_id = connection.execute("PRAGMA application_id").fetchone()[0]
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                counts = {name: connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                          for name in ("projects", "task_contracts", "attempts", "provider_calls")}
            checks["vm_created_ledger"] = {"status": "PASS" if revision == metadata["sqlite_schema_revision"]
                and application_id == metadata["sqlite_application_id"] and integrity == "ok" else "BLOCKED",
                "user_version": revision, "application_id": application_id, "integrity": integrity, "counts": counts}
        checks["ags_mcp_inventory"] = mcp_inventory(node, ags_root / "mcp-server/dist/server.mjs", ags_root, env, timeout)
        hello = checks["ags_mcp_inventory"].get("vm_a2_hello_response", {})
        epoch = hello.get("result", {}).get("serverEpoch")
        checks["ags_vm_a2_namespace"] = {"status": "PASS" if isinstance(epoch, str) and len(epoch) >= 32 else "BLOCKED",
                                          "response": hello}
        compatibility = run_command([*console, "governance", "check-plugin", "--plugin-root", str(ags_root)], state, env, timeout)
        report["commands"]["vm_governance_check_plugin"] = compatibility
        checks["vm_ags_compatibility"] = {"status": "PASS" if compatibility["exit_code"] == 0 else "BLOCKED",
                                           "exit_code": compatibility["exit_code"]}
    commands_ok = all(value["exit_code"] == 0 for value in report["commands"].values())
    checks_ok = all(value["status"] == "PASS" for value in checks.values())
    report["status"] = "PASS" if commands_ok and checks_ok else "BLOCKED"
    report["blocked_checks"] = [name for name, value in checks.items() if value["status"] != "PASS"]
    report["runtime_readiness"] = "NOT_QUALIFIED"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ags-root", type=Path, required=True)
    parser.add_argument("--vm-python", required=True, help="설치된 VM을 import하는 Python 실행 파일")
    parser.add_argument("--node", default=shutil.which("node"))
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--scratch-parent", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not args.node:
        parser.error("Node.js executable is required")
    lock = strict_json(args.lock.read_text(encoding="utf-8"))
    if args.output:
        output = args.output.resolve()
        if (output == args.lock.resolve() or output.is_relative_to(args.ags_root.resolve())
                or output.is_relative_to(Path(os.path.abspath(args.vm_python)).parent.parent)):
            parser.error("output must be outside the inspected artifacts and baseline")
    report = inspect_upstream(args.ags_root.resolve(strict=True), os.path.abspath(args.vm_python),
                              os.path.abspath(args.node), lock, args.scratch_parent)
    content = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
    else:
        print(content, end="")
    return 0 if report["status"] == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
