# AIK-013 — Anthropic API(Messages API) 연결 adapter (구현·fixture 검증)

상태: **adapter 코드 구현됨(`implementation_state=implemented`) — route qualification `NOT_RUN`, 실제 vendor 왕복 `NOT_RUN`.**

이 문서는 `src/aik_adapter_anthropic/`의 설계와 한계를 설명한다. 장부(`planning/`)의 AIK-013 상태와 완료 판정은 이 문서에서 바꾸지 않는다. 선행 작업 AIK-009·AIK-011의 장부 상태도 변경하지 않는다.

## 1. 범위

공식 API 키 연결을 두 가지 공통 계약으로 변환한다.

- AIK-011 공통 계약(`aik_provider.ProviderAdapter`)
- 공통 모델 turn 계약

실제 계정의 지원 여부는 이 코드가 판정하지 않는다.

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
adapter.discard_result("req-1")                                        # RAW 출력 메모리 해제(선택)
binding2 = adapter.rotate_key(binding, new_api_key)                    # 교체: revision+1
guard.sign_out(binding2)                                               # 해제: vault 삭제
```

| 메서드 | 동작 |
|---|---|
| `register_key` | 1. 키 형식을 확인한다(출력 가능한 ASCII이며, `OutputRedactor`가 등록할 수 있는 길이). 2. `vault.put`을 호출한다. 3. `secret://aik-vault/<id>` 참조와 fingerprint만 가진 `ConnectionBinding`을 반환한다. 4. 이 프로세스가 발급한 revision을 기억한다. vault 실패는 `vault_error` fault로 끝난다. |
| `describe` | `auth_status/sign_out/job_start/job_observe=supported`, `sign_in/job_interrupt=unsupported`, `isolation=isolated`, `implementation_state=implemented`, `qualification_status=NOT_RUN` |
| `auth_status` | vault에서 키를 해석하고 revision·fingerprint를 대조한다(불일치 시 `ACCOUNT_CHANGED`, 네트워크 없음). 이어서 `GET /v1/models?limit=1`을 호출한다. 결과: 200=`authenticated`, 401=`unauthenticated`, 참조 없음=`unauthenticated`(네트워크 없음) |
| `begin_sign_in` | API 키는 호스트 로그인이 아니므로 항상 `UNSUPPORTED` |
| `prepare` + `start_job` | 대화를 `prepare(request_id, MessagesCall)`로 미리 등록한다. `start_job`은 그 대화를 한 번만 꺼내 `POST /v1/messages`를 보낸다. 꺼낸 시점에 request_id는 **소비**된다. 호출이 실패해도(timeout·`UNKNOWN_OUTCOME` 포함) 같은 id의 `prepare`·`start_job`은 `DUPLICATE_REQUEST`로 거절되며 재전송하지 않는다. 재시도에는 새 request_id를 사용한다. 미처리 준비 항목은 최대 1024개다. 초과하면 `PROVIDER_FAILURE`/`prepared_capacity`가 된다. |
| `cancel_prepared` | 아직 전송하지 않은 준비 항목을 제거한다(반환: 제거 여부). 전송된 적이 없으므로 같은 id를 다시 준비할 수 있다. |
| `observe_job` | Messages 호출은 동기식이라 시작 시 terminal이다. 로컬 receipt를 반환한다. 전달한 receipt가 저장된 receipt와 정확히 같고, 요청 binding의 connection_id·revision·fingerprint가 시작 시점과 같을 때만 반환한다. 그 밖에는 `INVALID_RECEIPT`다. |
| `interrupt_job` | 지원 경로 없음 → `UNSUPPORTED` |
| `rotate_key` | 현재 binding이 유효할 때만 `vault.replace`를 호출한다. revision+1과 새 fingerprint를 가진 binding을 반환한다. 이전 revision의 binding은 이후 `ACCOUNT_CHANGED`다. 같은 키로 교체하거나 A→B→A로 되돌린 경우도 포함한다(이 프로세스가 기억하는 revision 기준). vault 실패는 `vault_error`, 교체 대상 참조 소실은 `AUTH_REQUIRED`/`credential_missing`이다. |
| `sign_out`(해제) | 현재 binding의 키일 때만 `vault.delete`를 호출하고 `unauthenticated`를 반환한다. 오래된 binding으로는 새 키를 지울 수 없다. |
| `check_model` | `GET /v1/models/{id}`. 200=available(provider가 보고한 ID), 404=unavailable |
| `result` / `discard_result` | 프로세스 내부 accessor다. `result`는 binding을 확인하지 않으므로 접근 통제는 호출자 책임이다. `discard_result`는 RAW 출력만 제거하며, receipt와 소비된 id 차단은 유지한다. |

