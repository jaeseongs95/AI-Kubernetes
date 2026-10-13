# AIK-012 — OpenAI API 연결 adapter (구현·fixture 계약 검증)

상태: **adapter 코드 구현, 합성 fixture 계약 시험 작성. 실제 vendor 왕복 NOT_RUN, route qualification NOT_RUN.** 이 문서는 장부(`planning/`)의 AIK-012 상태와 완료 판정을 바꾸지 않는다. 선행 AIK-009·AIK-011도 장부상 미완료이므로 이 구현으로 그 관계를 우회하지 않는다.

## 1. 범위와 재사용

`src/aik_adapter_openai/`는 공식 **API 키** 연결을 AIK-011 공통 계약(`aik_provider`)의 자원·usage·실행 관측 형태로 변환한다. 표준 라이브러리만 사용한다.

| 재사용한 기존 구현 | 사용 방식 |
|---|---|
| `aik_provider` (AIK-011) | `ProviderAdapter` Protocol, `ConnectionBinding`·`AdapterCapabilities`·`AuthObservation`·`JobReceipt`·`ProviderFault`, `fault_from_provider`. `GuardedAdapter`로 감싸 계정 고정·중복 차단·불명확 결과 fence를 그대로 적용 |
| `aik_vault` (AIK-009) | `VaultBackend.put/replace/delete/resolve`. adapter는 참조(`secret://aik-vault/<id>`)만 보관 |
| `aik_security.redaction` (AIK-017) | 응답 문자열의 키 반향 검사·마스킹, 이벤트 기록 정제(`output("log", ...)`), vendor 오류 code의 `safe_id` 검사 |
| `aik_runtime` (AIK-005) | 직접 호출하지 않음. 반환 참조가 `CredentialRef` locator 규칙을 만족하므로 runtime DB에는 참조만 저장 가능 |

새 Task/Attempt 원장, VM 인가, 영속 lease는 만들지 않는다.

## 2. 구성

- `transport.py`: `HttpRequest`/`HttpResponse`와 주입형 `Transport` Protocol(`send(request, *, timeout)`). `HttpRequest.__repr__`는 헤더를 숨긴다. 헤더 값의 CR/LF/NUL은 거절한다. `UrllibTransport`는 표준 라이브러리 HTTPS 구현이며 redirect를 거부한다(Authorization 헤더가 다른 위치로 전달되지 않도록). **실행 근거 없음(NOT_RUN).**
- `contract.py`: AIK-035 bounded agent tool loop 연결용 공통 레코드 — `Message`, `ToolSpec`, `ToolCall`, `ToolResult`, `TurnInput`, `TurnResult`, `Usage`, `ModelCheck`, `AdapterStatus`.
- `adapter.py`: `OpenAIApiAdapter`.

adapter는 transport와 vault를 **명시적으로 주입받아야** 생성된다. 기본 네트워크 transport를 자동 선택하지 않는다.

## 3. 수명주기 코드 경로

| 단계 | 메서드 | 동작 |
|---|---|---|
| 키 등록 | `register_key(connection_id, secret)` | 형식 검사(인쇄 가능 ASCII 12~256자, 공백·개행 금지) → `vault.put` → revision 1 `ConnectionBinding` 반환. 인증 상태는 `unknown` |
| 재연결 | `attach(binding)` | 재시작 뒤 저장된 binding을 다시 붙인다. fingerprint 재계산이 일치해야 하며 다시 인증 확인이 필요 |
| 인증 확인 | `verify_key(binding)` | `GET /models`. 2xx→`authenticated`, 401→`unauthenticated`, 403→`locked`, 그 외·transport 실패→`unknown`. vault 참조 없음→`unauthenticated`(요청 미전송) |
| 모델 확인 | `check_model(binding, model)` | `GET /models/{model}`. 응답 `id`가 요청 모델과 같을 때만 `available`. 404→`not_found`, 403→`denied`, 401→인증 철회 |
| 모델 호출 | `stage_turn(request, turn)` → `start_job(request)` | `POST /responses`(`background: true`). 인증 `authenticated`이고 해당 모델이 `available`일 때만 전송 |
| 관측 | `observe_job` | `GET /responses/{id}` |
| 중단 | `interrupt_job` | `POST /responses/{id}/cancel`. 수락 응답은 `cancelled` 관측 전까지 종료로 보지 않음 |
| 교체 | `rotate_key(binding, new_secret)` | 활성 작업이 있으면 `ACCOUNT_CONFLICT`. `vault.replace` 후 revision+1 binding 반환, 인증·모델 확인 초기화. 이전 binding은 `STALE_CONNECTION` |
| 해제 | `sign_out(binding)` (`release_key`) | 활성 작업이 있으면 거절. `vault.delete` 후 `unauthenticated`. **vendor 쪽 키 폐기는 수행하지 않으며** 사용자가 공급자 콘솔에서 처리해야 한다 |

