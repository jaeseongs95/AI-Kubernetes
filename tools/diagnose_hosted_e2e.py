"""Diagnostic end-to-end run of the AIK hosted runtime under the real VM (no model calls).

This is a diagnostic harness, not product qualification. It answers one
question that unit tests cannot: when the installed VM (vibemarshal 32bb0f9)
runs a Task through its RuntimeJob supervisor, does every provider effect pass
through ``AikHostedRuntime`` inside the VM operation trace scope, with the AIK
binding durably recorded before the effect, and can ``VmBridge.confirm_attempt``
complete the owner-confirmed binding afterwards?

How it runs:

* The Goal and Plan are prepared with the VM's own synthetic E2E preparation
  (``flowmarshal.engine.e2e_qualification._prepare`` and its
  ``tests/fixtures/engine/project-e2e`` fixture). Both are VM internals used
  only by this diagnostic, never by AIK product code. ``--vm-repo`` is a VM
  checkout (for ``config/`` and the fixture).
* The VM wheel deliberately leaves its developer modules out
  (``e2e_qualification``, ``qualification``, ``evaluation`` and others; see the
  VM ``setup.py``). ``--vm-source`` therefore imports the whole ``flowmarshal``
  package from the VM source checkout instead of an installed wheel; the report
  records the import origin. This is source-tree evidence, not installed-wheel
  evidence.
* The provider is VM's deterministic ``FakeCodexRuntime`` wrapped by
  ``AikHostedRuntime``. No model, account or network is used.
* The governance gate is a diagnostic allow gate, because the AGS A2 gate is
  not available yet (AIK-081). A PASS here is therefore not AGS conformance,
  not product qualification and not integration completion.

The VM 1.0 RuntimeJob owner lock is Windows-only; on POSIX the run stops at
``RUNTIME_OWNER_LOCK_UNAVAILABLE`` and the report says NOT_RUN.

    python tools/diagnose_hosted_e2e.py --vm-repo PATH --output report.json
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import threading
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_runtime import Lease, Placement, Pool  # noqa: E402
from aik_vm_bridge import (  # noqa: E402
    PINNED_SURFACE_REVISION, SUPPORTED_ENGINE_SCHEMA_REVISIONS, VmBridge, VmBridgeError,
)
from aik_vm_bridge.engine_port import build_hosted_application  # noqa: E402
from aik_vm_bridge.hosted_runtime import (  # noqa: E402
    HOSTED_TASK_PENDING, AikHostedRuntime, ExecutionBindingStore, HostedAssignment,
)

SURFACE_PATH = ROOT / "contracts/upstream/vm-public-surface.json"
DIGEST = "sha256:" + "0" * 64
FIXED_APP = "def add(left: int, right: int) -> int:\n    return left + right\n"


class DiagnosticAllowGate:
    """Same shape as VM's test allow gate; it checks nothing (not AGS)."""

    def before_execution(self, task):
        return None

    def before_completion(self, task):
        return None

    def close(self):
        return None


class DiagnosticAllowGovernance:
    def check_conformance(self):
        return {"verdict": "PASS", "checks": [], "provenance": "local_derived"}

    def open_gate(self, service, *, runtime, roles, runner):
        return DiagnosticAllowGate()


class ObservedInner:
    """Delegates to VM FakeCodexRuntime and records what the journal held at each effect."""

    EFFECTS = ("create_thread", "start_turn", "resume", "interrupt")

    def __init__(self, inner, store_ref: list, log: list):
        self._inner = inner
        self._store_ref = store_ref
        self._log = log

    def __getattr__(self, name):
        target = getattr(self._inner, name)
        if name not in self.EFFECTS or not callable(target):
            return target

        def observed(*args, **kwargs):
            store = self._store_ref[0]
            inflight = [dict(row) for row in store.inflight_effects()]
            self._log.append({"operation": name, "thread": threading.current_thread().name,
                              "main_thread": threading.current_thread() is threading.main_thread(),
                              "inflight_at_call": inflight})
            return target(*args, **kwargs)
        return observed


