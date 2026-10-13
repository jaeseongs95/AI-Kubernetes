"""Bounded API agent loop over AIK-085 development tools.

Order per tool call: journal intent (argument digest only) -> DevTools call
(behind the AIK-029 gateway held by DevTools) -> journal receipt (result kind
and digest) -> result in the next model message -> ``returned`` just before
the next model call. An unknown effect stops the loop; nothing is retried.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
import math
import re
import threading
from typing import Any, Callable, Mapping

from aik_devtools import DevToolError, EffectUnknown, Invocation
from aik_runtime.errors import InvalidRecord
from aik_runtime.models import identifier

from .errors import DispatchError, DispatchOutcomeUnknown
from .identity import DispatchIdentity, canonical_json, sha256_text
from .journal import DispatchJournal


TOOL_NAMES = ("read_file", "create_file", "update_file", "run_verification")
STOP_REASONS = ("end_turn", "max_model_calls", "max_tool_calls", "budget_stop", "cancelled", "effect_unknown")
MAX_LOOP_LIMIT = 10000
_MAX_TOOL_CALL_ID = 121  # keeps f"effect-{id}" within the 128-character identifier limit
_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_RESOLVED = frozenset({"receipt", "denied"})


@dataclass(frozen=True, slots=True)
class ModelToolCall:
    tool_call_id: str
    tool: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ModelTurn:
    stop_reason: str
    text: str
    tool_calls: tuple = ()


@dataclass(frozen=True, slots=True)
class LoopResult:
    """Loop end. Ending the loop does not complete every tool effect: see ``unresolved_tool_calls``."""

    stop_reason: str
    model_calls: int
    tool_calls: int
    unresolved_tool_calls: tuple
    messages: tuple = ()
    model_stop_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _Run:
    identity: DispatchIdentity
    session: object
    input_revision: str
    authorization_revision: int


class _BadArguments(Exception):
    pass


def assistant_message(turn: ModelTurn) -> dict:
    return {
        "role": "assistant", "text": turn.text,
        "tool_calls": [
            {"tool_call_id": call.tool_call_id, "tool": call.tool,
             "arguments": dict(call.arguments) if isinstance(call.arguments, Mapping) else call.arguments}
            for call in turn.tool_calls
        ],
    }


def tool_message(tool_call_id: str, payload: Mapping[str, Any]) -> dict:
    return {"role": "tool", "tool_call_id": tool_call_id, "result": dict(payload)}


def arguments_digest(arguments: object) -> str | None:
    if not isinstance(arguments, Mapping):
        return None
    try:
        return sha256_text(canonical_json(dict(arguments)))
    except (TypeError, ValueError):
        return None


def _plain(value: Any) -> Any:
    if value is None or type(value) in (bool, int, str):
        return value
    if isinstance(value, Enum):
        return _plain(value.value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return str(value)


def result_payload(result: object) -> dict:
    """Model-facing result without ``replayed``/``receipt``, so a replay has the same digest."""
    if not is_dataclass(result) or isinstance(result, type):
        raise DispatchError("INVALID_STATE", subject="tool_result")
    body = {field.name: _plain(getattr(result, field.name))
            for field in fields(result) if field.name not in ("replayed", "receipt")}
    return {"ok": True, "kind": type(result).__name__, "result": body}


def _payload_digest(payload: Mapping[str, Any]) -> str:
    return sha256_text(canonical_json(dict(payload)))


def _error_code(error: BaseException) -> str:
    code = getattr(error, "code", None)
    text = str(code.value if isinstance(code, Enum) else code) if code is not None else type(error).__name__
    return text if _CODE.fullmatch(text) else "DEV_TOOL_ERROR"


def _text(arguments: Mapping, name: str) -> str:
    value = arguments.get(name)
    if type(value) is not str:
        raise _BadArguments
    return value


def _keys(arguments: Mapping, required: set, optional: set = frozenset()) -> None:
    present = set(arguments)
    if not required <= present or not present <= (required | optional):
        raise _BadArguments


def _content(arguments: Mapping) -> bytes:
    try:
        return _text(arguments, "content").encode("utf-8")
    except UnicodeEncodeError:
        raise _BadArguments from None


class ApiToolLoop:
    """Model tool requests -> authorized tools -> results -> next model call, within fixed limits."""

    def __init__(self, model_port, tools, journal: DispatchJournal, *, max_model_calls: int, max_tool_calls: int,
                 budget_ok: Callable[[], bool], cancel: threading.Event | None = None):
        for name, value in (("max_model_calls", max_model_calls), ("max_tool_calls", max_tool_calls)):
            if type(value) is not int or not 1 <= value <= MAX_LOOP_LIMIT:
                raise ValueError(f"{name} must be an integer between 1 and {MAX_LOOP_LIMIT}.")
        if not callable(budget_ok):
            raise TypeError("budget_ok must be callable.")
        if cancel is not None and not isinstance(cancel, threading.Event):
            raise TypeError("cancel must be a threading.Event or None.")
        if not isinstance(journal, DispatchJournal):
            raise TypeError("A DispatchJournal is required.")
        self._model = model_port
        self._tools = tools
        self._journal = journal
        self._max_model_calls = max_model_calls
        self._max_tool_calls = max_tool_calls
        self._budget_ok = budget_ok
        self._cancel = cancel

    # -- public --------------------------------------------------------------

    def run(self, identity: DispatchIdentity, session, *, input_revision: str, authorization_revision: int,
            messages) -> LoopResult:
        run = self._start(identity, session, input_revision, authorization_revision)
        self._journal.register(identity)
        if self._journal.tool_events(identity.request_id):
            raise DispatchError("INVALID_STATE", subject="use_resume")
        return self._loop(run, self._initial(messages), [], 0, 0)

    def resume(self, identity: DispatchIdentity, session, *, input_revision: str, authorization_revision: int,
               messages) -> LoopResult:
        run = self._start(identity, session, input_revision, authorization_revision)
        existing = self._journal.find(identity.request_id)
        if existing is None:
            raise DispatchError("INVALID_STATE", subject="request_id")
        if existing.identity.identity_digest != identity.identity_digest:
            raise DispatchError("CONFLICT", subject="request_id")
        request_id = identity.request_id
        messages = self._initial(messages)
        states = self._states(self._journal.tool_events(request_id))
        assistant_turns = sum(1 for message in messages if message.get("role") == "assistant")

        unresolved = [call_id for call_id, seen in states.items() if not (seen.keys() & _RESOLVED)]
        if unresolved:
            # Intent without receipt: the effect may have happened. Never re-execute.
            for call_id in unresolved:
                if "effect_unknown" not in states[call_id]:
                    self._journal.tool_event(request_id, call_id, "effect_unknown",
                                             {"code": "INTERRUPTED_BEFORE_RECEIPT"})
            return self._result(run, "effect_unknown", messages, assistant_turns, len(states))

        calls = self._calls_from(messages)
        pending = []
        for call_id, seen in states.items():
            if "returned" in seen:
                continue
            intent = seen["intent"]
            call = calls.get(call_id)
            if call is None or arguments_digest(call.arguments) != intent.get("arguments_digest"):
                raise DispatchError("REPLAY_MISMATCH", subject="arguments")
            if "denied" in seen:
                payload = {"ok": False, "error": seen["denied"].get("code", "DEV_TOOL_ERROR")}
            else:
                payload = self._replay(run, call, intent, seen["receipt"])
            messages.append(tool_message(call_id, payload))
            pending.append(call_id)
        return self._loop(run, messages, pending, assistant_turns, len(states))

    # -- loop ----------------------------------------------------------------

    @staticmethod
    def _start(identity, session, input_revision, authorization_revision) -> _Run:
        if type(identity) is not DispatchIdentity or identity.path != "api":
            raise DispatchError("INVALID_STATE", subject="path")
        return _Run(identity, session, input_revision, authorization_revision)

    @staticmethod
    def _initial(messages) -> list:
        if isinstance(messages, (str, bytes, Mapping)):
            raise DispatchError("INVALID_STATE", subject="messages")
        try:
            items = list(messages)
        except TypeError:
            raise DispatchError("INVALID_STATE", subject="messages") from None
        if any(not isinstance(message, Mapping) for message in items):
            raise DispatchError("INVALID_STATE", subject="messages")
        return [dict(message) for message in items]

    def _gate(self, exceeded: bool, reason: str) -> str | None:
        if self._cancel is not None and self._cancel.is_set():
            return "cancelled"
        if self._budget_ok() is not True:
            return "budget_stop"
        return reason if exceeded else None

    def _loop(self, run: _Run, messages: list, pending: list, model_calls: int, tool_calls: int) -> LoopResult:
        request_id = run.identity.request_id
        while True:
            stop = self._gate(model_calls >= self._max_model_calls, "max_model_calls")
            if stop:
                return self._result(run, stop, messages, model_calls, tool_calls)
            for call_id in pending:
                self._journal.tool_event(request_id, call_id, "returned", {})
            pending = []
            turn = self._model.next(request_id, tuple(messages))
            model_calls += 1
            self._check_turn(turn)
            messages.append(assistant_message(turn))
            if not turn.tool_calls:
                return self._result(run, "end_turn", messages, model_calls, tool_calls, turn.stop_reason)
            known = {event.tool_call_id for event in self._journal.tool_events(request_id)}
            seen = set()
            for call in turn.tool_calls:
                self._check_tool_call_id(call.tool_call_id)
                if call.tool_call_id in known or call.tool_call_id in seen:
                    raise DispatchError("CONFLICT", subject="tool_call_id")
                seen.add(call.tool_call_id)
            for call in turn.tool_calls:
                stop = self._gate(tool_calls >= self._max_tool_calls, "max_tool_calls")
                if stop:
                    return self._result(run, stop, messages, model_calls, tool_calls)
                tool_calls += 1
                payload = self._execute(run, call)
                if payload is None:
                    return self._result(run, "effect_unknown", messages, model_calls, tool_calls)
                messages.append(tool_message(call.tool_call_id, payload))
                pending.append(call.tool_call_id)

    @staticmethod
    def _check_turn(turn: object) -> None:
        if (type(turn) is not ModelTurn or type(turn.stop_reason) is not str or type(turn.text) is not str
                or type(turn.tool_calls) is not tuple
                or any(type(call) is not ModelToolCall for call in turn.tool_calls)):
            raise DispatchError("INVALID_STATE", subject="model_turn")

    @staticmethod
    def _check_tool_call_id(value: object) -> None:
        try:
            identifier(value, "tool_call_id")
        except InvalidRecord:
            raise DispatchError("INVALID_STATE", subject="tool_call_id") from None
        if len(value) > _MAX_TOOL_CALL_ID:
            raise DispatchError("INVALID_STATE", subject="tool_call_id")

    def _invocation(self, run: _Run, call_id: str, effect_id: str | None) -> Invocation:
        try:
            return Invocation(call_id, run.input_revision, run.authorization_revision, effect_id, self._cancel)
        except (TypeError, ValueError):
            raise DispatchError("INVALID_STATE", subject="invocation") from None

    def _deny(self, request_id: str, call_id: str, code: str) -> dict:
        self._journal.tool_event(request_id, call_id, "denied", {"code": code})
        return {"ok": False, "error": code}

    def _execute(self, run: _Run, call: ModelToolCall) -> dict | None:
        request_id = run.identity.request_id
        call_id = call.tool_call_id
        tool = call.tool if call.tool in TOOL_NAMES else None
        digest = arguments_digest(call.arguments)
        effect_id = None if tool in (None, "read_file") else f"effect-{call_id}"
        invocation = self._invocation(run, call_id, effect_id)
        self._journal.tool_event(request_id, call_id, "intent", {
            "tool": tool or "unsupported", "arguments_digest": digest, "effect_id": effect_id,
            "input_revision": run.input_revision, "authorization_revision": run.authorization_revision,
        })
        if tool is None:
            return self._deny(request_id, call_id, "TOOL_NOT_ALLOWED")
        if digest is None:
            return self._deny(request_id, call_id, "INVALID_ARGUMENTS")
        try:
            result = self._invoke(tool, run.session, invocation, call.arguments)
        except _BadArguments:
            return self._deny(request_id, call_id, "INVALID_ARGUMENTS")
        except EffectUnknown as error:
            self._journal.tool_event(request_id, call_id, "effect_unknown", {"code": _error_code(error)})
            return None
        except DevToolError as error:
            return self._deny(request_id, call_id, _error_code(error))
        except Exception as error:
            self._journal.tool_event(request_id, call_id, "effect_unknown",
                                     {"code": "UNEXPECTED_" + type(error).__name__})
            raise DispatchOutcomeUnknown(subject="tool_call") from None
        payload = result_payload(result)
        self._journal.tool_event(request_id, call_id, "receipt", {
            "kind": payload["kind"], "result_digest": _payload_digest(payload),
            "replayed": getattr(result, "replayed", False) is True,
        })
        return payload

    def _invoke(self, tool: str, session, invocation: Invocation, arguments: Mapping):
        if not isinstance(arguments, Mapping):
            raise _BadArguments
        if tool == "read_file":
            _keys(arguments, {"path"}, {"expected_digest"})
            expected = arguments.get("expected_digest")
            if expected is not None and type(expected) is not str:
                raise _BadArguments
            return self._tools.read_file(session, invocation, _text(arguments, "path"), expected_digest=expected)
        if tool == "create_file":
            _keys(arguments, {"path", "content"})
            return self._tools.create_file(session, invocation, _text(arguments, "path"), _content(arguments))
        if tool == "update_file":
            _keys(arguments, {"path", "content", "expected_digest"})
            return self._tools.update_file(session, invocation, _text(arguments, "path"), _content(arguments),
                                           expected_digest=_text(arguments, "expected_digest"))
        if tool == "run_verification":
            _keys(arguments, {"command_id"})
            return self._tools.run_verification(session, invocation, _text(arguments, "command_id"))
        raise _BadArguments

    def _replay(self, run: _Run, call: ModelToolCall, intent: Mapping, receipt: Mapping) -> dict:
        if (call.tool != intent.get("tool") or intent.get("input_revision") != run.input_revision
                or intent.get("authorization_revision") != run.authorization_revision):
            raise DispatchError("REPLAY_MISMATCH", subject="invocation")
        invocation = self._invocation(run, call.tool_call_id, intent.get("effect_id"))
        try:
            result = self._invoke(call.tool, run.session, invocation, call.arguments)
        except EffectUnknown:
            raise DispatchOutcomeUnknown(subject="replay") from None
        except (_BadArguments, DevToolError):
            raise DispatchError("REPLAY_MISMATCH", subject="replay") from None
        payload = result_payload(result)
        if getattr(result, "replayed", False) is not True or _payload_digest(payload) != receipt.get("result_digest"):
            raise DispatchError("REPLAY_MISMATCH", subject="replay")
        return payload

    # -- journal views -------------------------------------------------------

    @staticmethod
    def _states(events) -> dict:
        states: dict = {}
        for event in events:
            states.setdefault(event.tool_call_id, {})[event.state] = event.data
        return states

    @staticmethod
    def _calls_from(messages: list) -> dict:
        calls = {}
        for message in messages:
            if message.get("role") != "assistant":
                continue
            entries = message.get("tool_calls") or ()
            if isinstance(entries, (str, bytes, Mapping)):
                raise DispatchError("REPLAY_MISMATCH", subject="messages")
            for entry in entries:
                if not isinstance(entry, Mapping):
                    raise DispatchError("REPLAY_MISMATCH", subject="messages")
                call_id = entry.get("tool_call_id")
                if type(call_id) is not str or call_id in calls:
                    raise DispatchError("REPLAY_MISMATCH", subject="messages")
                calls[call_id] = ModelToolCall(call_id, entry.get("tool"), entry.get("arguments"))
        return calls

    def _result(self, run: _Run, stop_reason: str, messages: list, model_calls: int, tool_calls: int,
                model_stop_reason: str | None = None) -> LoopResult:
        states = self._states(self._journal.tool_events(run.identity.request_id))
        unresolved = tuple(call_id for call_id, seen in states.items() if not (seen.keys() & _RESOLVED))
        return LoopResult(stop_reason, model_calls, tool_calls, unresolved, tuple(messages), model_stop_reason)
