"""Schema 1 -> 2 migration and the platform status command of tools/backlog.py.

Each test copies the tool and both schema files into a disposable directory
and builds a small schema 1 fixture there. The checked-in backlog is not used.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-10-12T00:00:00Z"
AT = "2026-10-14T03:00:00Z"
V1_TABLES = (
    "metadata", "sources", "requirements", "milestones", "epics", "tasks", "task_dependencies",
    "task_requirements", "task_sources", "acceptance_criteria", "decisions", "task_decisions",
    "task_events", "task_evidence",
)


def build_v1(database: Path, schema: Path, *, broken: bool = False) -> None:
    with closing(sqlite3.connect(database)) as conn:
        conn.executescript(schema.read_text(encoding="utf-8"))
        conn.executemany("INSERT INTO metadata VALUES (?, ?)", {
            "schema_version": "1", "project": "AI-Kubernetes", "created_at": NOW, "updated_at": NOW,
            "baseline_commit": "a" * 40, "canonical_data": "planning/backlog.sqlite3",
            "review_snapshot": "planning/backlog.sql", "ownership_policy": "fixture",
        }.items())
        conn.execute("INSERT INTO sources VALUES ('S-USER','Fixture','user','','','')")
        conn.execute("INSERT INTO requirements VALUES ('U01','user','Fixture requirement','Fixture description')")
        conn.execute("INSERT INTO milestones VALUES ('M0','Fixture',0,'first_release','Fixture exit')")
        conn.execute("INSERT INTO epics VALUES ('E01','Fixture epic','Fixture description')")
        conn.execute("INSERT INTO decisions VALUES ('DEC01','Fixture decision','confirmed','Do it','Because','S-USER')")
        for number, status in ((1, "done"), (2, "blocked")):
            task_id = f"AIK-{number:03d}"
            conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (task_id, f"Task {number}", "Description", "Deliverable", "Verification", "E01", "M0",
                          "P0", status, "feature", "AIK", number, NOW, NOW))
            conn.execute("INSERT INTO task_requirements VALUES (?, 'U01')", (task_id,))
            conn.execute("INSERT INTO task_sources VALUES (?, 'S-USER')", (task_id,))
            conn.execute("INSERT INTO task_decisions VALUES (?, 'DEC01')", (task_id,))
            conn.executemany("INSERT INTO acceptance_criteria VALUES (?, ?, ?)",
                             [(task_id, 1, "First result"), (task_id, 2, "Second result")])
            conn.execute("INSERT INTO task_events(task_id,event_type,old_status,new_status,note,occurred_at) "
                         "VALUES (?,'created',NULL,'todo','created',?)", (task_id, NOW))
            transitions = (("todo", "in_progress"), ("in_progress", "done")) if status == "done" else (("todo", "blocked"),)
            for old, new in transitions:
                conn.execute("INSERT INTO task_events(task_id,event_type,old_status,new_status,note,occurred_at) "
                             "VALUES (?,'status_change',?,?,'moved',?)", (task_id, old, new, NOW))
        if not broken:
            conn.execute("INSERT INTO task_evidence VALUES ('EV-1','AIK-001','test','ref','Observed',?)", (NOW,))
        conn.execute("INSERT INTO task_dependencies VALUES ('AIK-002','AIK-001')")
        conn.commit()


class BacklogMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="aik-backlog-migrate-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for relative in ("tools/backlog.py", "planning/schema.sql", "planning/schema-v1.sql"):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)
        self.db = self.root / "planning/backlog.sqlite3"
        build_v1(self.db, self.root / "planning/schema-v1.sql")

    def cli(self, *arguments):
        return subprocess.run([sys.executable, str(self.root / "tools/backlog.py"), *map(str, arguments)],
                              cwd=self.root, capture_output=True, text=True, encoding="utf-8", timeout=30)

    def ok(self, *arguments):
        result = self.cli(*arguments)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def rows(self, database: Path, table: str):
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            if table == "metadata":
                return dict(conn.execute("SELECT key, value FROM metadata"))
            names = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
            if "platform_id" in names and table == "task_evidence":
                names.remove("platform_id")
            return conn.execute(f'SELECT {",".join(names)} FROM "{table}" ORDER BY rowid').fetchall()

    def test_v1_rows_are_copied_unchanged(self):
        output = self.root / "migrated.sqlite3"
        self.ok("--db", self.db, "migrate", "--output", output, "--at", AT)
        for table in V1_TABLES:
            if table != "metadata":
                self.assertEqual(self.rows(self.db, table), self.rows(output, table), table)
        before, after = self.rows(self.db, "metadata"), self.rows(output, "metadata")
        self.assertEqual({key for key in before if before[key] != after[key]}, {"schema_version", "updated_at"})
        self.assertEqual((after["schema_version"], after["updated_at"]), ("2", AT))
        with closing(sqlite3.connect(output)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT entity_type, entity_id, old_value, new_value FROM change_log").fetchall(),
                             [("metadata", "schema_version", "1", "2")])
            self.assertEqual(conn.execute("SELECT count(*) FROM task_evidence WHERE platform_id IS NOT NULL").fetchone()[0], 0)
        self.ok("validate", "--db", output)

    def test_migration_is_deterministic_and_idempotent(self):
        first, second = self.root / "first.sqlite3", self.root / "second.sqlite3"
        self.ok("migrate", "--db", self.db, "--output", first, "--at", AT)
        self.ok("migrate", "--db", self.db, "--output", second, "--at", AT)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        before = first.read_bytes()
        result = self.ok("migrate", "--db", first)
        self.assertIn("nothing changed", result.stdout)
        self.assertEqual(first.read_bytes(), before)

    def test_in_place_migration_refreshes_the_sql_snapshot(self):
        self.ok("migrate", "--db", self.db, "--at", AT)
        self.ok("validate", "--db", self.db)
        snapshot = self.db.with_suffix(".sql")
        self.assertIn("PRAGMA user_version = 2;", snapshot.read_text(encoding="utf-8"))
        restored = self.root / "restored.sqlite3"
        self.ok("restore", "--source", snapshot, "--output", restored)
        self.assertEqual(self.rows(self.db, "change_log"), self.rows(restored, "change_log"))
        self.assertEqual(list(self.root.rglob("*.tmp")), [])

    def test_invalid_v1_source_is_refused_without_output(self):
        broken = self.root / "broken.sqlite3"
        build_v1(broken, self.root / "planning/schema-v1.sql", broken=True)
        before = broken.read_bytes()
        output = self.root / "never.sqlite3"
        result = self.cli("migrate", "--db", broken, "--output", output)
        self.assertEqual(result.returncode, 1)
        self.assertIn("done task has no recorded evidence", result.stderr)
        self.assertFalse(output.exists())
        self.assertEqual(broken.read_bytes(), before)
        self.assertEqual(list(self.root.rglob("*.tmp")), [])

    def test_unknown_version_and_existing_output_are_refused(self):
        existing = self.root / "existing.sqlite3"
        existing.write_bytes(b"keep")
        result = self.cli("migrate", "--db", self.db, "--output", existing)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(existing.read_bytes(), b"keep")
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("PRAGMA user_version = 7")
        result = self.cli("migrate", "--db", self.db, "--output", self.root / "other.sqlite3")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Cannot migrate schema user_version 7", result.stderr)

    def test_platform_status_command_records_change_and_event(self):
        self.ok("migrate", "--db", self.db, "--at", AT)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT INTO platforms VALUES ('LINUX-X64','Linux x64','linux','follow_up','')")
            conn.commit()
        result = self.cli("status", "AIK-002", "not_applicable", "--db", self.db, "--note", "no")
        self.assertEqual(result.returncode, 1)
        self.assertIn("only a platform status", result.stderr)
        self.ok("status", "AIK-002", "blocked", "--db", self.db, "--platform", "LINUX-X64", "--note", "VM POSIX")
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT status, note FROM task_platforms").fetchall(), [("blocked", "VM POSIX")])
            self.assertEqual(conn.execute("SELECT status FROM tasks WHERE id='AIK-002'").fetchone()[0], "blocked")
            self.assertEqual(conn.execute(
                "SELECT entity_type, entity_id, old_value, new_value FROM change_log ORDER BY id DESC LIMIT 1").fetchone(),
                ("task_platform", "AIK-002/LINUX-X64", None, "blocked"))
            self.assertEqual(conn.execute(
                "SELECT event_type, old_status, new_status FROM task_events ORDER BY id DESC LIMIT 1").fetchone(),
                ("platform_status", None, None))
        result = self.cli("status", "AIK-002", "blocked", "--db", self.db, "--platform", "LINUX-X64", "--note", "again")
        self.assertEqual(result.returncode, 1)
        result = self.cli("status", "AIK-002", "todo", "--db", self.db, "--platform", "NOPE", "--note", "x")
        self.assertIn("Unknown platform", result.stderr)
        self.ok("validate", "--db", self.db)


if __name__ == "__main__":
    unittest.main()
