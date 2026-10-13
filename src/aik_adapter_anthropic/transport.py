"""Injectable HTTP boundary for the AIK-013 adapter.

Tests inject a synthetic transport. ``UrllibTransport`` is the standard
library live transport; its real vendor round trip is NOT_RUN. Request headers
(which carry the API key) and bodies are excluded from ``repr``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import socket
from typing import Protocol, runtime_checkable
import urllib.error
import urllib.request

MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class TransportTimeout(Exception):
    """No response arrived before the deadline; the provider may have acted."""

    def __init__(self) -> None:
        super().__init__("TRANSPORT_TIMEOUT")


class TransportNotSent(Exception):
    """The request provably did not leave this process (DNS or connect refusal)."""

    def __init__(self) -> None:
        super().__init__("TRANSPORT_NOT_SENT")


def _headers(value: object) -> tuple[tuple[str, str], ...]:
    items = value.items() if isinstance(value, dict) else value
    if type(items) is not tuple and not isinstance(value, dict) and type(items) is not list:
        raise ValueError("INVALID_HTTP_HEADERS")
    result = []
    for item in items:
        if type(item) is not tuple or len(item) != 2:
            raise ValueError("INVALID_HTTP_HEADERS")
        name, text = item
        if type(name) is not str or type(text) is not str or not name:
            raise ValueError("INVALID_HTTP_HEADERS")
        if any(char in name + text for char in "\r\n\0"):
            raise ValueError("INVALID_HTTP_HEADERS")
        result.append((name.lower(), text))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class HttpRequest:
    """One outgoing request. Never log, pickle or persist this object."""

    method: str
    url: str
    headers: tuple[tuple[str, str], ...] = field(repr=False)
    body: bytes | None = field(default=None, repr=False)
    timeout: float = 60.0

    def __post_init__(self) -> None:
        if self.method not in ("GET", "POST"):
            raise ValueError("INVALID_HTTP_METHOD")
        if type(self.url) is not str or not self.url:
            raise ValueError("INVALID_HTTP_URL")
        object.__setattr__(self, "headers", _headers(self.headers))
        if self.body is not None and type(self.body) is not bytes:
            raise ValueError("INVALID_HTTP_BODY")
        if (isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float))
                or not math.isfinite(self.timeout) or self.timeout <= 0):
            raise ValueError("INVALID_HTTP_TIMEOUT")

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        for key, value in self.headers:
            if key == wanted:
                return value
        return None


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = field(default=b"", repr=False)

    def __post_init__(self) -> None:
        if type(self.status) is not int or not 100 <= self.status <= 599:
            raise ValueError("INVALID_HTTP_STATUS")
        object.__setattr__(self, "headers", _headers(self.headers))
        if type(self.body) is not bytes:
            raise ValueError("INVALID_HTTP_BODY")

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        for key, value in self.headers:
            if key == wanted:
                return value
        return None


@runtime_checkable
class Transport(Protocol):
    """``source`` is ``synthetic_fixture`` (tests) or ``live``; never qualification."""

    source: str

    def send(self, request: HttpRequest) -> HttpResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect could forward the API key header to another host.
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _response_headers(message) -> tuple[tuple[str, str], ...]:
    if message is None:
        return ()
    result = []
    for name, value in message.items():
        if type(name) is str and type(value) is str and not any(c in name + value for c in "\r\n\0"):
            result.append((name.lower(), value))
    return tuple(result)


class UrllibTransport:
    """Live HTTPS transport (standard library). Real vendor use is NOT_RUN."""

    source = "live"

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirect())

    def __repr__(self) -> str:
        return "UrllibTransport()"

    def send(self, request: HttpRequest) -> HttpResponse:
        if type(request) is not HttpRequest or not request.url.startswith("https://"):
            raise TransportNotSent()
        native = urllib.request.Request(
            request.url, data=request.body, method=request.method, headers=dict(request.headers),
        )
        try:
            with self._opener.open(native, timeout=request.timeout) as response:
                return HttpResponse(response.status, _response_headers(response.headers),
                                    response.read(MAX_RESPONSE_BYTES + 1))
        except urllib.error.HTTPError as error:
            try:
                body = error.read(MAX_RESPONSE_BYTES + 1)
            finally:
                error.close()
            return HttpResponse(error.code, _response_headers(error.headers), body)
        except TimeoutError:
            raise TransportTimeout() from None
        except urllib.error.URLError as error:
            reason = error.reason
            if isinstance(reason, TimeoutError):
                raise TransportTimeout() from None
            if isinstance(reason, (socket.gaierror, ConnectionRefusedError)):
                raise TransportNotSent() from None
            raise


__all__ = [
    "HttpRequest", "HttpResponse", "Transport", "TransportTimeout", "TransportNotSent",
    "UrllibTransport", "MAX_RESPONSE_BYTES",
]
