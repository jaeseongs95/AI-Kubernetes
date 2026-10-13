# AIK-017 — 출력 시크릿 제거와 자원 제한

## 책임과 범위

`src/aik_security/redaction.py`는 신뢰할 수 있는 backend/broker에서 출력 전에 호출하는 정제 모듈이다. 등록된 비밀 원문과 지원되는 표현, 인증 헤더·URL·민감 필드를 제거한다. 실행에 전달하는 사용자 RAW는 변경하지 않는다. 실제 vault·adapter·제품 전체 출력 호출부의 연결은 아직 미완료이며, 본 모듈의 테스트를 전체 제품 유출 방지 보증으로 사용하지 않는다.

## 겹치는 비밀과 진단 문자열

각 규칙이 원문에서 찾은 구간을 먼저 모두 수집하고 겹치는 구간의 합집합을 한 번만 치환한다. 앞선 치환이 다른 비밀의 일부를 없애 나머지 꼬리를 노출하던 결함을 수정했다. 등록 순서가 결과에 영향을 주지 않으며 생성된 마스킹 문자열을 다시 비밀값으로 해석하지 않는다.

알려진 비밀은 원문, URL 인코딩(퍼센트 escape 대소문자 혼합), base64(패딩 유무), hex와 다양성이 있는 연속 12글자 조각을 검사한다. 모든 임의 인코딩·분할·알 수 없는 비밀을 탐지한다는 뜻은 아니다. `api_key="..."`와 JSON 진단 문자열의 quoted value는 공백·escaped quote·줄바꿈을 포함해 처리한다. 닫히지 않은 민감 quoted value는 남은 문자열을 보수적으로 제거한다. `userAPIKey` 같은 약어가 섞인 필드명도 분리해서 판정한다. `redact_text`는 진단 문자열 정제기이지 JSON을 다시 파싱할 수 있게 보존하는 serializer가 아니다.

## 실패·등록·자원 제한

잘못된 유니코드와 과도한 입력은 원문/예외를 출력하는 대신 `[OMITTED]`로 반환한다. 잘못된 유니코드의 비밀 등록은 고정 `INVALID_SECRET_REGISTRY`로 거절하며 기존 레지스트리를 바꾸지 않는다. 등록은 12~256글자, 최대 16개이며 동일 비밀의 재등록은 용량에 도달해도 멱등이다. 한 호출 동안 일관된 레지스트리를 사용한다.

단일 텍스트와 누적 처리 텍스트는 64KiB, 방문 노드(키 포함)는 512개, 깊이는 8, 컨테이너 항목은 128개다. 알려진 값의 탐색 작업량은 16,777,216의 길이×패턴 수 예산, 단일 문자열의 일치 구간 수는 4096개로 제한한다. 전체 노드/텍스트/탐색 예산을 넘기면 부분 결과를 성공처럼 반환하지 않고 전체를 생략한다. 깊이/항목 제한의 기존 생략 표시는 유지한다.

`output`은 **외부 envelope까지 포함한** compact UTF-8 JSON(`ensure_ascii=False`, 구분자 `,`/`:`)이 65,536 bytes를 넘지 않도록 검사한다. 다르게 pretty-print하거나 ASCII escape로 재직렬화한 크기를 보장하지 않는다. `_unsafe_field_omitted` 등 예약된 생략 metadata를 호출자가 덮어쓸 수 없게 했다. 지원하지 않는 객체의 `str`·`repr`·iteration은 실행하지 않는다.

## API

`output(kind, payload)`의 kind는 `prompt_preview`, `ipc_response`, `peer`, `evidence`, `log`, `crash_bundle`, `export`다. 유효하고 제한 내인 프롬프트 미리보기는 마스킹·UTF-8 byte 길이·SHA-256만 반환한다. 잘못되거나 너무 큰 프롬프트는 hash를 만들어 실측처럼 표시하지 않고 body를 생략한다.

`sanitized_error(code, request_id=..., connection_id=..., task_id=..., exception=...)`는 고정 오류 문구와 안전한 추적 ID만 남기고 원시 exception을 무시한다. 정제 객체의 내부 레지스트리 자체를 로그/직렬화해서는 안 된다. `[OMITTED]`는 누락 신호이며 처리한 원문의 안전성을 인증하는 PASS가 아니다.

## 검증과 원장

```bash
python -m unittest discover -s tests -p "test_security_redaction*.py" -v
python tools/backlog.py validate
```

기존 17개 시험을 수정하지 않고 21개 경계 회귀를 추가했다. 수정 전 8개 선택 반례는 실패 7개·오류 1개였으며, 수정 후 38개 모두 통과했다. 로컬 실행과 Windows/Linux CI의 판본·소스 해시는 [근거 기록](../evidence/secret-redaction/2026-10-13-boundaries/README.ko.md)에 분리한다.

정본 `planning/backlog.sqlite3`에는 AIK-017의 증거와 상태 이력만 추가한다. 선행 AIK-004·009·011 및 전체 제품 출력 경로 연결이 남아 있으므로 상태는 `blocked`다. 다른 태스크의 정의·상태·완료 기준·의존성은 보존한다.
