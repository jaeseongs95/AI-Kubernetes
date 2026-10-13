"""AIK-029 scoped tool gateway tests with synthetic data only.

No provider, credential, network, VM or AGS installation is used. The VM
effect authority is an injected fake. Linux success is development
verification, not Windows support evidence.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import copy
import json
import pickle
import sys
import threading
from types import MappingProxyType
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_authority import (  # noqa: E402
    AUDIT_CAPACITY, AuthorityCode, AuthorityDenied, AuthorityGateway, EffectKind, EffectOutcomeUnknown,
    InvalidPolicy, ToolCall, ToolDeclaration, ToolPolicy, ToolSession, is_reserved_tool, map_vm_authorization,
)
from aik_broker import Grant  # noqa: E402
from aik_profiles import DirectorProfile, ExecutionChoice, ExecutionDecision, RoleProfile  # noqa: E402
from aik_provider.contracts import AuthKind  # noqa: E402
from aik_runtime.errors import RevisionConflict  # noqa: E402
from aik_vm_bridge import BridgeReceipt, Command, TaskExecutionBinding, VmRefs  # noqa: E402


SECRET_MARKER = "SYNTHETIC_SECRET_MUST_NOT_APPEAR"
ALL_KINDS = (EffectKind.READ_ONLY, EffectKind.IDEMPOTENT, EffectKind.NON_IDEMPOTENT)
SCOPE_TOOLS = ("read_file", "write_file", "deploy", "publish", "call_provider")
DIRECTOR_PROFILE_TOOLS = ("read_file", "search", "write_file", "deploy", "publish", "call_provider")
TARGET = "workspace://ws-1/root/src/app.py"


def make_binding(**overrides) -> TaskExecutionBinding:
    values = dict(
        vm_instance_id="vm-instance-1", vm_project_id="project-1", vm_goal_revision_id="goal-rev-1",
        vm_plan_revision_id="plan-rev-1", vm_task_id="task-1", vm_attempt_id="attempt-1",
        vm_execution_spec_digest="sha256:" + "a" * 64, vm_runtime_intent_id="intent-1",
        vm_provider_call_id="provider-call-1", cluster_id="cluster-1", route_id="route-1",
        route_revision="route-rev-1", connection_id="conn-1", connection_revision="conn-rev-1",
        provider="synthetic", auth_kind=next(iter(AuthKind)).value, provider_account_fingerprint="fp-1",
        pool_id="pool-1", placement_id="placement-1", lease_id="lease-1", lease_fencing_token=1,
        support_matrix_digest="sha256:" + "b" * 64, input_manifest_digest="sha256:" + "c" * 64,
        workspace_root_ref="workspace://ws-1/root",
    )
    values.update(overrides)
    return TaskExecutionBinding(**values)


def make_decision(role: str, tools, *, workspace: str = "ws-1", bound: bool = True,
                  connection: str = "conn-1") -> ExecutionDecision:
    choice = ExecutionChoice(connection, "synthetic-model", None, tuple(tools))
    if role == "director":
        profile = DirectorProfile(f"profile-{role}", workspace, choice, 10)
    else:
        profile = RoleProfile(f"profile-{role}", workspace, role, choice, 10)
    return ExecutionDecision("bound" if bound else "rejected", profile, 1, choice if bound else None,
                             "primary" if bound else None, None, (), (), "2029-01-01T00:00:00Z")


def make_policy(*, director_extra=(), worker_extra=()) -> ToolPolicy:
    return ToolPolicy(
        "ws-1",
        (
            ToolDeclaration("read_file", EffectKind.READ_ONLY),
            ToolDeclaration("search", EffectKind.READ_ONLY),
            ToolDeclaration("write_file", EffectKind.IDEMPOTENT),
            ToolDeclaration("deploy", EffectKind.NON_IDEMPOTENT),
            ToolDeclaration("publish", EffectKind.NON_IDEMPOTENT),
            ToolDeclaration("call_provider", EffectKind.IDEMPOTENT, requires_credential=True),
        ),
        director_tools=("read_file", "search", "write_file", "deploy", "call_provider", *director_extra),
        worker_tools=("read_file", "search", "write_file", "call_provider", *worker_extra),
        reviewer_tools=("read_file", "search"),
    )


def authorize_receipt(**refs) -> BridgeReceipt:
    values = dict(project_id="project-1", plan_revision_id="plan-rev-1", authorization_id="vm-auth-1")
    values.update(refs)
    return BridgeReceipt(Command.AUTHORIZE, "req-auth-1", "project-1", VmRefs(**values), MappingProxyType({}))


def make_scope(*, effect_kinds=ALL_KINDS, tools=SCOPE_TOOLS):
    return map_vm_authorization(authorize_receipt(), workspace_id="ws-1", tools=tools,
                                targets=("workspace://ws-1/root",), effect_kinds=effect_kinds,
                                expires_at="2030-01-01T00:00:00Z")


class FakeClock:
    def __init__(self):
        self.value = datetime(2029, 6, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


class FakeLease:
    def __init__(self):
        self.active = True
        self.calls = []

    def __call__(self, lease_id, fencing_token):
        self.calls.append((lease_id, fencing_token))
        return self.active


class CodedError(Exception):
    def __init__(self, code):
        super().__init__("raw VM text " + SECRET_MARKER)
        self.code = code


class FakeEffectPort:
    def __init__(self):
        self.intents, self.receipts, self.effects = [], [], {}
        self.intent_error = None
        self.receipt_error = None
        self.read_error = None
        self.echo_digest = None
        self.receipt_state = "receipt_recorded"

    def record_intent(self, intent):
        self.intents.append(dict(intent))
        if self.intent_error is not None:
            raise self.intent_error
        self.effects[intent["effect_id"]] = {"effect_id": intent["effect_id"], "state": "intent_recorded"}
        return {"effect_id": intent["effect_id"], "request_digest": self.echo_digest or intent["request_digest"]}

    def record_receipt(self, receipt):
        self.receipts.append(dict(receipt))
        if self.receipt_error is not None:
            raise self.receipt_error
        return {"effect_id": receipt["effect_id"], "state": self.receipt_state}

    def read_effect(self, effect_id):
        if self.read_error is not None:
            raise self.read_error
        return self.effects.get(effect_id)


class AuthorityGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock, self.lease, self.port = FakeClock(), FakeLease(), FakeEffectPort()
        self.gateway = AuthorityGateway(lease_checker=self.lease, clock=self.clock, effect_port=self.port)
        self.assertEqual(self.gateway.set_policy(make_policy()), 1)
        self.assertEqual(self.gateway.register_authorization(make_scope()), 1)
        self.director = self.open()

    def open(self, *, revision=1, decision=None, binding=None, grant=None) -> ToolSession:
        return self.gateway.open_director_session(
            binding=binding or make_binding(), vm_authorization_id="vm-auth-1", authorization_revision=revision,
            decision=decision or make_decision("director", DIRECTOR_PROFILE_TOOLS), grant=grant)

    @staticmethod
    def call(tool="read_file", call_id="tc-1", **overrides) -> ToolCall:
        values = dict(tool_call_id=call_id, tool_id=tool, workspace_id="ws-1", target=TARGET,
                      input_revision="plan-rev-1", authorization_revision=1, arguments={"line": 1})
        values.update(overrides)
        return ToolCall(**values)

    def denied(self, code, function, *args, **kwargs):
        with self.assertRaises(AuthorityDenied) as context:
            function(*args, **kwargs)
        self.assertEqual(context.exception.code, code)
        return context.exception

    def worker(self, tools=("read_file", "write_file")) -> ToolSession:
        return self.gateway.delegate(self.director, decision=make_decision("worker", tools), tools=tools)

    # --------------------------------------------------- required denials
    def test_undeclared_and_role_disallowed_tools_are_denied_by_default(self) -> None:
        self.denied(AuthorityCode.TOOL_NOT_DECLARED, self.gateway.request, self.director,
                    self.call("shell_exec"))
        worker = self.worker()
        self.denied(AuthorityCode.TOOL_NOT_ALLOWED_FOR_ROLE, self.gateway.request, worker,
                    self.call("deploy", effect_id="effect-d"))
        self.denied(AuthorityCode.TOOL_NOT_ALLOWED_FOR_ROLE, self.gateway.request, self.director,
                    self.call("publish", effect_id="effect-p"))

    def test_expired_authorization_denies_new_calls_and_replays(self) -> None:
        self.gateway.request(self.director, self.call(call_id="tc-replay"))
        self.clock.value = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.denied(AuthorityCode.AUTHORIZATION_EXPIRED, self.gateway.request, self.director, self.call())
        self.denied(AuthorityCode.AUTHORIZATION_EXPIRED, self.gateway.request, self.director,
                    self.call(call_id="tc-replay"))
        self.denied(AuthorityCode.AUTHORIZATION_EXPIRED, self.open)

    def test_other_workspace_is_denied_for_calls_profiles_and_grants(self) -> None:
        self.denied(AuthorityCode.WORKSPACE_MISMATCH, self.gateway.request, self.director,
                    self.call(workspace_id="ws-2"))
        self.denied(AuthorityCode.WORKSPACE_MISMATCH, self.open,
                    decision=make_decision("director", DIRECTOR_PROFILE_TOOLS, workspace="ws-2"))
        self.denied(AuthorityCode.WORKSPACE_MISMATCH, self.gateway.delegate, self.director,
                    decision=make_decision("worker", ("read_file",), workspace="ws-2"), tools=("read_file",))
        grant = Grant("grant-x", "ws-2", "task-1", "adapter-1", "lease-1", 1, ("conn-1",), 0.0, 60.0, 1)
        self.denied(AuthorityCode.WORKSPACE_MISMATCH, self.open, grant=grant)

    def test_role_mismatch_is_denied_for_sessions_and_delegation(self) -> None:
        self.denied(AuthorityCode.ROLE_MISMATCH, self.open, decision=make_decision("worker", ("read_file",)))
        self.denied(AuthorityCode.ROLE_MISMATCH, self.gateway.delegate, self.director,
                    decision=make_decision("director", ("read_file",)), tools=("read_file",))
        reviewer_decision = make_decision("reviewer", ("read_file", "write_file"))
        error = self.denied(AuthorityCode.TOOL_NOT_ALLOWED_FOR_ROLE, self.gateway.delegate, self.director,
                            decision=reviewer_decision, tools=("read_file", "write_file"))
        self.assertEqual(error.details, ("write_file",))
        self.denied(AuthorityCode.PROFILE_NOT_BOUND, self.open,
                    decision=make_decision("director", DIRECTOR_PROFILE_TOOLS, bound=False))

    def test_inactive_lease_denies_calls(self) -> None:
        self.lease.active = False
        self.denied(AuthorityCode.LEASE_INACTIVE, self.gateway.request, self.director, self.call())
        self.assertIn(("lease-1", 1), self.lease.calls)

    # --------------------------------- criterion 1: reuse existing scope
    def test_delegation_passes_existing_scope_without_new_approval(self) -> None:
        worker = self.worker()
        decision = self.gateway.request(worker, self.call("write_file", effect_id="effect-w"))
        self.assertEqual(decision.role, "worker")
        self.assertEqual(decision.vm_authorization_id, "vm-auth-1")
        self.assertEqual(self.gateway.authorization_revision("vm-auth-1"), 1)
        self.assertEqual(self.gateway.allowed_tools(worker), ("read_file", "write_file"))
        first = self.gateway.request(self.director, self.call(call_id="tc-same"))
        self.assertIs(self.gateway.request(self.director, self.call(call_id="tc-same")), first)

    # ------------------------------- criterion 2: scope expansion & stale
    def test_target_effect_tool_and_delegation_expansion_are_escalations(self) -> None:
        error = self.denied(AuthorityCode.TARGET_OUT_OF_SCOPE, self.gateway.request, self.director,
                            self.call(target="workspace://ws-1/other/file"))
        self.assertTrue(error.escalation)
        self.assertEqual(error.vm_contract, "CORE_CAPABILITY_DENIED")
        error = self.denied(AuthorityCode.TOOL_NOT_IN_AUTHORIZATION, self.gateway.request, self.director,
                            self.call("search"))
        self.assertTrue(error.escalation)
        error = self.denied(AuthorityCode.SCOPE_ESCALATION, self.gateway.delegate, self.director,
                            decision=make_decision("worker", ("read_file", "search")), tools=("read_file", "search"))
        self.assertTrue(error.escalation)
        self.assertEqual(error.details, ("search",))
        self.denied(AuthorityCode.TOOL_NOT_IN_PROFILE, self.gateway.delegate, self.director,
                    decision=make_decision("worker", ("read_file",)), tools=("read_file", "write_file"))
        self.assertEqual(self.gateway.register_authorization(
            make_scope(effect_kinds=(EffectKind.READ_ONLY,)), expected_revision=1), 2)
        narrowed = self.open(revision=2)
        error = self.denied(AuthorityCode.EFFECT_OUT_OF_SCOPE, self.gateway.request, narrowed,
                            self.call("write_file", authorization_revision=2, effect_id="effect-x"))
        self.assertTrue(error.escalation)

    def test_stale_authorization_and_input_revisions_are_denied(self) -> None:
        error = self.denied(AuthorityCode.STALE_INPUT_REVISION, self.gateway.request, self.director,
                            self.call(input_revision="plan-rev-0"))
        self.assertEqual(error.vm_contract, "STALE_EXECUTION_INPUT")
        self.assertFalse(error.escalation)
        self.denied(AuthorityCode.STALE_INPUT_REVISION, self.open,
                    binding=make_binding(vm_plan_revision_id="plan-rev-9"))
        self.denied(AuthorityCode.STALE_AUTHORIZATION_REVISION, self.gateway.request, self.director,
                    self.call(authorization_revision=2))
        with self.assertRaises(RevisionConflict):
            self.gateway.register_authorization(make_scope(), expected_revision=0)
        self.gateway.register_authorization(make_scope(), expected_revision=1)
        self.denied(AuthorityCode.STALE_AUTHORIZATION_REVISION, self.gateway.request, self.director, self.call())
        self.denied(AuthorityCode.STALE_AUTHORIZATION_REVISION, self.open, revision=1)
        renewed = self.open(revision=2)
        self.gateway.request(renewed, self.call(authorization_revision=2))
        self.gateway.withdraw_authorization("vm-auth-1", expected_revision=2)
        self.denied(AuthorityCode.AUTHORIZATION_WITHDRAWN, self.gateway.request, renewed,
                    self.call(call_id="tc-2", authorization_revision=2))

    def test_policy_changes_are_serialized_and_not_retroactive(self) -> None:
        with self.assertRaises(RevisionConflict):
            self.gateway.set_policy(make_policy(director_extra=("publish",)), expected_revision=0)
        self.assertEqual(self.gateway.set_policy(make_policy(director_extra=("publish",)), expected_revision=1), 2)
        self.denied(AuthorityCode.TOOL_NOT_ALLOWED_FOR_ROLE, self.gateway.request, self.director,
                    self.call("publish", effect_id="effect-p"))
        fresh = self.open()
        decision = self.gateway.request(fresh, self.call("publish", effect_id="effect-p"))
        self.assertEqual(decision.policy_revision, 2)

    # ------------------------------------- criterion 3: no self-granting
    def test_agents_cannot_grant_themselves_authority_or_write_verdicts(self) -> None:
        worker = self.worker()
        self.denied(AuthorityCode.ROLE_CANNOT_DELEGATE, self.gateway.delegate, worker,
                    decision=make_decision("worker", ("read_file",)), tools=("read_file",))
        self.assertEqual(ToolSession.__slots__, ("session_id", "role", "workspace_id", "__weakref__"))
        for name in ("scope", "policy", "authorization", "grant", "binding"):
            self.assertFalse(hasattr(worker, name))
        with self.assertRaises(TypeError):
            pickle.dumps(worker)
        with self.assertRaises(TypeError):
            copy.copy(worker)
        forged = ToolSession(worker.session_id, "director", "ws-1")
        self.denied(AuthorityCode.UNKNOWN_SESSION, self.gateway.request, forged, self.call())
        self.denied(AuthorityCode.UNKNOWN_SESSION, self.gateway.request,
                    {"session_id": worker.session_id, "role": "director"}, self.call())
        for tool in ("vm.verdict", "authority.grant", "Set_Verdict"):
            with self.subTest(tool=tool):
                with self.assertRaises(InvalidPolicy) as context:
                    ToolDeclaration(tool, EffectKind.NON_IDEMPOTENT)
                self.assertEqual(context.exception.code, AuthorityCode.RESERVED_TOOL)
        with self.assertRaises(InvalidPolicy):
            ToolPolicy("ws-1", (ToolDeclaration("write_file", EffectKind.IDEMPOTENT),),
                       reviewer_tools=("write_file",))
        self.assertTrue(self.gateway.close_session(self.director))
        self.denied(AuthorityCode.SESSION_CLOSED, self.gateway.request, worker, self.call(call_id="tc-c"))

    def test_mapper_requires_vm_authorization_evidence(self) -> None:
        status = BridgeReceipt(Command.STATUS, "req-s", "project-1", VmRefs(authorization_id="vm-auth-1",
                               plan_revision_id="plan-rev-1"), MappingProxyType({}))
        for receipt in (status, authorize_receipt(authorization_id=None), {"authorization_id": "vm-auth-1"}):
            with self.subTest(receipt=type(receipt).__name__):
                self.denied(AuthorityCode.AUTHORIZATION_EVIDENCE_MISSING, map_vm_authorization, receipt,
                            workspace_id="ws-1", tools=("read_file",), targets=("workspace://ws-1/root",),
                            effect_kinds=ALL_KINDS, expires_at="2030-01-01T00:00:00Z")
        self.denied(AuthorityCode.AUTHORIZATION_UNKNOWN, self.gateway.open_director_session,
                    binding=make_binding(), vm_authorization_id="vm-auth-unknown", authorization_revision=1,
                    decision=make_decision("director", DIRECTOR_PROFILE_TOOLS))

    # ------------------------------ criterion 4: identity, digest, effects
    def test_decision_binds_attempt_revision_lease_and_is_not_completion(self) -> None:
        decision = self.gateway.request(self.director, self.call("write_file", effect_id="effect-1"))
        binding = make_binding()
        self.assertEqual(decision.status, "authorized")
        self.assertEqual((decision.vm_attempt_id, decision.vm_task_id, decision.authorization_revision,
                          decision.lease_id, decision.lease_fencing_token, decision.binding_digest),
                         ("attempt-1", "task-1", 1, "lease-1", 1, binding.digest()))
        self.assertEqual(decision.effect_kind, EffectKind.IDEMPOTENT)
        self.assertTrue(decision.request_digest.startswith("sha256:"))
        intent = self.port.intents[0]
        self.assertEqual((intent["request_digest"], intent["vm_attempt_id"], intent["lease_fencing_token"]),
                         (decision.request_digest, "attempt-1", 1))
        self.assertEqual(self.gateway.effect_state("effect-1"), "intent_recorded")
        self.denied(AuthorityCode.TOOL_CALL_CONFLICT, self.gateway.request, self.director,
                    self.call("write_file", effect_id="effect-1", arguments={"line": 2}))
        self.gateway.register_authorization(make_scope(), expected_revision=1)
        renewed = self.open(revision=2)
        first = self.gateway.request(renewed, self.call(call_id="tc-r1", authorization_revision=2))
        other = AuthorityGateway(lease_checker=self.lease, clock=self.clock)
        other.set_policy(make_policy())
        other.register_authorization(make_scope())
        old = other.open_director_session(binding=make_binding(), vm_authorization_id="vm-auth-1",
                                          authorization_revision=1,
                                          decision=make_decision("director", DIRECTOR_PROFILE_TOOLS))
        self.assertNotEqual(other.request(old, self.call(call_id="tc-r1")).request_digest, first.request_digest)

    def test_effect_kinds_have_distinct_replay_and_identity_rules(self) -> None:
        self.gateway.request(self.director, self.call("read_file", call_id="tc-ro"))
        self.denied(AuthorityCode.INVALID_REQUEST, self.gateway.request, self.director,
                    self.call("read_file", call_id="tc-ro2", effect_id="effect-ro"))
        self.denied(AuthorityCode.INVALID_REQUEST, self.gateway.request, self.director,
                    self.call("write_file", call_id="tc-noeffect"))
        self.assertEqual(self.port.intents, [])
        idem = self.gateway.request(self.director, self.call("write_file", call_id="tc-i", effect_id="effect-i"))
        self.assertIs(self.gateway.request(self.director, self.call("write_file", call_id="tc-i",
                                                                    effect_id="effect-i")), idem)
        retry = self.gateway.request(self.director, self.call("write_file", call_id="tc-i2", effect_id="effect-i"))
        self.assertEqual(retry.request_digest, idem.request_digest)
        self.assertEqual(len(self.port.intents), 1)
        self.gateway.request(self.director, self.call("deploy", call_id="tc-n", effect_id="effect-n"))
        self.denied(AuthorityCode.EFFECT_ALREADY_AUTHORIZED, self.gateway.request, self.director,
                    self.call("deploy", call_id="tc-n", effect_id="effect-n"))
        self.denied(AuthorityCode.EFFECT_ID_CONFLICT, self.gateway.request, self.director,
                    self.call("deploy", call_id="tc-n2", effect_id="effect-n"))
        self.assertEqual(len(self.port.intents), 2)

    def test_missing_effect_seam_blocks_effects_but_not_reads(self) -> None:
        gateway = AuthorityGateway(lease_checker=self.lease, clock=self.clock)
        gateway.set_policy(make_policy())
        gateway.register_authorization(make_scope())
        session = gateway.open_director_session(binding=make_binding(), vm_authorization_id="vm-auth-1",
                                                authorization_revision=1,
                                                decision=make_decision("director", DIRECTOR_PROFILE_TOOLS))
        gateway.request(session, self.call())
        self.denied(AuthorityCode.SEAM_UNAVAILABLE, gateway.request, session,
                    self.call("write_file", call_id="tc-w", effect_id="effect-w"))

    def test_vm_rejection_keeps_code_and_unknown_intent_requires_reconciliation(self) -> None:
        self.port.intent_error = CodedError("CORE_CAPABILITY_DENIED")
        error = self.denied(AuthorityCode.VM_REJECTED, self.gateway.request, self.director,
                            self.call("deploy", effect_id="effect-v"))
        self.assertEqual(error.vm_code, "CORE_CAPABILITY_DENIED")
        self.assertNotIn(SECRET_MARKER, str(error))
        self.assertIsNone(error.__cause__)
        self.port.intent_error = RuntimeError("raw " + SECRET_MARKER)
        with self.assertRaises(EffectOutcomeUnknown) as context:
            self.gateway.request(self.director, self.call("deploy", call_id="tc-u", effect_id="effect-u"))
        self.assertEqual(context.exception.code, AuthorityCode.INTENT_UNKNOWN)
        self.assertNotIn(SECRET_MARKER, str(context.exception))
        self.assertIn("effect-u", self.gateway.unresolved_effects)
        self.port.intent_error = None
        self.denied(AuthorityCode.RECONCILIATION_REQUIRED, self.gateway.request, self.director,
                    self.call("deploy", call_id="tc-u", effect_id="effect-u"))
        self.assertIsNone(self.gateway.reconcile("effect-u"))
        self.gateway.request(self.director, self.call("deploy", call_id="tc-u", effect_id="effect-u"))
        self.assertEqual(self.gateway.effect_state("effect-u"), "intent_recorded")

    def test_receipts_are_forwarded_to_vm_and_never_become_verdicts(self) -> None:
        self.gateway.request(self.director, self.call("write_file", call_id="tc-w", effect_id="effect-w"))
        receipt = self.gateway.forward_receipt(self.director, "tc-w", "succeeded", receipt_ref="receipt-1")
        self.assertEqual((receipt.reported_status, receipt.vm_state, receipt.reconciliation_required),
                         ("succeeded", "receipt_recorded", False))
        self.assertEqual(self.gateway.effect_state("effect-w"), "receipt_forwarded")
        self.assertEqual(self.port.receipts[0]["reported_status"], "succeeded")
        self.gateway.request(self.director, self.call("deploy", call_id="tc-d", effect_id="effect-d"))
        unknown = self.gateway.forward_receipt(self.director, "tc-d", "unknown")
        self.assertTrue(unknown.reconciliation_required)
        self.denied(AuthorityCode.RECONCILIATION_REQUIRED, self.gateway.request, self.director,
                    self.call("deploy", call_id="tc-d", effect_id="effect-d"))
        self.port.effects["effect-d"] = {"effect_id": "effect-d", "state": "cancelled"}
        self.assertEqual(self.gateway.reconcile("effect-d"), "cancelled")
        self.assertEqual(self.gateway.effect_state("effect-d"), "vm:cancelled")
        self.gateway.request(self.director, self.call(call_id="tc-read"))
        self.denied(AuthorityCode.INVALID_REQUEST, self.gateway.forward_receipt, self.director, "tc-read",
                    "succeeded")
        worker = self.worker()
        self.denied(AuthorityCode.TOOL_CALL_UNKNOWN, self.gateway.forward_receipt, worker, "tc-w", "failed")

    def test_credential_tools_require_matching_broker_scope(self) -> None:
        self.denied(AuthorityCode.CREDENTIAL_SCOPE_MISSING, self.gateway.request, self.director,
                    self.call("call_provider", effect_id="effect-c", connection_id="conn-1"))
        grant = Grant("grant-1", "ws-1", "task-1", "adapter-1", "lease-1", 1, ("conn-1",), 0.0, 60.0, 1)
        session = self.open(grant=grant)
        self.gateway.request(session, self.call("call_provider", effect_id="effect-c", connection_id="conn-1"))
        self.denied(AuthorityCode.CONNECTION_OUT_OF_SCOPE, self.gateway.request, session,
                    self.call("call_provider", call_id="tc-2", effect_id="effect-c2", connection_id="conn-9"))
        self.denied(AuthorityCode.CREDENTIAL_SCOPE_MISSING, self.gateway.request, session,
                    self.call("call_provider", call_id="tc-3", effect_id="effect-c3"))
        wrong_lease = Grant("grant-2", "ws-1", "task-1", "adapter-1", "lease-1", 2, ("conn-1",), 0.0, 60.0, 1)
        self.denied(AuthorityCode.BROKER_SCOPE_MISMATCH, self.open, grant=wrong_lease)

    def test_audit_and_decisions_hold_digests_not_arguments(self) -> None:
        decision = self.gateway.request(self.director, self.call("write_file", effect_id="effect-s",
                                                                 arguments={"note": SECRET_MARKER}))
        with self.assertRaises(AuthorityDenied):
            self.gateway.request(self.director, self.call("shell_exec", call_id="tc-x",
                                                          arguments={"note": SECRET_MARKER}))
        records = self.gateway.audit_records()
        self.assertTrue(any(record.code == "TOOL_NOT_DECLARED" for record in records))
        text = json.dumps([record.to_dict() for record in records]) + repr(decision) + json.dumps(self.port.intents)
        self.assertNotIn(SECRET_MARKER, text)
        self.denied(AuthorityCode.INVALID_REQUEST, ToolCall, "tc-bad", "read_file", "ws-1", TARGET,
                    "plan-rev-1", 1, {"value": float("nan")})
        self.denied(AuthorityCode.INVALID_REQUEST, ToolCall, "tc-bad", "read_file", "ws-1",
                    "workspace://ws-1/root/../secret", "plan-rev-1", 1)

    def test_naive_or_failing_clock_fails_closed(self) -> None:
        self.clock.value = datetime(2029, 6, 1)
        self.denied(AuthorityCode.CLOCK_UNAVAILABLE, self.gateway.request, self.director, self.call())
        self.clock.value = datetime(2029, 6, 1, tzinfo=timezone(timedelta(hours=9)))
        self.gateway.request(self.director, self.call())

    # ------------------------------------------- review findings (F-1..F-8)
    def test_reconcile_without_vm_intent_never_reissues_an_issued_non_idempotent_effect(self) -> None:
        # F-1: a permission was issued (intent echoed); VM later answers "no intent".
        self.gateway.request(self.director, self.call("deploy", call_id="tc-n", effect_id="effect-n"))
        self.port.receipt_error = RuntimeError("raw " + SECRET_MARKER)
        with self.assertRaises(EffectOutcomeUnknown):
            self.gateway.forward_receipt(self.director, "tc-n", "succeeded")
        self.port.receipt_error = None
        self.port.effects.pop("effect-n")  # e.g. VM restart or another instance was queried
        self.denied(AuthorityCode.RECONCILIATION_REQUIRED, self.gateway.reconcile, "effect-n")
        self.assertIn("effect-n", self.gateway.unresolved_effects)
        self.assertEqual(self.gateway.effect_state("effect-n"), "outcome_unknown")
        self.denied(AuthorityCode.RECONCILIATION_REQUIRED, self.gateway.request, self.director,
                    self.call("deploy", call_id="tc-n2", effect_id="effect-n"))
        self.assertEqual(len(self.port.intents), 1)
        # Same path for an issued effect that was never marked unresolved.
        self.gateway.request(self.director, self.call("deploy", call_id="tc-m", effect_id="effect-m"))
        self.port.effects.pop("effect-m")
        self.denied(AuthorityCode.RECONCILIATION_REQUIRED, self.gateway.reconcile, "effect-m")
        self.denied(AuthorityCode.RECONCILIATION_REQUIRED, self.gateway.request, self.director,
                    self.call("deploy", call_id="tc-m2", effect_id="effect-m"))
        self.assertEqual(len(self.port.intents), 2)
        self.assertTrue(any(record.event == "effect_reconcile_blocked" for record in self.gateway.audit_records()))

    def test_unclear_intent_later_found_in_vm_is_marked_not_issued_for_vm_decision(self) -> None:
        # F-2: VM holds an intent the gateway never issued a permission for.
        self.port.intent_error = RuntimeError("raw " + SECRET_MARKER)
        with self.assertRaises(EffectOutcomeUnknown):
            self.gateway.request(self.director, self.call("deploy", call_id="tc-u", effect_id="effect-u"))
        self.port.intent_error = None
        self.port.effects["effect-u"] = {"effect_id": "effect-u", "state": "intent_recorded"}
        self.assertEqual(self.gateway.reconcile("effect-u"), "intent_recorded")
        self.assertEqual(self.gateway.effect_state("effect-u"), "vm:intent_recorded")
        self.assertNotIn("effect-u", self.gateway.unresolved_effects)
        error = self.denied(AuthorityCode.EFFECT_NOT_ISSUED, self.gateway.request, self.director,
                            self.call("deploy", call_id="tc-u", effect_id="effect-u"))
        self.assertFalse(error.escalation)
        self.denied(AuthorityCode.TOOL_CALL_UNKNOWN, self.gateway.forward_receipt, self.director, "tc-u",
                    "succeeded")
        self.assertEqual(len(self.port.intents), 1)
        # If VM later drops the never-issued intent, the record may be recreated.
        self.port.effects.pop("effect-u")
        self.assertIsNone(self.gateway.reconcile("effect-u"))
        self.gateway.request(self.director, self.call("deploy", call_id="tc-u", effect_id="effect-u"))
        self.assertEqual(self.gateway.effect_state("effect-u"), "intent_recorded")

    def test_director_profile_connection_must_match_attempt_binding(self) -> None:
        # F-4
        self.denied(AuthorityCode.BINDING_MISMATCH, self.open,
                    decision=make_decision("director", DIRECTOR_PROFILE_TOOLS, connection="conn-x"))
        self.denied(AuthorityCode.BINDING_MISMATCH, self.open, binding=make_binding(connection_id="conn-2"))

    def test_vm_errors_do_not_retain_raw_exception_context(self) -> None:
        # F-5: neither __cause__ nor __context__ may keep the raw VM exception.
        self.port.intent_error = CodedError("CORE_CAPABILITY_DENIED")
        error = self.denied(AuthorityCode.VM_REJECTED, self.gateway.request, self.director,
                            self.call("deploy", call_id="tc-v", effect_id="effect-v"))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.port.intent_error = CodedError("VM_STATE_UNKNOWN")
        with self.assertRaises(EffectOutcomeUnknown) as context:
            self.gateway.request(self.director, self.call("deploy", call_id="tc-k", effect_id="effect-k"))
        self.assertIsNone(context.exception.__context__)
        self.port.intent_error = None
        self.gateway.request(self.director, self.call("write_file", call_id="tc-r", effect_id="effect-r"))
        self.port.receipt_error = CodedError("CORE_CAPABILITY_DENIED")
        error = self.denied(AuthorityCode.VM_REJECTED, self.gateway.forward_receipt, self.director, "tc-r",
                            "succeeded")
        self.assertIsNone(error.__context__)
        self.port.receipt_error = RuntimeError("raw " + SECRET_MARKER)
        with self.assertRaises(EffectOutcomeUnknown) as context:
            self.gateway.forward_receipt(self.director, "tc-r", "succeeded")
        self.assertIsNone(context.exception.__context__)
        self.port.read_error = RuntimeError("raw " + SECRET_MARKER)
        error = self.denied(AuthorityCode.RECONCILIATION_REQUIRED, self.gateway.reconcile, "effect-r")
        self.assertIsNone(error.__context__)

    def test_audit_overflow_is_counted_not_silent(self) -> None:
        # F-6
        self.assertEqual(self.gateway.audit_dropped, 0)
        self.gateway.request(self.director, self.call(call_id="tc-audit"))
        for _ in range(AUDIT_CAPACITY):
            self.gateway.request(self.director, self.call(call_id="tc-audit"))
        records = self.gateway.audit_records()
        self.assertEqual(len(records), AUDIT_CAPACITY)
        self.assertGreater(self.gateway.audit_dropped, 0)
        self.assertEqual(records[0].sequence - 1, self.gateway.audit_dropped)

    def test_closing_a_session_prunes_read_only_decisions_but_keeps_effect_records(self) -> None:
        # F-6
        self.gateway.request(self.director, self.call(call_id="tc-ro-prune"))
        self.gateway.request(self.director, self.call("deploy", call_id="tc-keep", effect_id="effect-keep"))
        self.assertTrue(self.gateway.close_session(self.director))
        fresh = self.open()
        self.gateway.request(fresh, self.call(call_id="tc-ro-prune"))
        self.denied(AuthorityCode.TOOL_CALL_CONFLICT, self.gateway.request, fresh,
                    self.call("deploy", call_id="tc-keep", effect_id="effect-keep"))
        self.assertEqual(self.gateway.effect_state("effect-keep"), "intent_recorded")
        receipt = self.gateway.forward_receipt(self.director, "tc-keep", "succeeded")
        self.assertEqual(receipt.vm_state, "receipt_recorded")

    def test_verdict_authorization_and_completion_tool_names_are_reserved(self) -> None:
        # F-7
        for tool in ("vm.set_verdict", "goal.verdict", "vm.complete_task", "task.approve_plan",
                     "plan:approval", "broker.grant", "vm.authorization_request"):
            with self.subTest(tool=tool):
                self.assertTrue(is_reserved_tool(tool))
                with self.assertRaises(InvalidPolicy) as context:
                    ToolDeclaration(tool, EffectKind.NON_IDEMPOTENT)
                self.assertEqual(context.exception.code, AuthorityCode.RESERVED_TOOL)
        for tool in ("read_file", "write_file", "deploy", "publish", "call_provider", "search"):
            with self.subTest(allowed=tool):
                self.assertFalse(is_reserved_tool(tool))

    # F-8 coverage of rejection and unclear paths.
    def test_intent_echo_mismatch_unknown_token_and_receipt_exception_require_reconciliation(self) -> None:
        self.port.echo_digest = "sha256:" + "0" * 64
        with self.assertRaises(EffectOutcomeUnknown) as context:
            self.gateway.request(self.director, self.call("deploy", call_id="tc-e", effect_id="effect-e"))
        self.assertEqual(context.exception.code, AuthorityCode.INTENT_UNKNOWN)
        self.assertIn("effect-e", self.gateway.unresolved_effects)
        self.assertEqual(self.gateway.effect_state("effect-e"), "intent_unknown")
        self.port.echo_digest = None
        self.port.intent_error = CodedError("VM_STATE_UNKNOWN")
        with self.assertRaises(EffectOutcomeUnknown) as context:
            self.gateway.request(self.director, self.call("deploy", call_id="tc-k", effect_id="effect-k"))
        self.assertEqual((context.exception.code, context.exception.vm_code),
                         (AuthorityCode.INTENT_UNKNOWN, "VM_STATE_UNKNOWN"))
        self.assertIn("effect-k", self.gateway.unresolved_effects)
        self.port.intent_error = None
        self.gateway.request(self.director, self.call("write_file", call_id="tc-w", effect_id="effect-w"))
        self.port.receipt_error = RuntimeError("raw " + SECRET_MARKER)
        with self.assertRaises(EffectOutcomeUnknown) as context:
            self.gateway.forward_receipt(self.director, "tc-w", "succeeded")
        self.assertEqual(context.exception.code, AuthorityCode.RECEIPT_UNKNOWN)
        self.assertNotIn(SECRET_MARKER, str(context.exception))
        self.assertEqual(self.gateway.effect_state("effect-w"), "outcome_unknown")
        self.denied(AuthorityCode.RECONCILIATION_REQUIRED, self.gateway.request, self.director,
                    self.call("write_file", call_id="tc-w", effect_id="effect-w"))

    def test_withdrawn_authorization_blocks_sessions_calls_and_reregistration(self) -> None:
        self.assertEqual(self.gateway.withdraw_authorization("vm-auth-1", expected_revision=1), 2)
        self.denied(AuthorityCode.AUTHORIZATION_WITHDRAWN, self.open, revision=2)
        self.denied(AuthorityCode.AUTHORIZATION_WITHDRAWN, self.gateway.register_authorization,
                    make_scope(), expected_revision=2)
        self.denied(AuthorityCode.AUTHORIZATION_WITHDRAWN, self.gateway.request, self.director, self.call())
        self.assertEqual(self.gateway.authorization_revision("vm-auth-1"), 2)

    def test_renewed_authorization_makes_delegated_sessions_stale(self) -> None:
        worker = self.worker()
        self.gateway.register_authorization(make_scope(), expected_revision=1)
        self.denied(AuthorityCode.STALE_AUTHORIZATION_REVISION, self.gateway.request, worker,
                    self.call("write_file", call_id="tc-ws", effect_id="effect-ws"))
        self.denied(AuthorityCode.STALE_AUTHORIZATION_REVISION, self.gateway.delegate, self.director,
                    decision=make_decision("worker", ("read_file",)), tools=("read_file",))
        self.assertEqual(self.port.intents, [])

    def test_worker_non_idempotent_effect_follows_the_same_identity_rules(self) -> None:
        self.gateway.set_policy(make_policy(worker_extra=("deploy",)), expected_revision=1)
        director = self.open()
        worker = self.gateway.delegate(director, decision=make_decision("worker", ("deploy",)), tools=("deploy",))
        self.gateway.request(worker, self.call("deploy", call_id="tc-wd", effect_id="effect-wd"))
        self.denied(AuthorityCode.EFFECT_ALREADY_AUTHORIZED, self.gateway.request, worker,
                    self.call("deploy", call_id="tc-wd", effect_id="effect-wd"))
        self.denied(AuthorityCode.EFFECT_ID_CONFLICT, self.gateway.request, director,
                    self.call("deploy", call_id="tc-wd2", effect_id="effect-wd"))
        self.assertEqual(len(self.port.intents), 1)

    def test_concurrent_requests_for_one_non_idempotent_effect_issue_once(self) -> None:
        count = 8
        barrier = threading.Barrier(count)
        outcomes = []

        def attempt(number: int) -> None:
            barrier.wait(timeout=10)
            try:
                self.gateway.request(self.director, self.call("deploy", call_id=f"tc-race-{number}",
                                                              effect_id="effect-race"))
                outcomes.append("authorized")
            except AuthorityDenied as error:
                outcomes.append(error.code.value)

        threads = [threading.Thread(target=attempt, args=(number,)) for number in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(len(outcomes), count)
        self.assertEqual(outcomes.count("authorized"), 1)
        self.assertEqual({outcome for outcome in outcomes if outcome != "authorized"},
                         {AuthorityCode.EFFECT_ID_CONFLICT.value})
        self.assertEqual(len(self.port.intents), 1)


if __name__ == "__main__":
    unittest.main()
