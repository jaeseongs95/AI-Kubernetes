"""Conformance of the AIK VM bridge with the real upstream (vibemarshal 32bb0f9).

Two groups:

* ``EnginePortAdapterTests`` always run. They use a fake application object
  that returns pydantic-like objects and raises synthetic upstream-like errors.
* ``RealUpstreamConformanceTests`` run only when ``flowmarshal.engine`` is
  importable. They use a temporary synthetic ledger and project and call the
  real façade without a runtime, provider, model or network. Otherwise they
  are skipped with a NOT_RUN reason. A skip is not a pass.

Passing on Linux is development verification; installed-wheel and Windows
calls remain NOT_RUN.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import importlib
import importlib.util
import inspect
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_runtime import Lease, Placement, Pool  # noqa: E402
from aik_runtime.errors import InvalidRecord  # noqa: E402
from aik_runtime.models import identifier  # noqa: E402
from aik_vm_bridge import (  # noqa: E402
    JOB_STATUS_PHASES, PINNED_SURFACE_REVISION, SUPPORTED_ENGINE_SCHEMA_REVISIONS, AttemptQuery,
    BridgeCode, Command, EffectOutcome, EngineApplicationPort, EnginePortError, JobObservation,
    JobPhase, ObserveRequest, ProjectQuery, RunOnceRequest, TaskExecutionBinding, VmBridge,
    VmBridgeError, check_handshake, load_surface, parameter_names,
)
from aik_vm_bridge.surface import REQUIRED_PARAMETERS  # noqa: E402
from aik_workspace.errors import VmProjectUnavailable  # noqa: E402
from aik_workspace.vm_projects import BridgeProjectPort  # noqa: E402


SURFACE_PATH = ROOT / "contracts/upstream/vm-public-surface.json"
SECRET_MARKER = "SYNTHETIC_SECRET_MUST_NOT_LEAK"
NOT_FOUND_MESSAGE = "프로젝트를 찾을 수 없습니다."
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
NOW = datetime(2029, 6, 1, tzinfo=timezone.utc)
OBSERVED_STATUS_KEYS = frozenset({
    "active_runtime_job", "attempts", "authorization_state", "context_sources", "control_state",
    "current_stage", "drill_down", "execution_summary", "history_count", "history_valid", "next_action",
    "plans", "project", "reason", "recovery", "runtime_jobs", "task_status_counts", "tasks",
})
REAL_JOB_STATUSES = ("scheduled", "running", "interrupting", "provider_terminal", "collector_lost",
                     "consumed", "cancelled")
SIGNATURES = {
    "prepare": "self, project_id: str, *, source_request: str, candidate_count: int | None=None",
    "revise": "self, project_id: str, *, source_request: str, candidate_count: int | None=None",
    "authorize": ("self, project_id: str, *, source: str, operating_policy: GoalOperatingPolicy | None=None, "
                  "capability: GoalAuthorizationCapability | None=None, authorization_target: "
                  "GoalAuthorizationTarget | None=None, selected_plan_revision_id: str | None=None"),
    "replan": "self, project_id: str, *, rationale: str",
    "run_once": ("self, project_id: str, *, resume: bool=False, proposal: ExecutionSpecProposal | None=None, "
                 "goal_validation_step: ValidationExecutionStep | None=None, "
                 "goal_validation_retry: GoalValidationRetryRequest | None=None"),
    "observe": "self, project_id: str",
    "pause": "self, project_id: str, *, reason: str='사용자 요청'",
    "cancel": "self, project_id: str, *, reason: str='사용자 요청'",
    "status": "self, project_id: str",
    "final_report": "self, project_id: str, *, goal_verdict_id: str | None=None",
    "attempt_detail": "self, attempt_id: str",
    "model_binding_status": "self, project_id: str, *, inventory: ModelInventory | None=None",
}


def surface_dict() -> dict:
    return {
        "format": "aik-upstream-surface-v1", "revision": PINNED_SURFACE_REVISION,
        "classes": [{"name": "EngineApplication", "methods": [
            {"name": name, "signature": signature} for name, signature in SIGNATURES.items()
        ]}],
    }


def is_aik_identifier(value: object) -> bool:
    try:
        identifier(value)
    except InvalidRecord:
        return False
    return True


def run_request(project_id: str, request_id: str = "run-1") -> RunOnceRequest:
    binding = TaskExecutionBinding(
        vm_instance_id="vm-instance-1", vm_project_id=project_id, vm_goal_revision_id="goal-rev-1",
        vm_plan_revision_id="plan-rev-1", vm_task_id="task-1", vm_attempt_id="attempt-1",
        vm_execution_spec_digest=DIGEST_A, vm_runtime_intent_id="intent-1", vm_provider_call_id="call-1",
        cluster_id="cluster-1", route_id="route-1", route_revision="3", connection_id="connection-1",
        connection_revision="7", provider="synthetic", auth_kind="api_key",
        provider_account_fingerprint="f" * 64, pool_id="pool-1", placement_id="placement-1",
        lease_id="lease-1", lease_fencing_token=5, support_matrix_digest=DIGEST_B,
        input_manifest_digest=DIGEST_C, workspace_root_ref="workspace://local/root-1",
    )
    pool = Pool("pool-1", "synthetic", "connection-1", enabled=True)
    placement = Placement("placement-1", "pool-1", "profile-1", "vm-instance-1", project_id,
                          "task-1", "attempt-1", state="assigned")
    lease = Lease("lease-1", "placement-1", "pool-1", "worker-1", 5, "2030-01-01T00:00:00Z", state="active")
    return RunOnceRequest(request_id, binding, placement, lease, pool)


# ----------------------------------------------------------------- fakes

class FakeApplicationError(Exception):
    """Stands in for flowmarshal EngineApplicationError (code as message)."""


class FakeLedgerError(Exception):
    """Stands in for flowmarshal EngineLedgerError (message only, no code)."""


class FakeModel:
    """A pydantic-like return value; only model_dump(mode='json') is used."""

    def __init__(self, data: dict):
        self._data = data

    def model_dump(self, *, mode: str = "python") -> dict:
        if mode != "json":
            raise AssertionError("adapter must request JSON mode")
        return copy.deepcopy(self._data)


def fake_status(**overrides) -> dict:
    status = {
        "project": {"id": "project-1", "root": "/synthetic/project-root",
                    "active_plan_revision_id": "plan-rev-1", "active_goal_revision_id": None},
        "tasks": [{"id": "task_synthetic", "status": "ready"}], "attempts": [], "plans": [],
        "runtime_jobs": [], "active_runtime_job": None, "control_state": "running",
        "authorization_state": "authorized", "current_stage": "synthetic_stage", "reason": "synthetic",
        "next_action": "synthetic", "task_status_counts": {"ready": 1}, "drill_down": {},
        "execution_summary": {}, "recovery": {}, "history_count": 3, "history_valid": True,
        "context_sources": [],
    }
    status.update(overrides)
    return status


def fake_job(status: str) -> dict:
    return {"job_id": "job-1", "project_id": "project-1", "kind": "worker_turn", "status": status,
            "attempt_id": "attempt-1", "task_id": "task-1"}


class FakeApp:
    """Real 32bb0f9 method names and parameters; no ledger, no SQL."""

    def __init__(self):
        self.responses: dict = {
            "status": fake_status(),
            "attempt_detail": FakeModel({
                "project_id": "project-1", "plan_revision_id": "plan-rev-1", "task_ref": "T1",
                "attempt": {"attempt_id": "attempt-1", "task_id": "task-1", "execution_spec_digest": DIGEST_A,
                            "attempt_no": 1, "kind": "worker", "status": "running", "binding": None},
                "intents": [], "entity_refs": [],
                "history_cursor": {"project_id": "project-1", "sequence": 7, "event_id": None,
                                   "event_hash": None, "created_at": None},
                "error_code": None, "next_action": None,
            }),
            "observe": {"project_id": "project-1", "runtime_job": fake_job("provider_terminal"),
                        "status": fake_status()},
            "run_once": FakeModel({"action": "dispatched", "project_id": "project-1", "task_id": "task-1",
                                   "attempt_id": "attempt-1", "runtime_job_id": "job-2",
                                   "runtime_job_kind": "worker_turn", "runtime_job_status": "scheduled",
                                   "detail": "synthetic", "blocker_code": None}),
        }
        self.failures: dict = {}

    def _do(self, name):
        if name in self.failures:
            raise self.failures[name]
        return self.responses.get(name, {"project_id": "project-1"})

    def prepare(self, project_id, *, source_request, candidate_count=None):
        return self._do("prepare")

    def revise(self, project_id, *, source_request, candidate_count=None):
        return self._do("revise")

    def authorize(self, project_id, *, source, operating_policy=None, capability=None,
                  authorization_target=None, selected_plan_revision_id=None):
        return self._do("authorize")

    def replan(self, project_id, *, rationale):
        return self._do("replan")

    def run_once(self, project_id, *, resume=False, proposal=None, goal_validation_step=None,
                 goal_validation_retry=None):
        return self._do("run_once")

    def observe(self, project_id):
        return self._do("observe")

    def pause(self, project_id, *, reason="사용자 요청"):
        return self._do("pause")

    def cancel(self, project_id, *, reason="사용자 요청"):
        return self._do("cancel")

    def status(self, project_id):
        return self._do("status")

    def final_report(self, project_id, *, goal_verdict_id=None):
        return self._do("final_report")

    def attempt_detail(self, attempt_id):
        return self._do("attempt_detail")

    def model_binding_status(self, project_id, *, inventory=None):
        return self._do("model_binding_status")


def fake_port(app=None, **kwargs) -> EngineApplicationPort:
    kwargs.setdefault("surface_revision", PINNED_SURFACE_REVISION)
    kwargs.setdefault("engine_schema_revision", SUPPORTED_ENGINE_SCHEMA_REVISIONS[0])
    return EngineApplicationPort(FakeApp() if app is None else app,
                                 error_types=(FakeApplicationError, FakeLedgerError), **kwargs)


class RawNestedPort:
    """A port that returns the upstream nested status without adapter lifting."""

    def __init__(self, status: dict):
        self.value = status

    def handshake(self):
        return {"surface_revision": PINNED_SURFACE_REVISION, "engine_schema_revision": 4,
                "methods": {"status": "project_id"}, "extensions": []}

    def status(self, project_id):
        return self.value


class EnginePortAdapterTests(unittest.TestCase):
    def test_status_is_normalized_from_the_real_nested_project_layout(self) -> None:
        port = fake_port()
        data = port.status("project-1")
        self.assertEqual((data["project_id"], data["project_root"], data["plan_revision_id"]),
                         ("project-1", "/synthetic/project-root", "plan-rev-1"))
        self.assertEqual(data["project"]["root"], "/synthetic/project-root")
        self.assertNotIn("tasks", data)
        self.assertEqual(data["vm_tasks"], [{"id": "task_synthetic", "status": "ready"}])
        self.assertNotIn("goal_state", data)
        project = port.lookup_project("project-1")
        self.assertEqual((project.project_id, project.root), ("project-1", "/synthetic/project-root"))

    def test_goal_state_is_derived_only_from_control_and_authorization_state(self) -> None:
        cases = ((dict(control_state="paused"), "paused"), (dict(control_state="cancelled"), "cancelled"),
                 (dict(authorization_state="required"), "awaiting_authorization"),
                 (dict(authorization_state="not_ready"), None))
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                app = FakeApp()
                app.responses["status"] = fake_status(**overrides)
                self.assertEqual(fake_port(app).status("project-1").get("goal_state"), expected)

    def test_controller_view_reads_the_normalized_status_without_inventing_tasks(self) -> None:
        from aik_controller.models import VmView

        app = FakeApp()
        app.responses["status"] = fake_status(control_state="paused")
        view = VmView.from_payload(fake_port(app).status("project-1"))
        self.assertEqual((view.goal_state, view.plan_revision_id, view.tasks), ("paused", "plan-rev-1", ()))

    def test_bridge_project_port_reads_adapter_output_and_raw_nested_root(self) -> None:
        bridge = VmBridge(fake_port(), surface_dict(), commands=(Command.STATUS,))
        info = BridgeProjectPort(bridge).lookup_project("project-1")
        self.assertEqual(info.root, "/synthetic/project-root")
        raw = VmBridge(RawNestedPort(fake_status()), surface_dict(), commands=(Command.STATUS,))
        self.assertEqual(BridgeProjectPort(raw).lookup_project("project-1").root, "/synthetic/project-root")
        conflicting = fake_status()
        conflicting["project_root"] = "/synthetic/other-root"
        bad = VmBridge(RawNestedPort(conflicting), surface_dict(), commands=(Command.STATUS,))
        with self.assertRaises(VmProjectUnavailable):
            BridgeProjectPort(bad).lookup_project("project-1")
        foreign = fake_status(project={"id": "project-2", "root": "/synthetic/project-root"})
        other = VmBridge(RawNestedPort(foreign), surface_dict(), commands=(Command.STATUS,))
        with self.assertRaises(VmProjectUnavailable):
            BridgeProjectPort(other).lookup_project("project-1")

    def test_conflicting_adapter_lift_is_an_invalid_response(self) -> None:
        app = FakeApp()
        app.responses["status"] = {**fake_status(), "project_id": "project-2"}
        with self.assertRaises(EnginePortError) as raised:
            fake_port(app).status("project-1")
        self.assertEqual(raised.exception.code, "INVALID_ENGINE_RESPONSE")

    def test_missing_project_is_classified_without_leaking_the_message(self) -> None:
        app = FakeApp()
        app.failures["status"] = FakeLedgerError(NOT_FOUND_MESSAGE)
        port = fake_port(app)
        with self.assertRaises(EnginePortError) as raised:
            port.status("project-1")
        self.assertEqual((raised.exception.code, raised.exception.category), ("PROJECT_NOT_FOUND", "ledger"))
        self.assertNotIn("프로젝트", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(port.lookup_project("project-1"))
        bridge = VmBridge(port, surface_dict(), commands=(Command.STATUS,))
        self.assertIsNone(BridgeProjectPort(bridge).lookup_project("project-1"))

    def test_other_ledger_failures_are_not_treated_as_absence(self) -> None:
        app = FakeApp()
        app.failures["status"] = FakeLedgerError("other ledger failure " + SECRET_MARKER)
        port = fake_port(app)
        with self.assertRaises(EnginePortError) as raised:
            port.status("project-1")
        self.assertEqual(raised.exception.code, "ENGINE_LEDGER_ERROR")
        self.assertNotIn(SECRET_MARKER, str(raised.exception))
        bridge = VmBridge(port, surface_dict(), commands=(Command.STATUS,))
        with self.assertRaises(VmProjectUnavailable):
            BridgeProjectPort(bridge).lookup_project("project-1")

    def test_application_error_codes_are_kept_and_suffix_text_dropped(self) -> None:
        app = FakeApp()
        app.failures["revise"] = FakeApplicationError(
            "GOAL_REVISION_ACTIVE_PLAN: active Plan plan_x " + SECRET_MARKER)
        app.failures["attempt_detail"] = FakeApplicationError("ATTEMPT_NOT_FOUND")
        app.failures["observe"] = FakeApplicationError("RUNTIME_REQUIRED")
        app.failures["final_report"] = FakeApplicationError("lowercase text " + SECRET_MARKER)
        port = fake_port(app)
        expected = (
            (lambda: port.revise("project-1", source_request="x"), "GOAL_REVISION_ACTIVE_PLAN"),
            (lambda: port.attempt_detail("attempt-1"), "ATTEMPT_NOT_FOUND"),
            (lambda: port.observe("project-1"), "RUNTIME_REQUIRED"),
            (lambda: port.final_report("project-1"), "ENGINE_APPLICATION_ERROR"),
        )
        for call, code in expected:
            with self.subTest(code=code):
                with self.assertRaises(EnginePortError) as raised:
                    call()
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn(SECRET_MARKER, str(raised.exception) + repr(raised.exception))
        bridge = VmBridge(port, surface_dict())
        with self.assertRaises(VmBridgeError) as query:
            bridge.attempt_detail(AttemptQuery("req-1", "attempt-1"))
        self.assertEqual((query.exception.code, query.exception.vm_code, query.exception.outcome),
                         (BridgeCode.VM_REJECTED, "ATTEMPT_NOT_FOUND", EffectOutcome.KNOWN_FAILURE))

    def test_unclassified_and_unserializable_results_fail_closed(self) -> None:
        app = FakeApp()
        app.failures["status"] = ValueError(SECRET_MARKER)
        app.responses["final_report"] = {"project_id": "project-1", "opaque": object()}
        port = fake_port(app)
        with self.assertRaises(EnginePortError) as raised:
            port.status("project-1")
        self.assertEqual((raised.exception.code, raised.exception.category), (None, "unexpected"))
        self.assertNotIn(SECRET_MARKER, str(raised.exception))
        with self.assertRaises(EnginePortError) as invalid:
            port.final_report("project-1")
        self.assertEqual(invalid.exception.code, "INVALID_ENGINE_RESPONSE")
        bridge = VmBridge(port, surface_dict())
        with self.assertRaises(VmBridgeError) as query:
            bridge.status(ProjectQuery("req-1", "project-1"))
        self.assertEqual((query.exception.code, query.exception.outcome),
                         (BridgeCode.QUERY_FAILED, EffectOutcome.KNOWN_FAILURE))

    def test_attempt_detail_model_is_dumped_lifted_and_cursor_flattened(self) -> None:
        port = fake_port()
        data = port.attempt_detail("attempt-1")
        self.assertEqual((data["attempt_id"], data["task_id"], data["execution_spec_digest"]),
                         ("attempt-1", "task-1", DIGEST_A))
        self.assertEqual(data["history_cursor"], 7)
        self.assertEqual(data["vm_history_cursor"]["sequence"], 7)
        receipt = VmBridge(port, surface_dict()).attempt_detail(AttemptQuery("req-1", "attempt-1"))
        self.assertEqual((receipt.refs.attempt_id, receipt.refs.task_id, receipt.refs.history_cursor,
                          receipt.refs.plan_revision_id, receipt.refs.goal_revision_id),
                         ("attempt-1", "task-1", 7, "plan-rev-1", None))

    def test_run_once_result_fields_are_normalized_to_a_job(self) -> None:
        data = fake_port().run_once("project-1")
        self.assertEqual(data["job"], {"job_id": "job-2", "status": "scheduled"})
        self.assertEqual(data["runtime_job_status"], "scheduled")

    def test_observe_runtime_job_maps_to_real_status_phases(self) -> None:
        cases = (("provider_terminal", JobPhase.TERMINAL), ("consumed", JobPhase.TERMINAL),
                 ("collector_lost", JobPhase.UNKNOWN), ("cancelled", JobPhase.UNKNOWN),
                 ("interrupting", JobPhase.RUNNING), ("scheduled", JobPhase.PENDING))
        for status, phase in cases:
            with self.subTest(status=status):
                app = FakeApp()
                app.responses["observe"] = {"project_id": "project-1", "runtime_job": fake_job(status),
                                            "status": fake_status()}
                receipt = VmBridge(fake_port(app), surface_dict()).observe(ObserveRequest("o-1", "project-1"))
                self.assertEqual((receipt.job.phase, receipt.job.vm_status, receipt.job.job_id),
                                 (phase, status, "job-1"))
                self.assertEqual(receipt.refs.attempt_id, "attempt-1")
                self.assertEqual(receipt.job.core_consumed, status == "consumed")
                self.assertNotIn("tasks", receipt.payload["status"])

    def test_run_once_without_binding_extension_is_refused_before_any_engine_call(self) -> None:
        port = fake_port()
        self.assertFalse(port.upstream_binding_methods_present)
        self.assertFalse(hasattr(port, "bind_execution"))
        with self.assertRaises(VmBridgeError) as raised:
            VmBridge(port, surface_dict(), now=lambda: NOW).run_once(run_request("project-1"))
        self.assertEqual((raised.exception.code, raised.exception.subject),
                         (BridgeCode.SEAM_UNAVAILABLE, "aik-bound-runtime"))
        self.assertEqual(port.calls, ())

    def test_handshake_reports_names_and_requires_declared_revisions(self) -> None:
        port = fake_port()
        raw = port.handshake()
        self.assertEqual(raw["extensions"], [])
        self.assertEqual(set(raw["methods"]), {command.value for command in Command})
        check_handshake(load_surface(surface_dict()), raw, tuple(Command))
        self.assertEqual(port.calls, ())
        undeclared = EngineApplicationPort(FakeApp())
        with self.assertRaises(VmBridgeError) as raised:
            VmBridge(undeclared, surface_dict()).status(ProjectQuery("req-1", "project-1"))
        self.assertIs(raised.exception.code, BridgeCode.INVALID_RESPONSE)


# ----------------------------------------------------------- real upstream

_CANDIDATES = {
    "EngineApplication": ("flowmarshal.engine.application", "flowmarshal.engine"),
    "EngineService": ("flowmarshal.engine.service", "flowmarshal.engine", "flowmarshal.engine.application"),
    "SQLiteEngineLedger": ("flowmarshal.engine.ledger", "flowmarshal.engine"),
    "RuntimeJobStatus": ("flowmarshal.engine.domain", "flowmarshal.engine"),
}


def _locate(name: str, modules: tuple):
    for module in modules:
        try:
            value = getattr(importlib.import_module(module), name, None)
        except Exception:
            continue
        if value is not None:
            return value
    return None


def _upstream():
    try:
        spec = importlib.util.find_spec("flowmarshal.engine")
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        return None, "NOT_RUN: flowmarshal.engine (vibemarshal 32bb0f9) is not importable"
    found = {name: _locate(name, modules) for name, modules in _CANDIDATES.items()}
    missing = sorted(name for name, value in found.items() if value is None)
    if missing:
        return None, "NOT_RUN: upstream symbols are not importable: " + ", ".join(missing)
    return found, None


UPSTREAM, UPSTREAM_REASON = _upstream()


def _create_project(service, name: str, root: Path):
    try:
        return service.create_project(name=name, root=str(root))
    except (TypeError, ValueError):
        return service.create_project(name=name, root=root)


def _project_id(created) -> str:
    if isinstance(created, str):
        return created
    for attribute in ("project_id", "id"):
        value = getattr(created, attribute, None)
        if value is None and hasattr(created, "keys"):
            try:
                value = created[attribute]
            except (KeyError, IndexError, TypeError):
                value = None
        if isinstance(value, str):
            return value
    raise AssertionError("create_project returned no recognizable project identifier")


@unittest.skipIf(UPSTREAM is None, UPSTREAM_REASON or "NOT_RUN: upstream unavailable")
class RealUpstreamConformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aik-vm-conformance-", ignore_cleanup_errors=True)
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.base = base
        self.project_root = base / "synthetic-project"
        self.project_root.mkdir()
        (self.project_root / "README.md").write_text("synthetic conformance project\n", encoding="utf-8")
        ledger = UPSTREAM["SQLiteEngineLedger"](base / "engine-ledger.db")
        service = UPSTREAM["EngineService"](ledger)
        service.initialize()
        self.project_id = _project_id(_create_project(service, "aik-conformance", self.project_root))
        self.app = UPSTREAM["EngineApplication"](service)
        # Declared test assumptions: the pinned surface revision and the only
        # bridge-supported schema revision. Neither is read from the ledger.
        self.port = EngineApplicationPort(self.app, surface_revision=PINNED_SURFACE_REVISION,
                                          engine_schema_revision=SUPPORTED_ENGINE_SCHEMA_REVISIONS[0])

    def require_surface(self) -> None:
        if not SURFACE_PATH.is_file():
            self.skipTest("NOT_RUN: contracts/upstream/vm-public-surface.json is absent")

    def require_aik_project_id(self) -> None:
        self.assertTrue(is_aik_identifier(self.project_id),
                        "CONFORMANCE_FINDING: the real VM project ID does not satisfy the AIK identifier rule; "
                        "the bridge would reject every request for it.")

    def test_a_declared_surface_bridge_parameters_and_real_signatures_agree(self) -> None:
        self.require_surface()
        surface = load_surface(SURFACE_PATH)
        engine = UPSTREAM["EngineApplication"]
        for command in Command:
            with self.subTest(command=command.value):
                declared = parameter_names(surface.methods[command.value])
                real = tuple(name for name in inspect.signature(getattr(engine, command.value)).parameters
                             if name != "self")
                self.assertEqual(declared, real)
                self.assertTrue(set(REQUIRED_PARAMETERS[command]) <= set(real))
        handshake = check_handshake(surface, self.port.handshake(), tuple(Command))
        self.assertEqual(handshake.extensions, frozenset())

    def test_b_every_runtime_job_status_is_classified(self) -> None:
        values = {member.value for member in UPSTREAM["RuntimeJobStatus"]}
        self.assertEqual(values, set(REAL_JOB_STATUSES))
        self.assertEqual(values - set(JOB_STATUS_PHASES), set(), "unmapped RuntimeJobStatus values")
        self.assertEqual(set(JOB_STATUS_PHASES) - values, set(), "stale assumed job statuses")
        self.assertIs(JOB_STATUS_PHASES["provider_terminal"], JobPhase.TERMINAL)
        self.assertFalse(JobObservation(JobPhase.TERMINAL, "provider_terminal").core_consumed)

    def test_c_project_id_and_root_are_normalized_and_matched(self) -> None:
        raw = self.app.status(self.project_id)
        self.assertTrue(OBSERVED_STATUS_KEYS <= set(raw), sorted(OBSERVED_STATUS_KEYS - set(raw)))
        self.assertNotIn("project_root", raw)
        self.assertEqual(raw["project"]["id"], self.project_id)
        project = self.port.lookup_project(self.project_id)
        self.assertEqual(project.project_id, self.project_id)
        self.assertEqual(Path(project.root).resolve(), self.project_root.resolve())
        self.require_surface()
        self.require_aik_project_id()
        bridge = VmBridge(self.port, SURFACE_PATH, commands=(Command.STATUS,))
        info = BridgeProjectPort(bridge).lookup_project(self.project_id)
        other = self.base / "other-project"
        other.mkdir()
        self.assertEqual(Path(info.root).resolve(), self.project_root.resolve())  # READY-equivalent
        self.assertNotEqual(Path(info.root).resolve(), other.resolve())  # MISMATCH-equivalent

    def test_d_absent_project_attempt_and_runtime_are_coded_and_fail_closed(self) -> None:
        missing = "aik-missing-project-0001"
        with self.assertRaises(EnginePortError) as absent:
            self.port.status(missing)
        self.assertEqual(absent.exception.code, "PROJECT_NOT_FOUND")
        self.assertNotIn("프로젝트", str(absent.exception))
        self.assertIsNone(self.port.lookup_project(missing))
        with self.assertRaises(EnginePortError) as attempt:
            self.port.attempt_detail("aik-missing-attempt-0001")
        self.assertEqual(attempt.exception.code, "ATTEMPT_NOT_FOUND")
        with self.assertRaises(EnginePortError) as observe:
            self.port.observe(self.project_id)
        self.assertEqual(observe.exception.code, "RUNTIME_REQUIRED")
        with self.assertRaises(EnginePortError) as run:
            self.port.run_once(self.project_id)
        self.assertEqual(run.exception.code, "RUNTIME_REQUIRED")
        self.require_surface()
        bridge = VmBridge(self.port, SURFACE_PATH)
        self.assertIsNone(BridgeProjectPort(bridge).lookup_project(missing))
        with self.assertRaises(VmBridgeError) as query:
            bridge.attempt_detail(AttemptQuery("req-attempt", "aik-missing-attempt-0001"))
        self.assertEqual((query.exception.code, query.exception.vm_code, query.exception.outcome),
                         (BridgeCode.VM_REJECTED, "ATTEMPT_NOT_FOUND", EffectOutcome.KNOWN_FAILURE))
        self.require_aik_project_id()
        with self.assertRaises(VmBridgeError) as observed:
            bridge.observe(ObserveRequest("req-observe", self.project_id))
        self.assertEqual((observed.exception.vm_code, observed.exception.outcome),
                         ("RUNTIME_REQUIRED", EffectOutcome.KNOWN_FAILURE))
        self.assertEqual(bridge.unresolved_projects, frozenset())

    def test_e_missing_durable_binding_blocks_run_once_before_any_effect(self) -> None:
        self.assertFalse(hasattr(self.app, "bind_execution"))
        self.assertFalse(hasattr(self.app, "read_execution_binding"))
        self.assertFalse(self.port.upstream_binding_methods_present)
        self.require_surface()
        self.require_aik_project_id()
        bridge = VmBridge(self.port, SURFACE_PATH, now=lambda: NOW)
        with self.assertRaises(VmBridgeError) as raised:
            bridge.run_once(run_request(self.project_id))
        self.assertIn(raised.exception.code, (BridgeCode.SEAM_UNAVAILABLE, BridgeCode.CAPABILITY_MISSING))
        self.assertNotIn("run_once", self.port.calls)
        self.assertNotIn("attempt_detail", self.port.calls)


if __name__ == "__main__":
    unittest.main()