`GuardedAdapter`를 통한 해제는 공통 계약상 인증 확인이 먼저 성공해야 한다. vendor에서 이미 폐기된 키는 `adapter.sign_out(binding)`을 직접 호출해 로컬 vault 항목만 지울 수 있다.

`account_fingerprint`는 **키 자체에서 도메인 분리 SHA-256으로 유도한 값**이다.

- vendor 계정 ID가 아니다. 같은 조직의 서로 다른 키를 같은 계정으로 인식하지 못한다.
- 키가 고엔트로피라는 가정에 기댄다.
- 평문은 아니지만 비밀에서 유래한 값이므로 공개 출력에 불필요하게 노출하지 않는다.

## 4. 비밀 처리

- binding·runtime DB·설정에는 `credential_ref`만 둔다. 평문은 호출마다 `aik_vault` 공개 인터페이스(`resolve`)로 얻고, `HttpRequest.headers`의 `x-api-key`에만 넣는다.
- `HttpRequest`의 `headers`·`body`는 `repr`에서 제외된다.
- transport·vault 예외는 `__context__`/`__cause__`로 연결하지 않는다. 처리 블록 밖에서 새 오류를 발생시켜 고정 `ProviderFault`로 바꾼다. 여기에는 `register_key`의 `vault.put`과 `rotate_key`의 `vault.replace`도 포함된다.
- 오류 응답 본문에서는 `error.type`만 `fault_from_provider`의 제한 규칙으로 보존하고 message는 버린다.
- 호출마다 키를 등록한 `aik_security.redaction.OutputRedactor`를 만든다. 이 redactor로 응답의 `id`·`model`·tool call ID를 검사(`safe_id`/`redact_text`)하고, 감사 기록은 `output("log", ...)`으로만 남긴다(`audit_log()`).
- **redaction은 fail-closed다.** `OutputRedactor`가 등록하지 못하는 키(문서상 12~256자 범위 밖)는 다음과 같이 처리한다.
  - `register_key`·`rotate_key`가 `ValueError("INVALID_API_KEY_FORMAT")`로 거절한다.
  - 다른 경로로 vault에 이미 있는 경우에는 인증 확인·모델 조회·호출이 전송 전에 `INVALID_CONTRACT`/`unredactable_credential`로 끝난다.
  - 로컬 해제(`sign_out`)는 provider 데이터를 기록하지 않으므로 계속 가능하다.
- 모델 출력 text·도구 인자는 RAW로 전달하며 adapter가 변형하지 않는다. 외부 경계로 나갈 때의 정제는 AIK-017 호출부의 책임이다.
- Python 불변 문자열 사본의 메모리 잔존은 막을 수 없다(best effort). 테스트 transport는 검증을 위해 요청 객체를 보관한다.

## 5. 공통 계약 변환

### 5.1 model·usage·cache

`ModelResult.model`은 응답의 `model` 값을 그대로 기록한다. 값이 없거나 형식이 맞지 않으면 `None`이며, 요청한 모델 ID로 대체하지 않는다.

`Usage`는 다음 counter를 기록한다. 0 이상의 정수로 명시된 값만 기록하고, 나머지는 `None`(unknown)이다.

- `input_tokens`
- `output_tokens`
- `cache_creation_input_tokens`
- `cache_read_input_tokens`
- `cache_creation_5m_input_tokens`, `cache_creation_1h_input_tokens`: `cache_creation`의 보존 기간별 내역(AIK-093)

