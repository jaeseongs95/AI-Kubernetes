#!/usr/bin/env python3
"""Maintain the development backlog with Python's standard library.

Examples (paths may also be supplied with --db):
    python tools/backlog.py validate
    python tools/backlog.py list --ready
    python tools/backlog.py status AIK-001 in_progress --note "작업 시작"
    python tools/backlog.py export
    python tools/backlog.py report
    python tools/backlog.py restore --source planning/backlog.sql --output restored.sqlite3

The SQLite file is canonical. Status updates write an audit event in the same
transaction and refresh its sibling .sql snapshot. With the default database,
they also refresh the Korean report. Starting or completing a task requires
completed prerequisites. Completion additionally requires recorded evidence
and at least two acceptance criteria.
"""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import errno
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "planning" / "backlog.sqlite3"
DEFAULT_REPORT = ROOT / "docs" / "development-backlog.ko.md"
STATUSES = ("todo", "in_progress", "blocked", "done", "cancelled")
REQUIRED_COLUMNS = {
    "metadata": "key value",
    "sources": "id title kind url revision notes",
    "requirements": "id origin title description",
    "milestones": "id name sequence scope exit_criteria",
    "epics": "id name description",
    "tasks": "id title description deliverable verification epic_id milestone_id priority status work_type owner_component sequence created_at updated_at",
    "task_dependencies": "task_id depends_on",
    "task_requirements": "task_id requirement_id",
    "task_sources": "task_id source_id",
    "acceptance_criteria": "task_id ordinal criterion",
    "decisions": "id title status decision rationale source_id",
    "task_decisions": "task_id decision_id",
    "task_events": "id task_id event_type old_status new_status note occurred_at",
    "task_evidence": "id task_id kind reference summary recorded_at",
}
REQUIRED_VIEWS = ("v_backlog", "v_ready_tasks", "v_milestone_summary")
SCHEMA_VERSION = 1
REQUIRED_METADATA = (
    "schema_version", "project", "created_at", "updated_at", "baseline_commit",
    "canonical_data", "review_snapshot", "ownership_policy",
)
NONEMPTY_COLUMNS = {
    "metadata": "key",
    "sources": "id title kind",
    "requirements": "id origin title description",
    "milestones": "id name scope exit_criteria",
    "epics": "id name description",
    "tasks": "id title description deliverable verification epic_id milestone_id priority status work_type owner_component created_at updated_at",
    "task_dependencies": "task_id depends_on",
    "task_requirements": "task_id requirement_id",
    "task_sources": "task_id source_id",
    "acceptance_criteria": "task_id criterion",
    "decisions": "id title status decision rationale source_id",
    "task_decisions": "task_id decision_id",
    "task_events": "task_id event_type note occurred_at",
    "task_evidence": "id task_id kind reference summary recorded_at",
}
ENUM_COLUMNS = {
    ("sources", "kind"): ("user", "repository", "official_doc"),
    ("requirements", "origin"): ("user", "derived"),
    ("milestones", "scope"): ("first_release", "follow_up", "future"),
    ("tasks", "priority"): ("P0", "P1", "P2"),
    ("tasks", "status"): STATUSES,
    ("tasks", "work_type"): ("decision", "feature", "integration", "validation", "documentation", "research"),
    ("decisions", "status"): ("confirmed", "proposed", "open"),
    ("task_events", "old_status"): STATUSES,
    ("task_events", "new_status"): STATUSES,
}
INTEGER_COLUMNS = {
    ("tasks", "sequence"): 0,
    ("milestones", "sequence"): 0,
    ("acceptance_criteria", "ordinal"): 1,
    ("task_events", "id"): 1,
}


def normalized_schema_sql(sql: str) -> str:
    """Ignore SQL layout/case while preserving quoted values and identifiers."""
    result: list[str] = []
    quote: str | None = None
    position = 0
    while position < len(sql):
        char = sql[position]
        if quote is not None:
            result.append(char)
            if char == quote:
                if position + 1 < len(sql) and sql[position + 1] == quote:
                    result.append(sql[position + 1])
                    position += 1
                else:
                    quote = None
        elif char in "\"'`[":
            quote = "]" if char == "[" else char
            result.append(char)
        elif char.isspace():
            if result and result[-1] != " ":
                result.append(" ")
        else:
            result.append(char.lower())
        position += 1
    return "".join(result).strip()


