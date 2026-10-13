"""Injectable HTTP transport boundary for the OpenAI API adapter.

The adapter never opens sockets itself. Tests inject a synthetic fixture
transport. ``UrllibTransport`` is the standard-library network transport; it
has not been run against the vendor (NOT_RUN) and refuses redirects so an
Authorization header is never forwarded to another location.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Protocol, runtime_checkable
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_BASE_URL = "https://api.openai.com/v1"  # unverified assumption, see docs
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_METHODS = frozenset({"GET", "POST"})
_PATH = re.compile(r"/[A-Za-z0-9._~%:/-]{0,512}\Z")
_HEADER_NAME = re.compile(r"[A-Za-z0-9-]{1,64}\Z")


@dataclass(frozen=True, slots=True, repr=False)
class HttpRequest:
    """One outbound request. Header values may hold a credential: never log them."""

    method: str
    path: str
    headers: tuple = ()
    body: bytes | None = None

    def __post_init__(self) -> None:
        if type(self.method) is not str or self.method not in _METHODS:
            raise ValueError("INVALID_HTTP_METHOD")
        if type(self.path) is not str or _PATH.fullmatch(self.path) is None or any(
            segment in (".", "..") for segment in self.path.split("/")[1:]
        ):
            raise ValueError("INVALID_HTTP_PATH")
        if type(self.headers) is not tuple or len(self.headers) > 32:
            raise ValueError("INVALID_HTTP_HEADERS")
        for pair in self.headers:
            if (type(pair) is not tuple or len(pair) != 2 or type(pair[0]) is not str or type(pair[1]) is not str
                    or _HEADER_NAME.fullmatch(pair[0]) is None or len(pair[1]) > 8192
                    or any(char in pair[1] for char in "\r\n\0")):
                raise ValueError("INVALID_HTTP_HEADERS")
        if self.body is not None and type(self.body) is not bytes:
            raise ValueError("INVALID_HTTP_BODY")

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        for key, value in self.headers:
            if key.lower() == wanted:
                return value
        return None

    def __repr__(self) -> str:
        return f"HttpRequest(method={self.method!r}, path={self.path!r}, headers=[REDACTED])"


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: tuple = ()
    body: bytes = b""

    def __post_init__(self) -> None:
        if type(self.status) is not int or not 100 <= self.status <= 599:
            raise ValueError("INVALID_HTTP_STATUS")
        if type(self.headers) is not tuple or len(self.headers) > 256 or any(
            type(pair) is not tuple or len(pair) != 2 or type(pair[0]) is not str or type(pair[1]) is not str
            for pair in self.headers
        ):
            raise ValueError("INVALID_HTTP_HEADERS")
        if type(self.body) is not bytes or len(self.body) > MAX_RESPONSE_BYTES:
            raise ValueError("INVALID_HTTP_BODY")


@runtime_checkable
class Transport(Protocol):
    """``kind`` is ``fixture`` or ``network``; it labels evidence, not support."""

    def send(self, request: HttpRequest, *, timeout: float) -> HttpResponse: ...


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None  # a 3xx becomes an HTTPError instead of forwarding credentials


class UrllibTransport:
    """Standard-library HTTPS transport. Vendor round trip is NOT_RUN."""

    kind = "network"

    def __init__(self, base_url: str = DEFAULT_BASE_URL):
        if type(base_url) is not str:
            raise ValueError("INVALID_BASE_URL")
        parts = urllib.parse.urlsplit(base_url)
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.query or parts.fragment):
            raise ValueError("INVALID_BASE_URL")
        self._base = base_url.rstrip("/")
        self._opener = urllib.request.build_opener(_RefuseRedirect)

    def __repr__(self) -> str:
        return f"UrllibTransport(base_url={self._base!r})"

    def send(self, request: HttpRequest, *, timeout: float) -> HttpResponse:
        if type(request) is not HttpRequest:
            raise TypeError("HttpRequest required")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("INVALID_TIMEOUT")
        outgoing = urllib.request.Request(
            self._base + request.path, data=request.body, method=request.method, headers=dict(request.headers),
        )
        try:
            with self._opener.open(outgoing, timeout=timeout) as response:
                return HttpResponse(response.status, tuple(response.headers.items()),
                                    response.read(MAX_RESPONSE_BYTES + 1))
        except urllib.error.HTTPError as error:
            try:
                body = error.read(MAX_RESPONSE_BYTES + 1)
                headers = tuple(error.headers.items()) if error.headers is not None else ()
            finally:
                error.close()
            return HttpResponse(error.code, headers, body)


__all__ = ["HttpRequest", "HttpResponse", "Transport", "UrllibTransport", "DEFAULT_BASE_URL", "MAX_RESPONSE_BYTES"]
