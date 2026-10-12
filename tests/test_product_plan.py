"""Structural regressions for the product-completeness planning review.

The checked-in backlog is opened read-only. Optional release-scope mutations
use only an in-memory SQLite backup. These tests protect dependency order,
journey traceability, and release coverage; they do not establish that the
acceptance prose is sufficient or that a Windows product/runtime test passed.
No product or upstream implementation is imported or executed.
"""
from __future__ import annotations

from collections import deque
from pathlib import Path
import sqlite3
import unittest


ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "planning" / "backlog.sqlite3"
OPTIONAL_TASKS = {"AIK-066", "AIK-080"}
OPTIONAL_RELEASE_EDGES = (("AIK-089", "AIK-066"), ("AIK-090", "AIK-080"))

# Keep the implementation, user-facing path, and acceptance gate connected.
# These are meaningful ownership anchors, not a copy of every trace link.
JOURNEY_ANCHORS = {
    "P01": ({"AIK-010", "AIK-022"}, {"AIK-046", "AIK-047"}, {"AIK-059", "AIK-064"}),
    "P02": ({"AIK-084"}, {"AIK-087"}, {"AIK-059", "AIK-064"}),
    "P03": ({"AIK-023", "AIK-035", "AIK-086"}, {"AIK-049"}, {"AIK-059", "AIK-064"}),
    "P04": ({"AIK-026", "AIK-084", "AIK-085"}, {"AIK-050"}, {"AIK-044", "AIK-059", "AIK-064"}),
    "P05": ({"AIK-018", "AIK-029", "AIK-040"}, {"AIK-053", "AIK-055"}, {"AIK-061", "AIK-064"}),
    "P06": ({"AIK-035", "AIK-039", "AIK-085"}, {"AIK-052", "AIK-056"}, {"AIK-060", "AIK-064"}),
    "P07": ({"AIK-037", "AIK-038"}, {"AIK-054"}, {"AIK-059", "AIK-064"}),
    "P08": ({"AIK-006", "AIK-025", "AIK-039"}, {"AIK-050", "AIK-056"}, {"AIK-059", "AIK-064"}),
    "P09": ({"AIK-058"}, {"AIK-088"}, {"AIK-059", "AIK-064"}),
    "P10": ({"AIK-057", "AIK-058"}, {"AIK-045", "AIK-088"}, {"AIK-061", "AIK-062", "AIK-064"}),
}


class ProductPlanTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(DATABASE.resolve().as_uri() + "?mode=ro", uri=True)
        self.addCleanup(self.conn.close)
        self.conn.execute("PRAGMA query_only=ON")
        self.conn.execute("BEGIN")
        # Pin one read snapshot for all queries and any backup in this test.
        self.conn.execute("SELECT id FROM tasks LIMIT 1").fetchone()

    def memory_copy(self):
        copied = sqlite3.connect(":memory:")
        self.addCleanup(copied.close)
        self.conn.backup(copied)
        copied.execute("PRAGMA foreign_keys=ON")
        return copied

    def base_scope_copy(self):
        """Remove only explicit selections before exercising a scope variant."""
        copied = self.memory_copy()
        copied.executemany(
            "DELETE FROM task_dependencies WHERE task_id=? AND depends_on=?",
            OPTIONAL_RELEASE_EDGES,
        )
        return copied

    @staticmethod
    def explicit_selections(conn):
        edges = set(conn.execute("SELECT task_id,depends_on FROM task_dependencies"))
        return {task_id for gate, task_id in OPTIONAL_RELEASE_EDGES if (gate, task_id) in edges}

    @staticmethod
    def task_ids(conn):
        return {row[0] for row in conn.execute("SELECT id FROM tasks")}

    @staticmethod
    def scope_ids(conn, scope):
        return {
            row[0] for row in conn.execute(
                "SELECT t.id FROM tasks t JOIN milestones m ON m.id=t.milestone_id "
                "WHERE m.scope=?", (scope,),
            )
        }

    @classmethod
    def graph(cls, conn):
        dependencies = {task_id: set() for task_id in cls.task_ids(conn)}
        for task_id, prerequisite in conn.execute("SELECT task_id,depends_on FROM task_dependencies"):
            dependencies[task_id].add(prerequisite)
        return dependencies

    @classmethod
    def closure(cls, conn, target):
        """Return the gate itself and all of its transitive prerequisites."""
        dependencies = cls.graph(conn)
        seen = set()
        pending = [target]
        while pending:
            task_id = pending.pop()
            if task_id not in seen:
                seen.add(task_id)
                pending.extend(dependencies[task_id])
        return seen

    def assert_acyclic(self, conn):
        dependencies = self.graph(conn)
        remaining = {task_id: len(prerequisites) for task_id, prerequisites in dependencies.items()}
        dependents = {task_id: set() for task_id in dependencies}
        for task_id, prerequisites in dependencies.items():
            for prerequisite in prerequisites:
                self.assertIn(prerequisite, dependents, f"Unknown prerequisite: {prerequisite}")
                dependents[prerequisite].add(task_id)
        ready = deque(task_id for task_id, count in remaining.items() if count == 0)
        visited = set()
        while ready:
            task_id = ready.popleft()
            visited.add(task_id)
            for dependent in dependents[task_id]:
                remaining[dependent] -= 1
                if remaining[dependent] == 0:
                    ready.append(dependent)
        self.assertEqual(visited, set(dependencies), "Dependency cycle blocks the release plan")

    def assert_precedes(self, conn, earlier, later):
        self.assertIn(earlier, self.closure(conn, later) - {later}, f"{later} bypasses {earlier}")

    def test_first_release_gate_covers_exactly_its_declared_scope(self):
        self.assertEqual(self.closure(self.conn, "AIK-064"), self.scope_ids(self.conn, "first_release"))
        self.assert_acyclic(self.conn)

    def test_each_first_release_journey_retains_implementation_gui_and_gate_links(self):
        release_scope = self.closure(self.conn, "AIK-064")
        for requirement_id, groups in JOURNEY_ANCHORS.items():
            with self.subTest(requirement=requirement_id):
                requirement = self.conn.execute(
                    "SELECT origin,description FROM requirements WHERE id=?", (requirement_id,),
                ).fetchone()
                self.assertIsNotNone(requirement, f"Missing product journey {requirement_id}")
                self.assertEqual(requirement[0], "derived")
                self.assertTrue(requirement[1].strip(), "A journey cannot be just an identifier")
                links = {
                    row[0] for row in self.conn.execute(
                        "SELECT task_id FROM task_requirements WHERE requirement_id=?", (requirement_id,),
                    )
                }
                for role, anchors in zip(("implementation", "GUI", "gate"), groups):
                    self.assertTrue(anchors <= links, f"{requirement_id} lost {role} links: {anchors - links}")
                    self.assertTrue(anchors <= release_scope, f"{requirement_id} {role} is outside the release gate")

    def test_probe_and_candidate_routes_do_not_require_their_own_qualification(self):
        for earlier in ("AIK-082", "AIK-019", "AIK-033", "AIK-035"):
            with self.subTest(task=earlier):
                self.assertNotIn("AIK-086", self.closure(self.conn, earlier))
        order = ("AIK-035", "AIK-086", "AIK-044", "AIK-045", "AIK-059", "AIK-064")
        for earlier, later in zip(order, order[1:]):
            self.assert_precedes(self.conn, earlier, later)

    def test_workspace_and_write_tools_cannot_be_bypassed_by_the_gui_release(self):
        for prerequisite in ("AIK-084", "AIK-085"):
            self.assert_precedes(self.conn, prerequisite, "AIK-035")
            self.assert_precedes(self.conn, prerequisite, "AIK-044")
        self.assert_precedes(self.conn, "AIK-084", "AIK-087")
        self.assert_precedes(self.conn, "AIK-087", "AIK-050")
        for prerequisite in ("AIK-084", "AIK-085", "AIK-087"):
            self.assert_precedes(self.conn, prerequisite, "AIK-059")
            self.assert_precedes(self.conn, prerequisite, "AIK-064")

    def test_maintenance_backend_and_gui_are_required_before_installed_e2e(self):
        order = ("AIK-058", "AIK-088", "AIK-059", "AIK-064")
        for earlier, later in zip(order, order[1:]):
            self.assert_precedes(self.conn, earlier, later)

    def test_federation_release_honors_explicit_selection_and_preserves_base_scope(self):
        expected = (
            self.scope_ids(self.conn, "first_release") | self.scope_ids(self.conn, "follow_up")
        ) - {"AIK-066"}
        selected = self.explicit_selections(self.conn) & {"AIK-066"}
        self.assertEqual(self.closure(self.conn, "AIK-089"), expected | selected)
        base = self.base_scope_copy()
        self.assertEqual(self.closure(base, "AIK-089"), expected)
        self.assert_acyclic(base)

    def test_extension_release_honors_explicit_selections_and_preserves_base_scope(self):
        expected = self.task_ids(self.conn) - OPTIONAL_TASKS
        self.assertEqual(self.closure(self.conn, "AIK-090"), expected | self.explicit_selections(self.conn))
        base = self.base_scope_copy()
        self.assertEqual(self.explicit_selections(base), set())
        self.assertEqual(self.closure(base, "AIK-090"), expected)
        self.assert_acyclic(base)

    def test_selecting_cloud_adds_it_to_both_releases_without_selecting_kubernetes(self):
        copied = self.base_scope_copy()
        copied.execute("INSERT INTO task_dependencies VALUES ('AIK-089','AIK-066')")
        self.assertEqual(self.explicit_selections(copied), {"AIK-066"})
        expected_federation = self.scope_ids(copied, "first_release") | self.scope_ids(copied, "follow_up")
        self.assertEqual(self.closure(copied, "AIK-089"), expected_federation)
        self.assertEqual(self.closure(copied, "AIK-090"), self.task_ids(copied) - {"AIK-080"})
        self.assert_acyclic(copied)

    def test_selecting_kubernetes_does_not_implicitly_select_cloud_creation(self):
        copied = self.base_scope_copy()
        copied.execute("INSERT INTO task_dependencies VALUES ('AIK-090','AIK-080')")
        self.assertEqual(self.explicit_selections(copied), {"AIK-080"})
        self.assertEqual(self.closure(copied, "AIK-090"), self.task_ids(copied) - {"AIK-066"})
        self.assert_acyclic(copied)

    def test_selecting_both_extensions_covers_the_whole_plan_without_a_cycle(self):
        copied = self.base_scope_copy()
        copied.executemany(
            "INSERT INTO task_dependencies VALUES (?,?)",
            [("AIK-089", "AIK-066"), ("AIK-090", "AIK-080")],
        )
        self.assertEqual(self.explicit_selections(copied), OPTIONAL_TASKS)
        self.assertEqual(self.closure(copied, "AIK-090"), self.task_ids(copied))
        self.assert_acyclic(copied)

    def test_kubernetes_adapter_has_no_hidden_cloud_creation_prerequisite(self):
        self.assertNotIn("AIK-066", self.closure(self.conn, "AIK-080"))

    def test_local_and_additional_provider_development_can_start_alongside_federation(self):
        federation = self.scope_ids(self.conn, "follow_up")
        for task_id in ("AIK-076", "AIK-077"):
            with self.subTest(task=task_id):
                prerequisites = self.closure(self.conn, task_id)
                self.assertIn("AIK-064", prerequisites)
                self.assertFalse(prerequisites & federation, f"{task_id} unnecessarily waits for federation")


if __name__ == "__main__":
    unittest.main()
