"""JEV HTTP client for the contract observed on 2026-10-13 (TypeSafe 0.2.0).

It reuses the AIK-013 transport boundary (redirect refusal, response size
limit). There are no retries, redirects or model fallbacks. Bearer tokens are
resolved per request and never stored, logged, raised or returned.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit

from aik_http.transport import (
    MAX_RESPONSE_BYTES, HttpRequest, HttpResponse, Transport, TransportNotSent, TransportTimeout,
    UrllibTransport,
)

from .errors import JevError
from .models import MAX_INTEGER, check_model, unit_interval


DEFAULT_BASE_URL = "https://api.typesafe.ai"
OBSERVED_CONTRACT_VERSION = "0.2.0"
USER_AGENT = "aik-jev/0.1 (AI-Kubernetes)"
QUESTION_TYPES = ("noul", "choice", "score")
MAX_QUESTIONS = 64
MAX_MODELS = 1000


@dataclass(frozen=True, slots=True)
class ModelInfo:
    name: str
    description: str | None
    release_date: str  # kept verbatim; the observed format differs from the schema document


@dataclass(frozen=True, slots=True)
class SystemOneResult:
    observed_model: str
    answers: Mapping
    input_tokens: int
    output_tokens: int
    raw_body_sha256: str


def _text_ok(value: object, limit: int) -> bool:
    return type(value) is str and 1 <= len(value) <= limit and value.isprintable()


def _base_url(value: object) -> str:
    if type(value) is not str or not value or "?" in value or "#" in value or "@" in value:
        raise JevError("INVALID_BASE_URL")
    if any(char.isspace() or ord(char) < 32 for char in value):
        raise JevError("INVALID_BASE_URL")
    try:
        parts = urlsplit(value)
        parts.port
    except ValueError:
        raise JevError("INVALID_BASE_URL") from None
    if (parts.scheme != "https" or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.path not in ("", "/") or parts.query or parts.fragment):
        raise JevError("INVALID_BASE_URL")
    return f"https://{parts.netloc}"


def _status_code(status: int) -> str:
    if status in (401, 403):
        return "AUTH_REJECTED"
    if status == 404:
        return "NOT_FOUND"
    if status == 422:
        return "REQUEST_REJECTED"
    if status == 429:
        return "RATE_LIMITED"
    if 500 <= status <= 599:
        return "SERVICE_ERROR"
    return "UNEXPECTED_STATUS"


def _reject_constant(_name):
    raise ValueError("non-finite JSON constant")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _parse(body: bytes):
    try:
        return json.loads(body.decode("utf-8"), parse_constant=_reject_constant, object_pairs_hook=_unique_pairs)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise JevError("INVALID_RESPONSE") from None


def _normalize_questions(questions: object) -> dict:
    if not isinstance(questions, Mapping) or not 1 <= len(questions) <= MAX_QUESTIONS:
        raise JevError("INVALID_REQUEST")
    if any(not _text_ok(name, 128) for name in questions):
        raise JevError("INVALID_REQUEST")
    try:
        snapshot = json.loads(json.dumps(dict(questions), ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, RecursionError):
        raise JevError("INVALID_REQUEST") from None
    for question in snapshot.values():
        if type(question) is not dict or question.get("type") not in QUESTION_TYPES:
            raise JevError("INVALID_REQUEST")
        kind, criteria = question["type"], question.get("criteria")
        if kind == "choice":
            if type(criteria) is not dict or not criteria:
                raise JevError("INVALID_REQUEST")
            if any(not _text_ok(key, 256) or (value is not None and type(value) is not str)
                   for key, value in criteria.items()):
                raise JevError("INVALID_REQUEST")
        elif kind == "score":
            if type(criteria) is not list or not criteria:
                raise JevError("INVALID_REQUEST")
        elif criteria is not None and (type(criteria) is not dict or not set(criteria) <= {"true", "false"}):
            raise JevError("INVALID_REQUEST")
    return snapshot


def _unit_map(value: object, allowed=None) -> None:
    if type(value) is not dict:
        raise JevError("INVALID_RESPONSE")
    for key, probability in value.items():
        if not unit_interval(probability) or (allowed is not None and key not in allowed):
            raise JevError("INVALID_RESPONSE")


def _check_answer(answer: object, question: dict) -> None:
    if type(answer) is not dict or answer.get("type") != question["type"]:
        raise JevError("INVALID_RESPONSE")
    kind = question["type"]
    if kind == "noul":
        if not unit_interval(answer.get("noul")):
            raise JevError("INVALID_RESPONSE")
        return
    if not unit_interval(answer.get("confidence")):
        raise JevError("INVALID_RESPONSE")
    if kind == "choice":
        criteria = question["criteria"]
        choice = answer.get("choice")
        if type(choice) is not str or choice not in criteria:
            raise JevError("INVALID_RESPONSE")
        _unit_map(answer.get("probabilities"), criteria)
        return
    score = answer.get("score")
    if type(score) not in (int, float) or not math.isfinite(score):
        raise JevError("INVALID_RESPONSE")
    if type(answer.get("legend")) is not dict:
        raise JevError("INVALID_RESPONSE")
    _unit_map(answer.get("probabilities"))


class JevClient:
    """Bearer token (optional) is resolved per request and never retained."""

    def __init__(self, transport, *, base_url: str = DEFAULT_BASE_URL, token_resolver=None,
                 timeout: float = 30.0):
        if not isinstance(transport, Transport):
            raise JevError("INVALID_TRANSPORT")
        if token_resolver is not None and not callable(token_resolver):
            raise JevError("INVALID_TOKEN_RESOLVER")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 600):
            raise JevError("INVALID_TIMEOUT")
        self._transport = transport
        self._base_url = _base_url(base_url)
        self._resolver = token_resolver
        self._timeout = float(timeout)

    def __repr__(self) -> str:
        return (f"JevClient(base_url={self._base_url!r}, "
                f"authorization={'resolver' if self._resolver is not None else 'none'})")

    @property
    def base_url(self) -> str:
        return self._base_url

    def _exchange(self, method: str, path: str, body: bytes | None) -> bytes:
        headers = [("accept", "application/json"), ("user-agent", USER_AGENT)]
        if body is not None:
            headers.append(("content-type", "application/json"))
        if self._resolver is not None:
            token, failed = None, False
            try:
                token = self._resolver()
            except Exception:
                failed = True
            # Raise outside the except block so no exception context keeps a value.
            if failed or type(token) is not str or not 1 <= len(token) <= 8192 or any(
                    not 33 <= ord(char) <= 126 for char in token):
                token = None
                raise JevError("TOKEN_UNAVAILABLE")
            headers.append(("authorization", "Bearer " + token))
            token = None
        request = HttpRequest(method, self._base_url + path, tuple(headers), body, self._timeout)
        headers = None
        response, failure = None, None
        try:
            response = self._transport.send(request)
        except TransportTimeout:
            failure = "TIMEOUT"
        except TransportNotSent:
            failure = "NOT_SENT"
        except Exception:
            failure = "TRANSPORT_ERROR"
        request = None
        if failure is not None:
            raise JevError(failure)
        if type(response) is not HttpResponse:
            raise JevError("INVALID_RESPONSE")
        if len(response.body) > MAX_RESPONSE_BYTES:
            raise JevError("RESPONSE_TOO_LARGE", status=response.status)
        if not 200 <= response.status <= 299:
            raise JevError(_status_code(response.status), status=response.status)
        return response.body

    def list_models(self) -> tuple:
        data = _parse(self._exchange("GET", "/v1/models", None))
        if type(data) is not dict or type(data.get("models")) is not list or len(data["models"]) > MAX_MODELS:
            raise JevError("INVALID_RESPONSE")
        result = []
        for item in data["models"]:
            if type(item) is not dict or not _text_ok(item.get("name"), 256):
                raise JevError("INVALID_RESPONSE")
            description, release_date = item.get("description"), item.get("release_date")
            if description is not None and type(description) is not str:
                raise JevError("INVALID_RESPONSE")
            if type(release_date) is not str or len(release_date) > 128:
                raise JevError("INVALID_RESPONSE")
            result.append(ModelInfo(item["name"], description, release_date))
        return tuple(result)

    def evaluate(self, model: str, state, questions) -> SystemOneResult:
        check_model(model, "INVALID_REQUEST")
        snapshot = _normalize_questions(questions)
        if type(state) not in (str, dict, list):
            raise JevError("INVALID_REQUEST")
        try:
            body = json.dumps({"model": model, "state": state, "questions": snapshot},
                              ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            raise JevError("INVALID_REQUEST") from None
        response_body = self._exchange("POST", "/v1/systemone", body)
        data = _parse(response_body)
        if type(data) is not dict or not _text_ok(data.get("model"), 256):
            raise JevError("INVALID_RESPONSE")
        answers, usage = data.get("answers"), data.get("usage")
        if type(answers) is not dict or set(answers) != set(snapshot) or type(usage) is not dict:
            raise JevError("INVALID_RESPONSE")
        tokens = []
        for key in ("input_tokens", "output_tokens"):
            value = usage.get(key)
            if type(value) is not int or not 0 <= value <= MAX_INTEGER:
                raise JevError("INVALID_RESPONSE")
            tokens.append(value)
        for name, question in snapshot.items():
            _check_answer(answers[name], question)
        return SystemOneResult(data["model"], MappingProxyType(answers), tokens[0], tokens[1],
                               hashlib.sha256(response_body).hexdigest())


__all__ = [
    "JevClient", "ModelInfo", "SystemOneResult", "UrllibTransport", "DEFAULT_BASE_URL",
    "OBSERVED_CONTRACT_VERSION", "USER_AGENT", "QUESTION_TYPES",
]
