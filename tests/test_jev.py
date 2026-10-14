"""AIK-030 JEV bridge tests with synthetic data and a fake transport only.

No network, provider, credential, VM or AGS installation is used. Linux
results are development verification, not Windows support evidence.
"""
from __future__ import annotations

from contextlib import closing
import dataclasses
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aik_http.transport import (  # noqa: E402
    MAX_RESPONSE_BYTES, HttpResponse, TransportNotSent, TransportTimeout,
)
from aik_jev import (  # noqa: E402
    MAX_RAW_BYTES, AgentSelection, CommonReq, FallbackCandidate, JevBridge, JevClient, JevError,
    JevJournal, RawPrompt, SkillCandidate, select_fallback,
)


SYNTHETIC_TOKEN = "SYNTHETIC-JEV-TOKEN-4d1e9b77"
RAW_MARKER = "RAW-BODY-MARKER-91c3"
RAW_TEXT = f"  테스트 작성 부탁:\n\t\"인용\" \\ 역슬래시 {RAW_MARKER}  \r\n끝 "
SKILLS = (
    SkillCandidate("code-review", "Review a diff", "agent_manifest"),
    SkillCandidate("test-writer", "Write unit tests", "agent_manifest"),
    SkillCandidate("doc-writer", None, "ags_registry"),
)
CANDIDATES = (
    FallbackCandidate("anthropic", "route-cheap-unqualified", "synthetic-tiny", False, 0),
    FallbackCandidate("openai", "route-other-vendor", "synthetic-other", True, 0),
    FallbackCandidate("anthropic", "route-mid", "synthetic-mid", True, 2),
    FallbackCandidate("anthropic", "route-low-a", "synthetic-low-a", True, 1),
    FallbackCandidate("anthropic", "route-low-b", "synthetic-low-b", True, 1),
)
FIXED_NOW = datetime(2026, 10, 13, 1, 2, 3, tzinfo=timezone.utc)


class FakeTransport:
    source = "synthetic_fixture"

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected extra request")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def raw_response(body: bytes, status: int = 200) -> HttpResponse:
    return HttpResponse(status, (("content-type", "application/json"),), body)


def json_response(payload, status: int = 200) -> HttpResponse:
    return raw_response(json.dumps(payload).encode("utf-8"), status)


def success_payload(choice: str = "test-writer", model: str = "jev-2026-09-10") -> dict:
    return {
        "model": model,
        "answers": {"skill": {
            "type": "choice", "choice": choice, "confidence": 0.82,
            "probabilities": {"code-review": 0.1, "test-writer": 0.82, "doc-writer": 0.08},
        }},
        "usage": {"input_tokens": 120, "output_tokens": 3},
    }


class JevTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aik-jev-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.raw = RawPrompt(RAW_TEXT)
        self.req = CommonReq.for_raw(self.raw, work_mode="implementation", target="src/example",
                                     skills=SKILLS, constraints=("no-network",))

    def make_bridge(self, responses=(), *, mode="on", token=None, candidates=(), with_client=True,
                    name="journal.sqlite3"):
        transport = FakeTransport(responses)
        resolver = (lambda: token) if token is not None else None
        client = JevClient(transport, token_resolver=resolver) if with_client else None
        journal = JevJournal(self.root / "jev" / name)
        bridge = JevBridge(journal, client=client, mode=mode, vendor="anthropic",
                           fallback_candidates=candidates, clock=lambda: FIXED_NOW)
        return bridge, transport, journal

    # --- RAW / REQ -----------------------------------------------------------------

    def test_raw_is_sent_verbatim_and_req_carries_only_digest_and_metadata(self) -> None:
        bridge, transport, journal = self.make_bridge([json_response(success_payload())])
        bridge.classify("request-raw", self.raw, self.req)
        self.assertEqual(len(transport.requests), 1)
        request = transport.requests[0]
        self.assertEqual((request.method, request.url), ("POST", "https://api.typesafe.ai/v1/systemone"))
        body = json.loads(request.body.decode("utf-8"))
        self.assertEqual(body["state"]["raw"].encode("utf-8"), RAW_TEXT.encode("utf-8"))
        self.assertEqual(body["state"]["req"], self.req.canonical_dict())
        self.assertNotIn(RAW_MARKER, json.dumps(body["state"]["req"], ensure_ascii=False))
        self.assertNotIn(RAW_MARKER, self.req.canonical_json())
        self.assertEqual(body["state"]["req"]["raw_sha256"], hashlib.sha256(RAW_TEXT.encode("utf-8")).hexdigest())
        question = body["questions"]["skill"]
        self.assertEqual(question["type"], "choice")
        self.assertIn("advisory", question["instructions"])
        self.assertEqual(question["criteria"], {"code-review": "Review a diff", "test-writer": "Write unit tests",
                                                "doc-writer": None})
        self.assertEqual(body["model"], "jev-latest")
        self.assertNotIn(RAW_MARKER.encode("utf-8"), journal.path.read_bytes())

    def test_raw_prompt_and_req_validation(self) -> None:
        self.assertNotIn(RAW_MARKER, repr(self.raw))
        self.assertEqual(self.raw.sha256, hashlib.sha256(RAW_TEXT.encode("utf-8")).hexdigest())
        self.assertEqual(self.raw.byte_length, len(RAW_TEXT.encode("utf-8")))
        with self.assertRaises(JevError):
            RawPrompt("")
        with self.assertRaises(JevError) as too_large:
            RawPrompt("a" * (MAX_RAW_BYTES + 1))
        self.assertEqual(too_large.exception.code, "RAW_TOO_LARGE")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self.raw.text = "changed"
        self.assertEqual(self.req.canonical_json(), json.dumps(
            self.req.canonical_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False))
        self.assertEqual(self.req.sha256, hashlib.sha256(self.req.canonical_json().encode("utf-8")).hexdigest())
        with self.assertRaises(JevError) as duplicate:
            CommonReq.for_raw(self.raw, work_mode="implementation", skills=SKILLS + (SkillCandidate("code-review"),))
        self.assertEqual(duplicate.exception.code, "DUPLICATE_SKILL")
        with self.assertRaises(JevError):
            CommonReq.for_raw(self.raw, work_mode="implementation",
                              skills=tuple(SkillCandidate(f"skill-{n}") for n in range(65)))
        with self.assertRaises(JevError):
            CommonReq.for_raw(self.raw, work_mode="implementation", skills=())
        with self.assertRaises(JevError):
            CommonReq.for_raw(self.raw, work_mode="implementation", skills=SKILLS,
                              metadata_provenance="llm_normalized")

    def test_raw_req_mismatch_is_rejected_without_call_or_record(self) -> None:
        bridge, transport, journal = self.make_bridge([json_response(success_payload())])
        other = CommonReq.for_raw(RawPrompt("different synthetic prompt"), work_mode="implementation", skills=SKILLS)
        with self.assertRaises(JevError) as rejected:
            bridge.classify("request-mismatch", self.raw, other)
        self.assertEqual(rejected.exception.code, "RAW_REQ_MISMATCH")
        self.assertEqual(transport.requests, [])
        self.assertIsNone(journal.find_by_request("request-mismatch"))

    # --- ON ------------------------------------------------------------------------

    def test_on_success_records_provider_observed_fields_and_actual_model(self) -> None:
        bridge, _, _ = self.make_bridge([json_response(success_payload())])
        result = bridge.classify("request-on", self.raw, self.req)
        self.assertEqual((result.status, result.source, result.mode), ("classified", "jev", "on"))
        self.assertEqual(result.requested_model, "jev-latest")
        self.assertEqual(result.observed_model, "jev-2026-09-10")
        self.assertEqual(result.recommended_skill, "test-writer")
        self.assertEqual(result.confidence, 0.82)
        self.assertEqual(result.probabilities,
                         (("code-review", 0.1), ("doc-writer", 0.08), ("test-writer", 0.82)))
        self.assertEqual((result.usage_input_tokens, result.usage_output_tokens), (120, 3))
        self.assertIsNone(result.failure_code)
        self.assertIsNone(result.fallback)
        for name in ("observed_model", "recommended_skill", "confidence", "probabilities",
                     "usage_input_tokens", "usage_output_tokens"):
            self.assertEqual(result.provenance[name], "provider_observed")
        self.assertEqual(result.provenance["status"], "local_derived")
        self.assertEqual(result.provenance["source"], "local_derived")
        stored, selections = bridge.view(result.classification_id)
        self.assertEqual(stored, result)
        self.assertEqual(selections, ())

    def test_same_request_id_returns_stored_record_and_conflicting_content_is_rejected(self) -> None:
        bridge, transport, _ = self.make_bridge([json_response(success_payload())])
        first = bridge.classify("request-repeat", self.raw, self.req)
        second = bridge.classify("request-repeat", self.raw, self.req)
        self.assertEqual(first, second)
        self.assertEqual(len(transport.requests), 1)
        other_raw = RawPrompt("another synthetic prompt")
        other_req = CommonReq.for_raw(other_raw, work_mode="implementation", skills=SKILLS)
        with self.assertRaises(JevError) as conflict:
            bridge.classify("request-repeat", other_raw, other_req)
        self.assertEqual(conflict.exception.code, "REQUEST_CONFLICT")
        self.assertEqual(len(transport.requests), 1)

    # --- OFF / fallback ------------------------------------------------------------

    def test_off_never_calls_transport_and_selects_cheapest_qualified_same_vendor_candidate(self) -> None:
        self.assertEqual(select_fallback(CANDIDATES, "anthropic"), CANDIDATES[3])
        self.assertEqual(select_fallback(CANDIDATES, "openai"), CANDIDATES[1])
        self.assertIsNone(select_fallback(CANDIDATES[:1], "anthropic"))
        bridge, transport, _ = self.make_bridge(mode="off", candidates=CANDIDATES)
        result = bridge.classify("request-off", self.raw, self.req)
        self.assertEqual(transport.requests, [])
        self.assertEqual((result.status, result.source, result.mode), ("off", "fallback_candidate", "off"))
        self.assertEqual(result.fallback, CANDIDATES[3])
        self.assertIsNone(result.recommended_skill)
        self.assertIsNone(result.requested_model)
        self.assertIsNone(result.failure_code)
        self.assertEqual(bridge.view(result.classification_id)[0].fallback, CANDIDATES[3])

    def test_off_without_candidates_and_on_without_client_use_source_none(self) -> None:
        bridge, transport, _ = self.make_bridge(mode="off")
        result = bridge.classify("request-off-none", self.raw, self.req)
        self.assertEqual((result.status, result.source, result.fallback), ("off", "none", None))
        no_client, _, _ = self.make_bridge(mode="on", with_client=False, name="no-client.sqlite3")
        unconfigured = no_client.classify("request-no-client", self.raw, self.req)
        self.assertEqual((unconfigured.status, unconfigured.source), ("off", "none"))
        self.assertEqual(transport.requests, [])

    # --- failures ------------------------------------------------------------------

    def test_http_and_transport_failures_are_recorded_without_retry(self) -> None:
        cases = (
            (json_response({"detail": "synthetic"}, 401), "unavailable", "AUTH_REJECTED"),
            (json_response({"detail": "synthetic"}, 403), "unavailable", "AUTH_REJECTED"),
            (json_response({"detail": "synthetic"}, 404), "unavailable", "NOT_FOUND"),
            (json_response({"detail": [{"loc": ["body"], "msg": "x", "type": "y"}]}, 422), "failed",
             "REQUEST_REJECTED"),
            (json_response({"detail": "synthetic"}, 429), "unavailable", "RATE_LIMITED"),
            (json_response({"detail": "synthetic"}, 500), "unavailable", "SERVICE_ERROR"),
            (json_response({"detail": "synthetic"}, 503), "unavailable", "SERVICE_ERROR"),
            (json_response({"detail": "synthetic"}, 418), "unavailable", "UNEXPECTED_STATUS"),
            (json_response({}, 302), "unavailable", "UNEXPECTED_STATUS"),
            (TransportTimeout(), "unavailable", "TIMEOUT"),
            (TransportNotSent(), "unavailable", "NOT_SENT"),
            (OSError("synthetic socket reset"), "unavailable", "TRANSPORT_ERROR"),
        )
        bridge, transport, _ = self.make_bridge([case[0] for case in cases], candidates=CANDIDATES)
        for index, (_, status, code) in enumerate(cases):
            with self.subTest(code=code, index=index):
                result = bridge.classify(f"request-error-{index}", self.raw, self.req)
                self.assertEqual(len(transport.requests), index + 1)
                self.assertEqual((result.status, result.failure_code), (status, code))
                self.assertEqual((result.source, result.fallback), ("fallback_candidate", CANDIDATES[3]))
                self.assertIsNone(result.recommended_skill)
                self.assertEqual(result.requested_model, "jev-latest")

    def test_response_contract_violations_are_rejected_responses(self) -> None:
        def mutate(change):
            payload = success_payload()
            change(payload)
            return json_response(payload)

        valid = json.dumps(success_payload())
        cases = {
            "answer-key-mismatch": mutate(lambda p: p.update(answers={"other": p["answers"]["skill"]})),
            "extra-answer-key": mutate(lambda p: p["answers"].update(extra=p["answers"]["skill"])),
            "choice-outside-criteria": mutate(lambda p: p["answers"]["skill"].update(choice="unknown-skill")),
            "probability-range": mutate(lambda p: p["answers"]["skill"]["probabilities"].update({"test-writer": 1.5})),
            "probability-key": mutate(lambda p: p["answers"]["skill"]["probabilities"].update({"ghost": 0.1})),
            "confidence-range": mutate(lambda p: p["answers"]["skill"].update(confidence=-0.1)),
            "usage-bool": mutate(lambda p: p["usage"].update(input_tokens=True)),
            "usage-negative": mutate(lambda p: p["usage"].update(output_tokens=-1)),
            "empty-model": mutate(lambda p: p.update(model="")),
            "answer-type": mutate(lambda p: p["answers"]["skill"].update(type="score")),
            "nan-confidence": raw_response(valid.replace('"confidence": 0.82', '"confidence": NaN').encode()),
            "duplicate-key": raw_response(("{" + '"model": "x", ' + valid[1:]).encode()),
            "not-json": raw_response(b"<html>synthetic</html>"),
            "not-object": json_response(["synthetic"]),
        }
        bridge, transport, _ = self.make_bridge(list(cases.values()))
        for index, name in enumerate(cases):
            with self.subTest(case=name):
                result = bridge.classify(f"request-invalid-{index}", self.raw, self.req)
                self.assertEqual((result.status, result.failure_code), ("rejected_response", "INVALID_RESPONSE"))
                self.assertEqual(result.source, "none")
                self.assertIsNone(result.observed_model)
        self.assertEqual(len(transport.requests), len(cases))

    def test_oversized_response_is_rejected(self) -> None:
        bridge, _, _ = self.make_bridge([raw_response(b" " * (MAX_RESPONSE_BYTES + 1))])
        result = bridge.classify("request-oversize", self.raw, self.req)
        self.assertEqual((result.status, result.failure_code), ("rejected_response", "RESPONSE_TOO_LARGE"))

    # --- token secrecy -------------------------------------------------------------

    def test_token_is_sent_only_in_authorization_header_and_never_retained(self) -> None:
        echo = json_response({"detail": "invalid token " + SYNTHETIC_TOKEN}, 401)
        bridge, transport, journal = self.make_bridge([json_response(success_payload()), echo],
                                                      token=SYNTHETIC_TOKEN)
        classified = bridge.classify("request-token-ok", self.raw, self.req)
        failed = bridge.classify("request-token-401", self.raw, self.req)
        self.assertEqual(transport.requests[0].header("authorization"), "Bearer " + SYNTHETIC_TOKEN)
        for request in transport.requests:
            self.assertNotIn(SYNTHETIC_TOKEN, repr(request))
        self.assertEqual(failed.failure_code, "AUTH_REJECTED")
        for value in (classified, failed, bridge):
            self.assertNotIn(SYNTHETIC_TOKEN, repr(value))
        self.assertNotIn(SYNTHETIC_TOKEN.encode("ascii"), journal.path.read_bytes())

        client = JevClient(FakeTransport([echo]), token_resolver=lambda: SYNTHETIC_TOKEN)
        self.assertNotIn(SYNTHETIC_TOKEN, repr(client))
        with self.assertRaises(JevError) as rejected:
            client.list_models()
        self.assertEqual(str(rejected.exception), "AUTH_REJECTED (HTTP 401)")
        self.assertEqual(rejected.exception.status, 401)
        self.assertNotIn(SYNTHETIC_TOKEN, repr(rejected.exception))

        def failing_resolver():
            raise RuntimeError(SYNTHETIC_TOKEN)

        unavailable_transport = FakeTransport()
        failing = JevClient(unavailable_transport, token_resolver=failing_resolver)
        with self.assertRaises(JevError) as unavailable:
            failing.list_models()
        self.assertEqual(unavailable.exception.code, "TOKEN_UNAVAILABLE")
        self.assertIsNone(unavailable.exception.__context__)
        self.assertNotIn(SYNTHETIC_TOKEN, str(unavailable.exception))
        self.assertEqual(unavailable_transport.requests, [])

    def test_without_token_resolver_no_authorization_header_is_sent(self) -> None:
        bridge, transport, _ = self.make_bridge([json_response(success_payload())])
        bridge.classify("request-no-token", self.raw, self.req)
        request = transport.requests[0]
        self.assertIsNone(request.header("authorization"))
        self.assertEqual(request.header("user-agent"), "aik-jev/0.1 (AI-Kubernetes)")
        self.assertEqual(request.header("accept"), "application/json")
        self.assertEqual(request.header("content-type"), "application/json")

    # --- AGENT selection -----------------------------------------------------------

    def test_agent_selection_is_recorded_separately_and_never_overridden(self) -> None:
        bridge, _, _ = self.make_bridge([json_response(success_payload())])
        classification = bridge.classify("request-select", self.raw, self.req)
        same = bridge.record_agent_selection(classification.classification_id,
                                             AgentSelection("agent-1", "test-writer"))
        different = bridge.record_agent_selection(classification.classification_id,
                                                  AgentSelection("agent-1", "code-review"))
        outside = bridge.record_agent_selection(classification.classification_id,
                                                AgentSelection("agent-2", "unlisted-skill"))
        no_skill = bridge.record_agent_selection(classification.classification_id,
                                                 AgentSelection("agent-3", None))
        self.assertEqual((same.matches_recommendation, same.outside_candidates), (True, False))
        self.assertEqual((different.matches_recommendation, different.outside_candidates), (False, False))
        self.assertEqual(different.selected_skill_id, "code-review")
        self.assertEqual((outside.matches_recommendation, outside.outside_candidates), (False, True))
        self.assertEqual(outside.selected_skill_id, "unlisted-skill")
        self.assertEqual((no_skill.matches_recommendation, no_skill.outside_candidates, no_skill.selected_skill_id),
                         (False, False, None))
        for record in (same, different, outside, no_skill):
            self.assertEqual(record.provenance, "model_reported")
        stored, selections = bridge.view(classification.classification_id)
        self.assertEqual(stored, classification)
        self.assertEqual(stored.recommended_skill, "test-writer")
        self.assertEqual(selections, (same, different, outside, no_skill))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            classification.recommended_skill = "code-review"
        with self.assertRaises(JevError):
            AgentSelection("agent-1", "test-writer", provenance="jev")
        with self.assertRaises(JevError) as unknown:
            bridge.record_agent_selection("jevc-missing", AgentSelection("agent-1", "test-writer"))
        self.assertEqual(unknown.exception.code, "UNKNOWN_CLASSIFICATION")

    def test_selection_without_recommendation_has_null_match(self) -> None:
        bridge, _, _ = self.make_bridge(mode="off")
        classification = bridge.classify("request-off-select", self.raw, self.req)
        record = bridge.record_agent_selection(classification.classification_id,
                                               AgentSelection("agent-1", "doc-writer"))
        self.assertIsNone(record.matches_recommendation)
        self.assertFalse(record.outside_candidates)
        self.assertEqual(bridge.view(classification.classification_id)[1], (record,))

    # --- journal -------------------------------------------------------------------

    def test_journal_rejects_update_and_delete(self) -> None:
        bridge, _, journal = self.make_bridge(mode="off")
        classification = bridge.classify("request-append-only", self.raw, self.req)
        bridge.record_agent_selection(classification.classification_id, AgentSelection("agent-1", None))
        statements = (
            "UPDATE classifications SET status='classified'",
            "DELETE FROM classifications",
            "UPDATE agent_selections SET agent_id='agent-forged'",
            "DELETE FROM agent_selections",
        )
        for statement in statements:
            with self.subTest(statement=statement):
                with closing(sqlite3.connect(journal.path)) as connection:
                    with self.assertRaises(sqlite3.DatabaseError):
                        connection.execute(statement)
        self.assertEqual(bridge.view(classification.classification_id)[0], classification)

    def test_foreign_or_damaged_files_are_rejected_as_journal(self) -> None:
        foreign = self.root / "foreign.sqlite3"
        with closing(sqlite3.connect(foreign)) as connection:
            connection.execute("CREATE TABLE unrelated (payload TEXT)")
            connection.execute("PRAGMA application_id = 1")
            connection.commit()
        same_id = self.root / "same-id.sqlite3"
        with closing(sqlite3.connect(same_id)) as connection:
            connection.execute("CREATE TABLE unrelated (payload TEXT)")
            connection.execute(f"PRAGMA application_id = {0x41494B4A}")
            connection.execute("PRAGMA user_version = 1")
            connection.commit()
        text = self.root / "notes.sqlite3"
        text.write_bytes(b"synthetic text file, not a database")
        for path in (foreign, same_id, text):
            with self.subTest(path=path.name):
                before = path.read_bytes()
                with self.assertRaises(JevError) as rejected:
                    JevJournal(path)
                self.assertEqual(rejected.exception.code, "INVALID_JOURNAL")
                self.assertEqual(path.read_bytes(), before)

    # --- client --------------------------------------------------------------------

    def test_list_models_accepts_iso_datetime_release_date_verbatim(self) -> None:
        payload = {"models": [
            {"name": "jev-latest", "description": "synthetic", "release_date": "2026-09-10T18:38:01.391457+00:00"},
            {"name": "jev-preview", "description": "synthetic", "release_date": "2026-09-01"},
        ]}
        transport = FakeTransport([json_response(payload)])
        models = JevClient(transport).list_models()
        self.assertEqual([model.name for model in models], ["jev-latest", "jev-preview"])
        self.assertEqual(models[0].release_date, "2026-09-10T18:38:01.391457+00:00")
        self.assertEqual(models[1].release_date, "2026-09-01")
        self.assertEqual((transport.requests[0].method, transport.requests[0].url),
                         ("GET", "https://api.typesafe.ai/v1/models"))
        self.assertIsNone(transport.requests[0].body)

    def test_base_url_must_be_plain_https_origin(self) -> None:
        for url in ("http://api.typesafe.ai", "https://api.typesafe.ai/v1", "https://api.typesafe.ai?x=1",
                    "https://user:pw@api.typesafe.ai", "https://", "https://api.typesafe.ai#f", ""):
            with self.subTest(url=url):
                with self.assertRaises(JevError) as rejected:
                    JevClient(FakeTransport(), base_url=url)
                self.assertEqual(rejected.exception.code, "INVALID_BASE_URL")
        self.assertEqual(JevClient(FakeTransport(), base_url="https://jev.example.test/").base_url,
                         "https://jev.example.test")


if __name__ == "__main__":
    unittest.main()
