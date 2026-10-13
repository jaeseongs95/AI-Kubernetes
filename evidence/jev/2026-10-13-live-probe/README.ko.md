# AIK-030 JEV bridge 실제 서비스 최소 진단 — 2026-10-13

**결과: `partial_probe_verified_wire_only`.** AIK-030의 실제 JEV 인터페이스 확인 근거 중 일부다. 분류 품질 평가나 qualification이 아니며, AIK-030을 완료로 처리하지 않는다.

## 실행

- 도구: [`tools/probe_jev.py`](../../../tools/probe_jev.py)
- 원본 보고서: [`probe-report.json`](probe-report.json)
- 환경: Linux 원격 세션, Python 3.13. egress gateway가 `api.typesafe.ai`에 인증을 주입한다. 도구는 토큰을 읽거나 헤더에 넣지 않는다.
- 입력: 합성 RAW 한 문장(한국어, 83 bytes), 합성 스킬 후보 세 개(`write-tests`·`refactor`·`write-docs`), 요청 모델 `jev-latest`.
- 경로: `JevBridge.classify` → `JevClient.evaluate` → `UrllibTransport` → `POST /v1/systemone`. journal은 임시 디렉터리에 쓰고 삭제했다.
- 같은 입력으로 사전 확인 호출 한 번을 더 했다. 결과는 동일했다.

## 관측

| 항목 | 값 | 출처 |
|---|---|---|
| 공개 계약 | OpenAPI 3.1.0, TypeSafe **0.2.0**, `GET /v1/models`·`POST /v1/systemone` | 서비스 `/openapi.json` |
| 모델 목록 | `jev-latest`, `jev-preview` (`release_date`는 ISO datetime으로 반환되어 스키마 문서의 `YYYY-MM-DD`와 다르다) | `GET /v1/models` |
| 분류 상태 | `classified`, source `jev` | local_derived |
| 요청/관측 모델 | `jev-latest` / `jev-1.13.0` (alias와 실제 모델이 다르다) | provider_observed |
| 추천 | `write-tests`, confidence 1.0, 확률 write-tests 1.0·refactor 0.0·write-docs 0.0 | provider_observed |
| usage | input 606, output 43 | provider_observed |
| AGENT 선택 | 합성 AGENT가 일부러 `refactor`를 선택했다. 기록 결과는 `matches_recommendation=false`, `outside_candidates=false`, provenance `model_reported`이고 JEV 분류 기록은 바뀌지 않았다. | model_reported / local_derived |

응답은 클라이언트의 엄격 검증을 통과했다. 검증 항목은 답변 키와 질문 이름 일치, choice가 criteria 안에 있음, 확률 0..1, usage 정수다.

## 검증하지 않은 것 (NOT_RUN)

- `token_resolver`로 Bearer 헤더를 직접 붙이는 경로와 vault 연결
- 실제 사용자 요청과 실제 스킬 manifest에 대한 분류 품질
- VM/AGS 내부 JEV 편입: 두 고정 계약 저장소에 JEV 경계가 없다
- OFF/호출 불가 시 fallback 분류 LLM의 실제 실행과 벤더 품질 판정
- Windows