def schema_contract_issues(conn: sqlite3.Connection) -> list[str]:
    """Compare with the trusted versioned schema, including constraints/views."""
    query = (
        "SELECT name, type, sql FROM sqlite_master "
        "WHERE name NOT GLOB 'sqlite_*' ORDER BY name"
    )
    with closing(sqlite3.connect(":memory:")) as expected:
        expected.executescript((ROOT / "planning" / "schema.sql").read_text(encoding="utf-8"))
        if expected.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise BacklogError("The bundled schema.sql has an unsupported schema version.")
        canonical = {row[0]: (row[1], normalized_schema_sql(row[2] or ""))
                     for row in expected.execute(query)}
    actual = {row[0]: (row[1], normalized_schema_sql(row[2] or ""))
              for row in conn.execute(query)}
    issues = []
    for name in sorted(canonical.keys() | actual.keys()):
        if name not in canonical:
            issues.append(f"Unexpected schema object for version {SCHEMA_VERSION}: {name}")
        elif name not in actual:
            issues.append(f"Missing schema object for version {SCHEMA_VERSION}: {name}")
        elif actual[name] != canonical[name]:
            issues.append(f"Schema definition differs from version {SCHEMA_VERSION}: {name}")
    return issues


def value_domain_issues(conn: sqlite3.Connection) -> list[str]:
    """Validate values independently of SQLite's CHECK enforcement setting."""
    issues: list[str] = []
    for table, columns in NONEMPTY_COLUMNS.items():
        for column in columns.split():
            query = (f'SELECT rowid FROM "{table}" WHERE typeof("{column}") <> \'text\' '
                     f'OR length(trim("{column}")) = 0')
            issues.extend(f"{table} rowid={row[0]}: {column} must be nonempty text"
                          for row in conn.execute(query))
    for (table, column), choices in ENUM_COLUMNS.items():
        placeholders = ",".join("?" for _ in choices)
        query = f'SELECT rowid, "{column}" FROM "{table}" WHERE "{column}" NOT IN ({placeholders})'
        issues.extend(f"{table} rowid={row[0]}: invalid {column} {row[1]!r}"
                      for row in conn.execute(query, choices))
    for (table, column), minimum in INTEGER_COLUMNS.items():
        query = (f'SELECT rowid, "{column}" FROM "{table}" '
                 f'WHERE typeof("{column}") <> \'integer\' OR "{column}" < ?')
        issues.extend(f"{table} rowid={row[0]}: {column} must be an integer >= {minimum}"
                      for row in conn.execute(query, (minimum,)))
    return issues


