-- AI-Kubernetes development backlog, schema version 1.
-- Canonical data: planning/backlog.sqlite3; review snapshot: planning/backlog.sql.
PRAGMA foreign_keys = ON;
PRAGMA user_version = 1;

CREATE TABLE metadata (
    key TEXT PRIMARY KEY NOT NULL CHECK (length(trim(key)) > 0),
    value TEXT NOT NULL
);

CREATE TABLE sources (
    id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(id)) > 0),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    kind TEXT NOT NULL CHECK (kind IN ('user', 'repository', 'official_doc')),
    url TEXT NOT NULL DEFAULT '',
    revision TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE requirements (
    id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(id)) > 0),
    origin TEXT NOT NULL CHECK (origin IN ('user', 'derived')),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    description TEXT NOT NULL CHECK (length(trim(description)) > 0)
);

CREATE TABLE milestones (
    id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(id)) > 0),
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    sequence INTEGER NOT NULL UNIQUE CHECK (sequence >= 0),
    scope TEXT NOT NULL CHECK (scope IN ('first_release', 'follow_up', 'future')),
    exit_criteria TEXT NOT NULL CHECK (length(trim(exit_criteria)) > 0)
);

CREATE TABLE epics (
    id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(id)) > 0),
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    description TEXT NOT NULL CHECK (length(trim(description)) > 0)
);

CREATE TABLE tasks (
    id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(id)) > 0),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    description TEXT NOT NULL CHECK (length(trim(description)) > 0),
    deliverable TEXT NOT NULL CHECK (length(trim(deliverable)) > 0),
    verification TEXT NOT NULL CHECK (length(trim(verification)) > 0),
    epic_id TEXT NOT NULL REFERENCES epics(id) ON UPDATE CASCADE,
    milestone_id TEXT NOT NULL REFERENCES milestones(id) ON UPDATE CASCADE,
    priority TEXT NOT NULL CHECK (priority IN ('P0', 'P1', 'P2')),
    status TEXT NOT NULL CHECK (status IN ('todo', 'in_progress', 'blocked', 'done', 'cancelled')),
    work_type TEXT NOT NULL CHECK (work_type IN ('decision', 'feature', 'integration', 'validation', 'documentation', 'research')),
    owner_component TEXT NOT NULL CHECK (length(trim(owner_component)) > 0),
    sequence INTEGER NOT NULL UNIQUE CHECK (sequence >= 0),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0)
);

CREATE TABLE task_dependencies (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON UPDATE CASCADE ON DELETE CASCADE,
    depends_on TEXT NOT NULL REFERENCES tasks(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    PRIMARY KEY (task_id, depends_on),
    CHECK (task_id <> depends_on)
);

CREATE TABLE task_requirements (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON UPDATE CASCADE ON DELETE CASCADE,
    requirement_id TEXT NOT NULL REFERENCES requirements(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    PRIMARY KEY (task_id, requirement_id)
);

CREATE TABLE task_sources (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON UPDATE CASCADE ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES sources(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    PRIMARY KEY (task_id, source_id)
);

CREATE TABLE acceptance_criteria (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON UPDATE CASCADE ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    criterion TEXT NOT NULL CHECK (length(trim(criterion)) > 0),
    PRIMARY KEY (task_id, ordinal)
);

CREATE TABLE decisions (
    id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(id)) > 0),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    status TEXT NOT NULL CHECK (status IN ('confirmed', 'proposed', 'open')),
    decision TEXT NOT NULL CHECK (length(trim(decision)) > 0),
    rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0),
    source_id TEXT NOT NULL REFERENCES sources(id) ON UPDATE CASCADE
);

CREATE TABLE task_decisions (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON UPDATE CASCADE ON DELETE CASCADE,
    decision_id TEXT NOT NULL REFERENCES decisions(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    PRIMARY KEY (task_id, decision_id)
);

CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    event_type TEXT NOT NULL CHECK (length(trim(event_type)) > 0),
    old_status TEXT CHECK (old_status IN ('todo', 'in_progress', 'blocked', 'done', 'cancelled')),
    new_status TEXT CHECK (new_status IN ('todo', 'in_progress', 'blocked', 'done', 'cancelled')),
    note TEXT NOT NULL CHECK (length(trim(note)) > 0),
    occurred_at TEXT NOT NULL CHECK (length(trim(occurred_at)) > 0)
);

CREATE TABLE task_evidence (
    id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(id)) > 0),
    task_id TEXT NOT NULL REFERENCES tasks(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    kind TEXT NOT NULL CHECK (length(trim(kind)) > 0),
    reference TEXT NOT NULL CHECK (length(trim(reference)) > 0),
    summary TEXT NOT NULL CHECK (length(trim(summary)) > 0),
    recorded_at TEXT NOT NULL CHECK (length(trim(recorded_at)) > 0)
);

CREATE INDEX ix_tasks_milestone_status ON tasks(milestone_id, status, sequence);
CREATE INDEX ix_tasks_epic ON tasks(epic_id);
CREATE INDEX ix_dependencies_prerequisite ON task_dependencies(depends_on);
CREATE INDEX ix_requirement_tasks ON task_requirements(requirement_id);
CREATE INDEX ix_source_tasks ON task_sources(source_id);
CREATE INDEX ix_decision_tasks ON task_decisions(decision_id);
CREATE INDEX ix_events_task ON task_events(task_id, id);
CREATE INDEX ix_evidence_task ON task_evidence(task_id);

CREATE VIEW v_backlog AS
SELECT t.*,
       e.name AS epic_name,
       m.name AS milestone_name,
       m.scope AS milestone_scope,
       m.sequence AS milestone_sequence,
       (SELECT count(*)
        FROM task_dependencies d JOIN tasks p ON p.id = d.depends_on
        WHERE d.task_id = t.id AND p.status <> 'done') AS open_dependency_count,
       (SELECT count(*) FROM acceptance_criteria a
        WHERE a.task_id = t.id) AS acceptance_criterion_count,
       (SELECT count(*) FROM task_evidence x
        WHERE x.task_id = t.id) AS evidence_count
FROM tasks t
JOIN epics e ON e.id = t.epic_id
JOIN milestones m ON m.id = t.milestone_id;

CREATE VIEW v_ready_tasks AS
SELECT * FROM v_backlog
WHERE status = 'todo' AND open_dependency_count = 0;

CREATE VIEW v_milestone_summary AS
SELECT m.id AS milestone_id, m.name, m.sequence, m.scope,
       count(t.id) AS total_tasks,
       sum(CASE WHEN t.status = 'todo' THEN 1 ELSE 0 END) AS todo_tasks,
       sum(CASE WHEN t.status = 'in_progress' THEN 1 ELSE 0 END) AS in_progress_tasks,
       sum(CASE WHEN t.status = 'blocked' THEN 1 ELSE 0 END) AS blocked_tasks,
       sum(CASE WHEN t.status = 'done' THEN 1 ELSE 0 END) AS done_tasks,
       sum(CASE WHEN t.status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled_tasks,
       sum(CASE WHEN t.status = 'todo' AND t.open_dependency_count = 0
                THEN 1 ELSE 0 END) AS ready_tasks
FROM milestones m
LEFT JOIN v_backlog t ON t.milestone_id = m.id
GROUP BY m.id, m.name, m.sequence, m.scope;