`Usage.breakdown()`은 이 값을 벤더 중립 `UsageBreakdown`으로 나눈다. thinking 토큰은 provider가 따로 보고하지 않으므로 `reasoning`은 unknown이다. 자세한 내용은 [prompt-cache](prompt-cache.ko.md)에 있다.

모델 이름은 코드에 고정하지 않으며 호출자가 설정값으로 전달한다.

### 5.2 종료 상태 — 잘린 결과를 완료로 승격하지 않음

| `stop_reason` | `Completion` | `JobReceipt.state` |
|---|---|---|
| `end_turn`, `stop_sequence` | `complete` (`is_final=True`) | `succeeded` |
| `tool_use` (ToolCall 1개 이상) | `tool_use` (최종 아님) | `succeeded` |
| `max_tokens` (ToolCall이 있어도 동일) | `truncated` | `failed` |
| `refusal` | `refused` | `failed` |
| `pause_turn` | `incomplete` | `failed` |
| 기타·누락, 알 수 없는 content block(`thinking`·`redacted_thinking` 제외), ToolCall 없는 `tool_use` | `unknown` | `failed` |

`succeeded`는 provider 호출을 관측했다는 뜻이며, VM Goal/Task/Attempt 완료 판정이 아니다.

200 응답이지만 해석할 수 없으면 `UNKNOWN_OUTCOME`(outcome `unknown`, native `malformed_response`)으로 끝나고 guard가 host를 차단한다. 해석할 수 없는 경우는 다음과 같다.

- 형식 오류
- ID 누락, 키가 반향된 ID
- 공통 ID 형식에 맞지 않는 ID
- 불완전한 `tool_use` 입력
- 결과 계약 위반

### 5.3 도구 요청/결과 (AIK-035 연결)

응답의 `tool_use` block은 `ToolCall(call_id, name, arguments_json)`으로 변환한다. 도구는 이 adapter가 실행하지 않는다.

AIK-035 loop는 `assistant_message(result)`와 `tool_results_message([ToolResult(...)])`를 대화에 덧붙인 뒤, 새 request_id로 다음 `prepare`/`start_job`을 수행한다.

요청 시 변환 규칙은 다음과 같다.

| 공통 계약 | 요청 필드 |
|---|---|
| `ToolCall` | `tool_use` |
| `ToolResult` | `tool_result`(`tool_use_id`, `content`, `is_error`) |
| `ToolSpec` | `tools[]`(`name`, `description`, `input_schema`) |

`truncated`/`refused`/`unknown` 결과는 `assistant_message`가 거절한다.

AIK-093부터 다음이 추가됐다.
- `thinking`·`redacted_thinking` block은 해석하지 않는 `OpaqueBlock`(원래 index와 원문 JSON)으로 보존하고, 다음 요청에 같은 위치로 그대로 보낸다. 다른 provider의 블록이 섞인 대화는 `prepare`가 `INVALID_CONTRACT`(native `opaque_provider_mismatch`)로 거절한다.
- `ToolCall.wire_arguments_json`은 모델이 낸 인자의 key 순서를 보존한다. 다시 보낼 때는 이 순서를 쓰고, digest는 기존 canonical 값을 쓴다.
- `MessagesCall`의 `cache`·`thinking`·`effort`가 None이면 요청 본문은 이전과 같다. 값이 있을 때의 문법(`cache_control`, `thinking`, `output_config`)은 이 adapter에만 있다.
- `AnthropicTurnPort`(`loop_port.py`)가 ApiToolLoop의 model port를 이 adapter로 구현한다.

### 5.4 fixture와 실측 구분

`ModelResult.source`는 transport가 **스스로 선언한** `synthetic_fixture` 또는 `live`이다.

- `synthetic_fixture` 결과는 측정값으로 기록하면 안 된다(`measured=False`).
- adapter는 transport의 선언을 검증하지 못한다. `source="live"`를 선언한 local mock도 `measured=True`가 된다. 실측 기록은 제품이 실제 `UrllibTransport`를 사용했다는 별도 근거와 함께만 해야 한다.
- `live`도 route qualification이 아니며, 감사 기록에는 항상 `qualification_status=NOT_RUN`이 들어간다.