def metadata_issues(conn: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        issues.append(f"Unsupported schema user_version: {version}; expected {SCHEMA_VERSION}")
    metadata = dict(conn.execute("SELECT key, value FROM metadata"))
    for key in REQUIRED_METADATA:
        value = metadata.get(key)
        if not isinstance(value, str) or not value.strip():
            issues.append(f"Missing or empty required metadata: {key}")
    if metadata.get("schema_version") != str(SCHEMA_VERSION):
        issues.append(f"metadata.schema_version must equal supported user_version {SCHEMA_VERSION}")
    if metadata.get("project") != "AI-Kubernetes":
        issues.append("metadata.project must identify AI-Kubernetes")
    return issues


def event_history_issues(conn: sqlite3.Connection) -> list[str]:
    """Check relational audit continuity; this is not a tamper-proof log."""
    issues: list[str] = []
    histories: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute("SELECT * FROM task_events ORDER BY task_id, id"):
        histories.setdefault(row["task_id"], []).append(row)
    for task in conn.execute("SELECT id, status FROM tasks ORDER BY id"):
        task_id = task["id"]
        history = histories.get(task_id, [])
        created = [event for event in history if event["event_type"] == "created"]
        if len(created) != 1:
            issues.append(f"{task_id}: expected one created event, found {len(created)}")
        if not history:
            continue
        first = history[0]
        if (first["event_type"] != "created" or first["old_status"] is not None
                or first["new_status"] != "todo"):
            issues.append(f"{task_id}: history must begin with a created event from null to todo")
        current = "todo"
        for event in history[1:]:
            old, new = event["old_status"], event["new_status"]
            if event["event_type"] == "status_change":
                if old != current or new not in STATUSES or old == new:
                    issues.append(f"{task_id}: event {event['id']} has a broken status transition "
                                  f"({old!r} -> {new!r}; expected old_status {current!r})")
                if new in STATUSES:
                    current = new
            elif (old, new) not in ((None, None), (current, current)):
                issues.append(f"{task_id}: non-status event {event['id']} changes or mismatches state")
        if current != task["status"]:
            issues.append(f"{task_id}: task status {task['status']!r} differs from last event status {current!r}")
    return issues


class BacklogError(Exception):
    """An actionable backlog or filesystem error."""


def connect_database(path: Path, *, writable: bool = False) -> sqlite3.Connection:
    if not path.is_file():
        raise BacklogError(f"Database does not exist: {path}")
    mode = "rw" if writable else "ro"
    conn = sqlite3.connect(
        path.resolve().as_uri() + f"?mode={mode}",
        uri=True,
        isolation_level=None,
        timeout=5.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def dependency_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    """Return a concrete directed cycle without a recursion-depth limit."""
    state: dict[str, int] = {}
    for start in sorted(graph):
        if state.get(start, 0):
            continue
        path = [start]
        positions = {start: 0}
        state[start] = 1
        stack = [(start, iter(sorted(graph[start])))]
        while stack:
            node, children = stack[-1]
            child = next(children, None)
            if child is None:
                stack.pop()
                path.pop()
                positions.pop(node)
                state[node] = 2
                continue
            if state.get(child, 0) == 1:
                return path[positions[child]:] + [child]
            if not state.get(child, 0):
                state[child] = 1
                positions[child] = len(path)
                path.append(child)
                stack.append((child, iter(sorted(graph.get(child, [])))))
    return None


def validate_connection(conn: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    for row in conn.execute("PRAGMA integrity_check"):
        if row[0] != "ok":
            issues.append(f"SQLite integrity: {row[0]}")

    objects = {
        row[0]: row[1]
        for row in conn.execute("SELECT name, type FROM sqlite_master")
    }
    missing = [name for name in REQUIRED_COLUMNS if objects.get(name) != "table"]
    missing += [name for name in REQUIRED_VIEWS if objects.get(name) != "view"]
    if missing:
        return issues + ["Missing schema objects: " + ", ".join(missing)]

    for table, required in REQUIRED_COLUMNS.items():
        # Names come exclusively from the constant schema map above.
        actual = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        absent = sorted(set(required.split()) - actual)
        if absent:
            issues.append(f"{table}: missing columns {', '.join(absent)}")
    if any("missing columns" in issue for issue in issues):
        return issues

    schema_issues = schema_contract_issues(conn)
    if schema_issues:
        return issues + schema_issues
    issues.extend(metadata_issues(conn))
    domain_issues = value_domain_issues(conn)
    if domain_issues:
        return issues + domain_issues

    for row in conn.execute("PRAGMA foreign_key_check"):
        issues.append(
            f"Foreign key violation: table={row[0]}, rowid={row[1]}, parent={row[2]}"
        )

    task_ids = [row[0] for row in conn.execute("SELECT id FROM tasks")]
    if not task_ids:
        issues.append("The backlog contains no tasks.")

    for table in ("tasks", "milestones"):
        for row in conn.execute(
            f'SELECT sequence, count(*) FROM "{table}" '
            "GROUP BY sequence HAVING count(*) > 1"
        ):
            issues.append(f"{table}: duplicate sequence {row[0]} ({row[1]} rows)")

    checks = (
        (
            "SELECT t.id FROM tasks t LEFT JOIN acceptance_criteria a "
            "ON a.task_id = t.id GROUP BY t.id HAVING count(a.ordinal) < 2",
            "fewer than two acceptance criteria",
        ),
        (
            "SELECT t.id FROM tasks t WHERE NOT EXISTS "
            "(SELECT 1 FROM task_requirements r WHERE r.task_id = t.id)",
            "no linked requirement",
        ),
        (
            "SELECT t.id FROM tasks t WHERE NOT EXISTS "
            "(SELECT 1 FROM task_sources s WHERE s.task_id = t.id)",
            "no linked source",
        ),
        (
            "SELECT r.id FROM requirements r WHERE NOT EXISTS "
            "(SELECT 1 FROM task_requirements t WHERE t.requirement_id = r.id)",
            "requirement has no development task",
        ),
        (
            "SELECT t.id FROM tasks t WHERE t.status = 'done' AND NOT EXISTS "
            "(SELECT 1 FROM task_evidence e WHERE e.task_id = t.id)",
            "done task has no recorded evidence",
        ),
    )
    for query, message in checks:
        issues.extend(f"{row[0]}: {message}" for row in conn.execute(query))

    issues.extend(event_history_issues(conn))

    graph = {task_id: [] for task_id in task_ids}
    for task_id, prerequisite in conn.execute(
        "SELECT task_id, depends_on FROM task_dependencies"
    ):
        graph.setdefault(task_id, []).append(prerequisite)
        if task_id == prerequisite:
            issues.append(f"{task_id}: self dependency")
    cycle = dependency_cycle(graph)
    if cycle:
        issues.append("Dependency cycle: " + " -> ".join(cycle))

    dependency_rows = conn.execute(
        "SELECT t.id, p.id, t.status, p.status, tm.sequence, pm.sequence, "
        "tm.scope, pm.scope, tm.id, pm.id "
        "FROM task_dependencies d "
        "JOIN tasks t ON t.id = d.task_id "
        "JOIN tasks p ON p.id = d.depends_on "
        "JOIN milestones tm ON tm.id = t.milestone_id "
        "JOIN milestones pm ON pm.id = p.milestone_id"
    )
    for row in dependency_rows:
        task_id, prerequisite, status, prior_status = row[:4]
        if status in ("in_progress", "done") and prior_status != "done":
            issues.append(
                f"{task_id}: {status} while prerequisite {prerequisite} is {prior_status}"
            )
        if row[5] > row[4]:
            issues.append(
                f"{task_id}: depends on later milestone task {prerequisite} "
                f"({row[8]} -> {row[9]})"
            )
        if row[6] == "first_release" and row[7] != "first_release":
            issues.append(
                f"{task_id}: first release depends on {prerequisite} ({row[7]})"
            )
    return issues


def require_valid(conn: sqlite3.Connection) -> None:
    issues = validate_connection(conn)
    if issues:
        raise BacklogError("Validation failed:\n" + "\n".join(f"- {x}" for x in issues))


def sql_snapshot(conn: sqlite3.Connection) -> str:
    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    return (
        "-- Generated from the canonical AI-Kubernetes SQLite development backlog.\n"
        "-- Edit the SQLite database, then run: python tools/backlog.py export\n"
        "PRAGMA foreign_keys = OFF;\n"
        f"PRAGMA user_version = {user_version};\n"
        + "\n".join(conn.iterdump())
        + "\nPRAGMA foreign_keys = ON;\n"
    )


def markdown_report(conn: sqlite3.Connection, database: Path, destination: Path) -> str:
    """Render every task and its traceability directly from one DB snapshot."""
    def cell(value) -> str:
        return str(value).replace("|", "\\|").replace("\r\n", "\n").replace("\n", "<br>")

    def link(identifier: str) -> str:
        return f"[{identifier}](#{identifier.lower()})"

    def grouped(query: str, key: str) -> dict:
        result: dict = {}
        for row in conn.execute(query):
            result.setdefault(row[key], []).append(row)
        return result

    def references(rows: list, column: str) -> str:
        return ", ".join(link(row[column]) for row in rows) or "없음"

    metadata = dict(conn.execute("SELECT key, value FROM metadata ORDER BY key"))
    tasks = conn.execute("SELECT * FROM v_backlog ORDER BY milestone_sequence, sequence").fetchall()
    milestones = conn.execute("SELECT * FROM milestones ORDER BY sequence").fetchall()
    summaries = {row["milestone_id"]: row for row in conn.execute("SELECT * FROM v_milestone_summary")}
    requirements = conn.execute("SELECT * FROM requirements ORDER BY id").fetchall()
    decisions = conn.execute("SELECT * FROM decisions ORDER BY id").fetchall()
    source_rows = conn.execute("SELECT * FROM sources ORDER BY id").fetchall()
    sources = {row["id"]: row for row in source_rows}
    decision_index = {row["id"]: row for row in decisions}
    dependencies = grouped("SELECT * FROM task_dependencies ORDER BY task_id, depends_on", "task_id")
    requirement_links = grouped("SELECT * FROM task_requirements ORDER BY task_id, requirement_id", "task_id")
    source_links = grouped("SELECT * FROM task_sources ORDER BY task_id, source_id", "task_id")
    decision_links = grouped("SELECT * FROM task_decisions ORDER BY task_id, decision_id", "task_id")
    criteria = grouped("SELECT * FROM acceptance_criteria ORDER BY task_id, ordinal", "task_id")
    evidence = grouped("SELECT * FROM task_evidence ORDER BY task_id, recorded_at, id", "task_id")
    tasks_by_milestone: dict = {}
    for task in tasks:
        tasks_by_milestone.setdefault(task["milestone_id"], []).append(task)

    scope_labels = {"first_release": "첫 출시", "follow_up": "후속 개발", "future": "향후 확장"}
    status_labels = {"todo": "미착수", "in_progress": "진행 중", "blocked": "진행 차단", "done": "완료", "cancelled": "취소"}
    decision_labels = {"confirmed": "확정", "proposed": "제안", "open": "미정"}
    origin_labels = {"user": "사용자 요구", "derived": "설계에서 도출"}
    source_labels = {"user": "사용자 제공", "repository": "저장소 기준선", "official_doc": "공식 문서"}
    work_labels = {"decision": "결정", "feature": "기능 개발", "integration": "통합", "validation": "검증", "documentation": "문서", "research": "조사"}
    try:
        database_link = Path(os.path.relpath(database.resolve(), destination.parent.resolve())).as_posix()
    except ValueError:
        # Different Windows volumes cannot form a relative path.
        database_link = database.resolve().as_posix()
    user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    counts = {status: sum(task["status"] == status for task in tasks) for status in STATUSES}
    lines = [
        "# AI-Kubernetes 개발 태스크 장부",
        "",
        f"> 이 문서는 [정본 SQLite 데이터베이스]({database_link})에서 생성한 Markdown 스냅샷입니다. "
        "태스크·요구사항·결정·출처의 기준은 DB이며, 이 파일의 수동 수정은 다음 생성에서 덮어써집니다.",
        "",
        f"- 작성일 (UTC): `{metadata.get('created_at', '미기록')}`",
        f"- 수정일 (UTC): `{metadata.get('updated_at', '미기록')}`",
        f"- 검토일 (UTC): `{metadata.get('reviewed_at', '미기록')}`",
        f"- SQLite `user_version`: `{user_version}` · 메타데이터 스키마 버전: `{metadata.get('schema_version', '미기록')}`",
        f"- 태스크 {len(tasks)}개 · 요구사항 {len(requirements)}개 · 결정 {len(decisions)}개 · 출처 {len(sources)}개",
        "- 상태: " + " · ".join(f"{status_labels[status]} {counts[status]}개" for status in STATUSES),
        "",
        "## 유지보수 방법",
        "",
        "저장소 루트에서 Python 표준 라이브러리만으로 실행합니다. 상태 변경은 이유와 함께 기록하며, "
        "완료로 바꾸려면 완료 기준과 선행 작업 충족 및 `task_evidence`의 증거가 필요합니다. "
        "계획 등록 자체는 구현 완료의 근거가 아닙니다.",
        "",
        "```bash",
        "python tools/backlog.py validate",
        "python tools/backlog.py list --ready",
        "python tools/backlog.py status AIK-001 in_progress --note \"작업 시작\"",
        "```",
        "",
        "기본 DB에 대한 `status` 성공 시 SQL과 이 보고서도 갱신합니다. SQL로 DB를 직접 수정했거나 "
        "증거를 추가했다면 다음 두 명령으로 검증된 스냅샷을 다시 생성한 뒤 함께 커밋합니다.",
        "",
        "```bash",
        "python tools/backlog.py export",
        "python tools/backlog.py report",
        "```",
        "",
        "`--db`로 다른 DB를 선택할 수 있습니다. `report --output 경로.md`는 출력 경로를 바꿉니다. "
        "`restore --source planning/backlog.sql --output 새경로.sqlite3`는 기존 파일을 덮어쓰지 않고 복원·검증합니다.",
        "",
        "## 단계별 현황",
        "",
        "| 단계 | 범위 | 태스크 | 미착수 | 진행 중 | 차단 | 완료 | 취소 | 착수 가능 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for milestone in milestones:
        summary = summaries[milestone["id"]]
        lines.append(
            f"| {milestone['id']} · {cell(milestone['name'])} | {scope_labels[milestone['scope']]} | "
            + " | ".join(str(summary[key]) for key in (
                "total_tasks", "todo_tasks", "in_progress_tasks", "blocked_tasks", "done_tasks", "cancelled_tasks", "ready_tasks"
            )) + " |"
        )
    lines.extend(["", "착수 가능 수는 DB에 기록된 선행 작업이 모두 완료된 `todo` 태스크를 셉니다.", ""])

    for milestone in milestones:
        lines.extend([
            f"## {milestone['id']} · {milestone['name']}", "",
            f"**범위:** {scope_labels[milestone['scope']]} (`{milestone['scope']}`)", "",
            f"**단계 완료 조건:** {milestone['exit_criteria']}", "",
        ])
        for task in tasks_by_milestone.get(milestone["id"], []):
            identifier = task["id"]
            related_sources = []
            for row in source_links.get(identifier, []):
                source_id = row["source_id"]
                source = sources[source_id]
                related_sources.append(
                    f"[{source_id}]({source['url']}) ([색인](#{source_id.lower()}))"
                    if source["url"] else link(source_id)
                )
            related_decisions = [
                f"{link(row['decision_id'])} ({decision_labels[decision_index[row['decision_id']]['status']]})"
                for row in decision_links.get(identifier, [])
            ]
            lines.extend([
                f"### {identifier}", "", f"**{task['title']}**", "",
                "| 항목 | 내용 |", "|---|---|",
                f"| 상태 | {status_labels[task['status']]} (`{task['status']}`) |",
                f"| 우선순위 | {task['priority']} |",
                f"| 담당 컴포넌트 | {cell(task['owner_component'])} |",
                f"| 작업 유형 | {work_labels[task['work_type']]} (`{task['work_type']}`) |",
                f"| 에픽 | {task['epic_id']} · {cell(task['epic_name'])} |",
                f"| 실행 순서 | {task['sequence']} |",
                f"| 작성 / 수정 | `{task['created_at']}` / `{task['updated_at']}` |", "",
                task["description"], "",
                f"- **산출물:** {task['deliverable']}",
                f"- **검증 방법:** {task['verification']}",
                f"- **선행 작업:** {references(dependencies.get(identifier, []), 'depends_on')}",
                f"- **요구사항:** {references(requirement_links.get(identifier, []), 'requirement_id')}",
                f"- **출처:** {', '.join(related_sources) or '없음'}",
                f"- **관련 결정:** {', '.join(related_decisions) or '없음'}", "",
                "**완료 기준**", "",
            ])
            lines.extend(f"{row['ordinal']}. {row['criterion']}" for row in criteria.get(identifier, []))
            lines.extend(["", "**기록된 증거**", ""])
            if evidence.get(identifier):
                for row in evidence[identifier]:
                    lines.append(
                        f"- `{row['id']}` · {row['kind']} · `{row['reference']}` · "
                        f"{row['summary']} · `{row['recorded_at']}`"
                    )
            else:
                lines.append("기록 없음.")
            lines.append("")

    lines.extend(["## 요구사항 색인", ""])
    for requirement in requirements:
        related = [task["id"] for task in tasks if any(
            row["requirement_id"] == requirement["id"] for row in requirement_links.get(task["id"], [])
        )]
        lines.extend([
            f"### {requirement['id']}", "", f"**{requirement['title']}**", "",
            f"- 구분: {origin_labels[requirement['origin']]} (`{requirement['origin']}`)",
            f"- 개발 태스크: {', '.join(link(identifier) for identifier in related) or '없음'}", "",
            requirement["description"], "",
        ])

    lines.extend(["## 결정 색인", ""])
    for decision in decisions:
        related = [task["id"] for task in tasks if any(
            row["decision_id"] == decision["id"] for row in decision_links.get(task["id"], [])
        )]
        lines.extend([
            f"### {decision['id']}", "", f"**{decision['title']}**", "",
            f"- 상태: {decision_labels[decision['status']]} (`{decision['status']}`)",
            f"- 출처: {link(decision['source_id'])}",
            f"- 관련 태스크: {', '.join(link(identifier) for identifier in related) or '없음'}", "",
            f"**결정 내용:** {decision['decision']}", "",
            f"**이유:** {decision['rationale']}", "",
        ])

    lines.extend(["## 출처 색인", ""])
    for source in source_rows:
        lines.extend([
            f"### {source['id']}", "", f"**{source['title']}**", "",
            f"- 구분: {source_labels[source['kind']]} (`{source['kind']}`)",
            f"- 기준 버전 / 날짜: `{source['revision'] or '미기록'}`",
            f"- 원문: [{source['url']}]({source['url']})" if source["url"] else (
                "- 원문: 사용자 제공 대화" if source["kind"] == "user" else "- 원문: URL 미기록"
            ),
            "", source["notes"] or "추가 메모 없음.", "",
        ])

    lines.extend(["## DB 메타데이터", "", "| 키 | 값 |", "|---|---|"])
    lines.extend(f"| `{cell(key)}` | {cell(value)} |" for key, value in metadata.items())
    lines.append("")
    return "\n".join(lines)


def stage_text(destination: Path, text: str) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


@contextmanager
def maintenance_lock(database: Path, *, timeout: float = 10.0):
    """Serialize this tool's DB changes and snapshot publication across processes.

    The stable lockfile is deliberately retained: unlinking it lets a waiter
    and a new process acquire locks on different files. Symlinks to the DB use
    the same resolved path. Hardlinked DB aliases are rejected because separate
    names cannot share a reliable adjacent lockfile (or SQLite journal path).
    """
    canonical = database.resolve()
    if not canonical.is_file():
        raise BacklogError(f"Database does not exist: {database}")
    if canonical.stat().st_nlink > 1:
        raise BacklogError(
            "Maintenance commands do not support hardlinked database aliases. "
            "Use one canonical database path or an independent file copy."
        )
    lockfile = canonical.with_name(canonical.name + ".maintenance.lock")
    if lockfile.is_symlink():
        raise BacklogError(f"The maintenance lockfile must not be a symlink: {lockfile}")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lockfile, flags, 0o600)
    with os.fdopen(descriptor, "r+b", buffering=0) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or lockfile.is_symlink():
            raise BacklogError(f"The maintenance lockfile must be a regular file without link aliases: {lockfile}")
        if os.name == "nt":
            import msvcrt

            def acquire():
                stream.seek(0)
                # Windows permits byte-range locking beyond EOF, including an
                # initially empty lockfile. Initialize it only after acquiring.
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)

            def release():
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def acquire():
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            def release():
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                acquire()
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BacklogError(
                        f"Timed out waiting for another backlog maintenance operation: {canonical}. "
                        "The database and generated snapshots were not changed."
                    ) from error
                time.sleep(min(0.05, remaining))
        try:
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"\0")
                os.fsync(stream.fileno())
            yield
        finally:
            release()


def snapshot_import_authorizer(action, first, second, _database, _trigger):
    """Accept portable backlog SQL without filesystem or extension side effects."""
    if action in (
        sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH,
        sqlite3.SQLITE_CREATE_VTABLE, sqlite3.SQLITE_DROP_VTABLE,
        sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
    ):
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA:
        # These are the only settings needed by our SQL snapshot and supported
        # WAL imports. In particular, never inherit writable_schema,
        # ignore_check_constraints, temp_store_directory, or journal_mode=OFF.
        name = (first or "").lower()
        if name not in ("foreign_keys", "user_version", "journal_mode"):
            return sqlite3.SQLITE_DENY
        if name == "journal_mode" and second is not None and second.lower() not in ("delete", "wal"):
            return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION:
        if (second or first or "").lower() in ("load_extension", "readfile", "writefile", "fts3_tokenizer"):
            return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def same_path(first: Path, second: Path) -> bool:
    """Recognize existing hard links as well as lexical and symbolic aliases."""
    return first.resolve() == second.resolve() or (
        first.exists() and second.exists() and first.samefile(second)
    )


def default_sql_path(database: Path) -> Path:
    return database.resolve().with_suffix(".sql")


def default_report_path(database: Path) -> Path:
    return DEFAULT_REPORT if same_path(database, DEFAULT_DB) else database.resolve().with_suffix(".md")


def check_snapshot_path(database: Path, snapshot: Path, *, kind: str = "sql") -> None:
    """Keep generated text away from databases, locks and authoritative schema."""
    expected_suffix = {"sql": ".sql", "report": ".md"}[kind]
    protected = [ROOT / "planning" / "schema.sql"]
    for candidate in (database.resolve(), DEFAULT_DB.resolve()):
        protected.append(candidate)
        protected.extend(candidate.with_name(candidate.name + suffix) for suffix in (
            "-wal", "-shm", "-journal", ".maintenance.lock",
        ))
    for candidate in protected:
        if same_path(snapshot, candidate):
            raise BacklogError(f"The generated output must not overwrite a database, sidecar, lock, or schema: {snapshot}")
    if not same_path(database, DEFAULT_DB):
        for canonical_output in (default_sql_path(DEFAULT_DB), DEFAULT_REPORT):
            if same_path(snapshot, canonical_output):
                raise BacklogError(
                    "A custom database must not overwrite the repository's canonical generated outputs. "
                    "Use a sibling output or a separate --output path."
                )
    if snapshot.suffix.lower() != expected_suffix:
        raise BacklogError(f"The {kind} output must use a {expected_suffix} extension: {snapshot}")
    if snapshot.is_file():
        with snapshot.open("rb") as stream:
            if stream.read(16) == b"SQLite format 3\0":
                raise BacklogError(f"Refusing to overwrite a SQLite database with generated text: {snapshot}")


def command_validate(args: argparse.Namespace) -> None:
    with closing(connect_database(args.db)) as conn:
        conn.execute("BEGIN")
        require_valid(conn)
        counts = {
            table: conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            for table in ("tasks", "requirements", "task_dependencies", "task_evidence")
        }
        ready = conn.execute("SELECT count(*) FROM v_ready_tasks").fetchone()[0]
    print("VALID: " + ", ".join(f"{name}={count}" for name, count in counts.items()))
    print(f"Ready tasks: {ready}")


def command_export(args: argparse.Namespace) -> None:
    destination = args.output or default_sql_path(args.db)
    check_snapshot_path(args.db, destination)
    temporary = None
    try:
        with maintenance_lock(args.db), closing(connect_database(args.db)) as conn:
            conn.execute("BEGIN")
            require_valid(conn)
            temporary = stage_text(destination, sql_snapshot(conn))
            os.replace(temporary, destination)
            temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f"Exported SQL snapshot: {destination}")


