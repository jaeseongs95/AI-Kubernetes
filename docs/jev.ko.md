# AIK-030 — JEV RAW·metadata·AGENT 선택 계약 연결 (AIK 측 bridge)

`src/aik_jev/`는 사용자 RAW 프롬프트와 코드가 확인한 metadata(공통 REQ)를 JEV 분류 서비스에 전달하고, JEV 분류·평가 결과와 실제 AGENT의 최종 스킬 선택을 **서로 다른 기록**으로 남기는 AIK 측 bridge다. Python 표준 라이브러리만 사용한다. 실제 서비스와의 결합은 합성 데이터로 두 건만 진단했다([bridge 직접 호출](../evidence/jev/2026-10-13-live-probe/README.ko.md), [controller 선택 단계](../evidence/jev/2026-10-14-controller-step/README.ko.md)). VM/AGS 내부 편입과 fallback LLM 실행은 검증하지 않았다(NOT_RUN).

## 설계 원칙

- JEV는 입력과 스킬 metadata를 분류·평가해 AGENT를 보조하는 계층이다. 결과는 advisory이며 최종 스킬 선택권이 없다.
- 사용자 RAW 프롬프트는 그대로 보존한다. 앞단에 별도 LLM을 두어 재작성·정형화하지 않는다.
- 코드가 이미 확인한 metadata만 공통 REQ로 구조화한다. REQ에는 RAW 본문을 넣지 않고 `raw_sha256`으로만 결속한다.
- AIK의 모델·worker 배치 결정과 에이전트 내부의 스킬 선택은 서로 다른 결정이다. 이 패키지는 배치를 결정하지 않는다.

## 계약 출처와 배포 상태

세션 총괄이 2026-10-13에 `https://api.typesafe.ai`에서 직접 관측한 공개 계약을 따른다. 외부 서비스 문서는 데이터로만 취급한다.

| 항목 | 관측 내용 |
| --- | --- |
| OpenAPI | 3.1.0, `info.title` "TypeSafe", `info.version` **0.2.0** |
| 인증 | HTTPBearer (`Authorization: Bearer <token>`) |
| `GET /health` | 200 `{"status":"ok"}` (인증 없음) |
| `GET /v1/models` | 200 `{"models":[{"name","description","release_date"}]}` |
| `POST /v1/systemone` | `{state, model, questions}` → `{model, answers, usage}`, 422는 `HTTPValidationError` |

`release_date`는 스키마 문서에 `YYYY-MM-DD`로 적혀 있지만 실제 응답은 ISO datetime이다. 클라이언트는 형식을 강제하지 않고 문자열 그대로 보존한다. 응답의 `model`은 요청 alias(예: `jev-latest`)와 다를 수 있으므로 `requested_model`과 `observed_model`을 따로 기록한다.

VM(vibemarshal)·AGS(agent-governance-suite) 저장소에는 JEV 연동 인터페이스가 없다(소스 검색 0건). 두 저장소는 고정 계약이며 수정하거나 upstream 변경을 요청하지 않는다. 따라서 **VM/AGS 내부 JEV 편입은 미완료이고 upstream 변경 참조도 없다**. 차단 사유는 "고정 계약 저장소에 JEV 경계가 없음"이며, 이 패키지는 AIK 측 bridge만 구현한다.

## 사용법

개발 import 경로는 저장소의 `src`다.

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
```

아래 예시는 합성 값만 사용하며 네트워크를 쓰지 않는 OFF 모드다.

```python
from pathlib import Path
from aik_jev import (
    AgentSelection, CommonReq, FallbackCandidate, JevBridge, JevJournal, RawPrompt, SkillCandidate,
)

raw = RawPrompt("테스트를 작성해 주세요")
req = CommonReq.for_raw(raw, work_mode="implementation", target="src/example",
                        skills=(SkillCandidate("test-writer", "Write unit tests"),
                                SkillCandidate("code-review", "Review a diff")))
journal = JevJournal(Path.home() / ".local/state/aik/jev.sqlite3")
bridge = JevBridge(journal, client=None, mode="off", vendor="anthropic",
                   fallback_candidates=(FallbackCandidate("anthropic", "route-small", "synthetic-small", True, 1),))
