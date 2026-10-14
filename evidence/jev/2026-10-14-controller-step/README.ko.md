# AIK-030 controller 선택 단계 JEV 실호출 — 2026-10-14

**결과: `partial_probe_verified_wire_only`.** controller의 선택형 JEV 단계(`Controller(classifier=GoalClassifier(...))`)가 실제 JEV 서비스까지 이어지는지 한 건으로 확인했다. 분류 품질 평가나 qualification이 아니며, AIK-030을 완료로 처리하지 않는다.

## 실행

- 도구: [`tools/probe_controller_jev.py`](../../../tools/probe_controller_jev.py)
- 원본 보고서: [`probe-report.json`](probe-report.json)
- 환경: Linux 원격 세션, Python 3.13. egress gateway가 `api.typesafe.ai`에 인증을 주입한다. 도구는 토큰을 읽거나 헤더에 넣지 않는다.
- 경로: `Controller.start_goal` → 권한 검사(허용 stub) → `GoalClassifier` → `JevBridge.classify` → `JevClient.evaluate` → `UrllibTransport` → `POST /v1/systemone` → VM `prepare`(로컬 기록 stub).
- 입력: 합성 목표 한 문장(한국어), 합성 스킬 후보 세 개(`write-tests`·`refactor`·`write-docs`), 요청 모델 `jev-latest`. journal은 임시 디렉터리에 쓰고 삭제했다.
- JEV 호출은 이 한 건이다.

## 관측

| 항목 | 값 | 출처 |
|---|---|---|
| 실행 순서 | `authority.prepare` → `classify` → `vm.prepare` | local_derived |
| 원문 보존 | 목표 원문 sha256 = 분류 `raw_sha256` = VM이 받은 원문 sha256 (`37fa3604…9243`) | local_derived |
| 분류 상태 | `classified`, source `jev`, request ID `probe-ctl-000001`(VM 요청 ID와 같음) | local_derived |
| 요청/관측 모델 | `jev-latest` / `jev-1.13.0` | provider_observed |
| 추천 | `write-docs`, confidence 1.0, 확률 write-docs 1.0·refactor 0.0·write-tests 0.0 | provider_observed |
| usage | input 608, output 44 | provider_observed |
| AGENT 선택 | 합성 AGENT가 `write-docs`를 보고했다. 별도 기록이며 `matches_recommendation=true`, provenance `model_reported` | model_reported / local_derived |

## 검증하지 않은 것 (NOT_RUN)

- 실제 VM `prepare`: VM 쪽은 받은 원문만 기록하는 로컬 stub이다.
- 실제 `AuthorityGateway` 세션: 권한 검사는 모두 허용하는 stub이다.
- 실제 사용자 요청과 실제 스킬 manifest에 대한 분류 품질
- `token_resolver` 경로, fallback 분류 LLM 실행과 벤더 품질 판정
- VM/AGS 내부 JEV 편입(고정 계약 저장소에 JEV 경계 없음)
- Windows
