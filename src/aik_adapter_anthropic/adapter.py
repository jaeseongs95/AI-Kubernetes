"""AIK-013: API-key adapter implementing the AIK-011 ProviderAdapter contract.

Unverified assumptions (not confirmed against current official documents):
the base URL, ``/v1/messages`` and ``/v1/models`` paths, ``x-api-key`` and
``anthropic-version`` headers, the version value, and response/error fields.
The plaintext key is resolved from the vault for each request, placed only in
the transport request headers, and never logged, raised, recorded or returned.
Live vendor round trips are NOT_RUN; route qualification is never claimed.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
import hashlib
import hmac
import json
import math
import re
from threading import RLock
from urllib.parse import quote, urlsplit

from aik_provider.contracts import (
    AccountIsolation, AdapterCapabilities, AdapterFailure, AuthKind, AuthObservation,
    AuthState, ConnectionBinding, FaultKind, JobReceipt, JobRequest, JobState, Outcome,
    ProviderFault, Support, fault_from_provider,
)
from aik_security.redaction import OutputRedactor, RedactionError
from aik_vault import CredentialNotFound, InvalidCredentialRef, VaultAccessDenied

from .messages import (
    Completion, MessagesCall, ModelResult, TextPart, ToolCall, Usage,
)
from .transport import (
    MAX_RESPONSE_BYTES, HttpRequest, HttpResponse, TransportNotSent, TransportTimeout,
)
from .messages import SOURCES

PROVIDER = "anthropic"
DEFAULT_RUNTIME = "messages-api"
# --- Unverified vendor assumptions (see docs/adapter-anthropic.ko.md) ---
DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_API_VERSION = "2023-06-01"
MESSAGES_PATH = "/v1/messages"
MODELS_PATH = "/v1/models"
# -------------------------------------------------------------------------
ROUTE_QUALIFICATION = "NOT_RUN"

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}\Z")
_STOP_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_API_VERSION = re.compile(r"[A-Za-z0-9._-]{1,32}\Z")
_KEY_CHARS = re.compile(r"[\x21-\x7e]{1,4096}\Z")
_RETRY_AFTER = re.compile(r"[0-9]{1,9}\Z")
_MAX_BLOCKS = 1024
_MAX_PREPARED = 1024
_MAX_REVISION = 2**63 - 1
_MAX_RETRY_MS = 86_400_000
_FINGERPRINT_DOMAIN = b"AIK-013/anthropic-api-key-fingerprint/v1\x00"
_USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
_STOP_MAP = {
    "end_turn": (Completion.COMPLETE, JobState.SUCCEEDED),
    "stop_sequence": (Completion.COMPLETE, JobState.SUCCEEDED),
    "tool_use": (Completion.TOOL_USE, JobState.SUCCEEDED),
    "max_tokens": (Completion.TRUNCATED, JobState.FAILED),
    "refusal": (Completion.REFUSED, JobState.FAILED),
    "pause_turn": (Completion.INCOMPLETE, JobState.FAILED),
}


def _failure(kind: FaultKind, outcome: Outcome = Outcome.KNOWN_FAILURE, *, native: str | None = None,
             status: int | None = None, retry: int | None = None) -> AdapterFailure:
    return AdapterFailure(ProviderFault(kind, outcome, native, status, retry))


def _fingerprint(secret: str) -> str:
    # Identifies the stored key (detects rotation); it is not a vendor account ID.
    digest = hashlib.sha256(_FINGERPRINT_DOMAIN)
    digest.update(secret.encode("utf-8"))
    return digest.hexdigest()


def _redactor_for(secret: str) -> OutputRedactor:
    try:
        return OutputRedactor([secret])
    except RedactionError:
        return OutputRedactor()


def _load_json(body: bytes) -> object:
    if type(body) is not bytes or len(body) > MAX_RESPONSE_BYTES:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None


def _retry_after(value: str | None) -> int | None:
    if type(value) is not str or _RETRY_AFTER.fullmatch(value.strip()) is None:
        return None
    return min(int(value.strip()) * 1000, _MAX_RETRY_MS)


def _usage(value: object) -> Usage:
    if type(value) is not dict:
        return Usage()
    counters = {}
    for name in _USAGE_FIELDS:
        item = value.get(name)
        counters[name] = item if type(item) is int and 0 <= item <= _MAX_REVISION else None
    return Usage(**counters)


def _text_part(text: str) -> TextPart | None:
    try:
        return TextPart(text)
    except ValueError:
        return None


def _tool_call(call_id: object, name: object, arguments: object) -> ToolCall | None:
    if type(call_id) is not str or type(name) is not str or type(arguments) is not dict:
        return None
    try:
        return ToolCall.create(call_id, name, arguments)
    except ValueError:
        return None


def _encode_part(part) -> dict:
    if type(part) is TextPart:
        return {"type": "text", "text": part.text}
    if type(part) is ToolCall:
        return {"type": "tool_use", "id": part.call_id, "name": part.name, "input": part.arguments}
    return {"type": "tool_result", "tool_use_id": part.call_id, "content": part.content,
            "is_error": part.is_error}


def _encode_call(model: str, call: MessagesCall) -> bytes:
    body: dict = {
        "model": model,
        "max_tokens": call.max_tokens,
        "messages": [
            {"role": message.role, "content": [_encode_part(part) for part in message.parts]}
            for message in call.messages
        ],
    }
    if call.system is not None:
        body["system"] = call.system
    if call.tools:
        body["tools"] = [
            {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
            for tool in call.tools
        ]
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


@dataclass(frozen=True, slots=True)
class ModelAvailability:
    """Provider-reported model visibility for one key; not qualification."""

    requested_model: str
    state: str  # "available" | "unavailable"
    provider_model_id: str | None
    source: str


class AnthropicAdapter:
    """ProviderAdapter for API keys. Use through GuardedAdapter for dispatch.

    Key lifecycle: ``register_key`` (vault put) -> ``auth_status`` (vendor
    check) -> ``prepare`` + ``start_job`` (model call) -> ``rotate_key``
    (vault replace, new binding revision) -> ``sign_out`` (vault delete).
    """

    def __init__(self, vault, transport, *, runtime: str = DEFAULT_RUNTIME,
                 base_url: str = DEFAULT_BASE_URL, api_version: str = DEFAULT_API_VERSION,
                 timeout: float = 60.0):
        for name in ("put", "replace", "delete", "resolve"):
            if not callable(getattr(vault, name, None)):
                raise TypeError("A vault backend with put/replace/delete/resolve is required.")
        if not callable(getattr(transport, "send", None)) or getattr(transport, "source", None) not in SOURCES:
            raise TypeError("A transport with send() and a declared source is required.")
        parts = urlsplit(base_url) if type(base_url) is str else None
        if (parts is None or parts.scheme != "https" or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.path not in ("", "/") or parts.query or parts.fragment):
            raise ValueError("INVALID_BASE_URL")
        if type(api_version) is not str or _API_VERSION.fullmatch(api_version) is None:
            raise ValueError("INVALID_API_VERSION")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout)
                or not 0 < timeout <= 600):
            raise ValueError("INVALID_TIMEOUT")
        self._caps = AdapterCapabilities(
            provider=PROVIDER, runtime=runtime, auth_kind=AuthKind.API_KEY,
            isolation=AccountIsolation.ISOLATED,
            auth_status=Support.SUPPORTED, sign_in=Support.UNSUPPORTED, sign_out=Support.SUPPORTED,
            job_start=Support.SUPPORTED, job_observe=Support.SUPPORTED, job_interrupt=Support.UNSUPPORTED,
            implementation_state="implemented", qualification_status=ROUTE_QUALIFICATION,
        )
        self.runtime = runtime
        self._vault = vault
        self._transport = transport
        self._base_url = f"https://{parts.netloc}"
        self._api_version = api_version
        self._timeout = float(timeout)
        self._lock = RLock()
        self._prepared: dict[str, MessagesCall] = {}
        self._receipts: dict[str, JobReceipt] = {}
        self._results: dict[str, ModelResult] = {}
        self._audit_log: deque = deque(maxlen=256)

    def __repr__(self) -> str:
        return f"AnthropicAdapter(runtime={self.runtime!r})"

    # -- internal helpers ----------------------------------------------------
    def _check_binding(self, binding: object) -> None:
        if type(binding) is not ConnectionBinding:
            raise _failure(FaultKind.INVALID_CONTRACT)
        if (binding.provider, binding.runtime, binding.auth_kind) != (PROVIDER, self.runtime, AuthKind.API_KEY):
            raise _failure(FaultKind.STALE_CONNECTION)

    def _secret_for(self, binding: ConnectionBinding) -> str | None:
        """Resolve at call time. None: the vault holds no such credential."""
        failure = None
        secret = None
        try:
            secret = self._vault.resolve(binding.credential_ref)
        except (CredentialNotFound, InvalidCredentialRef):
            return None
        except VaultAccessDenied:
            failure = FaultKind.ACCESS_DENIED
        except Exception:
            failure = FaultKind.PROVIDER_FAILURE
        # Raised outside the handler so no vault exception becomes __context__.
        if failure is not None:
            raise _failure(failure, native="vault_error")
        if type(secret) is not str or not secret:
            raise _failure(FaultKind.PROVIDER_FAILURE, native="vault_error")
        if not hmac.compare_digest(_fingerprint(secret), binding.account_fingerprint):
            raise _failure(FaultKind.ACCOUNT_CHANGED)
        return secret

    def _headers(self, secret: str, *, json_body: bool = False) -> tuple:
        if _KEY_CHARS.fullmatch(secret) is None:
            raise _failure(FaultKind.AUTH_REQUIRED, native="invalid_credential_format")
        headers = [("x-api-key", secret), ("anthropic-version", self._api_version),
                   ("accept", "application/json")]
        if json_body:
            headers.append(("content-type", "application/json"))
        return tuple(headers)

    def _send(self, request: HttpRequest, *, effectful: bool) -> HttpResponse:
        problem = None
        response = None
        try:
            response = self._transport.send(request)
        except TransportTimeout:
            problem = "timeout"
        except TransportNotSent:
            problem = "connection_error"
        except Exception:
            problem = "transport_error"
        # Never chain the transport exception: its text may echo request data.
        if problem is not None:
            if effectful and problem != "connection_error":
                raise _failure(FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN, native=problem)
            raise _failure(FaultKind.PROVIDER_FAILURE, native=problem)
        if type(response) is not HttpResponse or len(response.body) > MAX_RESPONSE_BYTES:
            if effectful:
                raise _failure(FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN, native="malformed_response")
            raise _failure(FaultKind.PROVIDER_FAILURE, native="malformed_response")
        return response

    def _error_failure(self, response: HttpResponse) -> AdapterFailure:
        payload = _load_json(response.body)
        native = None
        if type(payload) is dict and type(payload.get("error")) is dict:
            candidate = payload["error"].get("type")
            native = candidate if type(candidate) is str else None
        # Only the bounded error type, status and retry-after survive; never the message.
        return AdapterFailure(fault_from_provider(
            http_status=response.status, native_code=native,
            retry_after_ms=_retry_after(response.header("retry-after")),
        ))

    def _authorized_get(self, binding: ConnectionBinding, path: str):
        secret = self._secret_for(binding)
        if secret is None:
            return None, OutputRedactor()
        redactor = _redactor_for(secret)
        request = HttpRequest("GET", self._base_url + path, self._headers(secret), None, self._timeout)
        del secret
        return self._send(request, effectful=False), redactor

    def _record(self, redactor: OutputRedactor, operation: str, binding: ConnectionBinding | None, *,
                request_id: str | None = None, fault: ProviderFault | None = None,
                extra: dict | None = None) -> None:
        event = {
            "operation": operation, "provider": PROVIDER, "runtime": self.runtime,
            "connection_id": None if binding is None else binding.connection_id,
            "revision": None if binding is None else binding.revision,
            "request_id": request_id, "result": "ok" if fault is None else "fault",
            "source": self._transport.source, "qualification_status": ROUTE_QUALIFICATION,
        }
        if fault is not None:
            event.update({
                "fault_kind": fault.kind.value, "outcome": fault.outcome.value,
                "http_status": fault.http_status, "native_code": fault.native_code,
                "retry_after_ms": fault.retry_after_ms,
            })
        if extra:
            event.update(extra)
        entry = redactor.output("log", event)
        with self._lock:
            self._audit_log.append(entry)

    # -- key lifecycle (not part of the provider protocol) ---------------------
    def register_key(self, secret: str, *, connection_id: str, revision: int = 1) -> ConnectionBinding:
        """Store a key in the vault and return a binding holding only its reference."""
        ConnectionBinding(PROVIDER, self.runtime, connection_id, revision, AuthKind.API_KEY,
                          "secret://aik-vault/" + "0" * 32, "0" * 64)
        if type(secret) is not str or _KEY_CHARS.fullmatch(secret) is None:
            raise ValueError("INVALID_API_KEY_FORMAT")
        reference = self._vault.put(secret)
        binding = ConnectionBinding(PROVIDER, self.runtime, connection_id, revision, AuthKind.API_KEY,
                                    reference, _fingerprint(secret))
        self._record(OutputRedactor(), "register_key", binding)
        return binding

    def rotate_key(self, binding: ConnectionBinding, secret: str) -> ConnectionBinding:
        """Replace the key under the same reference; returns the next revision."""
        self._check_binding(binding)
        if binding.revision >= _MAX_REVISION:
            raise _failure(FaultKind.INVALID_CONTRACT)
        if type(secret) is not str or _KEY_CHARS.fullmatch(secret) is None:
            raise ValueError("INVALID_API_KEY_FORMAT")
        current = self._secret_for(binding)  # a stale binding cannot rotate
        if current is None:
            raise _failure(FaultKind.AUTH_REQUIRED, native="credential_missing")
        del current
        self._vault.replace(binding.credential_ref, secret)
        rotated = replace(binding, revision=binding.revision + 1, account_fingerprint=_fingerprint(secret))
        self._record(OutputRedactor(), "rotate_key", rotated)
        return rotated

    # -- ProviderAdapter -----------------------------------------------------
    def describe(self, binding: ConnectionBinding) -> AdapterCapabilities:
        self._check_binding(binding)
        return self._caps

    def auth_status(self, binding: ConnectionBinding) -> AuthObservation:
        self._check_binding(binding)
        redactor = OutputRedactor()
        try:
            response, redactor = self._authorized_get(binding, MODELS_PATH + "?limit=1")
            if response is None:
                state = AuthState.UNAUTHENTICATED
            elif response.status == 200:
                state = AuthState.AUTHENTICATED
            elif response.status == 401:
                state = AuthState.UNAUTHENTICATED
            else:
                raise self._error_failure(response)
        except AdapterFailure as error:
            self._record(redactor, "auth_status", binding, fault=error.fault)
            raise
        self._record(redactor, "auth_status", binding, extra={"auth_state": state.value})
        return AuthObservation(
            binding.connection_id, binding.revision,
            binding.account_fingerprint if state is AuthState.AUTHENTICATED else None, state,
        )

    def begin_sign_in(self, binding: ConnectionBinding) -> AuthObservation:
        self._check_binding(binding)
        raise _failure(FaultKind.UNSUPPORTED)  # an API key is registered, not signed in

    def sign_out(self, binding: ConnectionBinding) -> AuthObservation:
        """Release: delete the vault entry of this exact binding (local effect)."""
        self._check_binding(binding)
        redactor = OutputRedactor()
        try:
            secret = self._secret_for(binding)
            if secret is not None:
                redactor = _redactor_for(secret)
                del secret
                problem = None
                try:
                    self._vault.delete(binding.credential_ref)
                except CredentialNotFound:
                    pass
                except VaultAccessDenied:
                    problem = FaultKind.ACCESS_DENIED
                except Exception:
                    problem = FaultKind.PROVIDER_FAILURE
                if problem is not None:
                    raise _failure(problem, native="vault_error")
        except AdapterFailure as error:
            self._record(redactor, "sign_out", binding, fault=error.fault)
            raise
        self._record(redactor, "sign_out", binding)
        return AuthObservation(binding.connection_id, binding.revision, None, AuthState.UNAUTHENTICATED)

    def prepare(self, request_id: str, call: MessagesCall) -> None:
        """Register the conversation for a later start_job with the same request_id."""
        if type(request_id) is not str or _ID.fullmatch(request_id) is None or type(call) is not MessagesCall:
            raise _failure(FaultKind.INVALID_CONTRACT)
        with self._lock:
            if request_id in self._prepared or request_id in self._receipts:
                raise _failure(FaultKind.DUPLICATE_REQUEST)
            if len(self._prepared) >= _MAX_PREPARED:
                raise _failure(FaultKind.INVALID_CONTRACT)
            self._prepared[request_id] = call

    def start_job(self, request: JobRequest) -> JobReceipt:
        if type(request) is not JobRequest:
            raise _failure(FaultKind.INVALID_CONTRACT)
        binding = request.binding
        self._check_binding(binding)
        with self._lock:
            if request.request_id in self._receipts:
                raise _failure(FaultKind.DUPLICATE_REQUEST)
            call = self._prepared.pop(request.request_id, None)  # never replayed
        redactor = OutputRedactor()
        try:
            if call is None:
                raise _failure(FaultKind.INVALID_CONTRACT, native="call_not_prepared")
            body = _encode_call(request.model, call)
            secret = self._secret_for(binding)
            if secret is None:
                raise _failure(FaultKind.AUTH_REQUIRED, native="credential_missing")
            redactor = _redactor_for(secret)
            http = HttpRequest("POST", self._base_url + MESSAGES_PATH,
                               self._headers(secret, json_body=True), body, self._timeout)
            del secret
            response = self._send(http, effectful=True)
            del http
            if response.status != 200:
                raise self._error_failure(response)
            result, state = self._decode(request, response, redactor)
        except AdapterFailure as error:
            self._record(redactor, "start_job", binding, request_id=request.request_id, fault=error.fault)
            raise
        receipt = JobReceipt(request.request_id, result.provider_job_id, binding.connection_id,
                             binding.revision, binding.account_fingerprint, state)
        with self._lock:
            self._receipts[request.request_id] = receipt
            self._results[request.request_id] = result
        self._record(redactor, "start_job", binding, request_id=request.request_id, extra={
            "provider_job_id": result.provider_job_id, "model": result.model,
            "stop_reason": result.stop_reason, "completion": result.completion.value,
            "job_state": state.value, "usage": result.usage.as_dict(),
        })
        return receipt

    def _decode(self, request: JobRequest, response: HttpResponse, redactor: OutputRedactor):
        payload = _load_json(response.body)
        decoded = self._decode_payload(request, payload, redactor) if type(payload) is dict else None
        if decoded is None:
            # A 200 arrived but cannot be interpreted: the turn's effect is unknown.
            raise _failure(FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN, native="malformed_response",
                           status=response.status)
        return decoded

    def _decode_payload(self, request: JobRequest, payload: dict, redactor: OutputRedactor):
        if payload.get("type") != "message" or payload.get("role") != "assistant":
            return None
        job_id = redactor.safe_id(payload.get("id"))
        content = payload.get("content")
        if job_id is None or type(content) is not list or len(content) > _MAX_BLOCKS:
            return None
        parts = []
        unrecognized = False
        for block in content:
            if type(block) is not dict:
                return None
            kind = block.get("type")
            if kind == "text":
                text = block.get("text")
                if type(text) is not str:
                    return None
                if text:
                    part = _text_part(text)
                    if part is None:
                        return None
                    parts.append(part)
            elif kind == "tool_use":
                call_id = block.get("id")
                if type(call_id) is not str or redactor.safe_id(call_id) != call_id:
                    return None
                part = _tool_call(call_id, block.get("name"), block.get("input"))
                if part is None:
                    return None
                parts.append(part)
            else:
                unrecognized = True
        model = payload.get("model")
        if not (type(model) is str and _MODEL.fullmatch(model) and redactor.redact_text(model) == model):
            model = None
        stop = payload.get("stop_reason")
        if type(stop) is not str or _STOP_REASON.fullmatch(stop) is None:
            stop = None
        completion, state = _STOP_MAP.get(stop, (Completion.UNKNOWN, JobState.FAILED))
        if unrecognized:
            completion, state = Completion.UNKNOWN, JobState.FAILED
        if completion is Completion.TOOL_USE and not any(type(p) is ToolCall for p in parts):
            completion, state = Completion.UNKNOWN, JobState.FAILED
        result = ModelResult(
            request_id=request.request_id, provider_job_id=job_id, model=model, stop_reason=stop,
            completion=completion, parts=tuple(parts), usage=_usage(payload.get("usage")),
            source=self._transport.source,
        )
        return result, state

    def observe_job(self, request: JobRequest, receipt: JobReceipt) -> JobReceipt:
        """Synchronous turns are terminal at start; this returns the local record."""
        if type(request) is not JobRequest or type(receipt) is not JobReceipt:
            raise _failure(FaultKind.INVALID_CONTRACT)
        self._check_binding(request.binding)
        with self._lock:
            stored = self._receipts.get(request.request_id)
        if stored is None or stored.provider_job_id != receipt.provider_job_id:
            raise _failure(FaultKind.INVALID_RECEIPT, Outcome.UNKNOWN)
        return stored

    def interrupt_job(self, request: JobRequest, receipt: JobReceipt) -> JobReceipt:
        if type(request) is not JobRequest:
            raise _failure(FaultKind.INVALID_CONTRACT)
        self._check_binding(request.binding)
        raise _failure(FaultKind.UNSUPPORTED)

    # -- additional read paths -------------------------------------------------
    def check_model(self, binding: ConnectionBinding, model: str) -> ModelAvailability:
        self._check_binding(binding)
        if type(model) is not str or _ID.fullmatch(model) is None:
            raise _failure(FaultKind.INVALID_CONTRACT)
        redactor = OutputRedactor()
        try:
            response, redactor = self._authorized_get(binding, MODELS_PATH + "/" + quote(model, safe=""))
            if response is None:
                raise _failure(FaultKind.AUTH_REQUIRED, native="credential_missing")
            if response.status == 200:
                payload = _load_json(response.body)
                reported = payload.get("id") if type(payload) is dict else None
                if not (type(reported) is str and _MODEL.fullmatch(reported)
                        and redactor.redact_text(reported) == reported):
                    reported = None
                availability = ModelAvailability(model, "available", reported, self._transport.source)
            elif response.status == 404:
                availability = ModelAvailability(model, "unavailable", None, self._transport.source)
            else:
                raise self._error_failure(response)
        except AdapterFailure as error:
            self._record(redactor, "check_model", binding, fault=error.fault)
            raise
        self._record(redactor, "check_model", binding,
                     extra={"model_state": availability.state, "model": availability.provider_model_id})
        return availability

    def result(self, request_id: str) -> ModelResult:
        with self._lock:
            try:
                return self._results[request_id]
            except (KeyError, TypeError):
                raise _failure(FaultKind.INVALID_CONTRACT) from None

    def audit_log(self) -> tuple:
        """Redacted, detached audit events (AIK-017 output kind ``log``)."""
        with self._lock:
            return tuple(json.loads(json.dumps(entry)) for entry in self._audit_log)


__all__ = [
    "AnthropicAdapter", "ModelAvailability", "PROVIDER", "DEFAULT_RUNTIME", "DEFAULT_BASE_URL",
    "DEFAULT_API_VERSION", "MESSAGES_PATH", "MODELS_PATH", "ROUTE_QUALIFICATION",
]