class RecordingHostedRuntime(AikHostedRuntime):
    """Records every port call made by VM and any refusal, then re-raises it unchanged."""

    PORT_METHODS = ("verify_execution_policy", "list_models", "create_thread", "start_turn", "read",
                    "resume", "interrupt", "read_stored")

    def __init__(self, *args, call_log: list, **kwargs):
        super().__init__(*args, **kwargs)
        self._call_log = call_log

    def __getattribute__(self, name):
        value = super().__getattribute__(name)
        if name not in RecordingHostedRuntime.PORT_METHODS:
            return value
        log = super().__getattribute__("_call_log")

        def recorded(*args, **kwargs):
            entry = {"method": name, "thread": threading.current_thread().name}
            log.append(entry)
            try:
                return value(*args, **kwargs)
            except BaseException as error:
                entry["error"] = type(error).__name__
                entry["code"] = str(getattr(error, "code", "") or "")
                entry["subject"] = str(getattr(error, "subject", "") or "")
                raise
        return recorded


def _installed_commit() -> str | None:
    try:
        import importlib.metadata
        text = importlib.metadata.distribution("flowmarshal-engine").read_text("direct_url.json")
        return (json.loads(text or "{}").get("vcs_info") or {}).get("commit_id")
    except Exception:
        return None


def _inventory(roles):
    from flowmarshal.engine.model_lock import RUNTIME_CAPABILITIES, ModelCapability, ModelInventory
    grouped: dict[str, set[str]] = {}
    for role in ("normalizer", "skeleton_generator", "plan_expander", "general_reviewer",
                 "critical_reviewer", "executor", "validator"):
        binding = roles.binding_for(role)
        grouped.setdefault(binding.model, set()).add(binding.effort)
    return ModelInventory(
        executable_digest=DIGEST, runtime_capabilities=RUNTIME_CAPABILITIES,
        source="aik-hosted-e2e-diagnostic-model-list",
        models=tuple(ModelCapability(model=model, supported_efforts=tuple(sorted(efforts)))
                     for model, efforts in sorted(grouped.items())),
    )