def command_report(args: argparse.Namespace) -> None:
    destination = args.output or default_report_path(args.db)
    check_snapshot_path(args.db, destination, kind="report")
    temporary = None
    try:
        with maintenance_lock(args.db), closing(connect_database(args.db)) as conn:
            conn.execute("BEGIN")
            require_valid(conn)
            temporary = stage_text(destination, markdown_report(conn, args.db, destination))
            os.replace(temporary, destination)
            temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f"Generated Korean development report: {destination}")


def command_restore(args: argparse.Namespace) -> None:
    source, destination = args.source, args.output
    if destination.exists() or destination.is_symlink():
        raise BacklogError(f"Refusing to overwrite existing restore destination: {destination}")
    if not source.is_file():
        raise BacklogError(f"SQL snapshot does not exist: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(name)
    created_destination = False
    try:
        with closing(sqlite3.connect(temporary, isolation_level=None)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA trusted_schema = OFF")
            conn.set_authorizer(snapshot_import_authorizer)
            conn.executescript(source.read_text(encoding="utf-8"))
            if conn.in_transaction:
                conn.commit()
            conn.set_authorizer(None)
            conn.execute("PRAGMA ignore_check_constraints = OFF")
            conn.execute("PRAGMA foreign_keys = ON")
            require_valid(conn)
            # Produce one portable database file, including after a WAL import.
            conn.execute("PRAGMA journal_mode = DELETE")
        # Exclusive creation also refuses an output created during validation.
        # This works on Windows filesystems that do not support hard links.
        with destination.open("xb") as output:
            created_destination = True
            with temporary.open("rb") as input_stream:
                shutil.copyfileobj(input_stream, output)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        if created_destination:
            destination.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Restored and validated database: {destination}")


def command_list(args: argparse.Namespace) -> None:
    view = "v_ready_tasks" if args.ready else "v_backlog"
    with closing(connect_database(args.db)) as conn:
        conn.execute("BEGIN")
        require_valid(conn)
        if args.milestone and not conn.execute(
            "SELECT 1 FROM milestones WHERE id = ?", (args.milestone,)
        ).fetchone():
            raise BacklogError(f"Unknown milestone: {args.milestone}")
        query = f"SELECT * FROM {view}"
        parameters = ()
        if args.milestone:
            query += " WHERE milestone_id = ?"
            parameters = (args.milestone,)
        rows = conn.execute(query + " ORDER BY milestone_sequence, sequence", parameters).fetchall()
    print("ID\tPRIORITY\tSTATUS\tMILESTONE\tOPEN_DEPS\tTITLE")
    for row in rows:
        values = (
            row["id"], row["priority"], row["status"], row["milestone_id"],
            str(row["open_dependency_count"]), row["title"],
        )
        print("\t".join(value.replace("\t", " ").replace("\n", " ") for value in values))
    print(f"{len(rows)} task(s)")


def command_status(args: argparse.Namespace) -> None:
    if not args.note.strip():
        raise BacklogError("A nonempty --note is required for a status change.")
    snapshot = args.snapshot or default_sql_path(args.db)
    check_snapshot_path(args.db, snapshot)
    temporary = None
    report_temporary = None
    report_path = DEFAULT_REPORT if args.db.resolve() == DEFAULT_DB.resolve() else None
    if report_path is not None:
        check_snapshot_path(args.db, report_path, kind="report")
    if report_path is not None and snapshot.resolve() == report_path.resolve():
        raise BacklogError("The SQL snapshot and Markdown report paths must differ.")
    committed = False
    previous_status = None
    try:
        with maintenance_lock(args.db), closing(connect_database(args.db, writable=True)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute("SELECT status FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
            if task is None:
                raise BacklogError(f"Unknown task: {args.task_id}")
            previous_status = task[0]
            if previous_status == args.new_status:
                raise BacklogError(f"{args.task_id} is already {args.new_status}; nothing changed.")
            now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (args.new_status, now, args.task_id),
            )
            conn.execute("UPDATE metadata SET value = ? WHERE key = 'updated_at'", (now,))
            conn.execute(
                "INSERT INTO task_events "
                "(task_id, event_type, old_status, new_status, note, occurred_at) "
                "VALUES (?, 'status_change', ?, ?, ?, ?)",
                (args.task_id, previous_status, args.new_status, args.note.strip(), now),
            )
            # Checks the proposed state before either the status or audit event commits.
            require_valid(conn)
            temporary = stage_text(snapshot, sql_snapshot(conn))
            if report_path is not None:
                report_temporary = stage_text(report_path, markdown_report(conn, args.db, report_path))
            conn.commit()
            committed = True
            try:
                os.replace(temporary, snapshot)
                temporary = None
                if report_temporary is not None:
                    os.replace(report_temporary, report_path)
                    report_temporary = None
            except OSError as error:
                raise BacklogError(
                    f"Status and audit event were committed, but a generated snapshot refresh failed: {error}. "
                    f"Run export --db {args.db} --output {snapshot}"
                    + (f" and report --db {args.db} --output {report_path}." if report_path is not None else ".")
                ) from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if report_temporary is not None:
            report_temporary.unlink(missing_ok=True)
    if committed:
        print(f"{args.task_id}: {previous_status} -> {args.new_status}")
        print(f"Updated SQL snapshot: {snapshot}")
        if report_path is not None:
            print(f"Updated development report: {report_path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def command(name: str, help_text: str, function):
        subparser = subparsers.add_parser(name, help=help_text)
        subparser.add_argument("--db", type=Path, default=argparse.SUPPRESS, help="SQLite database path")
        subparser.set_defaults(handler=function)
        return subparser

    command("validate", "Check integrity, coverage, dependencies, and completion evidence", command_validate)
    export = command("export", "Atomically export a reviewable SQL snapshot", command_export)
    export.add_argument("--output", type=Path, help="SQL output; defaults to the database's sibling .sql")
    report = command("report", "Generate the complete Korean development report from SQLite", command_report)
    report.add_argument("--output", type=Path, help=".md output; canonical DB uses docs/development-backlog.ko.md, custom DB uses its sibling .md")
    restore = command("restore", "Restore and validate SQL into a new database", command_restore)
    restore.add_argument("--source", type=Path, required=True)
    restore.add_argument("--output", type=Path, required=True)
    listing = command("list", "List tasks in milestone and execution order", command_list)
    listing.add_argument("--ready", action="store_true", help="Only todo tasks whose prerequisites are done")
    listing.add_argument("--milestone", help="Filter by milestone ID")
    status = command("status", "Change status, record an event, and refresh snapshots", command_status)
    status.add_argument("task_id")
    status.add_argument("new_status", choices=STATUSES)
    status.add_argument("--note", required=True, help="Reason or completion explanation")
    status.add_argument("--snapshot", type=Path, help="SQL output; defaults to the database's sibling .sql")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        args.handler(args)
    except (BacklogError, sqlite3.Error, OSError, UnicodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
