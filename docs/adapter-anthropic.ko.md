# AIK-013 — Anthropic API(Messages API) 연결 adapter (구현·fixture 검증)

상태: **adapter 코드 구현됨(`implementation_state=implemented`) — route qualification `NOT_RUN`, 실제 vendor 왕복 `NOT_RUN`.** 이 문서는 `src/aik_adapter_anthropic/`의 설계와 한계를 설명한다. 장부(`planning/`)의 AIK-013 상태와 완료 판정은 이 문서에서 바꾸지 않는다. 선행 작업 AIK-009·AIK-011의 장부 상태도 변경하지 않는다.

## 1. 범위

공식 API 키 연결을 AIK-011 공통 계약(`aik_provider.ProviderAdapter`)과 공통 모델 turn 계약으로 변환한다. 실제 계정의 지원 여부는 이 코드가 판정하지 않는다.

| 구분 | 이 태스크에서 한 것 | 판정 위치 |
|---|---|---|
| adapter 코드 경로 | 키 등록·인증 확인·모델 호출·교체·해제, 도구 요청/결과 변환, 오류 매핑 | 이 패키지 |
| protocol fixture | 합성 fake transport·fake vault로 성공/실패 fixture 검증 | `tests/test_adapter_anthropic.py` |
| 실제 인증 | **NOT_RUN** | AIK-022 |
| runtime/도구 실행 | **NOT_RUN** | AIK-082 사전 probe, AIK-086/044 |

## 2. 구성

| 파일 | 역할 |
|---|---|
| `transport.py` | 주입형 `Transport` 프로토콜(`source`, `send`), `HttpRequest`/`HttpResponse`, `TransportTimeout`/`TransportNotSent`, 표준 라이브러리 `UrllibTransport`(실사용 **NOT_RUN**) |
| `messages.py` | vendor 중립 계약: `TextPart`, `ToolCall`, `ToolResult`, `Message`, `ToolSpec`, `MessagesCall`, `Usage`, `Completion`, `ModelResult`, `assistant_message`, `tool_results_message` |
| `adapter.py` | `AnthropicAdapter`(ProviderAdapter 구현), `ModelAvailability` |

## 3. 키 수명주기와 계약 메서드

```python
from aik_adapter_anthropic import AnthropicAdapter, MessagesCall, Message, TextPart, UrllibTransport
from aik_provider import GuardedAdapter, JobRequest
from aik_vault import open_user_vault

adapter = AnthropicAdapter(open_user_vault(), UrllibTransport())   # 실제 왕복은 NOT_RUN
binding = adapter.register_key(api_key, connection_id="connection-1")  # vault put, 참조만 보유
guard = GuardedAdapter(adapter, authorize_dispatch=제품_인가_콜백)
guard.auth_status(binding)                                             # 인증 확인
adapter.prepare("req-1", MessagesCall((Message("user", (TextPart("..."),)),), max_tokens=1024))
job = guard.start_job(JobRequest(binding, "req-1", "route-1", "vm-attempt-1", 설정된_모델_ID))
result = adapter.result("req-1")                                       # 공통 ModelResult
binding2 = adapter.rotate_key(binding, new_api_key)                    # 교체: revision+1
guard.sign_out(binding2)                                               # 해제: vault 삭제
```

| 메서드 | 동작 |
|---|---|
| `register_key` | 키 형식(출력 가능한 ASCII) 확인 → `vault.put` → `secret://aik-vault/<id>` 참조와 fingerprint만 가진 `ConnectionBinding` 반환 |
| `describe` | `auth_status/sign_out/job_start/job_observe=supported`, `sign_in/job_interrupt=unsupported`, `isolation=isolated`, `implementation_state=implemented`, `qualification_status=NOT_RUN` |
| `auth_status` | vault에서 키 해석 → fingerprint 대조(불일치 `ACCOUNT_CHANGED`, 네트워크 없음) → `GET /v1/models?limit=1`. 200=`authenticated`, 401=`unauthenticated`, 참조 없음=`unauthenticated`(네트워크 없음) |
| `begin_sign_in` | API 키는 호스트 로그인이 아니므로 항상 `UNSUPPORTED` |
| `prepare` + `start_job` | 대화는 `prepare(request_id, MessagesCall)`로 미리 등록하고 `start_job`이 한 번만 꺼내 `POST /v1/messages`. 재전송하지 않는다 |
| `observe_job` | Messages 호출은 동기식이라 시작 시 terminal. 로컬 receipt를 반환 |
| `interrupt_job` | 지원 경로 없음 → `UNSUPPORTED` |
| `rotate_key` | 현재 binding의 fingerprint가 일치할 때만 `vault.replace`, revision+1·새 fingerprint binding 반환. 이전 binding은 이후 `ACCOUNT_CHANGED` |
| `sign_out`(해제) | 현재 binding의 키일 때만 `vault.delete`, `unauthenticated` 반환. 교체 후의 오래된 binding으로는 새 키를 지울 수 없다 |
| `check_model` | `GET /v1/models/{id}` 200=available(provider가 보고한 ID), 404=unavailable |

