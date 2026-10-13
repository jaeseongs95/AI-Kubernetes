"""Adapter from an injected upstream ``EngineApplication`` to ``VmApplicationPort``.

The adapter targets the pinned upstream revision (vibemarshal 32bb0f9). It
calls only public façade methods on the injected application object. It does
not open the VM ledger, run SQL, or import VM internals when this module is
imported. The upstream exception classes are looked up lazily, and only when a
call fails.

The adapter does two things:

* It converts pydantic return values (``RunOnceResult``, ``AttemptDetail``,
  ``FinalReport`` and others) into plain JSON-like mappings with
  ``model_dump(mode="json")``.
* It normalizes the observed key layout into the shape that the bridge,
  workspace and controller read.

Key rewrites are limited to the following. Every other original VM key and
value is kept unchanged.

* The ``tasks`` list of ``status()`` is moved to ``vm_tasks`` because its row format
  differs from the controller's assumed task view.
* A ``history_cursor`` mapping (upstream ``HistoryCursor``) is replaced by its
  integer ``sequence``. The original mapping is kept unchanged under
  ``vm_history_cursor``. A mapping without a valid sequence is left as is, and
  the bridge rejects it.
* Nested IDs (for example ``project.id``/``project.root``,
  ``project.active_goal_revision_id``/``project.active_plan_revision_id``,
  ``attempt.*`` and ``runtime_job.attempt_id``) are copied to top-level keys
  only when the top-level key is absent or equal. A disagreement is a
  malformed response.
* ``job`` is added from ``runtime_job`` or the flat ``runtime_job_*`` fields.
* ``goal_state`` is an AIK-derived label, not a VM field. It is added to a
  status only when absent, from ``control_state`` (paused/cancelled) or
  ``authorization_state == "required"`` (``awaiting_authorization``).

Errors keep their original classification. ``EngineApplicationError`` keeps
its leading code (for example ``ATTEMPT_NOT_FOUND`` or ``RUNTIME_REQUIRED``).
``EngineLedgerError`` has no code upstream; only the exact observed
"project not found" message is translated to ``PROJECT_NOT_FOUND``. Raw
messages are never copied into the raised error, and the upstream exception is
not retained in ``__cause__`` or ``__context__``. Codes made by the adapter
itself are listed in ``ADAPTER_CODES``; the bridge never reports them as VM
codes.

Durable binding. The upstream binding extension
(``bind_execution``/``read_execution_binding``) does not exist upstream and is
not planned by the VM design. This adapter never exposes ``bind_execution``
and never declares ``aik-task-execution-binding-v1``, so the extension-based
bound execution of the bridge stays fail-closed (``SEAM_UNAVAILABLE``).

Instead, the hosted path uses the VM design seam: an ``AikHostedRuntime``
injected as the VM runtime (``EngineApplication(service, runtime=...)``). When
the adapter is given ``hosted_runtime=...`` it declares
``aik-hosted-runtime-v1`` in its handshake and exposes
``read_execution_binding(attempt_id)``, which reads the owner-confirmed
binding from the hosted runtime's AIK-owned journal (not from VM). Without a
hosted runtime the ``read_execution_binding`` attribute does not exist.

The adapter cannot prove that the injected application was really built with
that runtime: it does not read VM private attributes. That link is the
caller's responsibility; ``build_hosted_application`` builds both together to
make the correct wiring easy. Installed-wheel and Windows calls are NOT_RUN.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
import importlib
import inspect
import math
import os
import re
import threading
from types import MappingProxyType
from typing import Any, Mapping

from .hosted_runtime import AikHostedRuntime
from .surface import HOSTED_RUNTIME_EXTENSION, Command


_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,63}\Z")
_MAX_DEPTH = 64
_COMMAND_NAMES = tuple(command.value for command in Command)
_BINDING_METHODS = ("bind_execution", "read_execution_binding")
_APPLICATION_ERROR_MODULES = ("flowmarshal.engine.application", "flowmarshal.engine", "flowmarshal.engine.errors")
_LEDGER_ERROR_MODULES = ("flowmarshal.engine.ledger", "flowmarshal.engine", "flowmarshal.engine.errors")
# Keyword options of build_hosted_application that configure the port, not the factory.
_PORT_OPTIONS = ("surface_revision", "engine_schema_revision", "error_types")

PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"
INVALID_ENGINE_RESPONSE = "INVALID_ENGINE_RESPONSE"
ENGINE_APPLICATION_ERROR = "ENGINE_APPLICATION_ERROR"
ENGINE_LEDGER_ERROR = "ENGINE_LEDGER_ERROR"
ENGINE_METHOD_ABSENT = "ENGINE_METHOD_ABSENT"
PROJECT_MISMATCH = "PROJECT_MISMATCH"
# Classifications produced by this adapter, not codes returned by the VM.
# PROJECT_NOT_FOUND is excluded: it translates the VM's own "not found" result.
ADAPTER_CODES = frozenset({
    INVALID_ENGINE_RESPONSE, ENGINE_APPLICATION_ERROR, ENGINE_LEDGER_ERROR, ENGINE_METHOD_ABSENT,
    PROJECT_MISMATCH,
})
# EngineLedgerError carries no code. Only this exact observed upstream message
# is classified; any other ledger message stays ENGINE_LEDGER_ERROR.
LEDGER_MESSAGE_CODES = MappingProxyType({"프로젝트를 찾을 수 없습니다.": PROJECT_NOT_FOUND})


class EnginePortError(Exception):
    """A classified upstream failure without the upstream message text.

    ``code`` is a VM code, an adapter classification (``ADAPTER_CODES``), or
    ``None`` when the failure is unclassified. ``category`` is one of
    ``application``, ``ledger``, ``response`` or ``unexpected``.
    """

    def __init__(self, code: str | None, category: str):
        self.code = code
        self.category = category
        text = f"ENGINE_PORT_{category.upper()}"
        if code:
            text += f" code={code}"
        super().__init__(text)


@dataclass(frozen=True, slots=True)
class EngineProject:
    """The project as reported by ``status()["project"]`` (id and root)."""

    project_id: str
    root: str


def _lookup_error(modules: tuple, name: str):
    for module in modules:
        try:
            value = getattr(importlib.import_module(module), name, None)
        except Exception:
            continue
        if isinstance(value, type) and issubclass(value, BaseException):
            return value
    return None


def _plain(value: Any, depth: int = 0) -> Any:
    """Convert a façade result into JSON-like builtins, or raise."""
    if depth > _MAX_DEPTH:
        raise ValueError("depth")
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("float")
        return value
    if isinstance(value, Enum):
        return _plain(value.value, depth + 1)
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, os.PathLike):
        path = os.fspath(value)
        if not isinstance(path, str):
            raise TypeError("path")
        return path
    dump = getattr(value, "model_dump", None)
    if callable(dump) and not isinstance(value, type):
        return _plain(dump(mode="json"), depth + 1)
    items = None
    if isinstance(value, Mapping):
        items = list(value.items())
    elif callable(getattr(value, "keys", None)) and hasattr(value, "__getitem__") \
            and not isinstance(value, (bytes, bytearray)):
        items = [(key, value[key]) for key in value.keys()]
    if items is not None:
        result: dict[str, Any] = {}
        for key, item in items:
            if type(key) is not str:
                raise TypeError("key")
            result[key] = _plain(item, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_plain(item, depth + 1) for item in value]
    raise TypeError("unsupported")


def _invalid() -> EnginePortError:
    return EnginePortError(INVALID_ENGINE_RESPONSE, "response")


def _lift(data: dict, key: str, value: Any) -> None:
    """Copy a nested value to the top level; disagreement is malformed."""
    if value is None:
        return
    if key not in data or data[key] is None:
        data[key] = value
    elif data[key] != value:
        raise _invalid()


def _flatten_cursor(data: dict) -> None:
    cursor = data.get("history_cursor")
    if isinstance(cursor, dict):
        sequence = cursor.get("sequence")
        data["vm_history_cursor"] = cursor
        if type(sequence) is int and sequence >= 0:
            data["history_cursor"] = sequence
        # Otherwise the mapping remains and the bridge rejects it (fail-closed).


def _derive_goal_state(data: dict) -> str | None:
    """An AIK-derived label from control_state/authorization_state only."""
    control = data.get("control_state")
    if control in ("paused", "cancelled"):
        return control
    if data.get("authorization_state") == "required":
        return "awaiting_authorization"
    return None


def _normalize_status(data: dict) -> dict:
    project = data.get("project")
    if isinstance(project, dict):
        _lift(data, "project_id", project.get("id"))
        _lift(data, "project_root", project.get("root"))
        _lift(data, "plan_revision_id", project.get("active_plan_revision_id"))
        _lift(data, "active_plan_revision_id", project.get("active_plan_revision_id"))
        _lift(data, "active_goal_revision_id", project.get("active_goal_revision_id"))
    goal_state = _derive_goal_state(data)
    if goal_state is not None and "goal_state" not in data:
        data["goal_state"] = goal_state
    if "tasks" in data and "vm_tasks" not in data:
        # Upstream task rows are not the controller's TaskView layout.
        data["vm_tasks"] = data.pop("tasks")
    _flatten_cursor(data)
    return data


def _attach_job(data: dict, *, lift_ids: bool) -> None:
    if "job" in data:
        return
    job = data.get("runtime_job")
    if isinstance(job, dict):
        data["job"] = {"job_id": job.get("job_id"), "status": job.get("status")}
        if lift_ids:
            _lift(data, "attempt_id", job.get("attempt_id"))
            _lift(data, "task_id", job.get("task_id"))
    elif data.get("runtime_job_status") is not None or data.get("runtime_job_id") is not None:
        data["job"] = {"job_id": data.get("runtime_job_id"), "status": data.get("runtime_job_status")}


def _signature_text(function) -> str:
    """Parameter names only; the bridge compares names, never annotations."""
    parts: list[str] = []
    starred = False
    for parameter in inspect.signature(function).parameters.values():
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            parts.append("*" + parameter.name)
            starred = True
        elif parameter.kind is inspect.Parameter.KEYWORD_ONLY:
            if not starred:
                parts.append("*")
                starred = True
            parts.append(parameter.name)
        elif parameter.kind is inspect.Parameter.VAR_KEYWORD:
            parts.append("**" + parameter.name)
        else:
            parts.append(parameter.name)
    return ", ".join(parts)


class EngineApplicationPort:
    """``VmApplicationPort`` over an injected upstream ``EngineApplication``.

    ``surface_revision`` and ``engine_schema_revision`` describe the installed
    upstream. The adapter cannot discover them through the public façade, so
    the caller must declare them. When they are omitted, the handshake is
    malformed and the bridge refuses every call.

    ``error_types`` may inject ``(application_error, ledger_error)`` classes
    (used by fake tests). By default they are imported lazily from the
    upstream package. When it is not importable, failures are unclassified.

    ``hosted_runtime`` declares the AIK hosted runtime path
    (``aik-hosted-runtime-v1``). The caller must have built the application
    with that same runtime (see ``build_hosted_application``); the adapter
    cannot verify it.
    """

    def __init__(self, application: object, *, surface_revision: str | None = None,
                 engine_schema_revision: int | None = None, error_types: tuple | None = None,
                 hosted_runtime: AikHostedRuntime | None = None):
        if application is None:
            raise TypeError("An EngineApplication instance is required.")
        if surface_revision is not None and (type(surface_revision) is not str or not surface_revision):
            raise TypeError("surface_revision must be a nonempty string.")
        if engine_schema_revision is not None and type(engine_schema_revision) is not int:
            raise TypeError("engine_schema_revision must be an integer.")
        if error_types is not None:
            if not isinstance(error_types, tuple) or len(error_types) != 2 or any(
                item is not None and not (isinstance(item, type) and issubclass(item, BaseException))
                for item in error_types
            ):
                raise TypeError("error_types must be (application_error, ledger_error).")
        if hosted_runtime is not None and not isinstance(hosted_runtime, AikHostedRuntime):
            raise TypeError("hosted_runtime must be an AikHostedRuntime.")
        self._app = application
        self._surface_revision = surface_revision
        self._schema_revision = engine_schema_revision
        self._error_types = error_types
        self._hosted_runtime = hosted_runtime
        self._lock = threading.Lock()
        self._calls: list[str] = []
        if hosted_runtime is not None:
            # Exposed only on the hosted path; absent otherwise (hasattr False).
            self.read_execution_binding = self._read_execution_binding

    # -- diagnostics --
    @property
    def calls(self) -> tuple:
        """Names of façade methods invoked so far (metadata only)."""
        with self._lock:
            return tuple(self._calls)

    @property
    def upstream_binding_methods_present(self) -> bool:
        """Whether the application has both durable-binding methods (False upstream)."""
        return all(callable(getattr(self._app, name, None)) for name in _BINDING_METHODS)

    @property
    def hosted_runtime(self) -> AikHostedRuntime | None:
        return self._hosted_runtime

    # -- internals --
    def _resolve_error_types(self) -> tuple:
        with self._lock:
            if self._error_types is None:
                self._error_types = (
                    _lookup_error(_APPLICATION_ERROR_MODULES, "EngineApplicationError"),
                    _lookup_error(_LEDGER_ERROR_MODULES, "EngineLedgerError"),
                )
            return self._error_types

    def _classify(self, error: Exception) -> EnginePortError:
        application_error, ledger_error = self._resolve_error_types()
        try:
            text = str(error)
        except Exception:
            text = ""
        if application_error is not None and isinstance(error, application_error):
            head = text.split(":", 1)[0].strip()
            return EnginePortError(head if _CODE.fullmatch(head) else ENGINE_APPLICATION_ERROR, "application")
        if ledger_error is not None and isinstance(error, ledger_error):
            return EnginePortError(LEDGER_MESSAGE_CODES.get(text.strip(), ENGINE_LEDGER_ERROR), "ledger")
        return EnginePortError(None, "unexpected")

    def _invoke(self, name: str, *args, **kwargs) -> dict:
        function = getattr(self._app, name, None)
        if not callable(function):
            raise EnginePortError(ENGINE_METHOD_ABSENT, "application")
        with self._lock:
            self._calls.append(name)
        failure = None
        try:
            result = function(*args, **kwargs)
        except Exception as error:
            failure = self._classify(error)
        if failure is not None:
            # Raised outside the handler: neither __cause__ nor __context__
            # keeps the upstream exception or its message.
            raise failure from None
        data = None
        try:
            data = _plain(result)
        except Exception:
            failure = _invalid()
        if failure is not None:
            raise failure from None
        if not isinstance(data, dict):
            raise _invalid()
        return data

    def _read_execution_binding(self, attempt_id: str) -> dict | None:
        """Owner-confirmed binding from the AIK journal (hosted path only), not from VM."""
        return self._hosted_runtime.store.read_execution_binding(attempt_id)

    # -- handshake (introspection of the installed façade) --
    def handshake(self) -> dict:
        methods: dict[str, str] = {}
        for name in _COMMAND_NAMES:
            function = getattr(self._app, name, None)
            if callable(function):
                try:
                    methods[name] = _signature_text(function)
                except (TypeError, ValueError):
                    continue
        return {
            "surface_revision": self._surface_revision,
            "engine_schema_revision": self._schema_revision,
            "methods": methods,
            # The upstream binding extension does not exist; only the AIK
            # hosted runtime path can be declared.
            "extensions": [HOSTED_RUNTIME_EXTENSION] if self._hosted_runtime is not None else [],
        }

    # -- VmApplicationPort --
    def prepare(self, project_id: str, *, source_request: str, candidate_count: int | None = None) -> dict:
        data = self._invoke("prepare", project_id, source_request=source_request, candidate_count=candidate_count)
        _flatten_cursor(data)
        return data

    def revise(self, project_id: str, *, source_request: str, candidate_count: int | None = None) -> dict:
        data = self._invoke("revise", project_id, source_request=source_request, candidate_count=candidate_count)
        _flatten_cursor(data)
        return data

    def authorize(self, project_id: str, *, source: str, selected_plan_revision_id: str | None = None) -> dict:
        data = self._invoke("authorize", project_id, source=source,
                            selected_plan_revision_id=selected_plan_revision_id)
        _flatten_cursor(data)
        return data

    def run_once(self, project_id: str, *, resume: bool = False) -> dict:
        data = self._invoke("run_once", project_id, resume=resume)
        _attach_job(data, lift_ids=False)
        _flatten_cursor(data)
        return data

    def observe(self, project_id: str) -> dict:
        data = self._invoke("observe", project_id)
        _attach_job(data, lift_ids=True)
        nested = data.get("status")
        if isinstance(nested, dict):
            _normalize_status(nested)
        _flatten_cursor(data)
        return data

    def pause(self, project_id: str, *, reason: str) -> dict:
        data = self._invoke("pause", project_id, reason=reason)
        _attach_job(data, lift_ids=False)
        return data

    def cancel(self, project_id: str, *, reason: str) -> dict:
        data = self._invoke("cancel", project_id, reason=reason)
        _attach_job(data, lift_ids=False)
        return data

    def status(self, project_id: str) -> dict:
        return _normalize_status(self._invoke("status", project_id))

    def replan(self, project_id: str, *, rationale: str) -> dict:
        data = self._invoke("replan", project_id, rationale=rationale)
        _flatten_cursor(data)
        return data

    def final_report(self, project_id: str, *, goal_verdict_id: str | None = None) -> dict:
        data = self._invoke("final_report", project_id, goal_verdict_id=goal_verdict_id)
        _flatten_cursor(data)
        return data

    def attempt_detail(self, attempt_id: str) -> dict:
        data = self._invoke("attempt_detail", attempt_id)
        attempt = data.get("attempt")
        if isinstance(attempt, dict):
            _lift(data, "attempt_id", attempt.get("attempt_id"))
            _lift(data, "task_id", attempt.get("task_id"))
            _lift(data, "execution_spec_digest", attempt.get("execution_spec_digest"))
            _lift(data, "attempt_status", attempt.get("status"))
        _flatten_cursor(data)
        return data

    def model_binding_status(self, project_id: str) -> dict:
        data = self._invoke("model_binding_status", project_id)
        _flatten_cursor(data)
        return data

    # -- project lookup helpers --
    def lookup_project(self, project_id: str) -> EngineProject | None:
        """Project id/root from ``status()["project"]``; None only for PROJECT_NOT_FOUND."""
        try:
            data = self.status(project_id)
        except EnginePortError as error:
            if error.code == PROJECT_NOT_FOUND:
                return None
            raise
        reported, root = data.get("project_id"), data.get("project_root")
        if reported != project_id:
            raise EnginePortError(PROJECT_MISMATCH, "response")
        if type(root) is not str or not root:
            raise _invalid()
        return EngineProject(project_id, root)

    def project_exists(self, project_id: str) -> bool:
        return self.lookup_project(project_id) is not None


def build_hosted_application(engine_application_factory, service, hosted_runtime: AikHostedRuntime,
                             **kwargs) -> EngineApplicationPort:
    """Build ``EngineApplication(service, runtime=hosted_runtime, **kwargs)`` and its hosted port.

    ``engine_application_factory`` is normally the upstream ``EngineApplication``
    class supplied by the caller (this module never imports it). The port
    options ``surface_revision``, ``engine_schema_revision`` and
    ``error_types`` are taken out of ``kwargs`` and given to the port, not to
    the factory. A ``runtime`` keyword is refused: the hosted runtime is the
    only runtime this helper wires.
    """
    if not callable(engine_application_factory):
        raise TypeError("engine_application_factory must be callable.")
    if not isinstance(hosted_runtime, AikHostedRuntime):
        raise TypeError("hosted_runtime must be an AikHostedRuntime.")
    if "runtime" in kwargs:
        raise TypeError("The runtime is the hosted runtime; do not pass another one.")
    options = {name: kwargs.pop(name) for name in _PORT_OPTIONS if name in kwargs}
    application = engine_application_factory(service, runtime=hosted_runtime, **kwargs)
    return EngineApplicationPort(application, hosted_runtime=hosted_runtime, **options)
