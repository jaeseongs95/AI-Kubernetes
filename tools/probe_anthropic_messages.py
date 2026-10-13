"""Minimal live diagnostic probe of the AIK-013 Anthropic adapter (AIK-082 scope).

Runs one real Messages API turn through ``AnthropicAdapter`` and the standard
``UrllibTransport``. It is meant for an environment where an egress gateway
injects the credential: ``--gateway-injects-credential`` removes the adapter's
placeholder ``x-api-key`` header before sending, so no real secret is read,
stored or printed by this tool.

The report states exactly what was observed. A pass is ``probe_verified`` for
this one path only. It is not route qualification (AIK-086), it does not
verify ``x-api-key`` authentication against the vendor directly, account
binding or ``/v1/models`` (the gateway may not allow that path).

    python tools/probe_anthropic_messages.py --base-url https://GATEWAY \
        --gateway-injects-credential --model claude-haiku-5-5 --output report.json
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_adapter_anthropic import (  # noqa: E402
    DEFAULT_API_VERSION, MESSAGES_PATH, AnthropicAdapter, HttpRequest, Message, MessagesCall,
    TextPart, UrllibTransport,
)
from aik_provider.contracts import JobRequest  # noqa: E402
from aik_provider.controller import GuardedAdapter  # noqa: E402

PLACEHOLDER = "aik-probe-placeholder-not-a-credential"
PROMPT = "Reply with the single word: ready"


class MemoryVault:
    """Holds only the placeholder; the probe never handles a real secret."""

    def __init__(self) -> None:
        self._items: dict[str, str] = {}

    def put(self, secret: str) -> str:
        reference = "secret://aik-probe/" + hashlib.sha256(secret.encode()).hexdigest()[:16]
        self._items[reference] = secret
        return reference

    def replace(self, reference: str, secret: str) -> None:
        self._items[reference] = secret

    def delete(self, reference: str) -> None:
        self._items.pop(reference, None)

    def resolve(self, reference: str) -> str:
        return self._items[reference]


class GatewayTransport:
    """UrllibTransport that drops the placeholder key; the gateway injects auth."""

    source = "live"

    def __init__(self, inner: UrllibTransport) -> None:
        self._inner = inner
        self.sent: list[dict] = []

    def send(self, request: HttpRequest):
        headers = tuple((k, v) for k, v in request.headers if k != "x-api-key")
        if request.header("x-api-key") != PLACEHOLDER:
            raise RuntimeError("refusing to forward an unexpected credential header")
        stripped = HttpRequest(request.method, request.url, headers, request.body, request.timeout)
        response = self._inner.send(stripped)
        self.sent.append({
            "method": request.method,
            "path": request.url.split("://", 1)[1].split("/", 1)[1] if "/" in request.url.split("://", 1)[1] else "",
            "header_names": sorted(k for k, _ in headers),
            "status": response.status,
            "body_bytes": len(response.body),
        })
        return response


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--gateway-injects-credential", action="store_true", required=True,
                        help="Required: this tool never takes a real API key.")
    parser.add_argument("--max-tokens", type=int, default=16)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    transport = GatewayTransport(UrllibTransport())
    adapter = AnthropicAdapter(MemoryVault(), transport, base_url=args.base_url, timeout=120)
    guard = GuardedAdapter(adapter, authorize_dispatch=lambda request: True)
    binding = adapter.register_key(PLACEHOLDER, connection_id="connection-probe")
    started = datetime.now(timezone.utc)
    adapter.prepare("probe-1", MessagesCall((Message("user", (TextPart(PROMPT),)),), max_tokens=args.max_tokens))
    report: dict = {
        "format": "aik-anthropic-messages-probe-v1",
        "probe": "AIK-082 minimal live diagnostic (Anthropic API path only)",
        "status": "FAIL",
        "started_at": started.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "requested_model": args.model,
        "api_version": DEFAULT_API_VERSION,
        "path": MESSAGES_PATH,
        "credential": "injected by egress gateway; adapter x-api-key placeholder removed; no secret handled",
        "not_verified": [
            "direct vendor x-api-key authentication",
            "account binding / fingerprint against the real account",
            "/v1/models listing and check_model",
            "Windows, AGS gate, VM binding, route qualification (AIK-086)",
        ],
    }
    def failure(error: Exception) -> dict:
        # Report the class and fault kind only; messages may echo request data.
        fault = getattr(error, "fault", None)
        return {"error": type(error).__name__, "fault": str(getattr(fault, "kind", fault) or "")[:80]}

    # 1. Product path: GuardedAdapter (AIK-011) requires auth_status (GET /v1/models) first.
    try:
        guard.start_job(JobRequest(binding, "probe-1", "route-probe", "vm-attempt-probe", args.model))
        report["guarded_path"] = {"status": "probe_verified"}
    except Exception as error:
        report["guarded_path"] = {"status": "NOT_RUN_GATEWAY_REFUSED_AUTH_PREFLIGHT", **failure(error),
                                  "note": "fail-closed by design; not bypassed. The gateway allows only /v1/messages."}
    # 2. Wire-format diagnostic: the adapter's own Messages request/decoder, direct.
    adapter.prepare("probe-2", MessagesCall((Message("user", (TextPart(PROMPT),)),), max_tokens=args.max_tokens))
    try:
        receipt = adapter.start_job(JobRequest(binding, "probe-2", "route-probe", "vm-attempt-probe", args.model))
        result = adapter.result("probe-2")
        report["adapter_messages_wire"] = {
            "status": "probe_verified",
            "receipt_state": str(getattr(receipt, "state", None)),
            "provider_job_id_present": bool(result.provider_job_id),
            "observed_model": result.model,
            "observed_model_provenance": "provider_raw_response" if result.model else None,
            "stop_reason": result.stop_reason,
            "completion": str(result.completion.value),
            "usage": result.usage.as_dict(),
            "parts": len(result.parts),
            "result_source": result.source,
        }
    except Exception as error:
        report["adapter_messages_wire"] = {"status": "FAIL", **failure(error)}
    guarded_ok = report["guarded_path"]["status"] == "probe_verified"
    wire_ok = report["adapter_messages_wire"]["status"] == "probe_verified"
    report["status"] = ("probe_verified" if guarded_ok and wire_ok
                        else "partial_probe_verified_wire_only" if wire_ok else "FAIL")
    report["exchanges"] = transport.sent
    report["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if report["status"] != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