def _value(item, name):
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def run(vm_repo: Path, work: Path) -> dict:
    from flowmarshal.engine.application import EngineApplication
    from flowmarshal.engine.e2e_qualification import _copy_fixture, _prepare
    from flowmarshal.engine.qualification import default_role_configuration
    from flowmarshal.engine.runtime import FakeCodexRuntime

    report: dict = {"steps": [], "checks": {}, "effects": []}
    step = report["steps"].append

    roles = default_role_configuration(vm_repo)
    models = _inventory(roles)
    workspace, _ = _copy_fixture(vm_repo, work)
    prepared = _prepare(workspace=workspace, state_root=work / "state", inventory=models, roles=roles)
    project_id = prepared.project_id
    step({"step": "prepare", "project_id": project_id, "plan_revision_id": prepared.plan_revision_id})

    store = ExecutionBindingStore(work / "aik" / "bindings.sqlite3")
    effects: list = report["effects"]
    inner = ObservedInner(FakeCodexRuntime(models), [store], effects)
    holder: dict = {}
    assignment_calls: list = []
    expiry = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat().replace("+00:00", "Z")

    def assignment_source(cwd, trace):
        attempt = None if trace is None else trace.attempt_id
        # The Task is learned at owner confirmation: VM refuses a host re-entry
        # (e.g. attempt_detail) from inside a role scope. The probe below records
        # that refusal once as evidence; the placement never guesses the Task.
        task = HOSTED_TASK_PENDING
        lookup_error = None
        if attempt is not None and not holder.get("reentry_probed"):
            holder["reentry_probed"] = True
            try:
                holder["app"].attempt_detail(attempt)
                lookup_error = "allowed"
            except Exception as error:
                lookup_error = f"{type(error).__name__}: {str(error)[:200]}"
        key = attempt or "preparation"
        assignment_calls.append({"attempt": attempt, "intent": None if trace is None else trace.intent_id,
                                 "call": None if trace is None else trace.call_id,
                                 "thread": threading.current_thread().name,
                                 "main_thread": threading.current_thread() is threading.main_thread(),
                                 "reentry_probe": lookup_error})
        pool = Pool(id="pool-1", provider="codex", connection_id="connection-1", enabled=True)
        placement = Placement(id=f"placement-{key}", pool_id="pool-1", profile_id="profile-1",
                              vm_instance_id="vm-instance-1", vm_project_id=project_id, vm_task_id=task,
                              vm_attempt_id=key, state="assigned")
        lease = Lease(id=f"lease-{key}", placement_id=placement.id, pool_id="pool-1", worker_id="worker-1",
                      fencing_token=1, expires_at=expiry, state="active")
        return HostedAssignment(
            vm_instance_id="vm-instance-1", vm_project_id=project_id, cluster_id="cluster-1",
            route_id="route-1", route_revision="1", connection_id="connection-1", connection_revision="1",
            provider="codex", auth_kind="native_subscription", provider_account_fingerprint="account-diagnostic",
            pool_id="pool-1", placement_id=placement.id, lease_id=lease.id, lease_fencing_token=1,
            support_matrix_digest=DIGEST, input_manifest_digest=DIGEST, workspace_root_ref="workspace://diagnostic",
            placement=placement, lease=lease, pool=pool,
        )

    port_calls: list = []
    report["port_calls"] = port_calls
    hosted = RecordingHostedRuntime(lambda connection_id: inner, store, assignment_source, call_log=port_calls)
    port = build_hosted_application(
        EngineApplication, prepared.service, hosted, governance=DiagnosticAllowGovernance(),
        surface_revision=PINNED_SURFACE_REVISION, engine_schema_revision=SUPPORTED_ENGINE_SCHEMA_REVISIONS[0])
    app = port._app if hasattr(port, "_app") else None
    if app is None:
        raise RuntimeError("diagnostic needs the application object behind the port")
    holder["app"] = app
    bridge = VmBridge(port, SURFACE_PATH, binding_store=store)

    def tick(**kwargs):
        result = app.run_once(project_id, **kwargs)
        action = str(_value(result, "action"))
        entry = {"step": "run_once", "action": action, "attempt_id": _value(result, "attempt_id"),
                 "blocker_code": _value(result, "blocker_code"), "detail": str(_value(result, "detail") or "")[:300]}
        last = report["steps"][-1] if report["steps"] else None
        if last is not None and {k: v for k, v in last.items() if k != "repeat"} == entry:
            last["repeat"] = last.get("repeat", 1) + 1
        else:
            step(entry)
        return result, action

    result, action = tick(proposal=prepared.proposal)
    for _ in range(400):
        if action.endswith("materialized"):
            break
        if action.endswith("blocked"):
            report["blocked"] = {"blocker_code": _value(result, "blocker_code"),
                                 "detail": str(_value(result, "detail") or "")[:300]}
            return report
        time.sleep(0.05)
        result, action = tick()
    if not action.endswith("materialized"):
        report["stopped_at"] = "materialize"
        return report

    first, action = tick()
    attempt_id = _value(first, "attempt_id")
    if not action.endswith("dispatched") or attempt_id is None:
        report["stopped_at"] = "dispatch"
        return report
    thread_id = turn_id = None
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        binding = _value(_value(app.attempt_detail(attempt_id), "attempt"), "binding")
        thread_id, turn_id = _value(binding, "thread_id"), _value(binding, "turn_id")
        if turn_id is not None:
            break
        time.sleep(0.05)
    step({"step": "worker_bound", "attempt_id": attempt_id, "thread_id": thread_id, "turn_id": turn_id})

    stored = store.read_attempt(attempt_id)
    task_effects = [e for e in effects if e["operation"] in ("create_thread", "start_turn")]
    checks = report["checks"]
    checks["dispatch_reached_hosted_runtime"] = bool(task_effects)
    checks["attempt_binding_recorded"] = stored is not None
    checks["trace_intents_recorded"] = bool(stored and stored.intents
                                            and all(i.vm_intent_id for i in stored.intents))
    checks["binding_before_every_effect"] = bool(task_effects) and all(
        any(row.get("vm_attempt_id") == attempt_id for row in e["inflight_at_call"]) for e in task_effects)
    checks["effects_off_main_thread"] = [not e["main_thread"] for e in task_effects]
    checks["inflight_cleared_after_success"] = all(
        row.get("vm_attempt_id") != attempt_id for row in (dict(r) for r in store.inflight_effects()))
    try:
        confirmation = bridge.confirm_attempt(project_id, attempt_id)
        checks["owner_confirmed"] = confirmation.state == "confirmed"
        step({"step": "confirm_attempt", "state": confirmation.state, "vm_task_id": confirmation.vm_task_id,
              "binding_digest": confirmation.binding_digest})
    except VmBridgeError as error:
        checks["owner_confirmed"] = False
        step({"step": "confirm_attempt", "error": str(error)})

    # Continue the Task to its verdict (extended; informational).
    if thread_id is not None:
        (workspace / "app.py").write_text(FIXED_APP, encoding="utf-8")
        inner.complete(thread_id, response="hosted diagnostic complete")
        goal_step = None
        steps = getattr(prepared.proposal, "validation_steps", None) or ()
        if steps:
            goal_step = steps[0].model_copy(update={"validation_id": "validation_goal"})
        # Same driving pattern as VM's own product-path E2E: every tick carries the
        # Goal Test step, and the asynchronous RuntimeJobs get time between ticks.
        deadline = time.monotonic() + 180
        result, action = tick(goal_validation_step=goal_step)
        while time.monotonic() < deadline:
            if _value(result, "goal_verdict_id") is not None or action.endswith("blocked"):
                break
            time.sleep(0.25)
            result, action = tick(goal_validation_step=goal_step)
        report["goal_verdict_id"] = _value(result, "goal_verdict_id")
        report["goal_final_action"] = action
    report["assignment_calls"] = assignment_calls
    report["effect_records"] = [dict(row) for row in store.effect_records()]
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vm-repo", type=Path, required=True)
    parser.add_argument("--vm-source", type=Path,
                        help="VM source directory (src) to import flowmarshal from instead of an installed wheel.")
    parser.add_argument("--vm-commit", help="Commit of the VM checkout, recorded as given (informational).")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require", action="store_true", help="Exit non-zero unless the key checks pass.")
    args = parser.parse_args(argv)
    if args.vm_source is not None:
        sys.path.insert(0, str(args.vm_source.resolve()))
    try:
        import flowmarshal
        origin = str(Path(flowmarshal.__file__).resolve().parent)
    except Exception as error:
        origin = f"unimportable: {type(error).__name__}"
    report: dict = {
        "format": "aik-hosted-e2e-diagnostic-v1",
        "platform": platform.platform(), "os_name": os.name, "python": platform.python_version(),
        "vm_installed_commit": None if args.vm_source is not None else _installed_commit(),
        "vm_import": "source" if args.vm_source is not None else "installed",
        "vm_import_origin": origin,
        "vm_checkout_commit": args.vm_commit,
        "scope": ("Diagnostic only: VM synthetic preparation, VM FakeCodexRuntime, diagnostic allow gate (not AGS). "
                  "Not product qualification and not integration completion."),
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    with tempfile.TemporaryDirectory(prefix="aik-hosted-e2e-", ignore_cleanup_errors=True) as directory:
        try:
            report.update(run(args.vm_repo.resolve(), Path(directory)))
        except Exception as error:
            report["error"] = f"{type(error).__name__}: {str(error)[:500]}"
            report["traceback_tail"] = traceback.format_exc().splitlines()[-12:]
    checks = report.get("checks", {})
    key = ("dispatch_reached_hosted_runtime", "attempt_binding_recorded", "trace_intents_recorded",
           "binding_before_every_effect", "inflight_cleared_after_success", "owner_confirmed")
    if os.name != "nt" and "RUNTIME_OWNER_LOCK_UNAVAILABLE" in json.dumps(report, ensure_ascii=False):
        report["status"] = "NOT_RUN_POSIX_RUNTIME_OWNER_LOCK"
    elif checks and all(checks.get(name) is True for name in key):
        report["status"] = "DIAGNOSTIC_PASS"
    else:
        report["status"] = "DIAGNOSTIC_FAIL"
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    if args.require:
        return 0 if report["status"] == "DIAGNOSTIC_PASS" else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
