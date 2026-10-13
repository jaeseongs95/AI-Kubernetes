"""AikEffectJournal and ExternalEffectSubmitter tests with synthetic data only.

No VM installation, provider, credential or network is used. The journal is a
disposable SQLite file in the OS temporary directory. The VM service is a
fake that imitates the documented ``record_effect_receipt`` keyword contract.
Profile binding (aik_profiles) is not under test here: the gateway helper
``_profile_view`` is patched only while a director session is opened.
Passing on Linux is development verification, not Windows or VM qualification.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_authority import (  # noqa: E402
    RECEIPT_STATUSES, TARGET_REOBSERVATION_REQUIRED, AikEffectJournal, AuthorityCode, AuthorityDenied,
    AuthorityGateway, AuthorizationScope, EffectIdentity, EffectJournalError, EffectKind,
    ExternalEffectError, ExternalEffectReceipt, ExternalEffectSubmitter, ToolCall, ToolDeclaration, ToolPolicy,
)
from aik_authority import effect_journal, gateway as gateway_module  # noqa: E402
from aik_runtime import InvalidDatabase, UnsafeStoragePath  # noqa: E402
from aik_vm_bridge import EffectOutcome, TaskExecutionBinding  # noqa: E402


SECRET_MARKER = "SYNTHETIC_SECRET_MUST_NOT_LEAK"
NOW = datetime(2029, 6, 1, tzinfo=timezone.utc)
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
DIGEST_R = "sha256:" + "d" * 64
TOOLS = ("fs.write", "fs.update", "fs.read")


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


BINDING = TaskExecutionBinding(**binding_fields())


def policy() -> ToolPolicy:
    return ToolPolicy("workspace-1", (
        ToolDeclaration("fs.write", EffectKind.NON_IDEMPOTENT),
        ToolDeclaration("fs.update", EffectKind.IDEMPOTENT),
        ToolDeclaration("fs.read", EffectKind.READ_ONLY),
    ), director_tools=TOOLS)


def scope() -> AuthorizationScope:
    return AuthorizationScope("authorization-1", "project-1", "workspace-1", "plan-rev-1", TOOLS,
                              ("workspace://local/root-1",),
                              (EffectKind.READ_ONLY, EffectKind.IDEMPOTENT, EffectKind.NON_IDEMPOTENT),
                              "2030-01-01T00:00:00Z")


def tool_call(tool_call_id="call-1", tool_id="fs.write", effect_id="effect-1") -> ToolCall:
    return ToolCall(tool_call_id, tool_id, "workspace-1", "workspace://local/root-1/out.txt", "plan-rev-1", 1,
                    {"path": "out.txt"}, effect_id)


def intent(**overrides) -> dict:
    base = {
        "effect_id": "effect-1", "tool_call_id": "call-1", "request_digest": DIGEST_R,
        "effect_kind": "idempotent", "tool_id": "fs.update", "vm_project_id": "project-1",
        "vm_task_id": "task-1", "vm_attempt_id": "attempt-1", "vm_authorization_id": "authorization-1",
        "authorization_revision": 1, "lease_id": "lease-1", "lease_fencing_token": 5,
        "binding_digest": BINDING.digest(),
    }
    base.update(overrides)
    return base


def receipt(**overrides) -> dict:
    base = {
        "effect_id": "effect-1", "tool_call_id": "call-1", "request_digest": DIGEST_R,
        "vm_attempt_id": "attempt-1", "vm_authorization_id": "authorization-1", "authorization_revision": 1,
        "lease_id": "lease-1", "lease_fencing_token": 5, "reported_status": "succeeded", "receipt_ref": None,
    }
    base.update(overrides)
    return base


def running_attempt(status="running", **overrides) -> dict:
    attempt = {"attempt_id": "attempt-1", "task_id": "task-1", "status": status}
    attempt.update(overrides)
    return {"project_id": "project-1", "plan_revision_id": "plan-rev-1", "attempt": attempt, "intents": []}


class JournalTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aik-effect-journal-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "journal space" / "effects.sqlite3"
        self.binding_answer: object = {"binding_digest": BINDING.digest()}
        self.binding_reads: list = []

    def read_binding(self, vm_attempt_id):
        self.binding_reads.append(vm_attempt_id)
        if isinstance(self.binding_answer, Exception):
            raise self.binding_answer
        return self.binding_answer

    def journal(self, **kwargs) -> AikEffectJournal:
        return AikEffectJournal(self.path, binding_reader=self.read_binding, now=lambda: NOW, **kwargs)

    def gateway(self, journal):
        gateway = AuthorityGateway(lease_checker=lambda lease_id, token: True, clock=lambda: NOW,
                                   effect_port=journal)
        gateway.set_policy(policy())
        gateway.register_authorization(scope())
        view = ("director", "workspace-1", frozenset(TOOLS), "connection-1")
        with mock.patch.object(gateway_module, "_profile_view", return_value=view):
            session = gateway.open_director_session(binding=BINDING, vm_authorization_id="authorization-1",
                                                    authorization_revision=1, decision=object())
        return gateway, session


class GatewayJournalIntegrationTests(JournalTestCase):
    def test_permission_records_intent_and_receipt_is_a_journal_observation(self) -> None:
        journal = self.journal()
        gateway, session = self.gateway(journal)
        decision = gateway.request(session, tool_call())
        self.assertEqual(decision.status, "authorized")
        self.assertEqual(self.binding_reads, ["attempt-1"])
        self.assertEqual(journal.read_effect("effect-1"), {"effect_id": "effect-1", "state": "intent_recorded"})
        forwarded = gateway.forward_receipt(session, "call-1", "succeeded")
        self.assertEqual((forwarded.vm_state, forwarded.reconciliation_required), ("succeeded", False))
        self.assertEqual(journal.read_effect("effect-1"), {"effect_id": "effect-1", "state": "succeeded"})
        self.assertEqual(gateway.effect_state("effect-1"), "receipt_forwarded")

    def test_unconfirmed_or_mismatched_binding_is_a_known_vm_rejection(self) -> None:
        cases = (
            (None, "BINDING_NOT_CONFIRMED"),
            ({"binding_digest": DIGEST_A}, "BINDING_MISMATCH"),
            ({"binding_digest": "sha256:bad"}, "BINDING_READ_FAILED"),
            (OSError(SECRET_MARKER), "BINDING_READ_FAILED"),
        )
        for answer, code in cases:
            with self.subTest(code=code, answer=type(answer).__name__):
                self.binding_answer = answer
                journal = self.journal()
                gateway, session = self.gateway(journal)
                with self.assertRaises(AuthorityDenied) as raised:
                    gateway.request(session, tool_call())
                self.assertEqual((raised.exception.code, raised.exception.vm_code),
                                 (AuthorityCode.VM_REJECTED, code))
                self.assertNotIn(SECRET_MARKER, str(raised.exception))
                self.assertIsNone(raised.exception.__context__)
                self.assertIsNone(journal.read_effect("effect-1"))
                self.assertEqual(gateway.unresolved_effects, frozenset())
                self.assertIsNone(gateway.effect_state("effect-1"))

    def test_attempt_reader_requires_a_running_matching_attempt(self) -> None:
        answers = {"value": running_attempt("succeeded")}
        journal = self.journal(attempt_reader=lambda attempt_id: answers["value"])
        gateway, session = self.gateway(journal)
        for value, code in ((running_attempt("succeeded"), "ATTEMPT_NOT_RUNNING"),
                            (running_attempt(task_id="task-2"), "ATTEMPT_MISMATCH"),
                            (running_attempt(attempt_id="attempt-2"), "ATTEMPT_MISMATCH"),
                            (["not", "mapping"], "ATTEMPT_READ_FAILED")):
            with self.subTest(code=code):
                answers["value"] = value
                with self.assertRaises(AuthorityDenied) as raised:
                    gateway.request(session, tool_call())
                self.assertEqual((raised.exception.code, raised.exception.vm_code),
                                 (AuthorityCode.VM_REJECTED, code))
                self.assertIsNone(journal.read_effect("effect-1"))
        # Normalized engine_port shape (lifted attempt_status) is accepted too.
        answers["value"] = {"project_id": "project-1", "attempt_id": "attempt-1", "task_id": "task-1",
                            "attempt_status": "running"}
        gateway.request(session, tool_call())
        self.assertEqual(journal.read_effect("effect-1")["state"], "intent_recorded")

    def test_restarted_gateway_cannot_obtain_a_second_non_idempotent_permission(self) -> None:
        first, session = self.gateway(self.journal())
        first.request(session, tool_call())
        restarted, new_session = self.gateway(self.journal())
        with self.assertRaises(AuthorityDenied) as raised:
            restarted.request(new_session, tool_call())
        self.assertEqual((raised.exception.code, raised.exception.vm_code),
                         (AuthorityCode.VM_REJECTED, "INTENT_ALREADY_RECORDED"))
        # An idempotent effect may be re-issued after restart (same intent).
        first.request(session, tool_call("call-2", "fs.update", "effect-2"))
        again, third_session = self.gateway(self.journal())
        decision = again.request(third_session, tool_call("call-2", "fs.update", "effect-2"))
        self.assertEqual(decision.effect_id, "effect-2")

    def test_unknown_receipt_stays_unresolved_and_cannot_be_overwritten(self) -> None:
        journal = self.journal()
        gateway, session = self.gateway(journal)
        gateway.request(session, tool_call())
        forwarded = gateway.forward_receipt(session, "call-1", "unknown")
        self.assertTrue(forwarded.reconciliation_required)
        self.assertEqual(gateway.reconcile("effect-1"), "unknown")
        self.assertIn("effect-1", gateway.unresolved_effects)
        with self.assertRaises(AuthorityDenied) as raised:
            gateway.forward_receipt(session, "call-1", "succeeded")
        self.assertEqual((raised.exception.code, raised.exception.vm_code),
                         (AuthorityCode.VM_REJECTED, "RECEIPT_CONFLICT"))
        self.assertEqual(journal.read_effect("effect-1")["state"], "unknown")

    def test_read_only_calls_do_not_touch_the_journal(self) -> None:
        journal = self.journal()
        gateway, session = self.gateway(journal)
        gateway.request(session, tool_call("call-read", "fs.read", None))
        self.assertEqual(self.binding_reads, [])


class JournalTests(JournalTestCase):
    def test_receipt_statuses_match_the_gateway(self) -> None:
        self.assertEqual(effect_journal.JOURNAL_RECEIPT_STATUSES, RECEIPT_STATUSES)

    def test_intent_reecord_is_idempotent_and_different_content_conflicts(self) -> None:
        journal = self.journal()
        echo = journal.record_intent(intent())
        self.assertEqual((echo["effect_id"], echo["request_digest"]), ("effect-1", DIGEST_R))
        self.assertEqual(journal.record_intent(intent()), echo)
        for change in ({"tool_call_id": "call-9"}, {"request_digest": DIGEST_A}, {"lease_fencing_token": 6}):
            with self.subTest(change=sorted(change)):
                with self.assertRaises(EffectJournalError) as raised:
                    journal.record_intent(intent(**change))
                self.assertEqual(raised.exception.code, "INTENT_CONFLICT")
        journal.record_intent(intent(effect_id="effect-n", effect_kind="non_idempotent", tool_id="fs.write"))
        with self.assertRaises(EffectJournalError) as raised:
            journal.record_intent(intent(effect_id="effect-n", effect_kind="non_idempotent", tool_id="fs.write"))
        self.assertEqual(raised.exception.code, "INTENT_ALREADY_RECORDED")

    def test_receipt_requires_matching_intent_and_is_idempotent(self) -> None:
        journal = self.journal()
        with self.assertRaises(EffectJournalError) as missing:
            journal.record_receipt(receipt())
        self.assertEqual(missing.exception.code, "INTENT_NOT_RECORDED")
        journal.record_intent(intent())
        for key, value in (("tool_call_id", "call-2"), ("request_digest", DIGEST_A), ("vm_attempt_id", "attempt-2"),
                           ("lease_id", "lease-2"), ("lease_fencing_token", 6), ("authorization_revision", 2)):
            with self.subTest(mismatch=key):
                with self.assertRaises(EffectJournalError) as raised:
                    journal.record_receipt(receipt(**{key: value}))
                self.assertEqual((raised.exception.code, raised.exception.subject), ("RECEIPT_MISMATCH", key))
        self.assertEqual(journal.read_effect("effect-1")["state"], "intent_recorded")
        self.assertEqual(journal.record_receipt(receipt()), {"effect_id": "effect-1", "state": "succeeded"})
        self.assertEqual(journal.record_receipt(receipt()), {"effect_id": "effect-1", "state": "succeeded"})
        with self.assertRaises(EffectJournalError) as conflict:
            journal.record_receipt(receipt(reported_status="failed"))
        self.assertEqual(conflict.exception.code, "RECEIPT_CONFLICT")
        self.assertIsNone(journal.read_effect("effect-absent"))

    def test_state_survives_restart(self) -> None:
        journal = self.journal()
        journal.record_intent(intent())
        journal.record_intent(intent(effect_id="effect-2", tool_call_id="call-2"))
        journal.record_receipt(receipt(reported_status="cancelled", receipt_ref="receipt-ref-1"))
        reopened = self.journal()
        self.assertEqual(reopened.read_effect("effect-1"), {"effect_id": "effect-1", "state": "cancelled"})
        self.assertEqual(reopened.read_effect("effect-2"), {"effect_id": "effect-2", "state": "intent_recorded"})
        self.assertEqual(reopened.record_receipt(receipt(reported_status="cancelled", receipt_ref="receipt-ref-1")),
                         {"effect_id": "effect-1", "state": "cancelled"})
        with self.assertRaises(EffectJournalError) as raised:
            reopened.record_intent(intent(tool_call_id="call-other"))
        self.assertEqual(raised.exception.code, "INTENT_CONFLICT")
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            self.assertEqual(connection.execute("PRAGMA application_id").fetchone()[0],
                             effect_journal.JOURNAL_APPLICATION_ID)

    def test_malformed_requests_are_refused_without_leaking_values(self) -> None:
        journal = self.journal()
        for bad in ({**intent(), "raw_secret": SECRET_MARKER}, intent(effect_kind="read_only"),
                    intent(authorization_revision=True), intent(binding_digest=SECRET_MARKER),
                    intent(effect_id="bad id"), ["not", "mapping"]):
            with self.subTest(case=repr(bad)[:40]):
                with self.assertRaises(EffectJournalError) as raised:
                    journal.record_intent(bad)
                self.assertEqual(raised.exception.code, "INTENT_INVALID")
                self.assertNotIn(SECRET_MARKER, str(raised.exception))
        journal.record_intent(intent())
        for bad in (receipt(reported_status="done"), receipt(receipt_ref="bad ref"),
                    {k: v for k, v in receipt().items() if k != "receipt_ref"}):
            with self.assertRaises(EffectJournalError) as raised:
                journal.record_receipt(bad)
            self.assertEqual(raised.exception.code, "RECEIPT_INVALID")
        with self.assertRaises(EffectJournalError) as raised:
            journal.read_effect("bad id")
        self.assertEqual(raised.exception.code, "INVALID_EFFECT_ID")
        self.assertNotIn(SECRET_MARKER.encode("ascii"), self.path.read_bytes())

    def test_naive_clock_is_refused_before_writing(self) -> None:
        journal = AikEffectJournal(self.path, binding_reader=self.read_binding, now=lambda: datetime(2029, 1, 1))
        with self.assertRaises(EffectJournalError) as raised:
            journal.record_intent(intent())
        self.assertEqual(raised.exception.code, "CLOCK_UNAVAILABLE")
        self.assertIsNone(journal.read_effect("effect-1"))

    def test_foreign_database_and_repository_paths_are_rejected(self) -> None:
        foreign = self.root / "foreign.sqlite3"
        with closing(sqlite3.connect(foreign)) as connection:
            connection.execute("CREATE TABLE unrelated (payload TEXT)")
            connection.execute("INSERT INTO unrelated VALUES ('synthetic')")
            connection.commit()
        before = foreign.read_bytes()
        with self.assertRaises(InvalidDatabase):
            AikEffectJournal(foreign, binding_reader=self.read_binding)
        self.assertEqual(foreign.read_bytes(), before)
        garbage = self.root / "garbage.sqlite3"
        garbage.write_bytes(b"not a database at all" * 10)
        with self.assertRaises(InvalidDatabase):
            AikEffectJournal(garbage, binding_reader=self.read_binding)
        project = self.root / "source-project"
        (project / ".git").mkdir(parents=True)
        inside = project / "data" / "effects.sqlite3"
        with self.assertRaises(UnsafeStoragePath):
            AikEffectJournal(inside, binding_reader=self.read_binding)
        self.assertFalse(inside.parent.exists())
        with self.assertRaises(TypeError):
            AikEffectJournal(self.path, binding_reader=None)


class FakeService:
    def __init__(self):
        self.calls: list = []
        self.failure = None
        self.response = None
        self.use_response = False

    def record_effect_receipt(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        if self.use_response:
            return self.response
        return {"task_id": kwargs["task_id"], "attempt_id": kwargs["attempt_id"],
                "provider_operation_id": kwargs["provider_operation_id"], "receipt_id": "effect-receipt-1"}


class CodedError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


IDENTITY = EffectIdentity("synthetic-provider", "synthetic-system", "repo://example/synthetic",
                          "account-synthetic", "create_issue", "task", "idem-key-0123456789",
                          "before_irreversible")


def external(**overrides) -> ExternalEffectReceipt:
    base = dict(task_id="task-ext-1", attempt_id="attempt-ext-1", provider_call_id="provider-call-1",
                runtime_intent_id="runtime-intent-1", runtime_receipt_id="runtime-receipt-1",
                thread_id="thread-1", turn_id="turn-1", identity=IDENTITY, provider_operation_id="operation-1",
                response_digest=DIGEST_A, target_observation_digest=None)
    base.update(overrides)
    return ExternalEffectReceipt(**base)


class ExternalEffectSubmitterTests(unittest.TestCase):
    def submitter(self, service, **kwargs):
        return ExternalEffectSubmitter(service, approved_identities={"task-ext-1": (IDENTITY,)}, **kwargs)

    def test_approved_identity_is_submitted_with_exact_keywords_and_never_confirmed(self) -> None:
        service = FakeService()
        result = self.submitter(service).submit(external())
        self.assertEqual(len(service.calls), 1)
        self.assertEqual(set(service.calls[0]), {
            "task_id", "attempt_id", "provider_call_id", "runtime_intent_id", "runtime_receipt_id", "thread_id",
            "turn_id", "effect_identity", "provider_operation_id", "response_digest", "target_observation_digest",
        })
        self.assertEqual(service.calls[0]["effect_identity"], IDENTITY.to_mapping())
        self.assertEqual(result.state, TARGET_REOBSERVATION_REQUIRED)
        self.assertFalse(result.effect_confirmed)
        self.assertEqual(result.vm_response["receipt_id"], "effect-receipt-1")
        with self.assertRaises(TypeError):
            result.vm_response["receipt_id"] = "x"  # type: ignore[index]

    def test_identity_factory_converts_the_identity(self) -> None:
        service = FakeService()
        self.submitter(service, identity_factory=lambda value: ("vm-type", value["operation"])).submit(external())
        self.assertEqual(service.calls[0]["effect_identity"], ("vm-type", "create_issue"))

    def test_unapproved_identity_or_task_is_refused_before_submission(self) -> None:
        service = FakeService()
        submitter = self.submitter(service)
        for receipt_value in (external(identity=replace(IDENTITY, operation="delete_issue")),
                              external(identity=replace(IDENTITY, idempotency_key="idem-key-9999999999")),
                              external(task_id="task-other")):
            with self.subTest(receipt=receipt_value.identity.operation + receipt_value.task_id):
                with self.assertRaises(ExternalEffectError) as raised:
                    submitter.submit(receipt_value)
                self.assertEqual((raised.exception.code, raised.exception.outcome),
                                 ("IDENTITY_NOT_APPROVED", EffectOutcome.KNOWN_FAILURE))
        self.assertEqual(service.calls, [])

    def test_vm_codes_are_preserved_and_uncoded_failures_are_unknown(self) -> None:
        for code in ("EFFECT_ADAPTER_OPERATION_REPLAY_CONFLICT", "EFFECT_ADAPTER_RECEIPT_CONFLICT"):
            with self.subTest(code=code):
                service = FakeService()
                service.failure = CodedError(code, "raw " + SECRET_MARKER)
                submitter = self.submitter(service)
                with self.assertRaises(ExternalEffectError) as raised:
                    submitter.submit(external())
                error = raised.exception
                self.assertEqual((error.code, error.vm_code, error.outcome),
                                 ("VM_REJECTED", code, EffectOutcome.KNOWN_FAILURE))
                self.assertNotIn(SECRET_MARKER, str(error) + repr(error))
                self.assertIsNone(error.__context__)
                self.assertEqual(submitter.unresolved_operations, frozenset())
        service = FakeService()
        service.failure = TimeoutError(SECRET_MARKER)
        submitter = self.submitter(service)
        with self.assertRaises(ExternalEffectError) as unknown:
            submitter.submit(external())
        self.assertEqual((unknown.exception.code, unknown.exception.outcome),
                         ("OUTCOME_UNKNOWN", EffectOutcome.UNKNOWN))
        self.assertNotIn(SECRET_MARKER, str(unknown.exception))
        key = ("synthetic-provider", "synthetic-system", "operation-1")
        self.assertEqual(submitter.unresolved_operations, frozenset({key}))
        self.assertEqual(len(service.calls), 1)  # no automatic retry
        service.failure = None
        submitter.submit(external())  # explicit identical resubmission
        self.assertEqual(submitter.unresolved_operations, frozenset())

    def test_malformed_or_mismatched_response_is_unknown(self) -> None:
        for response in (["not", "mapping"], None, {"attempt_id": "attempt-other"}):
            with self.subTest(response=repr(response)):
                service = FakeService()
                service.use_response = True
                service.response = response
                with self.assertRaises(ExternalEffectError) as raised:
                    self.submitter(service).submit(external())
                self.assertEqual((raised.exception.code, raised.exception.outcome),
                                 ("INVALID_RESPONSE", EffectOutcome.UNKNOWN))

    def test_provider_operation_is_bound_to_one_attempt(self) -> None:
        service = FakeService()
        submitter = self.submitter(service)
        submitter.submit(external())
        with self.assertRaises(ExternalEffectError) as replay:
            submitter.submit(external(attempt_id="attempt-ext-2"))
        self.assertEqual(replay.exception.code, "OPERATION_REPLAY_CONFLICT")
        with self.assertRaises(ExternalEffectError) as changed:
            submitter.submit(external(response_digest=DIGEST_B))
        self.assertEqual(changed.exception.code, "RECEIPT_CONFLICT")
        self.assertEqual(len(service.calls), 1)
        submitter.submit(external())  # identical, same attempt: idempotent resubmission
        self.assertEqual(len(service.calls), 2)
        # A rejected claim still binds the operation to its first attempt.
        service.failure = CodedError("PROVIDER_CALL_NOT_TERMINAL", "raw")
        with self.assertRaises(ExternalEffectError):
            submitter.submit(external(provider_operation_id="operation-2"))
        with self.assertRaises(ExternalEffectError) as rebound:
            submitter.submit(external(provider_operation_id="operation-2", attempt_id="attempt-ext-3"))
        self.assertEqual(rebound.exception.code, "OPERATION_REPLAY_CONFLICT")

    def test_identity_and_receipt_fields_are_validated(self) -> None:
        for change, subject in (({"idempotency_key": "short-key-15chr"}, "idempotency_key"),
                                ({"idempotency_key": "has space in the key"}, "idempotency_key"),
                                ({"checkpoint_policy": "sometimes"}, "checkpoint_policy"),
                                ({"provider": ""}, "provider"),
                                ({"target": "line\nbreak"}, "target")):
            with self.subTest(subject=subject, change=sorted(change)):
                with self.assertRaises(ExternalEffectError) as raised:
                    replace(IDENTITY, **change)
                self.assertEqual((raised.exception.code, raised.exception.subject), ("INVALID_REQUEST", subject))
        self.assertEqual(len(replace(IDENTITY, idempotency_key="k" * 16).idempotency_key), 16)
        for change, subject in (({"response_digest": "sha256:bad"}, "response_digest"),
                                ({"target_observation_digest": "md5:x"}, "target_observation_digest"),
                                ({"thread_id": "bad id"}, "thread_id"),
                                ({"identity": IDENTITY.to_mapping()}, "identity")):
            with self.subTest(subject=subject):
                with self.assertRaises(ExternalEffectError) as raised:
                    external(**change)
                self.assertEqual(raised.exception.subject, subject)

    def test_forged_frozen_identity_is_revalidated_on_submit(self) -> None:
        service = FakeService()
        # Forge a private copy; mutating the shared IDENTITY would leak into other tests.
        forged = external(identity=replace(IDENTITY))
        object.__setattr__(forged.identity, "checkpoint_policy", "sometimes")
        with self.assertRaises(ExternalEffectError) as raised:
            ExternalEffectSubmitter(service, approved_identities=(IDENTITY,)).submit(forged)
        self.assertEqual(raised.exception.subject, "checkpoint_policy")
        self.assertEqual(service.calls, [])
        with self.assertRaises(TypeError):
            ExternalEffectSubmitter(object(), approved_identities=())


if __name__ == "__main__":
    unittest.main()