`begin_sign_in`은 API 키 경로에 해당하지 않으므로 `UNSUPPORTED`다(AIK-011 계약상 API 키는 host sign-in이 아니다). 키 등록 경로는 `register_key`로 별도 구현되어 있다.

`auth_status`는 네트워크를 호출하지 않고 마지막 `verify_key` 결과를 반환한다. 기본 300초(`auth_ttl_seconds`)가 지나면 `unknown`으로 낮춰 실행 가능 자원에서 제외한다. 작업 중 401을 받으면 즉시 `unauthenticated`로 바꾼다.

## 4. 공통 계약 변환

- **request/job**: vendor 응답 ID는 AIK-011 ID 문법(`_` 불허)과 맞지 않을 수 있어 `provider_job_id = "openai-response:" + sha256(id)[:48]` 별칭을 쓰고 원래 ID는 내부 경로에만 쓴다. 응답 ID는 `[A-Za-z0-9][A-Za-z0-9_-]{0,127}`만 허용한다.
- **상태**: `queued→accepted`, `in_progress→running`, `completed→succeeded`, `failed`/`incomplete→failed`, `cancelled→cancelled`, 그 외·누락→`unknown`.
- **model**: 응답의 `model`이 있고 형식이 유효할 때만 기록한다. 요청 모델로 채우지 않는다. 없으면 `None`.
- **usage**: `usage.input_tokens/output_tokens/total_tokens` 중 provider가 명시한 정수만 기록한다. 누락은 `None`이며 합계를 계산해 채우지 않는다.
- **도구**: 출력 항목 `function_call`(`call_id`, `name`, `arguments` JSON 문자열)을 `ToolCall`로 변환한다. `arguments`가 JSON 객체가 아니면 `arguments=None, arguments_valid=False`. 다음 turn의 `ToolResult`는 `function_call_output` 입력 항목으로, `previous_response_id`와 함께 전송한다. 처리하지 않은 항목 종류는 `unhandled_item_types`에 남긴다.
- **도구 실행 자체**는 이 adapter가 하지 않는다. 실행·승인·반복 한도는 AIK-035 책임이다.

## 5. 비밀 처리

- binding·runtime DB·이벤트·결과에는 참조와 slot fingerprint만 있다. fingerprint는 `connection_id·credential_ref·revision`의 SHA-256이며 **비밀에서 유도하지 않는다**. 따라서 키가 실제로 어느 vendor 계정에 속하는지는 관측하지 않는다(검증된 계정 식별 엔드포인트 없음).
- 평문 키는 요청마다 `vault.resolve`로 얻어 Authorization 헤더 구성과 같은 호출의 응답 반향 검사에만 사용한다. 저장·로그·반환하지 않는다.
- transport 예외는 내용을 버리고 `except` 블록 밖에서 고정 `AdapterFailure`를 올려 `__context__`로도 연결하지 않는다.
- 응답 텍스트·도구 인자에 키(또는 redactor가 아는 인코딩)가 반향되면 마스킹하고 `secret_echo_redacted=True`로 표시한다. 64KiB를 넘는 텍스트는 원문 일치만 검사할 수 있으며 일치 시 텍스트를 보류한다.
- vendor 오류 본문 원문은 보존하지 않는다. `error.code`/`error.type` 중 안전한 식별자만 `fault_from_provider`에 전달한다.
- Python `str` 사본은 지울 수 없으므로 메모리 잔존 방지는 best effort다.

## 6. 실패 매핑

| 상황 | 결과 |
|---|---|
| 401 | `AUTH_REQUIRED`, 인증 철회·모델 광고 제거 |
| 403 | `ACCESS_DENIED`(호출 시 해당 모델 `denied`) |
| 404/405/501 | `UNSUPPORTED`(호출 시 해당 모델 `not_found`) |
| 429 | `RATE_LIMITED`, `Retry-After` 초 단위 값을 `retry_after_ms`로 |
| 시작·중단의 5xx/408, transport 예외 | `UNKNOWN_OUTCOME`(outcome `unknown`) → `GuardedAdapter`가 호스트 fence |
| 2xx인데 본문 해석 불가·ID 불일치 | `INVALID_RECEIPT`(outcome `unknown`) |
| vault 참조 없음/접근 거부 | 요청 미전송, `AUTH_REQUIRED`/`ACCESS_DENIED` |
| 교체 중 vault 결과 불명 | `UNKNOWN_OUTCOME`, 인증 초기화(재확인 필요) |

## 7. 자원 광고와 과금 구분

`executable_models(binding)`은 인증이 유효(`authenticated`, TTL 이내)하고 `check_model`이 `available`을 확인한 모델만 반환한다. 잘못된 키·권한·모델은 광고하지 않는다. `resource_view`는 `billing_basis="api_key_metered"`, `subscription_quota="not_applicable"`을 고정 표기한다. `Usage.billing_basis`도 API 과금 외 값을 거부하므로 구독 잔여량과 섞이지 않는다.

## 8. 구현 상태와 qualification 분리