`GuardedAdapter`를 통한 해제는 공통 계약상 인증 확인이 먼저 성공해야 한다. vendor에서 이미 폐기된 키는 `adapter.sign_out(binding)`을 직접 호출해 로컬 vault 항목만 지울 수 있다.

`account_fingerprint`는 **키 자체에서 도메인 분리 SHA-256으로 유도한 값**이다. vendor 계정 ID가 아니며, 같은 조직의 서로 다른 키를 같은 계정으로 인식하지 못한다. 키가 고엔트로피라는 가정에 기대며 평문은 아니지만 비밀 유래 값이므로 공개 출력에 불필요하게 노출하지 않는다.

## 4. 비밀 처리

- binding·runtime DB·설정에는 `credential_ref`만 둔다. 평문은 호출마다 `aik_vault` 공개 인터페이스(`resolve`)로 얻고, `HttpRequest.headers`의 `x-api-key`에만 넣는다.
- `HttpRequest`의 `headers`·`body`는 `repr`에서 제외된다. transport 예외는 `__context__`/`__cause__`로 연결하지 않고(처리 블록 밖에서 새 오류를 발생) 고정 `ProviderFault`로 바꾼다.
- 오류 응답 본문에서 `error.type`만 `fault_from_provider`의 제한 규칙으로 보존하고 message는 버린다.
- 호출별로 키를 등록한 `aik_security.redaction.OutputRedactor`를 만들어 응답의 `id`·`model`·tool call ID를 검사(`safe_id`/`redact_text`)하고, 감사 기록은 `output("log", ...)`으로만 남긴다(`audit_log()`). 키가 12~256자가 아니면 등록되지 않아 패턴 기반 정제만 적용된다.
- 모델 출력 text·도구 인자는 RAW로 전달하며 adapter가 변형하지 않는다. 외부 경계로 나갈 때의 정제는 AIK-017 호출부의 책임이다.
- Python 불변 문자열 사본의 메모리 잔존은 막을 수 없다(best effort). 테스트 transport는 검증을 위해 요청 객체를 보관한다.

## 5. 공통 계약 변환

### 5.1 model·usage·cache

`ModelResult.model`은 응답의 `model` 값을 그대로 기록하며, 없거나 형식이 맞지 않으면 `None`이다. 요청한 모델 ID로 대체하지 않는다. `Usage`의 `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`는 0 이상의 정수로 명시된 값만 기록하고 나머지는 `None`(unknown)이다. 모델 이름은 코드에 고정하지 않으며 호출자가 설정값으로 전달한다.

### 5.2 종료 상태 — 잘린 결과를 완료로 승격하지 않음

| `stop_reason` | `Completion` | `JobReceipt.state` |
|---|---|---|
| `end_turn`, `stop_sequence` | `complete` (`is_final=True`) | `succeeded` |
| `tool_use` (ToolCall 1개 이상) | `tool_use` (최종 아님) | `succeeded` |
| `max_tokens` | `truncated` | `failed` |
| `refusal` | `refused` | `failed` |
| `pause_turn` | `incomplete` | `failed` |
| 기타·누락, 알 수 없는 content block, ToolCall 없는 `tool_use` | `unknown` | `failed` |

`succeeded`는 provider 호출의 관측이지 VM Goal/Task/Attempt 완료 판정이 아니다. 200 응답이지만 해석할 수 없으면(형식 오류·ID 누락·키가 반향된 ID) `UNKNOWN_OUTCOME`(outcome `unknown`, native `malformed_response`)로 끝나 guard가 host를 차단한다.

### 5.3 도구 요청/결과 (AIK-035 연결)

응답의 `tool_use` block → `ToolCall(call_id, name, arguments_json)`. 도구는 이 adapter가 실행하지 않는다. AIK-035 loop는 `assistant_message(result)`와 `tool_results_message([ToolResult(...)])`를 대화에 덧붙여 다음 `prepare`/`start_job`을 수행한다. 요청 시 `ToolCall`은 `tool_use`, `ToolResult`는 `tool_result`(`tool_use_id`, `content`, `is_error`), `ToolSpec`은 `tools[]`(`name`, `description`, `input_schema`)로 변환한다. `truncated`/`refused`/`unknown` 결과는 `assistant_message`가 거절한다.

### 5.4 fixture와 실측 구분

`ModelResult.source`는 transport가 선언한 `synthetic_fixture` 또는 `live`이다. `synthetic_fixture` 결과는 측정값으로 기록하면 안 된다(`measured=False`). `live`도 route qualification이 아니며 감사 기록에는 항상 `qualification_status=NOT_RUN`이 들어간다.

## 6. 오류 매핑

모든 실패는 `aik_provider.AdapterFailure(ProviderFault)`로 노출한다.