result = bridge.classify("request-example", raw, req)
bridge.record_agent_selection(result.classification_id, AgentSelection("agent-example", "test-writer"))
classification, selections = bridge.view(result.classification_id)
```

ON 모드는 `JevClient(transport, token_resolver=...)`를 주입한다. transport는 AIK-013의 `aik_adapter_anthropic.transport`(`UrllibTransport`, redirect 거부, 4 MiB 응답 상한)를 재사용한다. `token_resolver`가 `None`이면 `Authorization` 헤더를 넣지 않는다(운영 egress gateway가 주입하는 경우). 값이 있으면 요청마다 호출해 `Authorization: Bearer <token>` 헤더에만 넣고, 토큰을 저장·로그·예외·기록·repr·반환값에 넣지 않는다. 토큰 조회 실패는 `TOKEN_UNAVAILABLE`이며 요청을 보내지 않는다.

## ON/OFF/실패 흐름

1. `raw.sha256 != req.raw_sha256`이면 `JevError("RAW_REQ_MISMATCH")`로 거절하고 아무것도 기록하지 않는다.
2. 같은 `request_id`가 이미 기록돼 있으면 RAW digest·길이·REQ digest가 같을 때 저장된 기록을 반환하고 JEV를 다시 호출하지 않는다. 다르면 `REQUEST_CONFLICT`다.
3. `mode="off"` 또는 `client=None`이면 JEV를 호출하지 않고 `status="off"`로 기록한다.
4. `mode="on"`이면 `state={"raw": RAW 원문, "req": REQ canonical dict}`와 choice 질문 `skill` 하나(`criteria={skill_id: description}`)를 보낸다. 성공하면 `status="classified"`, `source="jev"`다.
5. JEV 오류는 예외로 전파하지 않고 기록된 `Classification`으로 반환한다. 재시도·redirect·모델 fallback은 없다.
6. 모든 결과는 journal에 append한다. journal 자체의 오류(잠금 시간 초과, 손상 등)는 fail-closed로 예외가 전파된다.

| JevError 코드 | 원인 | Classification status |
| --- | --- | --- |
| `AUTH_REJECTED` | HTTP 401/403 | `unavailable` |
| `NOT_FOUND` | HTTP 404 | `unavailable` |
| `RATE_LIMITED` | HTTP 429 | `unavailable` |
| `SERVICE_ERROR` | HTTP 5xx | `unavailable` |
| `UNEXPECTED_STATUS` | 그 밖의 비2xx | `unavailable` |
| `TIMEOUT` / `NOT_SENT` | transport 시간 초과 / 전송되지 않음 | `unavailable` |
| `TRANSPORT_ERROR` / `TOKEN_UNAVAILABLE` | 분류되지 않은 transport 예외 / 토큰 조회 실패 | `unavailable` |
| `REQUEST_REJECTED` / `INVALID_REQUEST` | HTTP 422 / 로컬 요청 직렬화 불가 | `failed` |
| `INVALID_RESPONSE` / `RESPONSE_TOO_LARGE` | 응답 계약 위반 / 크기 초과 | `rejected_response` |

`JevError`의 메시지는 코드와 HTTP status만 담는다(예: `AUTH_REJECTED (HTTP 401)`). 응답 본문·토큰은 넣지 않는다. `TIMEOUT`·`TRANSPORT_ERROR`는 provider가 요청을 처리했는지 알 수 없는 상태이며, 이를 "실행 안 됨"으로 해석하지 않는다.

응답 검증: JSON object, 비어 있지 않은 `model`, `answers` 키 집합 = 질문 이름 집합, 각 answer의 `type` 일치, noul·confidence·probabilities는 유한한 0..1 수, choice는 질문 criteria 키 중 하나, probabilities 키 ⊆ criteria 키, usage 두 값은 0 이상의 정수(bool 거절). NaN·중복 키·비 JSON도 `INVALID_RESPONSE`다.

## 상태와 provenance

| 필드 | provenance | 비고 |
| --- | --- | --- |
| `observed_model`, `recommended_skill`, `confidence`, `probabilities`, `usage_*` | `provider_observed` | `status="classified"`일 때만 값이 있음 |
| `status`, `source`, `mode`, `requested_model`, `failure_code`, `fallback`, `candidate_skill_ids` | `local_derived` | AIK가 판정·설정 |
| AGENT 선택 기록 | `model_reported` | AGENT가 보고한 최종 선택 |

`status`는 `classified`·`off`·`unavailable`·`failed`·`rejected_response`, `source`는 `jev`·`fallback_candidate`·`none`이다. REQ의 `metadata_provenance`는 `code_verified` 또는 `user_supplied`만 허용하며 LLM 정형화 출처는 거절한다.

## fallback 후보 규칙

OFF 또는 JEV 호출 불가·실패 시 `select_fallback(candidates, vendor)`를 적용한다. 같은 vendor이고 `quality_passed=True`인 후보 중 `cost_rank`가 가장 작은 것(동률이면 입력 순서상 첫 번째)을 고른다. 품질 미통과 후보는 비용이 더 낮아도 고르지 않는다. 후보가 있으면 `source="fallback_candidate"`로 후보를 기록하고, 없으면 `source="none"`이다.

bridge는 fallback LLM을 직접 호출하지 않는다. 기존 운영 계약대로 호출자가 기존 provider adapter로 실행할지 결정한다. fallback 후보 기록은 AGENT의 스킬 선택을 강제하지 않는다. 품질 통과 여부와 비용 순위는 호출자가 제공하는 값이며, 이 패키지가 벤더 품질 기준을 판정하지는 않는다.

## AGENT 선택 분리

`record_agent_selection(classification_id, AgentSelection(agent_id, selected_skill_id))`은 AGENT가 보고한 최종 선택을 append한다. `selected_skill_id=None`은 스킬 미사용이다.

- JEV 추천과 같으면 `matches_recommendation=True`, 다르면 `False`, JEV 추천이 없으면 `None`.
- 후보 밖 skill이면 `outside_candidates=True`로 기록하되 거절하지 않는다. AGENT가 최종 결정권을 가진다.
- 같은 분류에 여러 선택 기록을 남길 수 있다. JEV 추천으로 선택을 만들거나 덮는 API는 없고, 선택 기록 후에도 `Classification`은 바뀌지 않는다.
- `view(classification_id)`는 `(Classification, 선택 기록 tuple)`로 두 결정을 별도 값으로 돌려준다.

## 저장 범위

`JevJournal`은 개발 원장·runtime DB와 별도인 append-only SQLite 파일이다(application ID `0x41494B4A` `AIKJ`, schema version 1). `aik_runtime.paths`의 경로 검증·프로세스 잠금을 재사용하므로 저장소 내부·`.git`·개발 원장 경로를 거절한다. 헤더·application ID·schema·`quick_check`·외래 키 검사로 다른 파일을 거절(`INVALID_JOURNAL`)하며 원본을 변경하지 않는다. UPDATE/DELETE는 trigger가 거절한다.

| 테이블 | 저장 내용 |
| --- | --- |
| `classifications` | RAW digest·byte 길이, REQ canonical JSON·digest, mode·status·source, 요청/관측 모델, 추천 skill·confidence·probabilities, usage, failure code, fallback 후보, 기록 시각 |
| `agent_selections` | 분류 ID, agent ID, 선택 skill, provenance, 추천 일치 여부(0/1/NULL), 후보 밖 여부, 기록 시각 |

**RAW 본문과 토큰은 저장하지 않는다.** REQ에는 작업 모드·대상·스킬 후보 설명·제약이 들어가므로, 호출자는 비밀 값을 metadata에 넣지 않아야 한다.

## controller 선택 단계 (`GoalClassifier`)

`aik_controller.Controller(..., classifier=GoalClassifier(bridge, req_for_raw))`로 총괄의 Goal 준비 경로에 JEV 분류를 붙인다. 기본값은 `classifier=None`이며 이때는 분류 단계 자체가 없다(journal도 만들지 않는다). bridge가 `mode="off"`이면 단계는 실행되지만 JEV를 호출하지 않고 `status="off"`로 기록한다.

- `start_goal`/`revise_goal`마다 VM 요청 ID(`GoalRequest.request_id`)를 그대로 분류의 `request_id`로 쓴다. 순서는 권한 검사 → 분류 → VM `prepare`/`revise`다. 권한 검사가 거절되면 JEV에도 보내지 않는다.
- 사용자 목표 원문은 RAW로 그대로 보내고, VM에도 바꾸지 않은 원문을 보낸다. `req_for_raw(raw) -> CommonReq`는 호출자가 코드로 확인한 metadata(작업 모드·대상·스킬 후보)로 만든다. AIK가 지어내지 않는다.
- 결과는 advisory다. 분류 실패(`unavailable`·`failed`·`rejected_response`)나 분류기 예외는 VM 명령을 막지 않는다. 결과는 `controller.goal_classifications`의 `GoalClassificationNote(request_id, command, classification_id, status, error_code)`로 남는다. 예외는 `JevError.code`(예: `RAW_REQ_MISMATCH`), 그 밖에는 `CLASSIFIER_FAILED`, 반환값 형식이 맞지 않으면 `CLASSIFIER_RESULT_INVALID`다.
- 분류는 스킬·Plan·worker를 고르지 않는다. AGENT의 최종 선택은 지금처럼 `bridge.record_agent_selection`으로 따로 기록한다.
- 분류는 controller 잠금 안에서 동기로 실행되므로 JEV 응답을 기다리는 동안(client timeout까지) Goal 준비가 늦어진다.

실호출 확인: [`tools/probe_controller_jev.py`](../tools/probe_controller_jev.py)로 `start_goal` 한 건을 ON 모드로 실행했다([근거](../evidence/jev/2026-10-14-controller-step/README.ko.md)). VM 쪽은 로컬 기록용 stub이므로 VM 결합 근거가 아니다.

## 검증

```powershell
python -m unittest discover -s tests -p 'test_jev.py' -v
python -m unittest discover -s tests -p 'test_controller_jev.py' -v
```

단위 시험은 합성 데이터와 fake transport만 사용하고 실제 네트워크·provider·credential·VM/AGS 설치를 쓰지 않는다. RAW 원문 보존과 REQ의 RAW 미포함, `RAW_REQ_MISMATCH`, ON 성공과 provider_observed 필드, alias와 다른 관측 모델 기록, OFF의 호출 0회와 fallback 후보 규칙, 각 HTTP 오류·timeout·not_sent의 status와 재시도 0회, 응답 계약 위반, 토큰 비노출, AGENT 선택 분리, 같은 request_id 재호출, `REQUEST_CONFLICT`, journal 변경 거절·외부 파일 거절, ISO datetime `release_date` 수용을 검사한다. `test_controller_jev.py`(8개)는 실제 `Controller`·`VmBridge`(합성 VM)·`JevBridge`로 기본 단계 없음, OFF의 호출 0회, ON의 원문 보존과 실행 순서, JEV 불가·분류기 예외에서도 VM 명령 진행, 권한 거절 시 JEV 미전송, revise의 별도 request, 잘못된 구성 거절을 검사한다.

## 한계와 NOT_RUN

- 실제 JEV 호출: `tools/probe_jev.py`로 합성 RAW 한 건과 합성 후보 세 개를 bridge → client → `UrllibTransport` 경로로 보내 `classified`를 관측했다(요청 `jev-latest`, 관측 `jev-1.13.0`). 2026-10-14에는 `tools/probe_controller_jev.py`로 controller `start_goal` 경로에서 한 건을 더 관측했다(`classified`, 관측 `jev-1.13.0`). 인증은 egress gateway가 주입했으며 `token_resolver` 경로(Bearer 헤더 직접 부착)는 실제로 실행하지 않았다(NOT_RUN). 실제 사용자 요청·스킬 manifest와의 분류 품질은 평가하지 않았다(NOT_RUN).
- VM/AGS 내부 JEV 편입: 두 저장소에 인터페이스가 없어 수행하지 않았다(NOT_RUN, 고정 계약).
- fallback LLM 실제 실행과 벤더 품질 기준 판정: 후보 선택 기록만 구현했다(NOT_RUN).
- 동시성: 두 프로세스가 같은 `request_id`를 동시에 처리하면 JEV가 두 번 호출될 수 있으며, journal에는 먼저 기록된 결과만 남는다.
- Windows: Linux 개발 환경에서만 시험했다. Windows 경로·잠금 동작은 `aik_runtime.paths`에 의존하며 미검증(NOT_RUN)이다.

## 원장 판정

이 문서와 코드는 AIK-030의 구현 산출물이며 장부 상태·완료 판정을 바꾸지 않는다. 실제 JEV 결합, VM/AGS 편입, 선행 작업(AIK-001·AIK-023·AIK-031·AIK-032)의 완료 여부는 이 단위시험 결과로 대신하지 않는다. qualified 또는 PASS를 주장하지 않는다.