## 6. 오류 매핑

모든 provider 계약 실패는 `aik_provider.AdapterFailure(ProviderFault)`로 노출한다.

| 상황 | FaultKind / outcome | native_code |
|---|---|---|
| HTTP 401 (호출·모델 조회) | `AUTH_REQUIRED` / known | provider `error.type` |
| HTTP 403 | `ACCESS_DENIED` / known | provider `error.type` |
| HTTP 429 | `RATE_LIMITED` / known, `retry-after` 초→ms(최대 24시간) | provider `error.type` |
| HTTP 404 (모델 불가 등) | `UNSUPPORTED` / known, `http_status=404` | 예: `not_found_error` |
| 그 밖의 HTTP 오류(500, 529 등) | `PROVIDER_FAILURE` / known | provider `error.type` |
| 모델 호출 timeout·원인 불명 transport 예외 | `UNKNOWN_OUTCOME` / unknown | `timeout`, `transport_error` |
| 모델 호출 응답 해석 불가 | `UNKNOWN_OUTCOME` / unknown | `malformed_response` |
| 전송 전 실패(DNS·연결 거부) | `PROVIDER_FAILURE` / known | `connection_error` |
| 인증 확인·모델 조회 timeout (읽기 전용) | `PROVIDER_FAILURE` / known | `timeout` |
| vault 참조 없음(호출·교체 시) | `AUTH_REQUIRED` / known | `credential_missing` |
| vault 접근 거부·기타 vault 오류(해석·등록·교체·삭제) | `ACCESS_DENIED` / `PROVIDER_FAILURE` | `vault_error` |
| 키 교체 후 이전 binding(이전 revision 포함) | `ACCOUNT_CHANGED` / known | — |
| redactor가 등록할 수 없는 저장 키 | `INVALID_CONTRACT` / known | `unredactable_credential` |
| `prepare` 없는(또는 취소된) 시작 | `INVALID_CONTRACT` / known | `call_not_prepared` |
| 이미 소비·완료된 request_id의 재준비·재시작 | `DUPLICATE_REQUEST` / known | — |
| 준비 항목 1024개 포화 | `PROVIDER_FAILURE` / known | `prepared_capacity` |

**모델 불가의 구분**은 다음과 같다.

- 호출 시 모델 불가는 `UNSUPPORTED`이며 `http_status=404`와 provider `error.type`(예: `not_found_error`)을 함께 가진다.
- 기능 미지원(`begin_sign_in`·`interrupt_job`)의 `UNSUPPORTED`는 `http_status`와 `native_code`가 모두 `None`이므로 위와 구분할 수 있다.
- 엔드포인트가 고정되어 있고 base URL에 경로를 허용하지 않으므로 호출 경로의 404는 주로 모델 문제로 해석된다. 다만 응답 message를 보존하지 않으므로 그 밖의 404 원인과 확실히 구분하지는 못한다.
- 사전 확인에는 `check_model`의 `unavailable` 상태를 사용한다.
- 400 `invalid_request_error`가 모델 문제인지는 구분하지 않는다.

## 7. 미검증 가정 (현재 공식 문서로 확인하지 못함)

| 항목 | 코드의 가정 |
|---|---|
| base URL | `https://api.anthropic.com` (생성자에서 변경 가능, https·경로 없음만 허용) |
| 엔드포인트 | `POST /v1/messages`, `GET /v1/models?limit=1`(인증 확인), `GET /v1/models/{id}` |
| 헤더 | `x-api-key`, `anthropic-version: 2023-06-01`(변경 가능), `content-type: application/json`, `user-agent: aik-adapter-anthropic/0.1 (AI-Kubernetes)`(제품 식별; 라이브러리 기본값은 일부 gateway가 거절) |
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
| 합성 fake transport·fake vault fixture | 개발용 단위 시험. 이 문서 작성 시점에 실행 결과 기록 없음 |
| 실제 vendor API 왕복(`UrllibTransport`) | **NOT_RUN** |
| 실제 키 인증 | **NOT_RUN** (AIK-022) |
| runtime/도구 실행, 제품 통합 | **NOT_RUN** (AIK-082/086/044) |
| Windows 실행(DPAPI vault 포함) | **NOT_RUN** |
| route qualification | **NOT_RUN** — adapter는 qualified를 주장하지 않는다 |

