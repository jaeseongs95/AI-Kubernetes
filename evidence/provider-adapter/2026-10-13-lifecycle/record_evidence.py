"""Verify and record only AIK-011 evidence. No network or Git ref writes."""
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

ROOT = Path(__file__).resolve().parents[3]
DB = ROOT / "planning/backlog.sqlite3"
BASE = "1a43ae80d1ebbd817b977172fc2cf76d31bb17e0"
BASE_DB = "97b9248ea0dafc1fba576552378a57a052820561"
BASE_CONTROLLER = "076bb08606e4ae800f40027a9e4d8bf04d1d99c1"
TASK = "AIK-011"
EVIDENCE = "EV-AIK-011-AUTH-LIFECYCLE-20261013"
REF = "evidence/provider-adapter/2026-10-13-lifecycle/README.ko.md"
SOURCES = ("src/aik_provider/controller.py", "src/aik_provider/contracts.py", "src/aik_provider/__init__.py",
           "tests/test_provider_contract.py", "tests/test_provider_lifecycle.py")
REPORT = "evidence/provider-adapter/2026-10-13-lifecycle/ci-validation.json"


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
    assert {d["id"] for d in deps} == {"AIK-001", "AIK-010"}
    assert len(criteria) == 3
    return {"task": task, "dependencies": deps, "criteria": criteria, "counts": counts,
            "ready_tasks": conn.execute("SELECT count(*) FROM v_ready_tasks").fetchone()[0]}


RUNNER = '''
import json, sys, unittest
sys.path.insert(0, "tests")
if sys.argv[1] == "baseline":
    from test_provider_lifecycle import ProviderLifecycleTests
    names = [n for n in unittest.defaultTestLoader.getTestCaseNames(ProviderLifecycleTests) if n.startswith("test_0")]
    suite = unittest.TestSuite(ProviderLifecycleTests(n) for n in names)
else:
    suite = unittest.defaultTestLoader.discover("tests", pattern="test_provider*.py")
result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
failed = sorted({getattr(case, "test_case", case).id() for case, _ in result.failures + result.errors})
print(json.dumps({"tests":result.testsRun, "passed":result.testsRun-len(failed)-len(result.skipped),
 "failing_cases":failed, "failure_records":len(result.failures), "error_records":len(result.errors),
 "skipped":len(result.skipped), "successful":result.wasSuccessful()}))
'''


def test_run(cwd, mode):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    result = subprocess.run([sys.executable, "-B", "-c", RUNNER, mode], cwd=cwd, env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=45)
    assert result.returncode == 0, "Test runner did not produce a report"
    summary = json.loads(result.stdout)
    summary["log_sha256"] = hashlib.sha256(result.stderr.encode()).hexdigest()
    print(result.stderr, end="")
    print("AIK011_" + mode.upper() + " " + json.dumps(summary))
    return summary


def verify():
    # Run the new selected regressions against the exact old controller in a
    # disposable checkout. No source file is swapped inside the real checkout.
    old = subprocess.check_output(["git", "show", BASE + ":src/aik_provider/controller.py"], cwd=ROOT)
    assert blob(old) == BASE_CONTROLLER
    with tempfile.TemporaryDirectory(prefix="aik011-old-") as temporary:
        target = Path(temporary)
        shutil.copytree(ROOT / "src/aik_provider", target / "src/aik_provider", ignore=shutil.ignore_patterns("__pycache__"))
        (target / "src/aik_provider/controller.py").write_bytes(old)
        (target / "tests").mkdir()
        for name in ("test_provider_contract.py", "test_provider_lifecycle.py"):
            shutil.copyfile(ROOT / "tests" / name, target / "tests" / name)
        baseline = test_run(target, "baseline")
    assert baseline["tests"] == 8 and baseline["passed"] == 0 and len(baseline["failing_cases"]) == 8
    assert baseline["skipped"] == 0
    current = test_run(ROOT, "current")
    assert current["tests"] == 60 and current["passed"] == 60 and current["successful"]
    assert current["skipped"] == 0 and not current["failing_cases"]
    return {"baseline": baseline, "current": current}


def snapshot(conn):
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    assert all(re.fullmatch(r"[a-z_]+", name) for name in tables)
    return {name: sorted((tuple(r) for r in conn.execute(f'SELECT * FROM "{name}"')), key=repr) for name in tables}


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
    old, new = dict(before["sqlite_sequence"]), dict(after["sqlite_sequence"])
    assert {k: v for k, v in old.items() if k != "task_events"} == {k: v for k, v in new.items() if k != "task_events"}
    assert new["task_events"] > old.get("task_events", 0)


def main():
    if not __debug__:
        raise RuntimeError("Evidence assertions must remain enabled")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    assert DB.is_file()
    with closing(sqlite3.connect(DB.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        before_read = read_task(conn)
        print("AIK011_LEDGER_READ " + json.dumps(before_read, ensure_ascii=False))
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
        "AIK-011 인증·실행 경합 수정: pending 로그인/로그아웃의 원래 connection/revision 점유 유지, "
        "일치하는 완료 관측만 해제, typed UNKNOWN과 중단 재호출 차단, callback 재진입 거절 및 인가 후 계정 재검사. "
        "기존36+신규24=60/60 PASS; 이전 source의 선택 반례8개 모두 실패 재현. "
        f"검증 source {commit}. AIK-010 및 실제 호스트/제품 연동 미완료이므로 blocked 기록."
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
    old = "blocked 4개(AIK-003·004·005·017), todo 84개, ready 0개"
    new = "blocked 5개(AIK-003·004·005·011·017), todo 83개, ready 0개"
    assert text.count(old) == 1
    readme.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
    report = {"task_id": TASK, "source_commit": commit, "base_commit": BASE, "recorded_at": timestamp,
              "github_run_id": os.environ.get("GITHUB_RUN_ID"), "tests": tests,
              "environment": {"python": sys.version, "platform": sys.platform},
              "sources_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES},
              "baseline_controller_git_blob": BASE_CONTROLLER,
              "canonical_ledger_before_git_blob": BASE_DB, "canonical_ledger_after_git_blob": blob(DB.read_bytes()),
              "ledger_delta": {"tasks_changed": [TASK], "evidence_added": 1, "events_added": 1,
                               "other_tasks_unchanged": True, "acceptance_criteria_unchanged": True,
                               "dependencies_unchanged": True, "schema_unchanged": True},
              "counts": final["counts"], "ready_tasks": final["ready_tasks"],
              "task_status": "blocked", "live_host_qualification": "NOT_RUN"}
    (ROOT / REPORT).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("AIK011_LEDGER_VERIFIED " + json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