| 상황 | FaultKind / outcome | native_code |
|---|---|---|
| HTTP 401 (호출) | `AUTH_REQUIRED` / known | provider `error.type` |
| HTTP 403 | `ACCESS_DENIED` / known | provider `error.type` |
| HTTP 429 | `RATE_LIMITED` / known, `retry-after` 초→ms(최대 24시간) | provider `error.type` |
| HTTP 404 (모델 불가 등) | `UNSUPPORTED` / known | 예: `not_found_error` |
| 그 밖의 HTTP 오류(500, 529 등) | `PROVIDER_FAILURE` / known | provider `error.type` |
| 모델 호출 timeout·원인 불명 transport 예외 | `UNKNOWN_OUTCOME` / unknown | `timeout`, `transport_error` |
| 모델 호출 응답 해석 불가 | `UNKNOWN_OUTCOME` / unknown | `malformed_response` |
| 전송 전 실패(DNS·연결 거부) | `PROVIDER_FAILURE` / known | `connection_error` |
| 인증 확인·모델 조회 timeout (읽기 전용) | `PROVIDER_FAILURE` / known | `timeout` |
| vault 참조 없음(호출 시) | `AUTH_REQUIRED` / known | `credential_missing` |
| vault 접근 거부·기타 vault 오류 | `ACCESS_DENIED` / `PROVIDER_FAILURE` | `vault_error` |
| 키 교체 후 이전 binding | `ACCOUNT_CHANGED` / known | — |
| `prepare` 없는 시작 | `INVALID_CONTRACT` / known | `call_not_prepared` |

모델 불가 상태는 호출 시 `UNSUPPORTED` 공통 오류로, 사전 조회 시 `check_model`의 `unavailable` 상태로 구분해 노출한다. 400 `invalid_request_error`가 모델 문제인지는 응답 message를 보존하지 않으므로 구분하지 않는다.

## 7. 미검증 가정 (현재 공식 문서로 확인하지 못함)

| 항목 | 코드의 가정 |
|---|---|
| base URL | `https://api.anthropic.com` (생성자에서 변경 가능, https·경로 없음만 허용) |
| 엔드포인트 | `POST /v1/messages`, `GET /v1/models?limit=1`(인증 확인), `GET /v1/models/{id}` |
| 헤더 | `x-api-key`, `anthropic-version: 2023-06-01`(변경 가능), `content-type: application/json` |
| 요청 필드 | `model`, `max_tokens`, `messages[].role/content`, `system`, `tools[].name/description/input_schema`, content block `text`/`tool_use`/`tool_result` |
| 응답 필드 | `id`, `type="message"`, `role="assistant"`, `model`, `content[]`, `stop_reason` 값 목록, `usage`의 네 counter |
| 오류 | `{"type":"error","error":{"type":...,"message":...}}`, `retry-after` 초 단위, 529 overloaded |
| 도구 이름 형식 | `[A-Za-z0-9_-]{1,128}` |
| 인증 확인 방법 | 모델 목록 조회의 200/401로 키 유효성을 판단 |
| isolation | API 키 HTTP 요청은 공유 호스트 로그인 문맥이 없다는 구현 가정(`isolated`), 실측 아님 |

위 가정이 실제와 다르면 fixture는 통과해도 실제 왕복은 실패할 수 있다. 확인은 AIK-022/082/086의 단계별 근거로만 한다.

## 8. 검증 상태

```bash
python -m unittest discover -s tests -p 'test_adapter_anthropic.py' -v
```

| 항목 | 상태 |
|---|---|
| 합성 fake transport·fake vault fixture(등록·인증·호출·usage/cache·도구 loop·잘림·401/429/404/529/timeout·교체·해제·비밀 비검출) | 개발용 단위 시험. 이 문서 작성 시점에 실행 결과 기록 없음 |
| 실제 vendor API 왕복(`UrllibTransport`) | **NOT_RUN** |
| 실제 키 인증 | **NOT_RUN** (AIK-022) |
| runtime/도구 실행, 제품 통합 | **NOT_RUN** (AIK-082/086/044) |
| Windows 실행(DPAPI vault 포함) | **NOT_RUN** |
| route qualification | **NOT_RUN** — adapter는 qualified를 주장하지 않는다 |

Linux 시험 통과는 개발 검증이며 Windows 지원 증거가 아니다. fixture 결과를 실측 증거로 기록하지 않는다.

## 9. 한계

- streaming, 재시도, 실행 중 취소, extended thinking 등 다른 content block 형식은 구현하지 않았다(알 수 없는 block은 `unknown`).
- 준비된 대화·receipt·결과·감사 기록은 프로세스 메모리에만 있다. 재시작 뒤 reconciliation은 AIK-031/033/035/038 범위다.
- `rotate_key`의 확인과 교체는 vault의 개별 잠금 사이에서 원자적이지 않다. 동시 교체는 호출자가 직렬화해야 한다.
- `UrllibTransport`는 redirect를 따르지 않고 HTTPS만 보낸다. proxy 환경 변수는 표준 라이브러리 기본 동작을 따른다(미검증).
- `authorize_dispatch`는 제품의 VM/AGS 인가·lease 검증과 연결해야 하며, 시험의 `lambda request: True`는 합성 fixture다.
