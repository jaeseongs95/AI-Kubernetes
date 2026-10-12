"""Exercise output-path mistakes against disposable repository copies."""
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BacklogPathTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="aik-output-paths-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "project"
        for name in ("tools/backlog.py", "planning/schema.sql", "planning/backlog.sqlite3"):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        self.tool = self.root / "tools/backlog.py"
        self.database = self.root / "planning/backlog.sqlite3"
        self.report = self.root / "docs/development-backlog.ko.md"
        self.report.parent.mkdir(parents=True)
        self.report.write_text("Canonical report sentinel\n", encoding="utf-8")
        self.snapshot = self.database.with_suffix(".sql")
        self.snapshot.write_text("Canonical SQL sentinel\n", encoding="utf-8")
        self.custom = Path(self.directory.name) / "separate.sqlite3"
        shutil.copyfile(self.database, self.custom)
        with sqlite3.connect(self.custom) as conn:
            conn.execute("UPDATE tasks SET title='CUSTOM COPY MARKER' WHERE id='AIK-001'")

    def run_tool(self, *arguments):
        return subprocess.run([sys.executable, str(self.tool), *map(str, arguments)],
                              cwd=self.root, text=True, capture_output=True, timeout=20)

    def assert_refused_without_changes(self, database, target, *arguments):
        before_db = database.read_bytes()
        before_target = target.read_bytes() if target.exists() else None
        result = self.run_tool("--db", database, *arguments)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("ERROR:", result.stderr)
        self.assertEqual(database.read_bytes(), before_db)
        self.assertEqual(target.read_bytes() if target.exists() else None, before_target)

    def test_custom_report_defaults_to_custom_database_sibling(self):
        before = self.report.read_bytes()
        result = self.run_tool("--db", self.custom, "report")
        self.assertEqual(result.returncode, 0, result.stderr)
        generated = self.custom.with_suffix(".md").read_text(encoding="utf-8")
        self.assertIn("CUSTOM COPY MARKER", generated)
        self.assertIn("(separate.sqlite3)", generated)
        self.assertEqual(self.report.read_bytes(), before)

    def test_custom_report_can_use_separate_explicit_output(self):
        target = Path(self.directory.name) / "review.md"
        result = self.run_tool("report", "--db", self.custom, "--output", target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CUSTOM COPY MARKER", target.read_text(encoding="utf-8"))

    def test_custom_database_cannot_overwrite_canonical_generated_outputs(self):
        for command, target in (("report", self.report), ("export", self.snapshot)):
            with self.subTest(command=command):
                self.assert_refused_without_changes(self.custom, target, command, "--output", target)

    def test_output_cannot_replace_database_schema_or_lock(self):
        lock = self.database.with_name(self.database.name + ".maintenance.lock")
        lock.write_bytes(b"lock sentinel")
        for command, target in (
            ("report", self.database),
            ("export", self.root / "planning/schema.sql"),
            ("export", lock),
            ("report", self.snapshot),
            ("export", self.tool),
        ):
            with self.subTest(command=command, target=target.name):
                self.assert_refused_without_changes(self.database, target, command, "--output", target)

    def test_status_rejects_reserved_snapshot_before_changing_task(self):
        target = self.database.with_name(self.database.name + ".maintenance.lock")
        self.assert_refused_without_changes(
            self.database, target, "status", "AIK-001", "in_progress", "--note", "fixture",
            "--snapshot", target,
        )

    def test_output_cannot_overwrite_another_sqlite_file_with_text_extension(self):
        target = Path(self.directory.name) / "other.md"
        shutil.copyfile(self.custom, target)
        self.assert_refused_without_changes(self.database, target, "report", "--output", target)

    def test_hardlinked_database_output_is_detected(self):
        target = Path(self.directory.name) / "alias.md"
        try:
            target.hardlink_to(self.database)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"Hard links unavailable: {error}")
        self.assert_refused_without_changes(self.database, target, "report", "--output", target)

    def test_symlink_database_defaults_to_canonical_sql_output(self):
        alias = Path(self.directory.name) / "alias.sqlite3"
        try:
            alias.symlink_to(self.database)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"Symbolic links unavailable: {error}")
        result = self.run_tool("--db", alias, "export")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CREATE TABLE tasks", self.snapshot.read_text(encoding="utf-8"))
        self.assertFalse(alias.with_suffix(".sql").exists())

    def test_list_rejects_a_ready_view_that_lies(self):
        with sqlite3.connect(self.custom) as conn:
            conn.executescript("DROP VIEW v_ready_tasks; CREATE VIEW v_ready_tasks AS SELECT * FROM v_backlog WHERE status='todo';")
        result = self.run_tool("--db", self.custom, "list", "--ready")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Schema definition differs", result.stderr)
        self.assertNotIn("82 task(s)", result.stdout)


if __name__ == "__main__":
    unittest.main()
