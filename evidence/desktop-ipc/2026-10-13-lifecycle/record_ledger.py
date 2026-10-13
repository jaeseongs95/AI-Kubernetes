"""One-task, idempotent AIK-004 evidence update. No network or branch writes.

Run from a complete checkout after the IPC tests. Every unrelated table/row,
acceptance criterion and dependency is checked before publishing any snapshot.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
DB = ROOT / "planning/backlog.sqlite3"
TASK = "AIK-004"
EVIDENCE = "EV-AIK-004-IPC-LIFECYCLE-20261013"
REF = "evidence/desktop-ipc/2026-10-13-lifecycle/README.ko.md"
EXPECTED_BASE_BLOB = "5f9d095c654d4b355af9936e58662ab83d2035d8"
SOURCES = ("src/aik_ipc/desktop.cjs", "src/aik_ipc/preload.cjs",
           "tests/test_desktop_ipc.cjs", "tests/test_desktop_ipc_lifecycle.cjs")
OUTPUTS = ("planning/backlog.sqlite3", "planning/backlog.sql",
           "docs/development-backlog.ko.md", "README.md",
           "evidence/desktop-ipc/2026-10-13-lifecycle/ci-validation.json")


def git_blob(raw: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def snapshot(conn: sqlite3.Connection) -> dict:
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    assert all(re.fullmatch(r"[a-z_]+", name) for name in tables)
    return {table: sorted((tuple(r) for r in conn.execute(f'SELECT * FROM "{table}"')), key=repr)
            for table in tables}


def inspect_ledger(conn: sqlite3.Connection) -> dict:
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    task = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (TASK,)).fetchone())
    deps = [dict(r) for r in conn.execute(
        "SELECT p.id,p.status FROM task_dependencies d JOIN tasks p ON p.id=d.depends_on "
        "WHERE d.task_id=? ORDER BY p.id", (TASK,))]
    criteria = [dict(r) for r in conn.execute(
        "SELECT ordinal,criterion FROM acceptance_criteria WHERE task_id=? ORDER BY ordinal", (TASK,))]
    counts = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("tasks", "acceptance_criteria", "task_dependencies")}
    assert counts == {"tasks": 90, "acceptance_criteria": 311, "task_dependencies": 365}
    assert {d["id"] for d in deps} == {"AIK-001", "AIK-002", "AIK-083"}
    assert len(criteria) == 3
    return {"task": task, "dependencies": deps, "criteria": criteria, "counts": counts,
            "ready_tasks": conn.execute("SELECT count(*) FROM v_ready_tasks").fetchone()[0]}


def validate_delta(conn, before, after, timestamp):
    assert set(before) == set(after)
    mutable = {"metadata", "tasks", "task_evidence", "task_events", "sqlite_sequence"}
    for table in set(before) - mutable:
        assert before[table] == after[table], f"Unrelated table changed: {table}"
    assert {k: v for k, v in before["metadata"] if k != "updated_at"} == {
        k: v for k, v in after["metadata"] if k != "updated_at"}
    columns = [r[1] for r in conn.execute("PRAGMA table_info(tasks)")]
    index = columns.index("id")
    old = {r[index]: dict(zip(columns, r)) for r in before["tasks"]}
    new = {r[index]: dict(zip(columns, r)) for r in after["tasks"]}
    assert set(old) == set(new)
    for task in old:
        if task != TASK:
            assert old[task] == new[task], f"Unrelated task changed: {task}"
        else:
            assert new[task] == {**old[task], "status": "blocked", "updated_at": timestamp}
    for table in ("task_events", "task_evidence"):
        removed = set(before[table]) - set(after[table])
        added = set(after[table]) - set(before[table])
        assert not removed and len(added) == 1
        row = next(iter(added))
        assert row[1] == TASK  # both tables have task_id as the second field
    old_sequence, new_sequence = dict(before["sqlite_sequence"]), dict(after["sqlite_sequence"])
    assert {k: v for k, v in old_sequence.items() if k != "task_events"} == {
        k: v for k, v in new_sequence.items() if k != "task_events"}
    assert new_sequence["task_events"] > old_sequence.get("task_events", 0)


def run_tests():
    command = ["node", "--test", "tests/test_desktop_ipc.cjs", "tests/test_desktop_ipc_lifecycle.cjs"]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, "IPC tests failed"
    for field, number in (("tests", 35), ("pass", 35), ("fail", 0), ("skipped", 0), ("cancelled", 0)):
        assert re.search(rf"^# {field} {number}$", result.stdout, re.MULTILINE), f"Missing test result: {field}"
    return {"command": command, "tests": 35, "passed": 35, "failed": 0, "skipped": 0,
            "tap_sha256": hashlib.sha256(result.stdout.encode()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    assert DB.is_file(), "Canonical ledger missing"
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    current = inspect_ledger(conn)
    print("AIK004_LEDGER_READ " + json.dumps(current, ensure_ascii=False))
    if not args.prepare:
        conn.close()
        return
    if conn.execute("SELECT 1 FROM task_evidence WHERE id=?", (EVIDENCE,)).fetchone():
        assert current["task"]["status"] == "blocked"
        print("AIK004_ALREADY_RECORDED")
        conn.close()
        return
    assert current["task"]["status"] == "todo"
    assert any(d["status"] != "done" for d in current["dependencies"])
    raw = DB.read_bytes()
    assert git_blob(raw) == EXPECTED_BASE_BLOB, "Canonical ledger changed; review before rebasing"
    test_result = run_tests()
    source_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    assert re.fullmatch(r"[a-f0-9]{40}", source_commit)
    sources = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    summary = (
        "AIK-004 IPC 수명주기 보강: 요청·응답 snapshot, UUID 대소문자 정규화, "
        "미종료 요청의 replay fence 보존, 기본 5초 응답 기한과 AbortSignal, "
        "실제 작업 종료 전까지 동시성 슬롯 보존, Proxy/toJSON/원시 오류 차단. "
        "기존 10개와 신규 25개 Node 시험 35/35 PASS, 실패/스킵 0. "
        f"검증 소스 {source_commit}. AIK-083 미완료 및 설치형 GUI/native 통합 미검증이므로 "
        "완료 처리하지 않고 blocked로 기록. 기존 90개 태스크·311개 완료 기준·365개 의존성 보존."
    )
    before = snapshot(conn)
    schema_before = [tuple(r) for r in conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")]
    with conn:
        conn.execute("UPDATE tasks SET status='blocked',updated_at=? WHERE id=?", (timestamp, TASK))
        conn.execute("UPDATE metadata SET value=? WHERE key='updated_at'", (timestamp,))
        conn.execute("INSERT INTO task_evidence(id,task_id,kind,reference,summary,recorded_at) VALUES(?,?,?,?,?,?)",
                     (EVIDENCE, TASK, "partial_implementation_validation", REF, summary, timestamp))
        conn.execute("INSERT INTO task_events(task_id,event_type,old_status,new_status,note,occurred_at) VALUES(?,?,?,?,?,?)",
                     (TASK, "implementation_validation", "todo", "blocked", summary, timestamp))
        after = snapshot(conn)
        validate_delta(conn, before, after, timestamp)
        assert schema_before == [tuple(r) for r in conn.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name")]
        final = inspect_ledger(conn)
    conn.close()
    for subcommand in ("validate", "export", "report"):
        subprocess.run([sys.executable, str(ROOT / "tools/backlog.py"), subcommand], cwd=ROOT, check=True)
    # Update only the README's derived counts for this one status transition.
    readme = ROOT / "README.md"
    old = "blocked 2개(AIK-003·005), todo 86개, ready 0개"
    new = "blocked 3개(AIK-003·004·005), todo 85개, ready 0개"
    text = readme.read_text(encoding="utf-8")
    assert text.count(old) == 1
    readme.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
    report = {"task_id": TASK, "source_commit": source_commit, "recorded_at": timestamp,
              "tests": test_result, "sources_sha256": sources,
              "canonical_ledger_before_git_blob": EXPECTED_BASE_BLOB,
              "canonical_ledger_after_git_blob": git_blob(DB.read_bytes()),
              "ledger_delta": {"tasks_changed": [TASK], "evidence_added": 1, "events_added": 1,
                               "acceptance_criteria_unchanged": True, "dependencies_unchanged": True,
                               "other_tasks_unchanged": True, "schema_unchanged": True},
              "ledger_counts": final["counts"], "ready_tasks": final["ready_tasks"],
              "task_status": "blocked", "installed_product_integration": "NOT_RUN"}
    (ROOT / OUTPUTS[-1]).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("AIK004_LEDGER_VERIFIED " + json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
