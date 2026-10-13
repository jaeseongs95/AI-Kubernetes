"""Declared VM public surface, capability preflight and version handshake.

The declared surface is the AIK-001 projection (``vm-public-surface.json``).
Only method names and parameter names are compared; the bridge never imports
the VM package or reads its storage.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .errors import BridgeCode, EffectOutcome, VmBridgeError


SURFACE_FORMAT = "aik-upstream-surface-v1"
PINNED_SURFACE_REVISION = "32bb0f9dd9f024045d24487312b50f5b703573a3"
SUPPORTED_ENGINE_SCHEMA_REVISIONS = (4,)
BINDING_EXTENSION = "aik-task-execution-binding-v1"
# integration-contract.json unresolved gates that block AIK-031 completion.
BLOCKING_GATES = ("AGS_HOST_INTEGRATION_MANIFEST_MISSING", "AIK_DURABLE_ROUTE_BINDING_NOT_IMPLEMENTED")

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")


class Command(StrEnum):
    """Values are the exact public EngineApplication method names."""

    PREPARE = "prepare"
    REVISE = "revise"
    AUTHORIZE = "authorize"
    RUN_ONCE = "run_once"
    OBSERVE = "observe"
    PAUSE = "pause"
    CANCEL = "cancel"
    STATUS = "status"
    REPLAN = "replan"
    FINAL_REPORT = "final_report"
    ATTEMPT_DETAIL = "attempt_detail"
    MODEL_BINDING_STATUS = "model_binding_status"


REQUIRED_PARAMETERS = MappingProxyType({
    Command.PREPARE: ("project_id", "source_request", "candidate_count"),
    Command.REVISE: ("project_id", "source_request", "candidate_count"),
    Command.AUTHORIZE: ("project_id", "source", "selected_plan_revision_id"),
    Command.RUN_ONCE: ("project_id", "resume"),
    Command.OBSERVE: ("project_id",),
    Command.PAUSE: ("project_id", "reason"),
    Command.CANCEL: ("project_id", "reason"),
    Command.STATUS: ("project_id",),
    Command.REPLAN: ("project_id", "rationale"),
    Command.FINAL_REPORT: ("project_id", "goal_verdict_id"),
    Command.ATTEMPT_DETAIL: ("attempt_id",),
    Command.MODEL_BINDING_STATUS: ("project_id",),
})


def parameter_names(signature: str) -> tuple[str, ...]:
    """Split a projected signature at top-level commas; omit self, * and /."""
    if type(signature) is not str:
        raise ValueError("signature")
    depth = 0
    quote = ""
    escaped = False
    parts: list[str] = []
    current: list[str] = []
    for char in signature:
        if quote:
            # Inside a quoted default value: brackets and commas are literal text.
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            current.append(char)
            continue
        if char in "'\"":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth < 0:
                raise ValueError("signature")
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if depth != 0 or quote:
        raise ValueError("signature")
    parts.append("".join(current))
    names = []
    for part in parts:
        text = part.strip()
        if not text or text in ("*", "/"):
            continue
        name = re.split(r"[:=]", text, maxsplit=1)[0].strip().lstrip("*")
        if _NAME.fullmatch(name) is None:
            raise ValueError("signature")
        if name != "self":
            names.append(name)
    return tuple(names)


@dataclass(frozen=True, slots=True)
class DeclaredSurface:
    revision: str
    methods: Mapping[str, str]


def load_surface(source: Mapping[str, Any] | str | Path) -> DeclaredSurface:
    """Read the EngineApplication methods from a declared surface projection."""
    def invalid() -> VmBridgeError:
        return VmBridgeError(BridgeCode.INVALID_SURFACE, subject="vm-public-surface")

    try:
        if isinstance(source, (str, Path)):
            data = json.loads(Path(source).read_text(encoding="utf-8"))
        else:
            data = source
    except (OSError, ValueError):
        raise invalid() from None
    if not isinstance(data, Mapping) or data.get("format") != SURFACE_FORMAT:
        raise invalid()
    revision = data.get("revision")
    classes = data.get("classes")
    if type(revision) is not str or not revision or not isinstance(classes, list):
        raise invalid()
    matches = [entry for entry in classes if isinstance(entry, Mapping) and entry.get("name") == "EngineApplication"]
    if len(matches) != 1 or not isinstance(matches[0].get("methods"), list):
        raise invalid()
    methods: dict[str, str] = {}
    for method in matches[0]["methods"]:
        if not isinstance(method, Mapping):
            raise invalid()
        name, signature = method.get("name"), method.get("signature")
        if type(name) is not str or type(signature) is not str or name in methods:
            raise invalid()
        if name != "__init__":
            methods[name] = signature
    return DeclaredSurface(revision, MappingProxyType(methods))


@dataclass(frozen=True, slots=True)
class CapabilityReport:
    revision: str
    available: tuple[Command, ...]
    missing: tuple[tuple[str, str], ...]

    @property
    def ok(self) -> bool:
        return not self.missing

    def require(self) -> None:
        if self.missing:
            raise VmBridgeError(
                BridgeCode.CAPABILITY_MISSING, subject=self.missing[0][0],
                details=tuple(f"{command}:{reason}" for command, reason in self.missing),
            )


def preflight(surface: DeclaredSurface, commands: Iterable[Command]) -> CapabilityReport:
    """Check that every requested command and its used parameters are declared."""
    if type(surface) is not DeclaredSurface:
        raise VmBridgeError(BridgeCode.INVALID_SURFACE, subject="vm-public-surface")
    available, missing = [], []
    for command in commands:
        command = Command(command)
        signature = surface.methods.get(command.value)
        if signature is None:
            missing.append((command.value, "method_absent"))
            continue
        try:
            names = parameter_names(signature)
        except ValueError:
            missing.append((command.value, "signature_unparseable"))
            continue
        absent = [name for name in REQUIRED_PARAMETERS[command] if name not in names]
        if absent:
            missing.extend((command.value, f"parameter_absent:{name}") for name in absent)
        else:
            available.append(command)
    return CapabilityReport(surface.revision, tuple(available), tuple(missing))


@dataclass(frozen=True, slots=True)
class Handshake:
    surface_revision: str
    engine_schema_revision: int
    extensions: frozenset


def check_handshake(surface: DeclaredSurface, raw: object, commands: Iterable[Command]) -> Handshake:
    """Compare the installed façade description with the declared surface."""
    invalid = VmBridgeError(BridgeCode.INVALID_RESPONSE, subject="handshake")
    if not isinstance(raw, Mapping):
        raise invalid
    revision = raw.get("surface_revision")
    schema_revision = raw.get("engine_schema_revision")
    methods = raw.get("methods")
    extensions = raw.get("extensions", ())
    if (type(revision) is not str or type(schema_revision) is not int or not isinstance(methods, Mapping)
            or not isinstance(extensions, (list, tuple)) or any(type(item) is not str for item in extensions)):
        raise invalid
    if revision != surface.revision:
        raise VmBridgeError(BridgeCode.VERSION_MISMATCH, subject="surface_revision")
    if schema_revision not in SUPPORTED_ENGINE_SCHEMA_REVISIONS:
        raise VmBridgeError(BridgeCode.VERSION_MISMATCH, subject="engine_schema_revision")
    mismatched = []
    for command in commands:
        command = Command(command)
        installed = methods.get(command.value)
        try:
            same = type(installed) is str and parameter_names(installed) == parameter_names(surface.methods[command.value])
        except (ValueError, KeyError):
            same = False
        if not same:
            mismatched.append(command.value)
    if mismatched:
        raise VmBridgeError(BridgeCode.CONTRACT_MISMATCH, EffectOutcome.KNOWN_FAILURE,
                            subject=mismatched[0], details=tuple(mismatched))
    return Handshake(revision, schema_revision, frozenset(extensions))