fixture 시험이 다루는 범위는 다음과 같다.

- 등록·인증·호출, usage/cache, 도구 loop, 잘림
- 401/403/429/404/529/timeout
- 교체(A→B→A 포함)·해제, vault 실패
- request_id 재준비 차단, receipt 대조, 준비 포화·취소
- 비밀 비검출

Linux 시험 통과는 개발 검증이며 Windows 지원 증거가 아니다. fixture 결과를 실측 증거로 기록하지 않는다.

## 9. 한계

- streaming, 재시도, 실행 중 취소는 구현하지 않았다. thinking 외의 다른 content block 형식(`server_tool_use` 등)은 여전히 `unknown`이다.
- 다음 상태는 프로세스 메모리에만 있다. 재시작 뒤 reconciliation은 AIK-031/033/035/038 범위다.
  - 준비된 대화, 소비된 request_id, receipt, 결과, credential별 현재 revision, 감사 기록
  - 재시작 후 또는 다른 adapter 인스턴스에서는 revision 기억이 없으므로 이전 binding 거절이 fingerprint 비교에만 의존한다. 따라서 A→B→A 교체 뒤의 옛 binding을 그 경우에는 거절하지 못한다.
  - request_id 재전송 차단도 프로세스 수명 동안만 유효하다.
- 메모리 상한
  - 소비된 request_id와 receipt는 재전송 차단을 위해 축출하지 않으므로 호출 수에 비례해 증가한다.
  - RAW 결과는 `discard_result`로 해제할 수 있다.
  - 준비 항목은 1024개 상한이며 `cancel_prepared`로 해제한다.
  - 장시간 실행 시 receipt·소비 id의 영속·축출 정책은 상위 reconciliation 설계의 범위다.
- `rotate_key`의 확인과 교체는 vault의 개별 잠금 사이에서 원자적이지 않다. 동시 교체는 호출자가 직렬화해야 한다. `vault.replace`가 쓰기 후에 예외를 내는 경우 vault에는 새 키가 있을 수 있다. 이때 기존 binding은 fingerprint 불일치(`ACCOUNT_CHANGED`)가 되므로 키를 다시 등록해야 한다.
- `UrllibTransport`
  - redirect를 따르지 않고 HTTPS만 보낸다.
  - proxy 환경 변수는 표준 라이브러리 기본 동작을 따른다(미검증).
  - `timeout`은 urllib/socket의 **개별 작업(연결·각 수신) 단위 timeout이며 요청 전체의 총 기한이 아니다.** 서버가 기한보다 짧은 간격으로 조금씩 응답을 보내면 호출이 설정값보다 오래 지속될 수 있다. 총 기한 강제는 상위 supervisor/AIK-031 계층의 책임이며, 이 transport에서는 구현하지 않았다.
- `authorize_dispatch`는 제품의 VM/AGS 인가·lease 검증과 연결해야 한다. 시험의 `lambda request: True`는 합성 fixture다.

## 실제 Messages API 최소 진단 (2026-10-13)

인증을 주입하는 egress gateway를 거쳐 어댑터의 `POST /v1/messages` 요청과 응답 decoder를 실제 API로 한 번 확인했다(`partial_probe_verified_wire_only`). 제품 경로인 guard는 `GET /v1/models` 사전 확인이 gateway에서 거절돼 실행되지 않았다. 직접 `x-api-key` 인증과 계정 binding도 검증하지 않았다. 근거: [실제 진단 기록](../evidence/provider-adapter/2026-10-13-anthropic-live-probe/README.ko.md).
