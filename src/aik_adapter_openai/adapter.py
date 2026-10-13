"""AIK-012 OpenAI API-key adapter implementing ``aik_provider.ProviderAdapter``.

Lifecycle: register_key -> verify_key -> check_model -> stage_turn/start_job ->
observe_job/interrupt_job -> rotate_key -> sign_out (release). The plaintext
key is resolved from ``aik_vault`` per request, placed in one request header and
used to scrub that request's response; it is never stored, logged, returned or
attached to an exception. Endpoint paths and response fields are unverified
assumptions (see docs/adapter-openai.ko.md). Nothing here qualifies a route.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import math
import re
from threading import RLock
import time
from urllib.parse import quote

from aik_provider.contracts import (
    AccountIsolation, AdapterCapabilities, AdapterFailure, AuthKind, AuthObservation,
    AuthState, ConnectionBinding, FaultKind, JobReceipt, JobRequest, JobState, Outcome,
    ProviderFault, Support, TERMINAL, fault_from_provider,
)
from aik_security.redaction import OMITTED, OutputRedactor
from aik_vault import (
    CredentialNotFound, InvalidCredentialRef, InvalidSecret, VaultAccessDenied,
    VaultBackend, VaultError,
)

from .contract import (
    BILLING_BASIS, CALL_ID, MAX_ITEMS, MAX_TEXT_CHARS, MODEL_AVAILABLE, MODEL_DENIED,
    MODEL_NOT_FOUND, MODEL_UNAUTHENTICATED, MODEL_UNKNOWN, RESPONSE_ID, TOOL_NAME,
    AdapterStatus, ModelCheck, ToolCall, TurnInput, TurnResult, Usage,
)
from .transport import HttpRequest, HttpResponse


PROVIDER = "openai"
RUNTIME = "openai-api"
MAX_EVENTS = 256
MAX_STAGED = 1024
MAX_MODELS = 256
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SECRET_FORMAT = re.compile(r"[\x21-\x7e]{12,256}\Z")  # printable ASCII; no header injection
_PROVIDER_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}\Z")
_SAFE_WORD = re.compile(r"[a-z][a-z0-9_.:]{0,63}\Z")
_RETRY = re.compile(r"\d{1,6}(?:\.\d{1,3})?\Z")
# Unverified mapping of documented-looking response statuses.
_STATUS = {
    "queued": JobState.ACCEPTED,
    "in_progress": JobState.RUNNING,
    "completed": JobState.SUCCEEDED,
    "failed": JobState.FAILED,
    "incomplete": JobState.FAILED,
    "cancelled": JobState.CANCELLED,
}
_LOG = OutputRedactor()


def _failure(kind: FaultKind, outcome: Outcome = Outcome.KNOWN_FAILURE) -> AdapterFailure:
    return AdapterFailure(ProviderFault(kind, outcome))


def slot_fingerprint(connection_id: str, credential_ref: str, revision: int) -> str:
    """Identity of a credential slot revision; not derived from the secret.

    The provider account behind a key is not observed (no verified endpoint),
    so this does not prove which vendor account a key belongs to.
    """
    material = f"aik-openai-api-key-slot/v1\0{connection_id}\0{credential_ref}\0{revision}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _vault_fault(error: BaseException) -> AdapterFailure:
    if isinstance(error, (InvalidSecret, InvalidCredentialRef)):
        return _failure(FaultKind.INVALID_CONTRACT)
    if isinstance(error, CredentialNotFound):
        return _failure(FaultKind.AUTH_REQUIRED)
    if isinstance(error, VaultAccessDenied):
        return _failure(FaultKind.ACCESS_DENIED)
    return _failure(FaultKind.PROVIDER_FAILURE)


def _word(value: object) -> str:
    return value if type(value) is str and _SAFE_WORD.fullmatch(value) else "unknown"


def _json(body: bytes) -> object:
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):
        return None


def _retry_after(value: object) -> int | None:
    if type(value) is not str or _RETRY.fullmatch(value.strip()) is None:
        return None
    return min(int(float(value.strip()) * 1000), 86_400_000)


def _native_code(data: object, redactor: OutputRedactor) -> str | None:
    error = data.get("error") if type(data) is dict else None
    if type(error) is not dict:
        return None
    for key in ("code", "type"):
        value = error.get(key)
        if type(value) is str:
            safe = redactor.safe_id(value)
            if safe is not None:
                return safe
    return None


def _scrub(text: object, secret: str, redactor: OutputRedactor) -> tuple[str | None, bool]:
    """Return (text, echo). Provider text changes only if the key is echoed."""
    if type(text) is not str:
        return None, False
    cleaned = redactor.redact_text(text)
    if cleaned == OMITTED:
        # Too large for the bounded redactor: only the raw form is checkable.
        return (None, True) if secret in text else (text, False)
    if secret in text or cleaned != _LOG.redact_text(text):
        return cleaned, True
    return text, False


def _usage(value: object) -> Usage:
    def count(item: object) -> int | None:
        return item if type(item) is int and 0 <= item < 2**63 else None

    if type(value) is not dict:
        return Usage()
    return Usage(count(value.get("input_tokens")), count(value.get("output_tokens")), count(value.get("total_tokens")))


def _alias(response_id: str) -> str:
    # Vendor IDs may contain '_', which the AIK-011 ID grammar does not allow.
    return "openai-response:" + hashlib.sha256(response_id.encode("ascii")).hexdigest()[:48]


def _tool_call(item: dict, scrub) -> tuple[ToolCall, bool] | None:
    call_id, name, arguments = item.get("call_id"), item.get("name"), item.get("arguments")
    if type(call_id) is not str or CALL_ID.fullmatch(call_id) is None:
        return None
    if type(name) is not str or TOOL_NAME.fullmatch(name) is None:
        return None
    parsed, echo = None, False
    if type(arguments) is str:
        _, echo = scrub(arguments)
        if not echo:
            try:
                value = json.loads(arguments)
            except (ValueError, RecursionError):
                value = None
            if type(value) is dict:
                parsed = value
    return ToolCall(call_id, name, parsed, parsed is not None), echo


def _turn_from(data: object, scrub, request_id: str, evidence_kind: str) -> TurnResult | None:
    if type(data) is not dict:
        return None
    raw_id = data.get("id")
    if type(raw_id) is not str or RESPONSE_ID.fullmatch(raw_id) is None:
        return None
    status = data.get("status")
    state = _STATUS.get(status, JobState.UNKNOWN) if type(status) is str else JobState.UNKNOWN
    provider_status = status if type(status) is str and _SAFE_WORD.fullmatch(status) else None
    model = data.get("model")
    model = model if type(model) is str and _PROVIDER_MODEL.fullmatch(model) else None
    texts: list[str] = []
    calls: list[ToolCall] = []
    unhandled: list[str] = []
    echo = False
    output = data.get("output")
    if output is not None and type(output) is not list:
        unhandled.append("output_invalid")
    for item in (output if type(output) is list else [])[:MAX_ITEMS]:
        kind = item.get("type") if type(item) is dict else None
        if kind == "message":
            content = item.get("content")
            for part in (content if type(content) is list else [])[:MAX_ITEMS]:
                part_type = part.get("type") if type(part) is dict else None
                if part_type == "output_text" and type(part.get("text")) is str:
                    text, found = scrub(part["text"])
                    echo = echo or found
                    if text is not None:
                        texts.append(text)
                else:
                    unhandled.append("message." + _word(part_type))
        elif kind == "function_call":
            call = _tool_call(item, scrub)
            if call is None:
                unhandled.append("function_call.invalid")
            else:
                calls.append(call[0])
                echo = echo or call[1]
        else:
            unhandled.append(_word(kind))
    if type(output) is list and len(output) > MAX_ITEMS:
        unhandled.append("output_truncated")
    joined = "".join(texts) if texts else None
    if joined is not None and len(joined) > MAX_TEXT_CHARS:
        joined = None
        unhandled.append("output_text_too_large")
    error_code = None
    error = data.get("error")
    if type(error) is dict:
        code = error.get("code")
        if type(code) is str and _LOG.safe_id(code) is not None and not scrub(code)[1]:
            error_code = code
    return TurnResult(
        request_id=request_id, provider_job_id=_alias(raw_id), response_id=raw_id, state=state,
        provider_status=provider_status, model=model, usage=_usage(data.get("usage")),
        output_text=joined, tool_calls=tuple(calls[:MAX_ITEMS]), unhandled_item_types=tuple(unhandled[:MAX_ITEMS]),
        error_code=error_code, secret_echo_redacted=echo, evidence_kind=evidence_kind,
    )


def _turn_payload(model: str, turn: TurnInput) -> dict:
    items: list[dict] = [{"role": message.role, "content": message.text} for message in turn.messages]
    items.extend({"type": "function_call_output", "call_id": result.call_id, "output": result.output}
                 for result in turn.tool_results)
    payload: dict = {"model": model, "input": items, "background": True}
    if turn.tools:
        payload["tools"] = [{"type": "function", "name": tool.name, "description": tool.description,
                             "parameters": tool.parameters} for tool in turn.tools]
    if turn.previous_response_id is not None:
        payload["previous_response_id"] = turn.previous_response_id
    if turn.max_output_tokens is not None:
        payload["max_output_tokens"] = turn.max_output_tokens
    return payload


class _Connection:
    __slots__ = ("binding", "auth_state", "auth_at", "models", "released")

    def __init__(self, binding: ConnectionBinding):
        self.binding = binding
        self.auth_state = AuthState.UNKNOWN
        self.auth_at: float | None = None
        self.models: dict[str, str] = {}
        self.released = False

    def reset(self) -> None:
        self.auth_state = AuthState.UNKNOWN
        self.auth_at = None
        self.models.clear()


class _Job:
    __slots__ = ("request", "alias", "raw_id", "state")

    def __init__(self, request: JobRequest, alias: str, raw_id: str, state: JobState):
        self.request, self.alias, self.raw_id, self.state = request, alias, raw_id, state


class _Exchange:
    __slots__ = ("vault_fault", "transport_failed", "status", "headers", "parsed", "native_code", "retry_after_ms")

    def __init__(self) -> None:
        self.vault_fault: AdapterFailure | None = None
        self.transport_failed = False
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.parsed = None
        self.native_code: str | None = None
        self.retry_after_ms: int | None = None


def _positive(value: object, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(code)
    return float(value)


class OpenAIApiAdapter:
    """API-key adapter; implemented, but its route is never self-qualified."""

    def __init__(self, *, transport, vault, clock=time.monotonic,
                 auth_ttl_seconds: float = 300.0, timeout_seconds: float = 60.0):
        if not callable(getattr(transport, "send", None)):
            raise TypeError("An explicit transport with send() is required.")
        if not isinstance(vault, VaultBackend):
            raise TypeError("An aik_vault VaultBackend is required.")
        if not callable(clock):
            raise TypeError("A callable clock is required.")
        self._ttl = _positive(auth_ttl_seconds, "INVALID_AUTH_TTL")
        self._timeout = _positive(timeout_seconds, "INVALID_TIMEOUT")
        kind = getattr(transport, "kind", None)
        self._evidence_kind = kind if kind in ("fixture", "network") else "unspecified"
        self._transport = transport
        self._vault = vault
        self._clock = clock
        self._lock = RLock()
        self._connections: dict[str, _Connection] = {}
        self._staged: dict[str, tuple[JobRequest, TurnInput]] = {}
        self._attempted: set[str] = set()
        self._jobs: dict[str, _Job] = {}
        self._aliases: dict[str, str] = {}
        self._results: dict[str, TurnResult] = {}
        self._events: list[dict] = []

    def __repr__(self) -> str:
        return "OpenAIApiAdapter()"

    # -- local helpers ----------------------------------------------------
    def _event(self, operation: str, *, binding: ConnectionBinding | None = None,
               request_id: str | None = None, http_status: int | None = None, result: str) -> None:
        record = {
            "operation": operation,
            "connection_id": binding.connection_id if binding else None,
            "revision": binding.revision if binding else None,
            "request_id": request_id, "http_status": http_status, "result": result,
        }
        self._events.append(_LOG.output("log", record))
        if len(self._events) > MAX_EVENTS:
            del self._events[0]

    def _lookup(self, binding: object, *, allow_released: bool = False) -> _Connection:
        if type(binding) is not ConnectionBinding:
            raise _failure(FaultKind.INVALID_CONTRACT)
        connection = self._connections.get(binding.connection_id)
        if connection is None or connection.binding != binding:
            raise _failure(FaultKind.STALE_CONNECTION)
        if connection.released and not allow_released:
            raise _failure(FaultKind.AUTH_REQUIRED)
        return connection

    def _auth_state(self, connection: _Connection) -> AuthState:
        if connection.released:
            return AuthState.UNAUTHENTICATED
        if connection.auth_state is AuthState.AUTHENTICATED and (
            connection.auth_at is None or self._clock() - connection.auth_at > self._ttl
        ):
            return AuthState.UNKNOWN
        return connection.auth_state

    def _observation(self, connection: _Connection) -> AuthObservation:
        state = self._auth_state(connection)
        binding = connection.binding
        return AuthObservation(binding.connection_id, binding.revision,
                               binding.account_fingerprint if state is AuthState.AUTHENTICATED else None, state)

    def _active(self, connection_id: str) -> bool:
        return any(job.request.binding.connection_id == connection_id and job.state not in TERMINAL
                   for job in self._jobs.values())

    def _drop_staged(self, connection_id: str) -> None:
        self._staged = {key: value for key, value in self._staged.items()
                        if value[0].binding.connection_id != connection_id}

    @staticmethod
    def _check_secret(secret: object) -> None:
        if type(secret) is not str or _SECRET_FORMAT.fullmatch(secret) is None:
            raise _failure(FaultKind.INVALID_CONTRACT)

    def _resolve(self, connection: _Connection) -> tuple[str | None, AdapterFailure | None]:
        secret, fault = None, None
        try:
            secret = self._vault.resolve(connection.binding.credential_ref)
        except VaultError as error:
            fault = _vault_fault(error)
        except Exception:
            fault = _failure(FaultKind.PROVIDER_FAILURE)
        if fault is None and (type(secret) is not str or _SECRET_FORMAT.fullmatch(secret) is None):
            secret, fault = None, _failure(FaultKind.INVALID_CONTRACT)
        return secret, fault

    def _call(self, connection: _Connection, method: str, path: str, payload, operation: str,
              *, parse=None, request_id: str | None = None) -> _Exchange:
        """Plaintext lives only in this call: header construction and response scrub."""
        exchange = _Exchange()
        body = None if payload is None else json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        secret, fault = self._resolve(connection)
        if fault is not None:
            exchange.vault_fault = fault
            self._event(operation, binding=connection.binding, request_id=request_id, result="credential_unavailable")
            return exchange
        redactor = OutputRedactor((secret,))
        headers = [("Authorization", "Bearer " + secret), ("Accept", "application/json")]
        if body is not None:
            headers.append(("Content-Type", "application/json"))
        response = None
        try:
            response = self._transport.send(HttpRequest(method, path, tuple(headers), body), timeout=self._timeout)
        except Exception:
            # The raw exception may echo headers; it is dropped, never chained.
            response = None
        headers = None
        if type(response) is not HttpResponse:
            exchange.transport_failed = True
            self._event(operation, binding=connection.binding, request_id=request_id, result="transport_failure")
            return exchange
        exchange.status = response.status
        exchange.headers = {name.lower(): value for name, value in response.headers}
        data = _json(response.body)
        if 200 <= response.status < 300:
            if parse is not None and data is not None:
                parsed = None
                try:
                    parsed = parse(data, lambda text: _scrub(text, secret, redactor))
                except Exception:
                    parsed = None
                exchange.parsed = parsed
        else:
            exchange.native_code = _native_code(data, redactor)
            exchange.retry_after_ms = _retry_after(exchange.headers.get("retry-after"))
        self._event(operation, binding=connection.binding, request_id=request_id,
                    http_status=response.status, result="http_response")
        return exchange

    def _job_result(self, connection: _Connection, exchange: _Exchange, *, model: str | None,
                    effect: bool) -> TurnResult:
        if exchange.vault_fault is not None:
            if exchange.vault_fault.fault.kind is FaultKind.AUTH_REQUIRED:
                connection.auth_state = AuthState.UNAUTHENTICATED
                connection.models.clear()
            raise exchange.vault_fault  # no request was sent
        if exchange.transport_failed:
            raise _failure(FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN)
        status = exchange.status
        if 200 <= status < 300:
            if type(exchange.parsed) is not TurnResult:
                raise _failure(FaultKind.INVALID_RECEIPT, Outcome.UNKNOWN)
            return exchange.parsed
        if status == 401:
            connection.auth_state = AuthState.UNAUTHENTICATED
            connection.models.clear()
        elif model is not None and status == 404:
            connection.models[model] = MODEL_NOT_FOUND
        elif model is not None and status == 403:
            connection.models[model] = MODEL_DENIED
        raise AdapterFailure(fault_from_provider(
            http_status=status, native_code=exchange.native_code, retry_after_ms=exchange.retry_after_ms,
            outcome_unknown=effect and (status >= 500 or status == 408),
        ))

    @staticmethod
    def _receipt(job: _Job) -> JobReceipt:
        binding = job.request.binding
        return JobReceipt(job.request.request_id, job.alias, binding.connection_id, binding.revision,
                          binding.account_fingerprint, job.state)

    # -- key lifecycle ----------------------------------------------------
    def register_key(self, connection_id: str, secret: str) -> ConnectionBinding:
        """Store a key in the vault and return a binding that holds only its reference."""
        self._check_secret(secret)
        if type(connection_id) is not str or _ID.fullmatch(connection_id) is None:
            raise _failure(FaultKind.INVALID_CONTRACT)
        with self._lock:
            if connection_id in self._connections:
                raise _failure(FaultKind.ACCOUNT_CONFLICT)
            reference, fault = None, None
            try:
                reference = self._vault.put(secret)
            except VaultError as error:
                fault = _vault_fault(error)
            except Exception:
                fault = _failure(FaultKind.PROVIDER_FAILURE)
            if fault is not None:
                raise fault
            binding = None
            try:
                binding = ConnectionBinding(PROVIDER, RUNTIME, connection_id, 1, AuthKind.API_KEY, reference,
                                            slot_fingerprint(connection_id, reference, 1))
            except ValueError:
                binding = None
            if binding is None:
                try:
                    self._vault.delete(reference)
                except Exception:
                    pass
                raise _failure(FaultKind.INVALID_CONTRACT)
            self._connections[connection_id] = _Connection(binding)
            self._event("register_key", binding=binding, result="stored")
            return binding

    def attach(self, binding: ConnectionBinding) -> None:
        """Re-attach a persisted binding (e.g. after restart); it must be re-verified."""
        if type(binding) is not ConnectionBinding or (binding.provider, binding.runtime, binding.auth_kind) != (
                PROVIDER, RUNTIME, AuthKind.API_KEY):
            raise _failure(FaultKind.INVALID_CONTRACT)
        if binding.account_fingerprint != slot_fingerprint(binding.connection_id, binding.credential_ref,
                                                           binding.revision):
            raise _failure(FaultKind.INVALID_CONTRACT)
        with self._lock:
            existing = self._connections.get(binding.connection_id)
            if existing is not None:
                if existing.binding == binding and not existing.released:
                    return
                raise _failure(FaultKind.ACCOUNT_CONFLICT)
            self._connections[binding.connection_id] = _Connection(binding)
            self._event("attach", binding=binding, result="attached")

    def verify_key(self, binding: ConnectionBinding) -> AuthObservation:
        """Authentication check (GET /models, unverified). Failures become states."""
        with self._lock:
            connection = self._lookup(binding)
            exchange = self._call(connection, "GET", "/models", None, "verify_key")
            if exchange.vault_fault is not None:
                kind = exchange.vault_fault.fault.kind
                state = (AuthState.LOCKED if kind is FaultKind.ACCESS_DENIED else
                         AuthState.UNAUTHENTICATED if kind in (FaultKind.AUTH_REQUIRED, FaultKind.INVALID_CONTRACT)
                         else AuthState.UNKNOWN)
            elif exchange.transport_failed:
                state = AuthState.UNKNOWN
            elif 200 <= exchange.status < 300:
                state = AuthState.AUTHENTICATED
            elif exchange.status == 401:
                state = AuthState.UNAUTHENTICATED
            elif exchange.status == 403:
                state = AuthState.LOCKED
            else:
                state = AuthState.UNKNOWN
            connection.auth_state = state
            connection.auth_at = self._clock()
            if state is not AuthState.AUTHENTICATED:
                connection.models.clear()
            return self._observation(connection)

    def check_model(self, binding: ConnectionBinding, model: str) -> ModelCheck:
        """Confirm one configured model name; nothing is advertised without this."""
        if type(model) is not str or _ID.fullmatch(model) is None:
            raise _failure(FaultKind.INVALID_CONTRACT)
        with self._lock:
            connection = self._lookup(binding)
            if self._auth_state(connection) is not AuthState.AUTHENTICATED:
                raise _failure(FaultKind.AUTH_REQUIRED)
            if model not in connection.models and len(connection.models) >= MAX_MODELS:
                raise _failure(FaultKind.INVALID_CONTRACT)
            exchange = self._call(
                connection, "GET", "/models/" + quote(model, safe="-._~"), None, "check_model",
                parse=lambda data, scrub: data.get("id") if type(data) is dict and type(data.get("id")) is str else None,
            )
            if exchange.vault_fault is not None:
                connection.reset()
                raise exchange.vault_fault
            status = exchange.status
            if exchange.transport_failed:
                availability = MODEL_UNKNOWN
            elif 200 <= status < 300:
                availability = MODEL_AVAILABLE if exchange.parsed == model else MODEL_UNKNOWN
            elif status == 404:
                availability = MODEL_NOT_FOUND
            elif status == 403:
                availability = MODEL_DENIED
            elif status == 401:
                availability = MODEL_UNAUTHENTICATED
                connection.auth_state = AuthState.UNAUTHENTICATED
                connection.models.clear()
            else:
                availability = MODEL_UNKNOWN
            if availability != MODEL_UNAUTHENTICATED:
                connection.models[model] = availability
            return ModelCheck(model, availability, status)

    def rotate_key(self, binding: ConnectionBinding, new_secret: str) -> ConnectionBinding:
        """Replace the vault secret in place; the binding revision advances."""
        self._check_secret(new_secret)
        with self._lock:
            connection = self._lookup(binding)
            if self._active(binding.connection_id):
                raise _failure(FaultKind.ACCOUNT_CONFLICT)
            fault = None
            try:
                self._vault.replace(binding.credential_ref, new_secret)
            except (InvalidSecret, InvalidCredentialRef, CredentialNotFound) as error:
                fault = _vault_fault(error)
            except Exception:
                # The vault may now hold either key: require re-verification.
                connection.reset()
                fault = _failure(FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN)
            if fault is not None:
                self._event("rotate_key", binding=binding, result="failed")
                raise fault
            revision = binding.revision + 1
            updated = replace(binding, revision=revision, account_fingerprint=slot_fingerprint(
                binding.connection_id, binding.credential_ref, revision))
            connection.binding = updated
            connection.reset()
            self._drop_staged(binding.connection_id)
            self._event("rotate_key", binding=updated, result="rotated")
            return updated

    def sign_out(self, binding: ConnectionBinding) -> AuthObservation:
        """Release: delete the local vault secret. Vendor-side revocation is not done."""
        with self._lock:
            connection = self._lookup(binding)
            if self._active(binding.connection_id):
                raise _failure(FaultKind.ACCOUNT_CONFLICT)
            fault = None
            try:
                self._vault.delete(binding.credential_ref)
            except CredentialNotFound:
                fault = None
            except Exception:
                fault = _failure(FaultKind.UNKNOWN_OUTCOME, Outcome.UNKNOWN)
            connection.released = True
            connection.auth_state = AuthState.UNAUTHENTICATED
            connection.models.clear()
            self._drop_staged(binding.connection_id)
            self._event("sign_out", binding=binding, result="released" if fault is None else "release_uncertain")
            if fault is not None:
                raise fault
            return self._observation(connection)

    release_key = sign_out

    def begin_sign_in(self, binding: ConnectionBinding) -> AuthObservation:
        # An API key is registered with register_key, never through host sign-in.
        raise _failure(FaultKind.UNSUPPORTED)

    # -- ProviderAdapter observation -------------------------------------
    def describe(self, binding: ConnectionBinding) -> AdapterCapabilities:
        if type(binding) is not ConnectionBinding:
            raise _failure(FaultKind.INVALID_CONTRACT)
        return AdapterCapabilities(
            provider=PROVIDER, runtime=RUNTIME, auth_kind=AuthKind.API_KEY,
            isolation=AccountIsolation.UNKNOWN,  # not claimed without live evidence
            auth_status=Support.SUPPORTED, sign_in=Support.UNSUPPORTED, sign_out=Support.SUPPORTED,
            job_start=Support.SUPPORTED, job_observe=Support.SUPPORTED, job_interrupt=Support.SUPPORTED,
        )

    def auth_status(self, binding: ConnectionBinding) -> AuthObservation:
        """Cached result of the last verify_key; expires to UNKNOWN after the TTL."""
        with self._lock:
            return self._observation(self._lookup(binding, allow_released=True))

    def executable_models(self, binding: ConnectionBinding) -> tuple[str, ...]:
        with self._lock:
            connection = self._lookup(binding, allow_released=True)
            if self._auth_state(connection) is not AuthState.AUTHENTICATED:
                return ()
            return tuple(sorted(name for name, state in connection.models.items() if state == MODEL_AVAILABLE))

    def resource_view(self, binding: ConnectionBinding) -> dict:
        with self._lock:
            connection = self._lookup(binding, allow_released=True)
            return {
                "provider": PROVIDER, "runtime": RUNTIME,
                "connection_id": connection.binding.connection_id, "revision": connection.binding.revision,
                "auth_state": self._auth_state(connection).value,
                "executable_models": list(self.executable_models(binding)),
                "billing_basis": BILLING_BASIS, "subscription_quota": "not_applicable",
                "implementation_state": "implemented", "route_qualification": "NOT_RUN",
                "live_vendor_round_trip": "NOT_RUN",
            }

    @staticmethod
    def status() -> AdapterStatus:
        return AdapterStatus()

    def events(self) -> tuple:
        with self._lock:
            return tuple(json.loads(json.dumps(event)) for event in self._events)

    def result(self, request_id: str) -> TurnResult | None:
        with self._lock:
            return self._results.get(request_id) if type(request_id) is str else None

    # -- jobs ---------------------------------------------------------------
    def stage_turn(self, request: JobRequest, turn: TurnInput) -> None:
        """Attach turn content to a JobRequest before start_job dispatches it."""
        if type(request) is not JobRequest or type(turn) is not TurnInput:
            raise _failure(FaultKind.INVALID_CONTRACT)
        with self._lock:
            self._lookup(request.binding)
            if request.request_id in self._attempted or request.request_id in self._staged:
                raise _failure(FaultKind.DUPLICATE_REQUEST)
            if len(self._staged) >= MAX_STAGED:
                raise _failure(FaultKind.INVALID_CONTRACT)
            self._staged[request.request_id] = (request, turn)

    def _parser(self, request: JobRequest):
        evidence_kind = self._evidence_kind
        return lambda data, scrub: _turn_from(data, scrub, request.request_id, evidence_kind)

    def start_job(self, request: JobRequest) -> JobReceipt:
        if type(request) is not JobRequest:
            raise _failure(FaultKind.INVALID_CONTRACT)
        with self._lock:
            connection = self._lookup(request.binding)
            if request.request_id in self._attempted:
                raise _failure(FaultKind.DUPLICATE_REQUEST)
            staged = self._staged.get(request.request_id)
            if staged is None or staged[0] != request:
                raise _failure(FaultKind.INVALID_CONTRACT)
            if self._auth_state(connection) is not AuthState.AUTHENTICATED:
                raise _failure(FaultKind.AUTH_REQUIRED)
            if connection.models.get(request.model) != MODEL_AVAILABLE:
                raise _failure(FaultKind.UNSUPPORTED)
            payload = _turn_payload(request.model, staged[1])
            del self._staged[request.request_id]
            self._attempted.add(request.request_id)
            exchange = self._call(connection, "POST", "/responses", payload, "start_job",
                                  parse=self._parser(request), request_id=request.request_id)
            result = self._job_result(connection, exchange, model=request.model, effect=True)
            if result.provider_job_id in self._aliases:
                raise _failure(FaultKind.INVALID_RECEIPT, Outcome.UNKNOWN)
            job = _Job(request, result.provider_job_id, result.response_id, result.state)
            self._jobs[request.request_id] = job
            self._aliases[job.alias] = request.request_id
            self._results[request.request_id] = result
            return self._receipt(job)

    def _job_for(self, request: object, receipt: object) -> _Job:
        if type(request) is not JobRequest or type(receipt) is not JobReceipt:
            raise _failure(FaultKind.INVALID_CONTRACT)
        job = self._jobs.get(request.request_id)
        if job is None or job.request != request or receipt.request_id != request.request_id or \
                receipt.provider_job_id != job.alias:
            raise _failure(FaultKind.INVALID_RECEIPT, Outcome.UNKNOWN)
        return job

    def _advance(self, request: JobRequest, receipt: JobReceipt, operation: str) -> JobReceipt:
        with self._lock:
            job = self._job_for(request, receipt)
            interrupt = operation == "interrupt_job"
            if interrupt and job.state in TERMINAL:
                return self._receipt(job)
            connection = self._lookup(request.binding)
            if interrupt:
                exchange = self._call(connection, "POST", f"/responses/{job.raw_id}/cancel", None, operation,
                                      parse=self._parser(request), request_id=request.request_id)
            else:
                exchange = self._call(connection, "GET", f"/responses/{job.raw_id}", None, operation,
                                      parse=self._parser(request), request_id=request.request_id)
            result = self._job_result(connection, exchange, model=None, effect=interrupt)
            if result.response_id != job.raw_id:
                raise _failure(FaultKind.INVALID_RECEIPT, Outcome.UNKNOWN)
            job.state = result.state
            self._results[request.request_id] = result
            return self._receipt(job)

    def observe_job(self, request: JobRequest, receipt: JobReceipt) -> JobReceipt:
        return self._advance(request, receipt, "observe_job")

    def interrupt_job(self, request: JobRequest, receipt: JobReceipt) -> JobReceipt:
        """A cancel acknowledgement is not termination until CANCELLED is observed."""
        return self._advance(request, receipt, "interrupt_job")


__all__ = ["OpenAIApiAdapter", "PROVIDER", "RUNTIME", "slot_fingerprint"]