- `describe()`: `implementation_state="implemented"`, `qualification_status="NOT_RUN"`(AIK-011 계약상 다른 값 불가). `isolation`은 실측 근거가 없어 `unknown`.
- `status()`: `AdapterStatus(implementation_state, route_qualification="NOT_RUN", live_vendor_round_trip="NOT_RUN", endpoint_assumptions="UNVERIFIED")`. `qualified`를 넣으면 생성이 거절된다.
- `TurnResult.evidence_kind`는 transport의 `kind`를 복사한다(`fixture`/`network`/`unspecified`). fixture 결과를 provider 실측으로 기록하지 않는다. `network`라도 qualification이 아니다.
- 실제 지원 판정은 AIK-022(실제 인증)·AIK-082(사전 probe)·AIK-086/044(runtime·도구 실행)의 단계별 근거로만 한다.

## 9. 미검증 가정 (vendor 공식 문서로 확인하지 못함)

| 항목 | 가정 |
|---|---|
| base URL | `https://api.openai.com/v1` |
| 인증 헤더 | `Authorization: Bearer <key>` (조직/프로젝트 헤더 미사용) |
| 인증 확인 | `GET /models` 2xx를 키 유효로 간주 |
| 모델 확인 | `GET /models/{model}` 응답 `id` |
| 호출 | `POST /responses`, 본문 `model`·`input`·`tools`(`type: function`)·`previous_response_id`·`max_output_tokens`·`background` |
| 관측·중단 | `GET /responses/{id}`, `POST /responses/{id}/cancel` |
| 응답 필드 | `id`, `status`, `model`, `usage.{input,output,total}_tokens`, `output[].type`(`message`/`function_call`), `content[].output_text.text`, `error.code` |
| 도구 결과 | 입력 항목 `{"type":"function_call_output","call_id","output"}` |
| 오류 의미 | 401/403/404/429/5xx의 의미, `Retry-After` 초 단위, 429 시 미실행 |

시험의 응답 본문은 이 가정에 맞춘 **합성 protocol fixture**이며 vendor 응답 사본이 아니다.

## 10. 검증

```powershell
python -m unittest discover -s tests -p 'test_adapter_openai.py' -v
```

| 항목 | 상태 |
|---|---|
| 합성 fake transport·`FakeVaultBackend`로 등록·인증 확인·모델 확인·호출·관측·중단·교체·해제, 성공/실패 fixture, `GuardedAdapter` 결합, 평문 비검출 | 단위 시험 작성. 이 문서 작성 시점의 실행 결과 기록 없음 |
| 실제 vendor 왕복(`UrllibTransport`) | **NOT_RUN** |
| 실제 키 인증(AIK-022), runtime/도구 실행(AIK-082/086/044) | **NOT_RUN** |
| Windows DPAPI vault와 결합한 실행 | **NOT_RUN** (Linux 통과는 Windows 지원 증거가 아님) |

## 11. 한계

- 상태(연결·작업·중복 기록)는 프로세스 메모리에만 있다. 재시작 뒤 영속 reconciliation은 AIK-031/033/035/038 범위다.
- adapter는 호출 중 하나의 잠금을 유지하므로 같은 인스턴스의 요청은 직렬화된다.
- 키 → vendor 계정 대응, 조직·프로젝트 범위, 과금 한도는 관측하지 않는다.
- 해제는 로컬 vault 삭제이며 vendor 쪽 키 폐기가 아니다.
- streaming, 파일·이미지 입력, 내장(hosted) 도구, 추론 토큰 세부 usage는 다루지 않는다.

## 구현 메모

- 엔드포인트·헤더·응답 필드·상태 문자열·오류 의미는 모두 공식 문서로 확인하지 못한 가정이다. 문서 9절에 표시했다. 실제 형식이 다르면 `_STATUS`, `_turn_from`, `_turn_payload`, 경로 문자열을 고쳐야 한다.
- 시험은 이 환경에서 실행하지 않았다. 결과 PASS를 주장하지 않는다. `FakeVaultBackend`의 경로 검증 때문에 임시 디렉터리 경로에 symlink가 있는 환경(예: macOS `/tmp`)에서는 setUp이 실패할 수 있다.
- 키 형식은 인쇄 가능 ASCII 12~256자로 제한했다. 헤더 주입 방지와 `OutputRedactor.add_secret`의 길이 제약을 함께 맞추기 위한 가정이다.
- `account_fingerprint`는 slot 식별값이라 vendor 계정 변경 탐지를 제공하지 않는다. 교체할 때 revision과 fingerprint가 바뀌는 것으로 이전 binding을 무효화한다.
- `auth_status`는 캐시 기반이다. TTL 동안 vendor 쪽 키 폐기는 다음 401 응답 전까지 반영되지 않는다.
- `isolation`은 근거가 없어 `unknown`으로 두었다. API 키 경로에서는 `GuardedAdapter`의 실행 차단 조건에 영향이 없다.
- `UrllibTransport`는 환경 변수 프록시 설정을 따른다. 실행 근거가 없다(NOT_RUN).
