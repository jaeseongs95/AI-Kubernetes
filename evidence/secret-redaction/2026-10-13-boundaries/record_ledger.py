"""Read and record only AIK-017 evidence; no network or branch writes."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
DB = ROOT / "planning/backlog.sqlite3"
TASK = "AIK-017"
EVIDENCE = "EV-AIK-017-REDACTION-BOUNDARIES-20261013"
REF = "evidence/secret-redaction/2026-10-13-boundaries/README.ko.md"
BASE_BLOB = "611bcdb4eb30d0a1c73687deb5d2080130db704f"
SOURCES = ("src/aik_security/redaction.py", "tests/test_security_redaction.py",
           "tests/test_security_redaction_boundaries.py")
OUTPUTS = ("planning/backlog.sqlite3", "planning/backlog.sql", "docs/development-backlog.ko.md",
           "README.md", "evidence/secret-redaction/2026-10-13-boundaries/ci-validation.json")


def blob(raw):
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def snapshot(conn):
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    assert all(re.fullmatch(r"[a-z_]+", name) for name in tables)
    return {name: sorted((tuple(row) for row in conn.execute(f'SELECT * FROM "{name}"')), key=repr)
            for name in tables}


def read_task(conn):
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    task = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (TASK,)).fetchone())
    deps = [dict(r) for r in conn.execute(
        "SELECT p.id,p.status FROM task_dependencies d JOIN tasks p ON p.id=d.depends_on "
        "WHERE d.task_id=? ORDER BY p.id", (TASK,))]
    criteria = [dict(r) for r in conn.execute(
        "SELECT ordinal,criterion FROM acceptance_criteria WHERE task_id=? ORDER BY ordinal", (TASK,))]
    counts = {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
              for table in ("tasks", "acceptance_criteria", "task_dependencies")}
    assert counts == {"tasks": 90, "acceptance_criteria": 311, "task_dependencies": 365}
    assert {d["id"] for d in deps} == {"AIK-004", "AIK-009", "AIK-011"}
    assert len(criteria) == 3
    return {"task": task, "dependencies": deps, "criteria": criteria, "counts": counts,
            "ready_tasks": conn.execute("SELECT count(*) FROM v_ready_tasks").fetchone()[0]}


def check_delta(conn, before, after, timestamp):
    assert set(before) == set(after)
    mutable = {"tasks", "metadata", "task_evidence", "task_events", "sqlite_sequence"}
    for name in set(before) - mutable:
        assert before[name] == after[name], name
    assert {k: v for k, v in before["metadata"] if k != "updated_at"} == {
        k: v for k, v in after["metadata"] if k != "updated_at"}
    columns = [r[1] for r in conn.execute("PRAGMA table_info(tasks)")]
    old = {dict(zip(columns, r))["id"]: dict(zip(columns, r)) for r in before["tasks"]}
    new = {dict(zip(columns, r))["id"]: dict(zip(columns, r)) for r in after["tasks"]}
    assert set(old) == set(new)
    for task_id in old:
        expected = {**old[task_id], "status": "blocked", "updated_at": timestamp} if task_id == TASK else old[task_id]
        assert new[task_id] == expected, task_id
    for name in ("task_events", "task_evidence"):
        assert not set(before[name]) - set(after[name])
        added = set(after[name]) - set(before[name])
        assert len(added) == 1 and next(iter(added))[1] == TASK
    a, b = dict(before["sqlite_sequence"]), dict(after["sqlite_sequence"])
    assert {k: v for k, v in a.items() if k != "task_events"} == {k: v for k, v in b.items() if k != "task_events"}
    assert b["task_events"] > a.get("task_events", 0)


def main():
    if not __debug__:
        raise RuntimeError("Run without optimization; verification assertions are required")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    assert DB.is_file()
    with closing(sqlite3.connect(DB)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        current = read_task(conn)
        print("AIK017_LEDGER_READ " + json.dumps(current, ensure_ascii=False))
        if not args.prepare:
            return
        if conn.execute("SELECT 1 FROM task_evidence WHERE id=?", (EVIDENCE,)).fetchone():
            assert current["task"]["status"] == "blocked"
            print("AIK017_ALREADY_RECORDED")
            return
        assert blob(DB.read_bytes()) == BASE_BLOB, "Canonical ledger changed; review before rebasing"
        assert current["task"]["status"] == "todo" and any(d["status"] != "done" for d in current["dependencies"])
        command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_security_redaction*.py", "-v"]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert result.returncode == 0 and re.search(r"Ran 38 tests in", result.stderr)
        assert not re.search(r"skipped=|FAILED|ERROR:", result.stderr)
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        assert re.fullmatch(r"[0-9a-f]{40}", commit)
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        summary = (
            "AIK-017 시크릿 정제 결함 수정: 원문의 겹치는 일치 구간을 병합해 부분 키 노출 방지, "
            "quoted JSON 값·약어 필드·혼합 percent/base64 처리, 잘못된 Unicode의 안전 생략과 원자적 등록, "
            "전체 노드/텍스트/탐색 예산 및 envelope 포함 64KiB 제한. 기존17+신규21=38/38 PASS, 오류/실패/skip0. "
            f"검증 소스 {commit}. 선행 AIK-004·009·011과 전체 제품 출력 연결은 미완료이므로 blocked 기록."
        )
        before = snapshot(conn)
        schema = [tuple(r) for r in conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")]
        with conn:
            conn.execute("UPDATE tasks SET status='blocked',updated_at=? WHERE id=?", (timestamp, TASK))
            conn.execute("UPDATE metadata SET value=? WHERE key='updated_at'", (timestamp,))
            conn.execute("INSERT INTO task_evidence(id,task_id,kind,reference,summary,recorded_at) VALUES(?,?,?,?,?,?)",
                         (EVIDENCE, TASK, "partial_implementation_validation", REF, summary, timestamp))
            conn.execute("INSERT INTO task_events(task_id,event_type,old_status,new_status,note,occurred_at) VALUES(?,?,?,?,?,?)",
                         (TASK, "status_change", "todo", "blocked", summary, timestamp))
            check_delta(conn, before, snapshot(conn), timestamp)
            assert schema == [tuple(r) for r in conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")]
            final = read_task(conn)
            sys.path.insert(0, str(ROOT / "tools"))
            from backlog import require_valid
            require_valid(conn)
    for subcommand in ("validate", "export", "report"):
        subprocess.run([sys.executable, str(ROOT / "tools/backlog.py"), subcommand], cwd=ROOT, check=True)
    readme = ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    old = "blocked 3개(AIK-003·004·005), todo 85개, ready 0개"
    new = "blocked 4개(AIK-003·004·005·017), todo 84개, ready 0개"
    assert text.count(old) == 1
    readme.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
    report = {
        "task_id": TASK, "source_commit": commit, "recorded_at": timestamp,
        "github_run_id": os.environ.get("GITHUB_RUN_ID"),
        "tests": {"tests": 38, "passed": 38, "failed": 0, "errors": 0, "skipped": 0,
                  "log_sha256": hashlib.sha256(result.stderr.encode()).hexdigest()},
        "sources_sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in SOURCES},
        "canonical_ledger_before_git_blob": BASE_BLOB,
        "canonical_ledger_after_git_blob": blob(DB.read_bytes()),
        "ledger_delta": {"tasks_changed": [TASK], "evidence_added": 1, "events_added": 1,
                         "other_tasks_unchanged": True, "acceptance_criteria_unchanged": True,
                         "dependencies_unchanged": True, "schema_unchanged": True},
        "counts": final["counts"], "ready_tasks": final["ready_tasks"],
        "task_status": "blocked", "installed_product_integration": "NOT_RUN"
    }
    (ROOT / OUTPUTS[-1]).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("AIK017_LEDGER_VERIFIED " + json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
