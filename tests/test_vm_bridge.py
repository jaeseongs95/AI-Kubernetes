"""AIK-031 bridge tests against a fake VM port only.

No VM installation, provider, credential, network or subprocess is used. The
fake port returns synthetic mappings. Job statuses use the real upstream
RuntimeJobStatus values (vibemarshal 32bb0f9). Passing on Linux is
development verification; installed VM calls remain NOT_RUN.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_runtime import Lease, Placement, Pool  # noqa: E402
from aik_vm_bridge import (  # noqa: E402
    BINDING_EXTENSION, BINDING_FIELDS, BLOCKING_GATES, JOB_STATUS_PHASES, PINNED_SURFACE_REVISION,
    AttemptQuery, AuthorizeRequest, BridgeCode, CancelRequest, Command, EffectOutcome,
    FinalReportRequest, GoalRequest, InventoryRequest, JobPhase, ObserveRequest, PauseRequest,
    ProjectQuery, ReplanRequest, RunOnceRequest, TaskExecutionBinding, VmBridge, VmBridgeError,
    load_surface, parameter_names, preflight, verify_assignment,
)


SECRET_MARKER = "SYNTHETIC_SECRET_MUST_NOT_LEAK"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
NOW = datetime(2029, 6, 1, tzinfo=timezone.utc)
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
REAL_JOB_STATUSES = ("scheduled", "running", "interrupting", "provider_terminal", "collector_lost",
                     "consumed", "cancelled")
_NO_OVERRIDE = object()


def surface_dict(methods=None, revision=PINNED_SURFACE_REVISION) -> dict:
    methods = SIGNATURES if methods is None else methods
    return {
        "format": "aik-upstream-surface-v1", "revision": revision,
        "classes": [{"name": "EngineApplication", "methods": [
            {"name": "__init__", "signature": "self, service: EngineService"},
            *({"name": name, "signature": sig} for name, sig in methods.items()),
        ]}],
    }


def binding_fields(**overrides) -> dict:
    base = dict(
        vm_instance_id="vm-instance-1", vm_project_id="project-1", vm_goal_revision_id="goal-rev-1",
        vm_plan_revision_id="plan-rev-1", vm_task_id="task-1", vm_attempt_id="attempt-1",
        vm_execution_spec_digest=DIGEST_A, vm_runtime_intent_id="intent-1", vm_provider_call_id="call-1",
        cluster_id="cluster-1", route_id="route-1", route_revision="3", connection_id="connection-1",
        connection_revision="7", provider="synthetic", auth_kind="api_key",
        provider_account_fingerprint="f" * 64, pool_id="pool-1", placement_id="placement-1",
        lease_id="lease-1", lease_fencing_token=5, support_matrix_digest=DIGEST_B,
        input_manifest_digest=DIGEST_C, workspace_root_ref="workspace://local/root-1",
    )
    base.update(overrides)
    return base


def canonical_digest(mapping) -> str:
    text = json.dumps(dict(mapping), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(text.encode("ascii")).hexdigest()


def assignment(**lease_overrides):
    pool = Pool("pool-1", "synthetic", "connection-1", enabled=True)
    placement = Placement("placement-1", "pool-1", "profile-1", "vm-instance-1", "project-1",
                          "task-1", "attempt-1", state="assigned")
    lease_args = dict(id="lease-1", placement_id="placement-1", pool_id="pool-1", worker_id="worker-1",
                      fencing_token=5, expires_at="2030-01-01T00:00:00Z", state="active")
    lease_args.update(lease_overrides)
    return placement, Lease(**lease_args), pool


class CodedError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class FakeVm:
    """Records façade calls; has no database and no raw SQL capability."""

    def __init__(self, *, extensions=(), revision=PINNED_SURFACE_REVISION, schema_revision=4, methods=None):
        self.calls: list = []
        self.failures: dict = {}
        self.handshake_value = {
            "surface_revision": revision, "engine_schema_revision": schema_revision,
            "methods": dict(SIGNATURES if methods is None else methods), "extensions": list(extensions),
        }
        self.responses = {
            "prepare": {"project_id": "project-1", "goal_id": "goal-1", "goal_revision_id": "goal-rev-1",
                        "history_cursor": 11},
            "revise": {"project_id": "project-1", "goal_revision_id": "goal-rev-2", "history_cursor": 12},
            "authorize": {"project_id": "project-1", "authorization_id": "authorization-9",
                          "plan_revision_id": "plan-rev-1", "history_cursor": 13},
            "replan": {"project_id": "project-1", "plan_revision_id": "plan-rev-2"},
            "pause": {"project_id": "project-1", "state": "paused"},
            "cancel": {"project_id": "project-1", "state": "cancel_requested"},
            "status": {"project_id": "project-1", "history_cursor": 21, "phase": "ready"},
            "final_report": {"project_id": "project-1", "report": "synthetic"},
            "attempt_detail": {"project_id": "project-1", "attempt_id": "attempt-1", "task_id": "task-1",
                               "goal_revision_id": "goal-rev-1", "plan_revision_id": "plan-rev-1",
                               "execution_spec_digest": DIGEST_A},
            "run_once": {"project_id": "project-1", "task_id": "task-1", "attempt_id": "attempt-1",
                         "job": {"job_id": "job-1", "status": "running"}},
            "observe": {"project_id": "project-1", "attempt_id": "attempt-1",
                        "job": {"job_id": "job-1", "status": "consumed"}},
            "model_binding_status": {"project_id": "project-1", "models": ["synthetic-model"]},
        }

    def names(self) -> list:
        return [call[0] for call in self.calls]

    def handshake(self):
        self.calls.append(("handshake", (), {}))
        return self.handshake_value

    def _do(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if name in self.failures:
            raise self.failures[name]
        return self.responses[name]

    def prepare(self, project_id, *, source_request, candidate_count=None):
        return self._do("prepare", project_id, source_request=source_request, candidate_count=candidate_count)

    def revise(self, project_id, *, source_request, candidate_count=None):
        return self._do("revise", project_id, source_request=source_request, candidate_count=candidate_count)

    def authorize(self, project_id, *, source, selected_plan_revision_id=None):
        return self._do("authorize", project_id, source=source, selected_plan_revision_id=selected_plan_revision_id)

    def run_once(self, project_id, *, resume=False):
        return self._do("run_once", project_id, resume=resume)

    def observe(self, project_id):
        return self._do("observe", project_id)

    def pause(self, project_id, *, reason):
        return self._do("pause", project_id, reason=reason)

    def cancel(self, project_id, *, reason):
        return self._do("cancel", project_id, reason=reason)

    def status(self, project_id):
        return self._do("status", project_id)

    def replan(self, project_id, *, rationale):
        return self._do("replan", project_id, rationale=rationale)

    def final_report(self, project_id, *, goal_verdict_id=None):
        return self._do("final_report", project_id, goal_verdict_id=goal_verdict_id)

    def attempt_detail(self, attempt_id):
        return self._do("attempt_detail", attempt_id)

    def model_binding_status(self, project_id):
        return self._do("model_binding_status", project_id)


class BoundFakeVm(FakeVm):
    """Simulates the required upstream binding extension that VM lacks today."""

    def __init__(self, **kwargs):
        super().__init__(extensions=(BINDING_EXTENSION,), **kwargs)
        self.persisted: dict = {}
        self.bind_echo = None
        self.read_override = _NO_OVERRIDE

    def bind_execution(self, binding):
        self.calls.append(("bind_execution", (dict(binding),), {}))
        if "bind_execution" in self.failures:
            raise self.failures["bind_execution"]
        digest = canonical_digest(binding)
        self.persisted[binding["vm_attempt_id"]] = digest
        return {"binding_digest": digest if self.bind_echo is None else self.bind_echo}

    def read_execution_binding(self, vm_attempt_id):
        self.calls.append(("read_execution_binding", (vm_attempt_id,), {}))
        if "read_execution_binding" in self.failures:
            raise self.failures["read_execution_binding"]
        if self.read_override is not _NO_OVERRIDE:
            return self.read_override
        digest = self.persisted.get(vm_attempt_id)
        return None if digest is None else {"binding_digest": digest}


def run_request(request_id="run-1", **binding_overrides) -> RunOnceRequest:
    placement, lease, pool = assignment()
    return RunOnceRequest(request_id, TaskExecutionBinding(**binding_fields(**binding_overrides)),
                          placement, lease, pool)


def observed_job(job_id: str, status: str, attempt_id: str = "attempt-1") -> dict:
    return {"project_id": "project-1", "attempt_id": attempt_id, "job": {"job_id": job_id, "status": status}}


def find_contract(name: str) -> Path | None:
    matches = sorted((ROOT / "contracts").glob(f"**/{name}")) if (ROOT / "contracts").is_dir() else []
    return matches[0] if matches else None


class SurfaceTests(unittest.TestCase):
    def test_parameter_names_respect_nested_brackets(self) -> None:
        self.assertEqual(parameter_names("self, a: dict[str, Any] | None=None, *, b: Iterable[str]=(), c='x, y'"),
                         ("a", "b", "c"))
        with self.assertRaises(ValueError):
            parameter_names("self, a: dict[str, Any")
        with self.assertRaises(ValueError):
            parameter_names("self, a='unclosed, b")

    @unittest.skipUnless((ROOT / "contracts/upstream/vm-public-surface.json").is_file(),
                         "NOT_RUN: contracts/upstream/vm-public-surface.json is absent")
    def test_declared_vm_public_surface_satisfies_every_bridge_command(self) -> None:
        surface = load_surface(ROOT / "contracts/upstream/vm-public-surface.json")
        self.assertEqual(surface.revision, PINNED_SURFACE_REVISION)
        report = preflight(surface, tuple(Command))
        self.assertTrue(report.ok, report.missing)
        self.assertEqual(set(report.available), set(Command))

    def test_missing_method_or_parameter_is_reported_before_any_port_call(self) -> None:
        methods = dict(SIGNATURES)
        del methods["cancel"]
        methods["run_once"] = "self, project_id: str"
        report = preflight(load_surface(surface_dict(methods)), tuple(Command))
        self.assertIn(("cancel", "method_absent"), report.missing)
        self.assertIn(("run_once", "parameter_absent:resume"), report.missing)
        vm = FakeVm()
        with self.assertRaises(VmBridgeError) as raised:
            VmBridge(vm, surface_dict(methods))
        self.assertIs(raised.exception.code, BridgeCode.CAPABILITY_MISSING)
        self.assertEqual(vm.calls, [])

    def test_invalid_surface_document_is_rejected(self) -> None:
        with self.assertRaises(VmBridgeError) as raised:
            load_surface({"format": "other", "revision": "x", "classes": []})
        self.assertIs(raised.exception.code, BridgeCode.INVALID_SURFACE)

    def test_handshake_rejects_revision_schema_signature_and_malformed_descriptions(self) -> None:
        cases = (
            (FakeVm(revision="0" * 40), BridgeCode.VERSION_MISMATCH, "surface_revision"),
            (FakeVm(schema_revision=5), BridgeCode.VERSION_MISMATCH, "engine_schema_revision"),
            (FakeVm(methods={**SIGNATURES, "cancel": "self, project_id: str, *, why: str"}),
             BridgeCode.CONTRACT_MISMATCH, "cancel"),
        )
        for vm, code, subject in cases:
            with self.subTest(code=code, subject=subject):
                with self.assertRaises(VmBridgeError) as raised:
                    VmBridge(vm, surface_dict()).status(ProjectQuery("status-1", "project-1"))
                self.assertIs(raised.exception.code, code)
                self.assertEqual(raised.exception.subject, subject)
                self.assertNotIn("status", vm.names())
        broken = FakeVm()
        broken.handshake_value = ["not", "a", "mapping"]
        with self.assertRaises(VmBridgeError) as raised:
            VmBridge(broken, surface_dict()).handshake()
        self.assertIs(raised.exception.code, BridgeCode.INVALID_RESPONSE)


class ContractDriftTests(unittest.TestCase):
    """F13: compare fixed constants with contract files when they exist."""

    def test_binding_fields_match_the_schema_file_when_present(self) -> None:
        path = find_contract("task-execution-binding.schema.json")
        if path is None:
            self.skipTest("NOT_RUN: task-execution-binding.schema.json is absent")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIsInstance(data.get("properties"), dict, "schema has no top-level properties")
        self.assertEqual(set(data["properties"]), set(BINDING_FIELDS))
        self.assertEqual(set(data.get("required", ())), set(BINDING_FIELDS))
        self.assertIs(data.get("additionalProperties"), False)

    def test_blocking_gates_appear_in_the_integration_contract_when_present(self) -> None:
        path = find_contract("integration-contract.json")
        if path is None:
            self.skipTest("NOT_RUN: integration-contract.json is absent")
        text = path.read_text(encoding="utf-8")
        for gate in BLOCKING_GATES:
            with self.subTest(gate=gate):
                self.assertIn(gate, text)


class CommandQueryTests(unittest.TestCase):
    def test_public_commands_pass_exact_arguments_and_preserve_vm_identifiers(self) -> None:
        vm = FakeVm()
        bridge = VmBridge(vm, surface_dict())
        prepared = bridge.prepare(GoalRequest("req-prepare", "project-1", "synthetic goal", candidate_count=2))
        self.assertEqual((prepared.request_id, prepared.refs.goal_id, prepared.refs.goal_revision_id,
                          prepared.refs.history_cursor), ("req-prepare", "goal-1", "goal-rev-1", 11))
        authorized = bridge.authorize(AuthorizeRequest("req-auth", "project-1", "gui",
                                                       selected_plan_revision_id="plan-rev-1"))
        self.assertEqual(authorized.refs.authorization_id, "authorization-9")
        bridge.revise(GoalRequest("req-revise", "project-1", "synthetic revision"))
        bridge.replan(ReplanRequest("req-replan", "project-1", "synthetic rationale"))
        bridge.pause(PauseRequest("req-pause", "project-1", "synthetic pause"))
        status = bridge.status(ProjectQuery("req-status", "project-1"))
        self.assertEqual(status.refs.history_cursor, 21)
        self.assertEqual(status.payload["phase"], "ready")
        bridge.final_report(FinalReportRequest("req-report", "project-1", goal_verdict_id="verdict-1"))
        self.assertEqual(vm.calls[1], ("prepare", ("project-1",), {"source_request": "synthetic goal", "candidate_count": 2}))
        self.assertEqual(vm.calls[2], ("authorize", ("project-1",), {"source": "gui", "selected_plan_revision_id": "plan-rev-1"}))
        self.assertIn(("final_report", ("project-1",), {"goal_verdict_id": "verdict-1"}), vm.calls)
        self.assertEqual(vm.names().count("handshake"), 1)

    def test_payload_is_a_snapshot_independent_of_the_vm_object(self) -> None:
        vm = FakeVm()
        receipt = VmBridge(vm, surface_dict()).status(ProjectQuery("req-1", "project-1"))
        vm.responses["status"]["phase"] = "mutated"
        self.assertEqual(receipt.payload["phase"], "ready")
        with self.assertRaises(TypeError):
            receipt.payload["phase"] = "x"  # type: ignore[index]

    def test_f7_nested_payload_values_are_read_only_and_still_equal_plain_values(self) -> None:
        vm = FakeVm()
        vm.responses["status"] = {"project_id": "project-1", "nested": {"k": [1]}, "items": [{"a": 1}]}
        payload = VmBridge(vm, surface_dict()).status(ProjectQuery("req-1", "project-1")).payload
        self.assertEqual(payload["nested"], {"k": [1]})
        self.assertEqual(payload["items"], [{"a": 1}])
        with self.assertRaises(TypeError):
            payload["nested"]["k"].append(2)
        with self.assertRaises(TypeError):
            payload["nested"]["x"] = 1
        with self.assertRaises(TypeError):
            payload["items"][0]["a"] = 2
        with self.assertRaises(TypeError):
            payload["items"].extend([3])
        self.assertEqual(payload["nested"], {"k": [1]})

    def test_bridge_sources_do_not_import_vm_internals_or_sqlite(self) -> None:
        for path in (ROOT / "src/aik_vm_bridge").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                self.assertNotIn("sqlite3", text)
                self.assertNotIn("import flowmarshal", text)
                self.assertNotIn("from flowmarshal", text)

    def test_stale_execution_input_keeps_vm_code_and_never_leaks_raw_message(self) -> None:
        vm = FakeVm()
        vm.failures["authorize"] = CodedError("STALE_EXECUTION_INPUT", "raw detail " + SECRET_MARKER)
        with self.assertRaises(VmBridgeError) as raised:
            VmBridge(vm, surface_dict()).authorize(AuthorizeRequest("req-1", "project-1", "gui"))
        error = raised.exception
        self.assertEqual((error.code, error.vm_code, error.outcome),
                         (BridgeCode.VM_REJECTED, "STALE_EXECUTION_INPUT", EffectOutcome.KNOWN_FAILURE))
        self.assertNotIn(SECRET_MARKER, str(error) + repr(error))
        self.assertIsNone(error.__cause__)
        self.assertTrue(error.__suppress_context__)

    def test_f8_port_exception_is_not_retained_in_the_error_context(self) -> None:
        vm = FakeVm()
        vm.failures["status"] = OSError(SECRET_MARKER)
        vm.failures["pause"] = CodedError("EXTERNAL_UNKNOWN", SECRET_MARKER)
        bridge = VmBridge(vm, surface_dict())
        with self.assertRaises(VmBridgeError) as query:
            bridge.status(ProjectQuery("req-1", "project-1"))
        with self.assertRaises(VmBridgeError) as effect:
            bridge.pause(PauseRequest("req-2", "project-1", "stop"))
        for error in (query.exception, effect.exception):
            with self.subTest(code=error.code):
                self.assertIsNone(error.__cause__)
                self.assertIsNone(error.__context__)

    def test_external_unknown_keeps_vm_code_and_reports_unknown_outcome(self) -> None:
        vm = FakeVm()
        vm.failures["pause"] = CodedError("EXTERNAL_UNKNOWN", SECRET_MARKER)
        with self.assertRaises(VmBridgeError) as raised:
            VmBridge(vm, surface_dict()).pause(PauseRequest("req-1", "project-1", "stop"))
        self.assertEqual((raised.exception.vm_code, raised.exception.outcome),
                         ("EXTERNAL_UNKNOWN", EffectOutcome.UNKNOWN))
        self.assertEqual(vm.names().count("pause"), 1)

    def test_uncoded_failures_are_unknown_for_commands_and_known_for_queries(self) -> None:
        vm = FakeVm()
        vm.failures["replan"] = TimeoutError(SECRET_MARKER)
        vm.failures["status"] = OSError(SECRET_MARKER)
        bridge = VmBridge(vm, surface_dict())
        with self.assertRaises(VmBridgeError) as command:
            bridge.replan(ReplanRequest("req-1", "project-1", "why"))
        self.assertEqual((command.exception.code, command.exception.outcome),
                         (BridgeCode.OUTCOME_UNKNOWN, EffectOutcome.UNKNOWN))
        with self.assertRaises(VmBridgeError) as query:
            bridge.status(ProjectQuery("req-2", "project-1"))
        self.assertEqual((query.exception.code, query.exception.outcome),
                         (BridgeCode.QUERY_FAILED, EffectOutcome.KNOWN_FAILURE))
        self.assertEqual(vm.names().count("replan"), 1)

    def test_f7_uncoded_observe_failure_is_unknown_without_adding_guards(self) -> None:
        vm = FakeVm()
        vm.failures["observe"] = TimeoutError(SECRET_MARKER)
        bridge = VmBridge(vm, surface_dict())
        with self.assertRaises(VmBridgeError) as raised:
            bridge.observe(ObserveRequest("observe-1", "project-1"))
        self.assertEqual((raised.exception.code, raised.exception.outcome, raised.exception.subject),
                         (BridgeCode.OUTCOME_UNKNOWN, EffectOutcome.UNKNOWN, "observe"))
        self.assertNotIn(SECRET_MARKER, str(raised.exception) + repr(raised.exception))
        self.assertIsNone(raised.exception.__context__)
        self.assertEqual((bridge.unresolved_attempts, bridge.unresolved_projects), (frozenset(), frozenset()))
        # A VM-coded precondition rejection keeps the query classification.
        vm.failures["observe"] = CodedError("RUNTIME_REQUIRED", SECRET_MARKER)
        with self.assertRaises(VmBridgeError) as coded:
            bridge.observe(ObserveRequest("observe-2", "project-1"))
        self.assertEqual((coded.exception.code, coded.exception.vm_code, coded.exception.outcome),
                         (BridgeCode.VM_REJECTED, "RUNTIME_REQUIRED", EffectOutcome.KNOWN_FAILURE))
        self.assertEqual(bridge.unresolved_projects, frozenset())

    def test_f6_coded_failures_of_effects_are_unknown_unless_known_pre_effect(self) -> None:
        vm = FakeVm()
        vm.failures["pause"] = CodedError("INDETERMINATE_RESULT", SECRET_MARKER)
        vm.failures["status"] = CodedError("TIMEOUT", SECRET_MARKER)
        bridge = VmBridge(vm, surface_dict())
        with self.assertRaises(VmBridgeError) as effect:
            bridge.pause(PauseRequest("req-1", "project-1", "stop"))
        self.assertEqual((effect.exception.code, effect.exception.vm_code, effect.exception.outcome),
                         (BridgeCode.VM_REJECTED, "INDETERMINATE_RESULT", EffectOutcome.UNKNOWN))
        self.assertIn("project-1", bridge.unresolved_projects)
        with self.assertRaises(VmBridgeError) as query:
            bridge.status(ProjectQuery("req-2", "project-1"))
        self.assertEqual(query.exception.outcome, EffectOutcome.KNOWN_FAILURE)

    def test_malformed_responses_and_foreign_project_are_rejected(self) -> None:
        vm = FakeVm()
        vm.responses["status"] = ["not", "mapping"]
        vm.responses["prepare"] = {"project_id": "project-other"}
        bridge = VmBridge(vm, surface_dict())
        with self.assertRaises(VmBridgeError) as query:
            bridge.status(ProjectQuery("req-1", "project-1"))
        self.assertEqual((query.exception.code, query.exception.outcome),
                         (BridgeCode.INVALID_RESPONSE, EffectOutcome.KNOWN_FAILURE))
        with self.assertRaises(VmBridgeError) as command:
            bridge.prepare(GoalRequest("req-2", "project-1", "goal"))
        self.assertEqual((command.exception.code, command.exception.outcome),
                         (BridgeCode.CONTRACT_MISMATCH, EffectOutcome.UNKNOWN))

    def test_malformed_history_cursor_or_digest_responses_are_rejected(self) -> None:
        for cursor in (-1, "11", True):
            with self.subTest(cursor=cursor):
                vm = FakeVm()
                vm.responses["status"] = {"project_id": "project-1", "history_cursor": cursor}
                with self.assertRaises(VmBridgeError) as raised:
                    VmBridge(vm, surface_dict()).status(ProjectQuery("req-1", "project-1"))
                self.assertEqual((raised.exception.code, raised.exception.subject),
                                 (BridgeCode.INVALID_RESPONSE, "history_cursor"))
        vm = FakeVm()
        vm.responses["attempt_detail"] = {"attempt_id": "attempt-1", "execution_spec_digest": "sha256:bad"}
        with self.assertRaises(VmBridgeError) as raised:
            VmBridge(vm, surface_dict()).attempt_detail(AttemptQuery("req-1", "attempt-1"))
        self.assertEqual((raised.exception.code, raised.exception.subject),
                         (BridgeCode.INVALID_RESPONSE, "execution_spec_digest"))

    def test_duplicate_request_id_and_invalid_requests_are_refused_locally(self) -> None:
        vm = FakeVm()
        bridge = VmBridge(vm, surface_dict())
        bridge.status(ProjectQuery("req-1", "project-1"))
        with self.assertRaises(VmBridgeError) as duplicate:
            bridge.status(ProjectQuery("req-1", "project-1"))
        self.assertIs(duplicate.exception.code, BridgeCode.DUPLICATE_REQUEST)
        self.assertEqual(vm.names().count("status"), 1)
        for build in (lambda: ProjectQuery("req 2", "project-1"),
                      lambda: GoalRequest("req-3", "project-1", ""),
                      lambda: GoalRequest("req-3", "project-1", "goal", candidate_count=0)):
            with self.assertRaises(VmBridgeError) as invalid:
                build()
            self.assertIs(invalid.exception.code, BridgeCode.INVALID_REQUEST)

    def test_attempt_detail_query_rejects_a_different_attempt(self) -> None:
        vm = FakeVm()
        vm.responses["attempt_detail"] = {"attempt_id": "attempt-2", "task_id": "task-1"}
        with self.assertRaises(VmBridgeError) as raised:
            VmBridge(vm, surface_dict()).attempt_detail(AttemptQuery("req-1", "attempt-1"))
        self.assertIs(raised.exception.code, BridgeCode.CONTRACT_MISMATCH)


class JobStatusTests(unittest.TestCase):
    """Job phases follow the real upstream RuntimeJobStatus values (32bb0f9)."""

    EXPECTED = {
        "scheduled": JobPhase.PENDING, "running": JobPhase.RUNNING, "interrupting": JobPhase.RUNNING,
        "provider_terminal": JobPhase.TERMINAL, "collector_lost": JobPhase.UNKNOWN,
        "consumed": JobPhase.TERMINAL, "cancelled": JobPhase.UNKNOWN,
    }

    def observe_with(self, job: dict):
        vm = FakeVm()
        vm.responses["observe"] = {"project_id": "project-1", **job}
        return VmBridge(vm, surface_dict()).observe(ObserveRequest("observe-1", "project-1"))

    def test_every_real_runtime_job_status_is_classified(self) -> None:
        self.assertEqual(set(JOB_STATUS_PHASES), set(REAL_JOB_STATUSES))
        self.assertEqual(dict(JOB_STATUS_PHASES), self.EXPECTED)
        for status, phase in self.EXPECTED.items():
            with self.subTest(status=status):
                receipt = self.observe_with({"job": {"job_id": "job-1", "status": status}})
                self.assertEqual((receipt.job.phase, receipt.job.vm_status), (phase, status))

    def test_previously_assumed_status_strings_are_now_unknown(self) -> None:
        for status in ("queued", "pending", "reserved", "prepared", "started", "in_progress", "succeeded",
                       "completed", "failed", "interrupted", "proven_no_effect", "expired"):
            with self.subTest(status=status):
                receipt = self.observe_with({"job": {"job_id": "job-1", "status": status}})
                self.assertEqual(receipt.job.phase, JobPhase.UNKNOWN)

    def test_provider_terminal_is_terminal_but_not_core_consumption(self) -> None:
        terminal = self.observe_with({"job": {"job_id": "job-1", "status": "provider_terminal"}})
        self.assertEqual(terminal.job.phase, JobPhase.TERMINAL)
        self.assertFalse(terminal.job.core_consumed)
        consumed = self.observe_with({"job": {"job_id": "job-1", "status": "consumed"}})
        self.assertTrue(consumed.job.core_consumed)

    def test_runtime_job_dump_and_flat_run_once_fields_are_read(self) -> None:
        receipt = self.observe_with({"runtime_job": {"job_id": "job-3", "status": "interrupting"}})
        self.assertEqual((receipt.job.phase, receipt.job.job_id), (JobPhase.RUNNING, "job-3"))
        vm = BoundFakeVm()
        vm.responses["run_once"] = {"project_id": "project-1", "task_id": "task-1", "attempt_id": "attempt-1",
                                    "runtime_job_id": "job-2", "runtime_job_kind": "worker_turn",
                                    "runtime_job_status": "scheduled"}
        bridge = VmBridge(vm, surface_dict(), now=lambda: NOW)
        run = bridge.run_once(run_request())
        self.assertEqual((run.job.phase, run.job.vm_status, run.job.job_id),
                         (JobPhase.PENDING, "scheduled", "job-2"))
        self.assertEqual(bridge.unresolved_attempts, frozenset())

    def test_cancelled_or_collector_lost_observation_keeps_the_attempt_unresolved(self) -> None:
        for status in ("cancelled", "collector_lost"):
            with self.subTest(status=status):
                vm = BoundFakeVm()
                vm.failures["run_once"] = ConnectionResetError(SECRET_MARKER)
                bridge = VmBridge(vm, surface_dict(), now=lambda: NOW)
                with self.assertRaises(VmBridgeError):
                    bridge.run_once(run_request("run-1"))
                del vm.failures["run_once"]
                vm.responses["observe"] = {"project_id": "project-1", "attempt_id": "attempt-1",
                                           "job": {"job_id": "job-1", "status": status}}
                binding = TaskExecutionBinding(**binding_fields())
                observed = bridge.observe(ObserveRequest("observe-1", "project-1", binding))
                self.assertEqual(observed.job.phase, JobPhase.UNKNOWN)
                self.assertIn("attempt-1", bridge.unresolved_attempts)
                with self.assertRaises(VmBridgeError) as blocked:
                    bridge.run_once(run_request("run-2"))
                self.assertIs(blocked.exception.code, BridgeCode.RECONCILIATION_REQUIRED)
                self.assertEqual(vm.names().count("run_once"), 1)


class BindingTests(unittest.TestCase):
    def test_binding_matches_schema_fields_and_has_a_stable_digest(self) -> None:
        binding = TaskExecutionBinding.from_mapping(binding_fields())
        self.assertEqual(tuple(binding.to_mapping()), BINDING_FIELDS)
        self.assertEqual(len(BINDING_FIELDS), 24)
        self.assertEqual(binding.digest(), canonical_digest(binding_fields()))
        self.assertNotEqual(binding.digest(),
                            TaskExecutionBinding(**binding_fields(connection_revision="8")).digest())

    def test_invalid_bindings_are_rejected(self) -> None:
        missing = binding_fields()
        del missing["lease_id"]
        cases = (missing, {**binding_fields(), "raw_secret": SECRET_MARKER},
                 binding_fields(vm_execution_spec_digest="sha256:XYZ"),
                 binding_fields(lease_fencing_token=0), binding_fields(lease_fencing_token=True),
                 binding_fields(auth_kind="password"), binding_fields(workspace_root_ref="workspace://local/../x"),
                 binding_fields(vm_task_id=""))
        for fields in cases:
            with self.subTest(fields=sorted(set(fields) ^ set(BINDING_FIELDS)) or "value"):
                with self.assertRaises(VmBridgeError) as raised:
                    TaskExecutionBinding.from_mapping(fields)
                self.assertIs(raised.exception.code, BridgeCode.INVALID_REQUEST)
                self.assertNotIn(SECRET_MARKER, str(raised.exception))

    def test_assignment_must_match_aik_placement_lease_and_pool(self) -> None:
        binding = TaskExecutionBinding(**binding_fields())
        placement, lease, pool = assignment()
        verify_assignment(binding, placement=placement, lease=lease, pool=pool, now=lambda: NOW)
        _, expired, _ = assignment(expires_at="2029-01-01T00:00:00Z")
        _, released, _ = assignment(state="released")
        _, fenced, _ = assignment(fencing_token=6)
        cases = (
            (dict(lease=expired), BridgeCode.LEASE_NOT_USABLE, "lease_expired"),
            (dict(lease=released), BridgeCode.LEASE_NOT_USABLE, "lease_state"),
            (dict(lease=fenced), BridgeCode.BINDING_MISMATCH, "lease_fencing_token"),
            (dict(pool=Pool("pool-1", "synthetic", "connection-2", enabled=True)), BridgeCode.BINDING_MISMATCH, "connection_id"),
            (dict(pool=Pool("pool-1", "synthetic", "connection-1")), BridgeCode.LEASE_NOT_USABLE, "pool_enabled"),
        )
        for override, code, subject in cases:
            arguments = dict(placement=placement, lease=lease, pool=pool, **{})
            arguments.update(override)
            with self.subTest(subject=subject):
                with self.assertRaises(VmBridgeError) as raised:
                    verify_assignment(binding, now=lambda: NOW, **arguments)
                self.assertEqual((raised.exception.code, raised.exception.subject), (code, subject))
        with self.assertRaises(VmBridgeError):
            verify_assignment(TaskExecutionBinding(**binding_fields(vm_attempt_id="attempt-2")),
                              placement=placement, lease=lease, pool=pool, now=lambda: NOW)

    def test_assignment_attempt_mismatch_reports_code_and_subject(self) -> None:
        placement, lease, pool = assignment()
        with self.assertRaises(VmBridgeError) as raised:
            verify_assignment(TaskExecutionBinding(**binding_fields(vm_attempt_id="attempt-2")),
                              placement=placement, lease=lease, pool=pool, now=lambda: NOW)
        self.assertEqual((raised.exception.code, raised.exception.subject, raised.exception.outcome),
                         (BridgeCode.BINDING_MISMATCH, "vm_attempt_id", EffectOutcome.KNOWN_FAILURE))


class BoundExecutionTests(unittest.TestCase):
    def test_run_once_is_refused_when_vm_lacks_the_binding_seam(self) -> None:
        vm = FakeVm()
        with self.assertRaises(VmBridgeError) as raised:
            VmBridge(vm, surface_dict(), now=lambda: NOW).run_once(run_request())
        self.assertEqual((raised.exception.code, raised.exception.subject),
                         (BridgeCode.SEAM_UNAVAILABLE, "aik-bound-runtime"))
        self.assertEqual(vm.names(), ["handshake"])

    def test_declared_extension_without_port_methods_is_seam_unavailable(self) -> None:
        vm = FakeVm(extensions=(BINDING_EXTENSION,))
        with self.assertRaises(VmBridgeError) as raised:
            VmBridge(vm, surface_dict(), now=lambda: NOW).run_once(run_request())
        self.assertIs(raised.exception.code, BridgeCode.SEAM_UNAVAILABLE)
        self.assertEqual(vm.names(), ["handshake"])

    def test_run_once_queries_owner_binds_then_reserves_and_returns_job_reference(self) -> None:
        vm = BoundFakeVm()
        receipt = VmBridge(vm, surface_dict(), now=lambda: NOW).run_once(run_request())
        self.assertEqual(vm.names(), ["handshake", "attempt_detail", "read_execution_binding",
                                      "bind_execution", "run_once"])
        self.assertEqual(vm.calls[4], ("run_once", ("project-1",), {"resume": False}))
        self.assertEqual(vm.calls[3][1][0], binding_fields())
        self.assertEqual((receipt.refs.task_id, receipt.refs.attempt_id), ("task-1", "attempt-1"))
        self.assertEqual((receipt.job.phase, receipt.job.vm_status, receipt.job.job_id),
                         (JobPhase.RUNNING, "running", "job-1"))
        self.assertEqual(receipt.binding_digest, canonical_digest(binding_fields()))

    def test_owner_attempt_mismatch_or_expired_lease_stops_before_any_effect(self) -> None:
        vm = BoundFakeVm()
        vm.responses["attempt_detail"] = {**vm.responses["attempt_detail"], "plan_revision_id": "plan-rev-2"}
        bridge = VmBridge(vm, surface_dict(), now=lambda: NOW)
        with self.assertRaises(VmBridgeError) as raised:
            bridge.run_once(run_request())
        self.assertEqual((raised.exception.code, raised.exception.subject, raised.exception.outcome),
                         (BridgeCode.BINDING_MISMATCH, "plan_revision_id", EffectOutcome.KNOWN_FAILURE))
        self.assertNotIn("bind_execution", vm.names())
        self.assertNotIn("run_once", vm.names())
        late = VmBridge(BoundFakeVm(), surface_dict(), now=lambda: datetime(2031, 1, 1, tzinfo=timezone.utc))
        with self.assertRaises(VmBridgeError) as expired:
            late.run_once(run_request())
        self.assertIs(expired.exception.code, BridgeCode.LEASE_NOT_USABLE)

    def test_f1_owner_query_with_missing_binding_fields_stops_before_any_effect(self) -> None:
        full = BoundFakeVm().responses["attempt_detail"]
        for field in ("project_id", "goal_revision_id", "plan_revision_id", "execution_spec_digest"):
            with self.subTest(missing=field):
                vm = BoundFakeVm()
                vm.responses["attempt_detail"] = {k: v for k, v in full.items() if k != field}
                with self.assertRaises(VmBridgeError) as raised:
                    VmBridge(vm, surface_dict(), now=lambda: NOW).run_once(run_request())
                self.assertEqual((raised.exception.code, raised.exception.subject, raised.exception.outcome),
                                 (BridgeCode.INVALID_RESPONSE, field, EffectOutcome.KNOWN_FAILURE))
                self.assertEqual(vm.names(), ["handshake", "attempt_detail"])
        vm = BoundFakeVm()
        vm.responses["attempt_detail"] = {"attempt_id": "attempt-1", "task_id": "task-1"}
        bridge = VmBridge(vm, surface_dict(), now=lambda: NOW)
        with self.assertRaises(VmBridgeError) as raised:
            bridge.run_once(run_request())
        self.assertIs(raised.exception.code, BridgeCode.INVALID_RESPONSE)
        self.assertNotIn("bind_execution", vm.names())
        self.assertNotIn("run_once", vm.names())
        self.assertEqual(bridge.unresolved_attempts, frozenset())

    def test_f2_run_once_response_without_task_or_attempt_is_unknown_and_blocks_relaunch(self) -> None:
        responses = (
            {"project_id": "project-1", "job": {"job_id": "job-7", "status": "running"}},
            {"project_id": "project-1", "attempt_id": "attempt-1", "job": {"job_id": "job-7", "status": "running"}},
            {"project_id": "project-1", "task_id": "task-1", "job": {"job_id": "job-7", "status": "running"}},
        )
        for response in responses:
            with self.subTest(keys=sorted(response)):
                vm = BoundFakeVm()
                vm.responses["run_once"] = response
                bridge = VmBridge(vm, surface_dict(), now=lambda: NOW)
                with self.assertRaises(VmBridgeError) as raised:
                    bridge.run_once(run_request("run-1"))
                self.assertEqual((raised.exception.code, raised.exception.outcome),
                                 (BridgeCode.INVALID_RESPONSE, EffectOutcome.UNKNOWN))
                self.assertIn("attempt-1", bridge.unresolved_attempts)
                with self.assertRaises(VmBridgeError) as blocked:
                    bridge.run_once(run_request("run-2"))
                self.assertIs(blocked.exception.code, BridgeCode.RECONCILIATION_REQUIRED)
                self.assertEqual(vm.names().count("run_once"), 1)

    def test_f3_rotated_binding_cannot_rebind_or_relaunch_a_bound_attempt(self) -> None:
        vm = BoundFakeVm()
        bridge = VmBridge(vm, surface_dict(), now=lambda: NOW)
        bridge.run_once(run_request("run-1"))
        with self.assertRaises(VmBridgeError) as raised:
            bridge.run_once(run_request("run-2", connection_revision="8"))
        self.assertEqual((raised.exception.code, raised.exception.subject, raised.exception.outcome),
                         (BridgeCode.BINDING_MISMATCH, "persisted_binding", EffectOutcome.KNOWN_FAILURE))
        self.assertEqual(vm.names().count("bind_execution"), 1)
        self.assertEqual(vm.names().count("run_once"), 1)
        self.assertEqual(vm.persisted["attempt-1"], canonical_digest(binding_fields()))
        original = TaskExecutionBinding(**binding_fields())
        cancelled = bridge.cancel(CancelRequest("cancel-1", "project-1", "stop", original))
        self.assertEqual(cancelled.binding_digest, original.digest())

    def test_f3_same_binding_requires_a_terminal_observation_before_relaunch(self) -> None:
        vm = BoundFakeVm()
        bridge = VmBridge(vm, surface_dict(), now=lambda: NOW)
        bridge.run_once(run_request("run-1"))
        for resume in (False, True):
            with self.subTest(resume=resume):
                placement, lease, pool = assignment()
                request = RunOnceRequest(f"run-again-{resume}", TaskExecutionBinding(**binding_fields()),
                                         placement, lease, pool, resume=resume)
                with self.assertRaises(VmBridgeError) as raised:
                    bridge.run_once(request)
                self.assertEqual((raised.exception.code, raised.exception.subject, raised.exception.outcome),
                                 (BridgeCode.RECONCILIATION_REQUIRED, "attempt_bound", EffectOutcome.KNOWN_FAILURE))
        self.assertEqual(vm.names().count("run_once"), 1)
        self.assertEqual(vm.names().count("bind_execution"), 1)
        bridge.observe(ObserveRequest("observe-1", "project-1", TaskExecutionBinding(**binding_fields())))
        bridge.run_once(run_request("run-3"))
        self.assertEqual(vm.names().count("run_once"), 2)
        self.assertEqual(vm.names().count("bind_execution"), 1)

    def test_review_f1_provider_terminal_observation_never_permits_relaunch(self) -> None:
        binding = TaskExecutionBinding(**binding_fields())
        # Launched job still bound: provider_terminal is not Core consumption.
        vm = BoundFakeVm()
        bridge = VmBridge(vm, surface_dict(), now=lambda: NOW)
        bridge.run_once(run_request("run-1"))
        vm.responses["observe"] = observed_job("job-1", "provider_terminal")
        observed = bridge.observe(ObserveRequest("observe-1", "project-1", binding))
        self.assertEqual((observed.job.phase, observed.job.core_consumed), (JobPhase.TERMINAL, False))
        with self.assertRaises(VmBridgeError) as raised:
            bridge.run_once(run_request("run-2"))
        self.assertEqual((raised.exception.code, raised.exception.subject),
                         (BridgeCode.RECONCILIATION_REQUIRED, "attempt_bound"))
        self.assertEqual(vm.names().count("run_once"), 1)
        # An UNKNOWN Attempt stays unresolved after provider_terminal as well.
        vm = BoundFakeVm()
        vm.failures["run_once"] = ConnectionResetError(SECRET_MARKER)
        bridge = VmBridge(vm, surface_dict(), now=lambda: NOW)
        with self.assertRaises(VmBridgeError):
            bridge.run_once(run_request("run-1"))
        del vm.failures["run_once"]
        vm.responses["observe"] = observed_job("job-1", "provider_terminal")
        bridge.observe(ObserveRequest("observe-1", "project-1", binding))
        self.assertIn("attempt-1", bridge.unresolved_attempts)
        with self.assertRaises(VmBridgeError) as blocked:
            bridge.run_once(run_request("run-2"))
        self.assertEqual((blocked.exception.code, blocked.exception.subject),
                         (BridgeCode.RECONCILIATION_REQUIRED, "vm_attempt_id"))
        self.assertEqual(vm.names().count("run_once"), 1)
        vm.responses["observe"] = observed_job("job-1", "consumed")
        bridge.observe(ObserveRequest("observe-2", "project-1", binding))
        self.assertEqual(bridge.unresolved_attempts, frozenset())
        bridge.run_once(run_request("run-3"))
        self.assertEqual(vm.names().count("run_once"), 2)

    def test_review_f2_unrelated_job_observation_does_not_clear_a_project_unknown(self) -> None:
        vm = BoundFakeVm()
        vm.failures["authorize"] = CodedError("EXTERNAL_UNKNOWN", SECRET_MARKER)
        bridge = VmBridge(vm, surface_dict(), now=lambda: NOW)
        with self.assertRaises(VmBridgeError):
            bridge.authorize(AuthorizeRequest("auth-1", "project-1", "gui"))
        for status in ("provider_terminal", "consumed"):
            with self.subTest(status=status):
                vm.responses["observe"] = observed_job("job-7", status, attempt_id="attempt-7")
                bridge.observe(ObserveRequest(f"observe-{status}", "project-1"))
                self.assertEqual(bridge.unresolved_projects, frozenset({"project-1"}))
        with self.assertRaises(VmBridgeError) as blocked:
            bridge.run_once(run_request("run-1"))
        self.assertEqual((blocked.exception.code, blocked.exception.subject),
                         (BridgeCode.RECONCILIATION_REQUIRED, "vm_project_id"))
        self.assertNotIn("run_once", vm.names())
        with self.assertRaises(VmBridgeError) as invalid:
            bridge.reconcile_project("project 1")
        self.assertIs(invalid.exception.code, BridgeCode.INVALID_REQUEST)
        bridge.reconcile_project("project-1")
        self.assertEqual(bridge.unresolved_projects, frozenset())
        bridge.run_once(run_request("run-1"))
        self.assertEqual(vm.names().count("run_once"), 1)

    def test_review_f3_consumed_older_job_does_not_clear_a_newer_unknown_launch(self) -> None:
        vm = BoundFakeVm()
        binding = TaskExecutionBinding(**binding_fields())
        bridge = VmBridge(vm, surface_dict(), now=lambda: NOW)
        bridge.run_once(run_request("run-1"))
        bridge.observe(ObserveRequest("observe-1", "project-1", binding))  # job-1 consumed
        vm.responses["run_once"] = {"project_id": "project-1", "task_id": "task-1", "attempt_id": "attempt-1",
                                    "job": {"job_id": "job-2", "status": "mystery_state"}}
        bridge.run_once(run_request("run-2"))
        self.assertIn("attempt-1", bridge.unresolved_attempts)
        # The VM still reports the older consumed job-1.
        bridge.observe(ObserveRequest("observe-2", "project-1", binding))
        self.assertIn("attempt-1", bridge.unresolved_attempts)
        with self.assertRaises(VmBridgeError) as blocked:
            bridge.run_once(run_request("run-3"))
        self.assertIs(blocked.exception.code, BridgeCode.RECONCILIATION_REQUIRED)
        self.assertEqual(vm.names().count("run_once"), 2)
        vm.responses["observe"] = observed_job("job-2", "consumed")
        bridge.observe(ObserveRequest("observe-3", "project-1", binding))
        self.assertEqual(bridge.unresolved_attempts, frozenset())
        bridge.run_once(run_request("run-4"))
        self.assertEqual(vm.names().count("run_once"), 3)

    def test_review_f3_observation_overlapping_a_new_launch_does_not_release_it(self) -> None:
        class InterleavingVm(BoundFakeVm):
            def __init__(self):
                super().__init__()
                self.during_observe = None

            def observe(self, project_id):
                hook, self.during_observe = self.during_observe, None
                if hook is not None:
                    hook()
                return super().observe(project_id)

        vm = InterleavingVm()
        binding = TaskExecutionBinding(**binding_fields())
        bridge = VmBridge(vm, surface_dict(), now=lambda: NOW)
        bridge.run_once(run_request("run-1"))
        bridge.observe(ObserveRequest("observe-1", "project-1", binding))  # job-1 consumed

        def relaunch_with_unclear_result():
            vm.failures["run_once"] = ConnectionResetError(SECRET_MARKER)
            try:
                with self.assertRaises(VmBridgeError):
                    bridge.run_once(run_request("run-2"))
            finally:
                del vm.failures["run_once"]

        vm.during_observe = relaunch_with_unclear_result
        # This observation started before run-2 and returns the stale job-1.
        bridge.observe(ObserveRequest("observe-2", "project-1", binding))
        self.assertIn("attempt-1", bridge.unresolved_attempts)
        with self.assertRaises(VmBridgeError) as blocked:
            bridge.run_once(run_request("run-3"))
        self.assertEqual((blocked.exception.code, blocked.exception.subject),
                         (BridgeCode.RECONCILIATION_REQUIRED, "vm_attempt_id"))
        self.assertEqual(vm.names().count("run_once"), 2)

    def test_read_execution_binding_failure_or_malformed_stops_before_binding(self) -> None:
        cases = (
            ("raise", OSError(SECRET_MARKER), BridgeCode.QUERY_FAILED),
            ("non-mapping", ["not", "mapping"], BridgeCode.INVALID_RESPONSE),
            ("bad-digest", {"binding_digest": "sha256:bad"}, BridgeCode.INVALID_RESPONSE),
        )
        for label, value, code in cases:
            with self.subTest(case=label):
                vm = BoundFakeVm()
                if label == "raise":
                    vm.failures["read_execution_binding"] = value
                else:
                    vm.read_override = value
                bridge = VmBridge(vm, surface_dict(), now=lambda: NOW)
                with self.assertRaises(VmBridgeError) as raised:
                    bridge.run_once(run_request())
                self.assertEqual((raised.exception.code, raised.exception.outcome),
                                 (code, EffectOutcome.KNOWN_FAILURE))
                self.assertNotIn(SECRET_MARKER, str(raised.exception))
                self.assertNotIn("bind_execution", vm.names())
                self.assertNotIn("run_once", vm.names())
                self.assertEqual(bridge.unresolved_attempts, frozenset())

    def test_bind_execution_echo_mismatch_is_unknown_and_blocks_relaunch(self) -> None:
        vm = BoundFakeVm()
        vm.bind_echo = DIGEST_A
        bridge = VmBridge(vm, surface_dict(), now=lambda: NOW)
        with self.assertRaises(VmBridgeError) as raised:
            bridge.run_once(run_request())
        self.assertEqual((raised.exception.code, raised.exception.subject, raised.exception.outcome),
                         (BridgeCode.BINDING_MISMATCH, "bind_execution", EffectOutcome.UNKNOWN))
        self.assertNotIn("run_once", vm.names())
        self.assertIn("attempt-1", bridge.unresolved_attempts)

    def test_unclear_run_once_is_unknown_never_relaunched_until_observed(self) -> None:
        vm = BoundFakeVm()
        vm.failures["run_once"] = ConnectionResetError(SECRET_MARKER)
        bridge = VmBridge(vm, surface_dict(), now=lambda: NOW)
        with self.assertRaises(VmBridgeError) as raised:
            bridge.run_once(run_request("run-1"))
        self.assertEqual((raised.exception.code, raised.exception.outcome),
                         (BridgeCode.OUTCOME_UNKNOWN, EffectOutcome.UNKNOWN))
        self.assertEqual(vm.names().count("run_once"), 1)
        self.assertEqual(bridge.unresolved_attempts, frozenset({"attempt-1"}))
        del vm.failures["run_once"]
        with self.assertRaises(VmBridgeError) as blocked:
            bridge.run_once(run_request("run-2"))
        self.assertIs(blocked.exception.code, BridgeCode.RECONCILIATION_REQUIRED)
        self.assertEqual(vm.names().count("run_once"), 1)
        binding = TaskExecutionBinding(**binding_fields())
        observed = bridge.observe(ObserveRequest("observe-1", "project-1", binding))
        self.assertEqual(observed.job.phase, JobPhase.TERMINAL)
        self.assertEqual(bridge.unresolved_attempts, frozenset())
        bridge.run_once(run_request("run-3"))
        self.assertEqual(vm.names().count("run_once"), 2)

    def test_f5_running_observation_keeps_the_attempt_unresolved(self) -> None:
        vm = BoundFakeVm()
        vm.failures["run_once"] = ConnectionResetError(SECRET_MARKER)
        bridge = VmBridge(vm, surface_dict(), now=lambda: NOW)
        with self.assertRaises(VmBridgeError):
            bridge.run_once(run_request("run-1"))
        del vm.failures["run_once"]
        vm.responses["observe"] = {"project_id": "project-1", "attempt_id": "attempt-1",
                                   "job": {"job_id": "job-1", "status": "running"}}
        binding = TaskExecutionBinding(**binding_fields())
        observed = bridge.observe(ObserveRequest("observe-1", "project-1", binding))
        self.assertEqual(observed.job.phase, JobPhase.RUNNING)
        self.assertIn("attempt-1", bridge.unresolved_attempts)
        with self.assertRaises(VmBridgeError) as blocked:
            bridge.run_once(run_request("run-2"))
        self.assertIs(blocked.exception.code, BridgeCode.RECONCILIATION_REQUIRED)
        self.assertEqual(vm.names().count("run_once"), 1)
        # provider_terminal is not Core consumption: the guard stays.
        vm.responses["observe"] = {"project_id": "project-1", "attempt_id": "attempt-1",
                                   "job": {"job_id": "job-1", "status": "provider_terminal"}}
        bridge.observe(ObserveRequest("observe-2", "project-1", binding))
        self.assertIn("attempt-1", bridge.unresolved_attempts)
        with self.assertRaises(VmBridgeError) as still_blocked:
            bridge.run_once(run_request("run-3"))
        self.assertIs(still_blocked.exception.code, BridgeCode.RECONCILIATION_REQUIRED)
        self.assertEqual(vm.names().count("run_once"), 1)
        vm.responses["observe"] = {"project_id": "project-1", "attempt_id": "attempt-1",
                                   "job": {"job_id": "job-1", "status": "consumed"}}
        bridge.observe(ObserveRequest("observe-3", "project-1", binding))
        self.assertEqual(bridge.unresolved_attempts, frozenset())
        bridge.run_once(run_request("run-4"))
        self.assertEqual(vm.names().count("run_once"), 2)

    def test_f6_coded_transport_error_from_run_once_is_unknown_and_blocks_relaunch(self) -> None:
        vm = BoundFakeVm()
        vm.failures["run_once"] = CodedError("ECONNRESET", SECRET_MARKER)
        bridge = VmBridge(vm, surface_dict(), now=lambda: NOW)
        with self.assertRaises(VmBridgeError) as raised:
            bridge.run_once(run_request("run-1"))
        self.assertEqual((raised.exception.code, raised.exception.vm_code, raised.exception.outcome),
                         (BridgeCode.VM_REJECTED, "ECONNRESET", EffectOutcome.UNKNOWN))
        self.assertNotIn(SECRET_MARKER, str(raised.exception))
        self.assertIn("attempt-1", bridge.unresolved_attempts)
        del vm.failures["run_once"]
        with self.assertRaises(VmBridgeError) as blocked:
            bridge.run_once(run_request("run-2"))
        self.assertIs(blocked.exception.code, BridgeCode.RECONCILIATION_REQUIRED)
        self.assertEqual(vm.names().count("run_once"), 1)

    def test_f4_project_level_unknown_blocks_run_once_until_explicit_reconciliation(self) -> None:
        vm = BoundFakeVm()
        vm.failures["pause"] = CodedError("EXTERNAL_UNKNOWN", SECRET_MARKER)
        bridge = VmBridge(vm, surface_dict(), now=lambda: NOW)
        with self.assertRaises(VmBridgeError):
            bridge.pause(PauseRequest("pause-1", "project-1", "stop"))
        self.assertEqual(bridge.unresolved_projects, frozenset({"project-1"}))
        with self.assertRaises(VmBridgeError) as blocked:
            bridge.run_once(run_request("run-1"))
        self.assertEqual((blocked.exception.code, blocked.exception.subject),
                         (BridgeCode.RECONCILIATION_REQUIRED, "vm_project_id"))
        self.assertEqual(vm.names(), ["handshake", "pause"])
        bridge.observe(ObserveRequest("observe-1", "project-1"))
        self.assertEqual(bridge.unresolved_projects, frozenset({"project-1"}))
        with self.assertRaises(VmBridgeError) as still_blocked:
            bridge.run_once(run_request("run-1"))
        self.assertEqual((still_blocked.exception.code, still_blocked.exception.subject),
                         (BridgeCode.RECONCILIATION_REQUIRED, "vm_project_id"))
        bridge.reconcile_project("project-1")
        self.assertEqual(bridge.unresolved_projects, frozenset())
        bridge.run_once(run_request("run-1"))
        self.assertEqual(vm.names().count("run_once"), 1)

    def test_f4_unbound_cancel_unknown_after_bound_run_blocks_run_once(self) -> None:
        vm = BoundFakeVm()
        bridge = VmBridge(vm, surface_dict(), now=lambda: NOW)
        bridge.run_once(run_request("run-1"))
        vm.failures["cancel"] = CodedError("EXTERNAL_UNKNOWN", SECRET_MARKER)
        with self.assertRaises(VmBridgeError) as raised:
            bridge.cancel(CancelRequest("cancel-1", "project-1", "stop"))
        self.assertEqual(raised.exception.outcome, EffectOutcome.UNKNOWN)
        self.assertIn("project-1", bridge.unresolved_projects)
        with self.assertRaises(VmBridgeError) as blocked:
            bridge.run_once(run_request("run-2"))
        self.assertEqual((blocked.exception.code, blocked.exception.subject),
                         (BridgeCode.RECONCILIATION_REQUIRED, "vm_project_id"))
        self.assertEqual(vm.names().count("run_once"), 1)

    def test_f8_local_pre_rejection_does_not_consume_the_request_id(self) -> None:
        # Pins the documented behavior: rejections before the first port call
        # of the operation leave the local request ID available.
        vm = BoundFakeVm()
        clock = [datetime(2031, 1, 1, tzinfo=timezone.utc)]
        bridge = VmBridge(vm, surface_dict(), now=lambda: clock[0])
        with self.assertRaises(VmBridgeError) as rejected:
            bridge.run_once(run_request("run-1"))
        self.assertIs(rejected.exception.code, BridgeCode.LEASE_NOT_USABLE)
        self.assertEqual(vm.names(), ["handshake"])
        clock[0] = NOW
        bridge.run_once(run_request("run-1"))
        self.assertEqual(vm.names().count("run_once"), 1)
        with self.assertRaises(VmBridgeError) as duplicate:
            bridge.status(ProjectQuery("run-1", "project-1"))
        self.assertIs(duplicate.exception.code, BridgeCode.DUPLICATE_REQUEST)

    def test_f10_concurrent_run_once_for_the_same_attempt_is_refused(self) -> None:
        class BlockingVm(BoundFakeVm):
            def __init__(self):
                super().__init__()
                self.entered = threading.Event()
                self.release = threading.Event()
                self.block = True

            def attempt_detail(self, attempt_id):
                if self.block:
                    self.block = False
                    self.entered.set()
                    self.release.wait(5)
                return super().attempt_detail(attempt_id)

        vm = BlockingVm()
        bridge = VmBridge(vm, surface_dict(), now=lambda: NOW)
        bridge.handshake()
        outcome: list = []

        def first():
            try:
                outcome.append(bridge.run_once(run_request("run-1")))
            except Exception as error:  # pragma: no cover - reported below
                outcome.append(error)

        thread = threading.Thread(target=first)
        thread.start()
        try:
            self.assertTrue(vm.entered.wait(5), "first run_once did not reach the owner query")
            with self.assertRaises(VmBridgeError) as raised:
                bridge.run_once(run_request("run-2"))
            self.assertEqual((raised.exception.code, raised.exception.subject),
                             (BridgeCode.RECONCILIATION_REQUIRED, "attempt_in_flight"))
        finally:
            vm.release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertNotIsInstance(outcome[0], Exception)
        self.assertEqual(vm.names().count("run_once"), 1)
        self.assertEqual(vm.names().count("bind_execution"), 1)

    def test_unrecognized_job_status_is_unknown(self) -> None:
        vm = BoundFakeVm()
        vm.responses["run_once"] = {"project_id": "project-1", "task_id": "task-1", "attempt_id": "attempt-1",
                                    "job": {"job_id": "job-1", "status": "mystery_state"}}
        bridge = VmBridge(vm, surface_dict(), now=lambda: NOW)
        receipt = bridge.run_once(run_request())
        self.assertEqual((receipt.job.phase, receipt.job.vm_status), (JobPhase.UNKNOWN, "mystery_state"))
        self.assertIn("attempt-1", bridge.unresolved_attempts)

    def test_run_once_response_for_another_attempt_is_a_binding_mismatch(self) -> None:
        vm = BoundFakeVm()
        vm.responses["run_once"] = {"project_id": "project-1", "attempt_id": "attempt-9",
                                    "job": {"job_id": "job-1", "status": "running"}}
        bridge = VmBridge(vm, surface_dict(), now=lambda: NOW)
        with self.assertRaises(VmBridgeError) as raised:
            bridge.run_once(run_request())
        self.assertEqual((raised.exception.code, raised.exception.outcome),
                         (BridgeCode.BINDING_MISMATCH, EffectOutcome.UNKNOWN))
        self.assertIn("attempt-1", bridge.unresolved_attempts)

    def test_cancel_observe_and_inventory_require_the_original_persisted_binding(self) -> None:
        vm = BoundFakeVm()
        bridge = VmBridge(vm, surface_dict(), now=lambda: NOW)
        bridge.run_once(run_request())
        rotated = TaskExecutionBinding(**binding_fields(connection_revision="8"))
        for call in (lambda: bridge.cancel(CancelRequest("cancel-1", "project-1", "stop", rotated)),
                     lambda: bridge.observe(ObserveRequest("observe-1", "project-1", rotated)),
                     lambda: bridge.inventory(InventoryRequest("inventory-1", rotated))):
            with self.assertRaises(VmBridgeError) as raised:
                call()
            self.assertEqual((raised.exception.code, raised.exception.subject),
                             (BridgeCode.BINDING_MISMATCH, "persisted_binding"))
        self.assertNotIn("cancel", vm.names())
        self.assertNotIn("observe", vm.names())
        original = TaskExecutionBinding(**binding_fields())
        inventory = bridge.inventory(InventoryRequest("inventory-2", original))
        self.assertEqual(inventory.payload["models"], ["synthetic-model"])
        cancelled = bridge.cancel(CancelRequest("cancel-2", "project-1", "synthetic stop", original))
        self.assertEqual(vm.calls[-1], ("cancel", ("project-1",), {"reason": "synthetic stop"}))
        self.assertEqual(cancelled.binding_digest, original.digest())

    def test_cancel_with_unclear_result_marks_attempt_unknown(self) -> None:
        vm = BoundFakeVm()
        bridge = VmBridge(vm, surface_dict(), now=lambda: NOW)
        bridge.run_once(run_request())
        vm.failures["cancel"] = CodedError("EXTERNAL_UNKNOWN", SECRET_MARKER)
        with self.assertRaises(VmBridgeError) as raised:
            bridge.cancel(CancelRequest("cancel-1", "project-1", "stop", TaskExecutionBinding(**binding_fields())))
        self.assertEqual(raised.exception.outcome, EffectOutcome.UNKNOWN)
        self.assertIn("attempt-1", bridge.unresolved_attempts)


if __name__ == "__main__":
    unittest.main()
