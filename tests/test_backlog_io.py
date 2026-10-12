"""Regression tests for backlog I/O safety, using only isolated DB fixtures.

Run with: python -m unittest discover -s tests -p 'test_backlog_io.py' -v
The process harness calls the real CLI and only controls scheduling/failures.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
HARNESS = r'''
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import sys
import time

root, settings, arguments = Path(sys.argv[1]), json.loads(sys.argv[2]), sys.argv[3:]
spec = importlib.util.spec_from_file_location('backlog_fixture', root / 'tools/backlog.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

original_lock = module.maintenance_lock
@contextmanager
def traced_lock(database, **options):
    if settings.get('attempt'):
        Path(settings['attempt']).write_text('attempting')
    if 'timeout' in settings:
        options['timeout'] = settings['timeout']
    with original_lock(database, **options):
        if settings.get('acquired'):
            Path(settings['acquired']).write_text('acquired')
        yield
module.maintenance_lock = traced_lock

original_replace = module.os.replace
paused = False
def controlled_replace(source, destination):
    global paused
    if settings.get('fail_replace'):
        raise PermissionError('simulated publication sharing violation')
    if settings.get('pause') and not paused:
        paused = True
        Path(settings['pause']).write_text('reached publication')
        deadline = time.monotonic() + 15
        while not Path(settings['release']).exists():
            if time.monotonic() >= deadline:
                raise RuntimeError('test did not release publication gate')
            time.sleep(0.01)
    return original_replace(source, destination)
module.os.replace = controlled_replace

if settings.get('fail_stage'):
    def failed_stage(*arguments):
        raise PermissionError('simulated staging write failure')
    module.stage_text = failed_stage

sys.argv = [str(root / 'tools/backlog.py'), *arguments]
raise SystemExit(module.main())
'''


class BacklogIOTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='aik-backlog-io-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for relative in ('tools/backlog.py', 'planning/schema.sql'):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)
        self.db = self.root / 'planning/backlog.sqlite3'
        # Two independent tasks keep this I/O test independent of the production
        # backlog's changing statuses, task count, and product dependencies.
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript((self.root / 'planning/schema.sql').read_text(encoding='utf-8'))
            metadata = {
                'schema_version': '1', 'project': 'AI-Kubernetes',
                'created_at': '2026-10-12T00:00:00Z', 'updated_at': '2026-10-12T00:00:00Z',
                'baseline_commit': 'isolated-test-fixture',
                'canonical_data': 'planning/backlog.sqlite3', 'review_snapshot': 'planning/backlog.sql',
                'ownership_policy': 'I/O test fixture only',
            }
            conn.executemany('INSERT INTO metadata(key,value) VALUES (?,?)', metadata.items())
            conn.execute("INSERT INTO sources(id,title,kind) VALUES ('S1','Fixture source','user')")
            conn.execute("INSERT INTO requirements VALUES ('U1','user','Fixture requirement','Test the ledger')")
            conn.execute("INSERT INTO milestones VALUES ('M0','Fixture milestone',0,'first_release','Fixture complete')")
            conn.execute("INSERT INTO epics VALUES ('E0','Fixture epic','Isolated maintenance checks')")
            for sequence, task_id in enumerate(('AIK-001', 'AIK-002')):
                conn.execute(
                    'INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (task_id, task_id, 'Fixture task', 'Fixture result', 'Fixture check', 'E0', 'M0',
                     'P0', 'todo', 'feature', 'fixture', sequence,
                     metadata['created_at'], metadata['updated_at']),
                )
                conn.execute('INSERT INTO task_requirements VALUES (?,?)', (task_id, 'U1'))
                conn.execute('INSERT INTO task_sources VALUES (?,?)', (task_id, 'S1'))
                conn.executemany('INSERT INTO acceptance_criteria VALUES (?,?,?)',
                                 [(task_id, 1, 'Result exists'), (task_id, 2, 'Result verified')])
                conn.execute(
                    'INSERT INTO task_events(task_id,event_type,old_status,new_status,note,occurred_at) '
                    'VALUES (?,\'created\',NULL,\'todo\',\'Fixture created\',?)',
                    (task_id, metadata['created_at']),
                )
            conn.commit()
        self.sql = self.db.with_suffix('.sql')
        self.report = self.root / 'docs/development-backlog.ko.md'
        self.assert_command_ok('export')
        self.assert_command_ok('report')

    def cli(self, *arguments, settings=None):
        if settings is None:
            command = [sys.executable, str(self.root / 'tools/backlog.py'), *map(str, arguments)]
        else:
            command = [sys.executable, '-c', HARNESS, str(self.root), json.dumps(settings), *map(str, arguments)]
        return subprocess.run(command, cwd=self.root, capture_output=True, text=True,
                              encoding='utf-8', timeout=20)

    def assert_command_ok(self, *arguments, settings=None):
        result = self.cli(*arguments, settings=settings)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def spawn(self, *arguments, settings):
        process = subprocess.Popen(
            [sys.executable, '-c', HARNESS, str(self.root), json.dumps(settings), *map(str, arguments)],
            cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8',
        )
        self.addCleanup(self.stop_process, process)
        return process

    @staticmethod
    def stop_process(process):
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=5)

    def wait_marker(self, marker, process):
        deadline = time.monotonic() + 10
        while not marker.exists():
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail(f'Process exited before {marker.name}: {stdout}{stderr}')
            if time.monotonic() >= deadline:
                self.fail(f'Timed out waiting for {marker.name}')
            time.sleep(0.01)

    def file_hashes(self):
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (self.db, self.sql, self.report)}

    def data(self, query, *, database=None):
        with closing(sqlite3.connect(database or self.db)) as conn:
            return conn.execute(query).fetchall()

    def load_module(self):
        spec = importlib.util.spec_from_file_location('backlog_io_fixture', self.root / 'tools/backlog.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def assert_snapshots_equal_database(self):
        with closing(sqlite3.connect(':memory:')) as snapshot:
            snapshot.executescript(self.sql.read_text(encoding='utf-8'))
            self.assertEqual(
                self.data('SELECT id,status FROM tasks ORDER BY id'),
                snapshot.execute('SELECT id,status FROM tasks ORDER BY id').fetchall(),
            )
            self.assertEqual(self.data('SELECT * FROM task_events ORDER BY id'),
                             snapshot.execute('SELECT * FROM task_events ORDER BY id').fetchall())
        prior = self.report.read_bytes()
        self.assert_command_ok('report')
        self.assertEqual(prior, self.report.read_bytes())

    def overlap_with_status(self, first_arguments, *, second_db=None):
        paused, release = self.root / 'paused', self.root / 'release'
        attempted, acquired = self.root / 'attempted', self.root / 'acquired'
        first = self.spawn(*first_arguments, settings={'pause': str(paused), 'release': str(release)})
        self.wait_marker(paused, first)
        second_arguments = ['status', 'AIK-002', 'in_progress', '--note', 'concurrent process B']
        if second_db is not None:
            second_arguments.extend(['--db', second_db, '--snapshot', self.sql])
        second = self.spawn(*second_arguments, settings={'attempt': str(attempted), 'acquired': str(acquired)})
        self.wait_marker(attempted, second)
        try:
            # The second CLI has reached its actual lock acquisition. It must
            # remain blocked while the first process is publishing snapshots.
            with self.assertRaises(subprocess.TimeoutExpired):
                second.wait(timeout=0.2)
            self.assertFalse(acquired.exists())
            self.assertEqual(self.data("SELECT status FROM tasks WHERE id='AIK-002'"), [('todo',)])
        finally:
            release.write_text('go')
        for process in (first, second):
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertTrue(acquired.exists())
        self.assertEqual(self.data("SELECT status FROM tasks WHERE id='AIK-002'"), [('in_progress',)])
        self.assert_snapshots_equal_database()

    def test_two_status_processes_publish_latest_state(self):
        self.overlap_with_status(['status', 'AIK-001', 'in_progress', '--note', 'concurrent process A'])

    def test_wal_export_cannot_publish_over_newer_status(self):
        self.assertEqual(self.data('PRAGMA journal_mode=WAL'), [('wal',)])
        self.overlap_with_status(['export'])

    def test_wal_report_cannot_publish_over_newer_status(self):
        self.assertEqual(self.data('PRAGMA journal_mode=WAL'), [('wal',)])
        self.overlap_with_status(['report'])

    def test_database_symlink_shares_lock(self):
        alias = self.root / 'alias.sqlite3'
        try:
            alias.symlink_to(self.db)
        except OSError as error:
            self.skipTest(f'Database symlinks unavailable: {error}')
        # Keep the SQL output canonical while testing the DB alias itself.
        self.overlap_with_status(['status', 'AIK-001', 'in_progress', '--note', 'process A'], second_db=alias)

    def test_lock_timeout_preserves_database_and_snapshots(self):
        module = self.load_module()
        before = self.file_hashes()
        with module.maintenance_lock(self.db):
            result = self.cli('status', 'AIK-001', 'in_progress', '--note', 'must time out', settings={'timeout': 0.1})
        self.assertEqual(result.returncode, 1)
        self.assertIn('Timed out waiting', result.stderr)
        self.assertEqual(before, self.file_hashes())
        self.assertTrue(self.db.with_name(self.db.name + '.maintenance.lock').is_file())
        self.assert_command_ok('status', 'AIK-001', 'in_progress', '--note', 'lock released')

    def test_staging_failure_rolls_back_and_releases_lock(self):
        before = self.file_hashes()
        result = self.cli('status', 'AIK-001', 'in_progress', '--note', 'stage failure', settings={'fail_stage': True})
        self.assertEqual(result.returncode, 1)
        self.assertIn('staging write failure', result.stderr)
        self.assertEqual(before, self.file_hashes())
        self.assert_command_ok('status', 'AIK-001', 'in_progress', '--note', 'retry after rollback')

    def test_publication_failure_discloses_commit_and_releases_lock(self):
        result = self.cli('status', 'AIK-001', 'in_progress', '--note', 'replace failure', settings={'fail_replace': True})
        self.assertEqual(result.returncode, 1)
        self.assertIn('Status and audit event were committed', result.stderr)
        self.assertEqual(self.data("SELECT status FROM tasks WHERE id='AIK-001'"), [('in_progress',)])
        self.assert_command_ok('export')
        self.assert_command_ok('report')
        self.assert_snapshots_equal_database()
        self.assertEqual(list(self.root.rglob('*.tmp')), [])

    def test_hardlinked_database_alias_is_rejected_before_changes(self):
        alias = self.root / 'alias.sqlite3'
        try:
            os.link(self.db, alias)
        except OSError as error:
            self.skipTest(f'Hardlinks unavailable: {error}')
        before = self.file_hashes()
        result = self.cli('status', 'AIK-001', 'in_progress', '--note', 'unsupported hardlink')
        self.assertEqual(result.returncode, 1)
        self.assertIn('hardlinked database aliases', result.stderr)
        self.assertEqual(before, self.file_hashes())

    def restore_source(self, extra, *, prefix=''):
        source = self.root / 'input.sql'
        source.write_text(prefix + self.sql.read_text(encoding='utf-8') + '\n' + extra, encoding='utf-8')
        output = self.root / 'restored.sqlite3'
        return source, output, self.cli('restore', '--source', source, '--output', output)

    def test_restore_blocks_check_constraint_bypass(self):
        before = self.file_hashes()
        _, output, result = self.restore_source(
            "PRAGMA ignore_check_constraints=ON; UPDATE tasks SET status='invalid-state' WHERE id='AIK-001';")
        self.assertEqual(result.returncode, 1)
        self.assertIn('not authorized', result.stderr)
        self.assertFalse(output.exists())
        self.assertEqual(before, self.file_hashes())

    def test_restore_blocks_dangerous_settings_and_extensions(self):
        for statement in ('PRAGMA writable_schema=ON;', 'PRAGMA journal_mode=OFF;',
                          'PRAGMA trusted_schema=ON;', "SELECT load_extension('never-load');"):
            with self.subTest(statement=statement):
                _, output, result = self.restore_source(statement)
                self.assertEqual(result.returncode, 1)
                self.assertFalse(output.exists())

    def test_restore_cannot_modify_an_external_database(self):
        external = self.root / 'external.sqlite3'
        with closing(sqlite3.connect(external)) as conn:
            conn.execute('CREATE TABLE sentinel(value)')
            conn.execute('INSERT INTO sentinel VALUES (17)')
            conn.commit()
        before = hashlib.sha256(external.read_bytes()).hexdigest()
        quoted = str(external).replace("'", "''")
        _, output, result = self.restore_source(f"ATTACH DATABASE '{quoted}' AS outside; DELETE FROM outside.sentinel;")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(output.exists())
        self.assertEqual(before, hashlib.sha256(external.read_bytes()).hexdigest())

    def test_restore_preserves_existing_destination(self):
        before = self.file_hashes()
        result = self.cli('restore', '--source', self.sql, '--output', self.db)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Refusing to overwrite', result.stderr)
        self.assertEqual(before, self.file_hashes())

    def test_restore_malformed_sql_leaves_no_partial_output(self):
        _, output, result = self.restore_source('INSERT INTO nonexistent_table VALUES(1);')
        self.assertEqual(result.returncode, 1)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.rglob('*.tmp')), [])

    def test_valid_wal_restore_is_a_portable_single_database(self):
        _, output, result = self.restore_source('', prefix='PRAGMA journal_mode=WAL;\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.data('PRAGMA journal_mode', database=output), [('delete',)])
        self.assertEqual(self.data('SELECT id,status FROM tasks ORDER BY id'),
                         self.data('SELECT id,status FROM tasks ORDER BY id', database=output))
        self.assert_command_ok('validate', '--db', output)
        self.assertEqual(list(self.root.glob('*.tmp*')), [])


if __name__ == '__main__':
    unittest.main()
