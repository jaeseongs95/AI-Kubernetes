"""AIK-083 one-task evidence preparation. No network, branch or commit writes.

Generate the patched checker deterministically, reproduce eight old regressions,
verify the new source, then optionally update only the canonical AIK-083 record.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

from apply_checker_patch import patched, ORIGINAL

ROOT = Path(__file__).resolve().parents[3]
DB = ROOT / "planning/backlog.sqlite3"
BASE = "b60f7c71c96dd0d95fa98214d744db04dd65e562"
BASE_DB = "241f72295d8f07a05b498089962c47b3c97e104a"
TASK = "AIK-083"
EVIDENCE = "EV-AIK-083-FILE-SNAPSHOTS-20261013"
REF = "evidence/product-acceptance/2026-10-13-snapshots/README.ko.md"
REPORT = "evidence/product-acceptance/2026-10-13-snapshots/ci-validation.json"
SOURCES = ("tools/product_acceptance.py", "tools/acceptance_io.py",
           "tests/test_product_acceptance.py", "tests/test_product_acceptance_snapshots.py")


def blob(raw):
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


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
    counts = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("tasks", "acceptance_criteria", "task_dependencies")}
    assert counts == {"tasks": 90, "acceptance_criteria": 311, "task_dependencies": 365}
    assert {d["id"] for d in deps} == {"AIK-001", "AIK-002", "AIK-003"}
    assert len(criteria) == 4
    return {"task": task, "dependencies": deps, "criteria": criteria, "counts": counts,
            "ready_tasks": conn.execute("SELECT count(*) FROM v_ready_tasks").fetchone()[0]}


RUNNER = '''
import json, sys, unittest
sys.path.insert(0, "tests")
if sys.argv[1] == "baseline":
    from test_product_acceptance_snapshots import AcceptanceSnapshotTests
    names = [n for n in unittest.defaultTestLoader.getTestCaseNames(AcceptanceSnapshotTests) if n.startswith("test_0")]
    suite = unittest.TestSuite(AcceptanceSnapshotTests(n) for n in names)
else:
    suite = unittest.defaultTestLoader.discover("tests", pattern="test_product_acceptance*.py")
result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
failed = sorted({getattr(case, "test_case", case).id() for case, _ in result.failures + result.errors})
print(json.dumps({"tests":result.testsRun, "passed":result.testsRun-len(failed)-len(result.skipped),
 "failing_cases":failed, "failure_records":len(result.failures), "error_records":len(result.errors),
 "skipped":len(result.skipped), "successful":result.wasSuccessful()}))
'''


def run_tests(root, mode):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    result = subprocess.run([sys.executable, "-B", "-c", RUNNER, mode], cwd=root,
                            capture_output=True, text=True, encoding="utf-8", env=env, timeout=45)
    assert result.returncode == 0, "Test runner could not finish"
    summary = json.loads(result.stdout)
    summary["log_sha256"] = hashlib.sha256(result.stderr.encode()).hexdigest()
    print(result.stderr, end="")
    print("AIK083_" + mode.upper() + " " + json.dumps(summary))
    return summary


def verify():
    original = subprocess.check_output(["git", "show", BASE + ":tools/product_acceptance.py"], cwd=ROOT)
    assert blob(original) == ORIGINAL
    result = patched(original)
    destination = ROOT / "tools/product_acceptance.py"
    assert destination.read_bytes() in (original, result), "Unexpected checker changes"
    destination.write_bytes(result)
    with tempfile.TemporaryDirectory(prefix="aik083-old-") as directory:
        target = Path(directory)
        (target / "tools").mkdir()
        (target / "tools/product_acceptance.py").write_bytes(original)
        shutil.copyfile(ROOT / "tools/acceptance_io.py", target / "tools/acceptance_io.py")
        shutil.copytree(ROOT / "contracts", target / "contracts")
        (target / "docs").mkdir()
        shutil.copyfile(ROOT / "docs/development-backlog.ko.md", target / "docs/development-backlog.ko.md")
        (target / "tests").mkdir()
        shutil.copyfile(ROOT / "tests/test_product_acceptance_snapshots.py", target / "tests/test_product_acceptance_snapshots.py")
        baseline = run_tests(target, "baseline")
    assert baseline["tests"] == 8 and baseline["passed"] == 0 and len(baseline["failing_cases"]) == 8
    assert baseline["skipped"] == 0
    current = run_tests(ROOT, "current")
    assert current["tests"] == 36 and current["passed"] == 36 and current["successful"]
    assert not current["failing_cases"] and current["skipped"] == 0
    return {"baseline": baseline, "current": current}


def snapshot(conn):
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    assert all(re.fullmatch(r"[a-z_]+", t) for t in tables)
    return {t: sorted((tuple(r) for r in conn.execute(f'SELECT * FROM "{t}"')), key=repr) for t in tables}


def check_delta(conn, before, after, timestamp):
    assert set(before) == set(after)
    for table in set(before) - {"tasks", "metadata", "task_evidence", "task_events", "sqlite_sequence"}:
        assert before[table] == after[table], table
    assert {k: v for k, v in before["metadata"] if k != "updated_at"} == {
        k: v for k, v in after["metadata"] if k != "updated_at"}
    columns = [r[1] for r in conn.execute("PRAGMA table_info(tasks)")]
    a = {dict(zip(columns, r))["id"]: dict(zip(columns, r)) for r in before["tasks"]}
    b = {dict(zip(columns, r))["id"]: dict(zip(columns, r)) for r in after["tasks"]}
    assert set(a) == set(b)
    for task in a:
        expected = {**a[task], "status": "blocked", "updated_at": timestamp} if task == TASK else a[task]
        assert b[task] == expected, task
    for table in ("task_events", "task_evidence"):
        assert not set(before[table]) - set(after[table])
        added = set(after[table]) - set(before[table])
        assert len(added) == 1 and next(iter(added))[1] == TASK
    a, b = dict(before["sqlite_sequence"]), dict(after["sqlite_sequence"])
    assert {k: v for k, v in a.items() if k != "task_events"} == {k: v for k, v in b.items() if k != "task_events"}
    assert b["task_events"] > a.get("task_events", 0)


def main():
    if not __debug__:
        raise RuntimeError("Evidence checks require assertions")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    with closing(sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        before_read = read_task(conn)
        print("AIK083_LEDGER_READ " + json.dumps(before_read, ensure_ascii=False))
    tests = verify()
    if not args.prepare:
        return
    assert blob(DB.read_bytes()) == BASE_DB, "Canonical ledger moved; review required"
    assert before_read["task"]["status"] == "todo"
    assert any(d["status"] != "done" for d in before_read["dependencies"])
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    assert re.fullmatch(r"[0-9a-f]{40}", commit)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    summary = (
        "AIK-083 파일 oracle 보강: 검증·계산·digest를 동일한 bounded byte snapshot에 결속, "
        "검사 중 파일 변경/교체 감지, JSON object 키 순서 오판 수정, 비정상 Unicode·숫자 overflow·중첩/크기 제한. "
        "기존12+신규24=36/36 PASS, 이전 source 선택 반례8개 실패 재현. "
        f"검증 source {commit}; 생성 checker의 정확한 해시는 증거에 기록. "
        "AIK-003 및 실제 VM/AGS·설치본·인수계약 승인 미완료로 blocked 기록."
    )
    with closing(sqlite3.connect(DB)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
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
    old = "blocked 5개(AIK-003·004·005·011·017), todo 83개, ready 0개"
    new = "blocked 6개(AIK-003·004·005·011·017·083), todo 82개, ready 0개"
    assert text.count(old) == 1
    readme.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
    report = {"task_id": TASK, "source_commit": commit, "base_commit": BASE, "recorded_at": timestamp,
              "github_run_id": os.environ.get("GITHUB_RUN_ID"), "tests": tests,
              "environment": {"python": sys.version, "platform": sys.platform},
              "sources_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES},
              "patched_checker_git_blob": blob((ROOT / "tools/product_acceptance.py").read_bytes()),
              "baseline_checker_git_blob": ORIGINAL, "canonical_ledger_before_git_blob": BASE_DB,
              "canonical_ledger_after_git_blob": blob(DB.read_bytes()),
              "ledger_delta": {"tasks_changed": [TASK], "evidence_added": 1, "events_added": 1,
                               "other_tasks_unchanged": True, "acceptance_criteria_unchanged": True,
                               "dependencies_unchanged": True, "schema_unchanged": True},
              "counts": final["counts"], "ready_tasks": final["ready_tasks"],
              "task_status": "blocked", "installed_product_qualification": "NOT_RUN"}
    (ROOT / REPORT).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("AIK083_LEDGER_VERIFIED " + json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
