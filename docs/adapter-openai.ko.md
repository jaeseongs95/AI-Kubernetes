# AIK-012 — OpenAI API 연결 adapter (구현·fixture 계약 검증)

상태: **adapter 코드는 구현했고 합성 fixture 계약 시험을 작성했다. 실제 vendor 왕복과 route qualification은 NOT_RUN이다.** 이 문서는 장부(`planning/`)의 AIK-012 상태와 완료 판정을 바꾸지 않는다. 선행 AIK-009·AIK-011도 장부상 미완료이며, 이 구현으로 그 의존 관계를 우회하지 않는다.

## 1. 범위와 재사용

`src/aik_adapter_openai/`는 공식 **API 키** 연결을 AIK-011 공통 계약(`aik_provider`)의 자원·usage·실행 관측 형태로 변환한다. 표준 라이브러리만 사용한다.

| 재사용한 기존 구현 | 사용 방식 |
|---|---|
| `aik_provider` (AIK-011) | `ProviderAdapter` Protocol, `ConnectionBinding`·`AdapterCapabilities`·`AuthObservation`·`JobReceipt`·`ProviderFault`, `fault_from_provider`를 사용한다. `GuardedAdapter`로 감싸 계정 고정·중복 차단·불명확 결과 fence를 그대로 적용한다 |
| `aik_vault` (AIK-009) | `VaultBackend.put/delete/resolve`를 사용한다. adapter는 참조(`secret://aik-vault/<id>`)만 보관한다 |
| `aik_security.redaction` (AIK-017) | 응답 문자열의 키 반향 검사·마스킹, 이벤트 기록 정제(`output("log", ...)`), vendor 오류 code의 `safe_id` 검사에 사용한다 |
| `aik_runtime` (AIK-005) | 직접 호출하지 않는다. 반환 참조가 `CredentialRef` locator 규칙을 만족하므로 runtime DB에는 참조만 저장할 수 있다 |

새 Task/Attempt 원장, VM 인가, 영속 lease는 만들지 않는다.

## 2. 구성

- `transport.py`: `HttpRequest`/`HttpResponse`와 주입형 `Transport` Protocol(`send(request, *, timeout)`)을 정의한다.
  - `HttpRequest.__repr__`는 헤더를 숨기고, 헤더 값의 CR/LF/NUL은 거절한다.
  - `UrllibTransport`는 표준 라이브러리 HTTPS 구현이다. Authorization 헤더가 다른 위치로 전달되지 않도록 redirect를 거부한다. **실행 근거 없음(NOT_RUN).**
- `contract.py`: AIK-035 bounded agent tool loop 연결용 공통 레코드다 — `Message`, `ToolSpec`, `ToolCall`, `ToolResult`, `TurnInput`, `TurnResult`, `Usage`, `ModelCheck`, `AdapterStatus`.
- `adapter.py`: `OpenAIApiAdapter`.

adapter는 transport와 vault를 **명시적으로 주입받아야** 생성된다. 기본 네트워크 transport를 자동으로 선택하지 않는다.

## 3. 수명주기 코드 경로

| 단계 | 메서드 | 동작 |
|---|---|---|
| 키 등록 | `register_key(connection_id, secret)` | 형식 검사(인쇄 가능 ASCII 12~256자, 공백·개행 금지) → `vault.put` → revision 1 `ConnectionBinding` 반환. 인증 상태는 `unknown` |
| 재연결 | `attach(binding)` | 재시작 뒤 저장된 binding을 다시 붙인다. 재계산한 fingerprint가 일치해야 하며, 인증 확인을 다시 거쳐야 한다 |
| 인증 확인 | `verify_key(binding)` | `GET /models` 결과에 따라 2xx→`authenticated`, 401→`unauthenticated`, 403→`locked`, 그 외·transport 실패→`unknown`. vault 참조가 없으면 `unauthenticated`, vault 접근이 거부되면 `locked`(두 경우 모두 요청 미전송) |
| 모델 확인 | `check_model(binding, model)` | `GET /models/{model}`. 응답 `id`가 요청 모델과 같을 때만 `available`. 404→`not_found`, 403→`denied`, 401→인증 철회 |
| 모델 호출 | `stage_turn(request, turn)` → `start_job(request)` | `POST /responses`(`background: true`). 인증이 `authenticated`이고 해당 모델이 `available`일 때만 전송 |
| 관측 | `observe_job` | `GET /responses/{id}` |
| 중단 | `interrupt_job` | `POST /responses/{id}/cancel`. 수락 응답만으로는 종료로 보지 않고, `cancelled`를 관측해야 종료 |
| 교체 | `rotate_key(binding, new_secret)` | 아래 설명 참조 |
| 해제 | `sign_out(binding)` (`release_key`) | 아래 설명 참조 |

**교체(`rotate_key`)**

- 이 인스턴스에 응답 ID를 받은 미종료 작업이 있으면 `ACCOUNT_CONFLICT`로 거절한다.
- 다음 순서로 진행한다.
  1. 새 키를 `vault.put`으로 **새 참조**에 저장한다.
  2. revision+1, 새 참조, 새 fingerprint로 binding을 구성한다.
  3. 이전 참조를 `vault.delete`로 삭제한다.
  4. 인증·모델 확인을 초기화한다.
- 이전 binding의 처리:
  - 같은 인스턴스에서는 `STALE_CONNECTION`이 된다.
  - 다른 인스턴스는 이전 binding으로 vault를 조회할 때 참조가 없으므로 `AUTH_REQUIRED`(요청 미전송)를 받고, 상태가 `unauthenticated`로 바뀐다.
- 실패 처리:
  - 이전 참조 삭제 결과가 불명확하면 새 참조를 best effort로 버린다. 이전 binding은 현재 binding으로 유지하되 인증을 초기화하고 `UNKNOWN_OUTCOME`을 보고한다. 같은 이전 binding으로 다시 시도할 수 있다.
  - 새 키 저장 결과가 불명확한 경우에도 인증을 초기화하고 `UNKNOWN_OUTCOME`을 보고한다.

**해제(`sign_out`)**

- 활성 작업이 있으면 거절한다.
- `vault.delete` 후 `unauthenticated`가 된다.
- 삭제 결과가 불명확하면 `UNKNOWN_OUTCOME`을 보고한다. 이때 연결은 실행 차단 상태(released)로 남는다. `sign_out`을 다시 호출해 삭제를 재시도할 수 있으며, 이미 삭제된 참조에 대한 재호출은 성공한다(멱등).
- **vendor 쪽 키 폐기는 수행하지 않는다.** 사용자가 공급자 콘솔에서 처리해야 한다.

`begin_sign_in`은 API 키 경로에 해당하지 않으므로 `UNSUPPORTED`다. AIK-011 계약상 API 키는 host sign-in이 아니며, 키 등록은 `register_key`로 별도 구현되어 있다.

`auth_status`는 네트워크를 호출하지 않고 마지막 `verify_key` 결과를 반환한다. 기본 300초(`auth_ttl_seconds`)가 지나면 `unknown`으로 낮춰 실행 가능 자원에서 제외한다. 작업 중 401을 받으면 즉시 `unauthenticated`로 바꾼다.

## 4. 공통 계약 변환

- **request/job**: vendor 응답 ID는 AIK-011 ID 문법(`_` 불허)과 맞지 않을 수 있다.
  - 그래서 `provider_job_id = "openai-response:" + sha256(id)[:48]` 별칭을 쓰고, 원래 ID는 내부 경로에만 쓴다.
  - 응답 ID는 `[A-Za-z0-9][A-Za-z0-9_-]{0,127}`만 허용한다.
  - 키를 반향한 응답 ID는 수락하지 않는다(`INVALID_RECEIPT`).
- **상태**: `queued→accepted`, `in_progress→running`, `completed→succeeded`, `failed`/`incomplete→failed`, `cancelled→cancelled`, 그 외·누락→`unknown`.
- **model**: 응답의 `model`이 있고 형식이 유효하며 키 반향이 아닐 때만 기록한다. 요청 모델로 채우지 않으며, 없으면 `None`이다.
- **usage**: `usage.input_tokens/output_tokens/total_tokens` 중 provider가 명시한 정수만 기록한다. 누락은 `None`이며, 합계를 계산해 채우지 않는다.
- **도구**:
  - 출력 항목 `function_call`(`call_id`, `name`, `arguments` JSON 문자열)을 `ToolCall`로 변환한다.
  - `arguments`가 JSON 객체가 아니면 `arguments=None, arguments_valid=False`로 둔다.
  - `call_id`나 `name`이 키를 반향하면 그 호출을 `function_call.invalid`로 처리한다.
  - 다음 turn의 `ToolResult`는 `function_call_output` 입력 항목으로, `previous_response_id`와 함께 전송한다.
  - 처리하지 않은 항목 종류는 `unhandled_item_types`에 남긴다.
- **도구 실행 자체**는 이 adapter가 하지 않는다. 실행·승인·반복 한도는 AIK-035의 책임이다.

## 5. 비밀 처리

- binding·runtime DB·이벤트·결과에는 참조와 slot fingerprint만 둔다.
  - fingerprint는 `connection_id·credential_ref·revision`의 SHA-256이며 **비밀에서 유도하지 않는다**.
  - 따라서 키가 실제로 어느 vendor 계정에 속하는지는 관측하지 않는다(검증된 계정 식별 엔드포인트가 없음).
- 평문 키는 요청마다 `vault.resolve`로 얻는다. Authorization 헤더 구성과 같은 호출의 응답 반향 검사에만 사용하며, 저장·로그·반환하지 않는다.
- transport 예외는 내용을 버린다. 고정된 `AdapterFailure`를 `except` 블록 밖에서 올리므로 `__context__`로도 연결되지 않는다.
- 응답 텍스트·도구 인자에 키(또는 redactor가 아는 인코딩)가 반향되면 마스킹하고 `secret_echo_redacted=True`로 표시한다.
  - 응답 ID, `model`, `status`, `call_id`, 도구 `name` 같은 식별자 필드는 마스킹하면 의미가 깨진다. 그래서 반향이 있으면 값을 버린다(응답 ID는 수신 거절).
  - 64KiB를 넘는 텍스트는 원문 일치만 검사할 수 있으며, 일치하면 텍스트를 보류한다.
- vendor 오류 본문 원문은 보존하지 않는다. `error.code`/`error.type` 중 안전한 식별자만 `fault_from_provider`에 전달한다.
- Python `str` 사본은 지울 수 없으므로 메모리 잔존 방지는 best effort다.

## 6. 실패 매핑

| 상황 | 결과 |
|---|---|
| 401 | `AUTH_REQUIRED`, 인증 철회·모델 광고 제거 |
| 403 | `fault_from_provider` 매핑(문서상 `ACCESS_DENIED` 기대). 호출 시 해당 모델을 `denied`로 표시 |
| 404 | `fault_from_provider` 매핑. 호출 시 해당 모델을 `not_found`로 표시 |
| 405/501 | `fault_from_provider` 매핑. **모델 표시는 바꾸지 않음** |
| 429 | `RATE_LIMITED`. 초 단위 `Retry-After` 값을 `retry_after_ms`로 변환 |
| 시작·중단의 5xx/408, transport 예외 | `UNKNOWN_OUTCOME`(outcome `unknown`) → `GuardedAdapter`가 호스트를 fence |
| 2xx인데 본문 해석 불가·ID 불일치·ID 키 반향 | `INVALID_RECEIPT`(outcome `unknown`) |
| vault 참조 없음/접근 거부 | 요청 미전송, `AUTH_REQUIRED`/`ACCESS_DENIED` |
| 교체 중 vault 결과 불명(새 키 저장 또는 이전 참조 삭제) | `UNKNOWN_OUTCOME`, 이전 binding 유지, 인증 초기화(재확인 필요) |
| 해제 중 vault 결과 불명 | `UNKNOWN_OUTCOME`, 실행 차단, `sign_out` 재시도 가능 |

403/404/405/501의 fault 종류는 `aik_provider.fault_from_provider` 구현에 따른다. 이 문서는 그 구현을 다시 검증하지 않았다.

## 7. 자원 광고와 과금 구분

`executable_models(binding)`은 인증이 유효(`authenticated`, TTL 이내)하고 `check_model`이 `available`을 확인한 모델만 반환한다. 잘못된 키·권한·모델은 광고하지 않는다.

`resource_view`는 `billing_basis="api_key_metered"`, `subscription_quota="not_applicable"`을 고정 표기한다. `Usage.billing_basis`도 API 과금 외 값을 거부하므로 구독 잔여량과 섞이지 않는다.

## 8. 구현 상태와 qualification 분리

- `describe()`: `implementation_state="implemented"`, `qualification_status="NOT_RUN"`이다(AIK-011 계약상 다른 값 불가). `isolation`은 실측 근거가 없어 `unknown`이다.
- `status()`: `AdapterStatus(implementation_state, route_qualification="NOT_RUN", live_vendor_round_trip="NOT_RUN", endpoint_assumptions="UNVERIFIED")`. `qualified`를 넣으면 생성이 거절된다.
- `TurnResult.evidence_kind`는 transport의 자기 신고 `kind`를 그대로 복사하지 않는다.
  - 정확히 `UrllibTransport`이면서 base URL이 기본값(`DEFAULT_BASE_URL`)일 때만 `network`다.
  - `kind="fixture"`를 신고한 transport는 `fixture`, 그 밖의 경우(하위 클래스, 로컬 mock URL, 사용자 정의 transport)는 모두 `unspecified`다.
  - 이 값은 증거 라벨일 뿐이다. `network`도 실제 vendor 도달 증명(환경 프록시 경유 가능)이나 qualification이 아니다.
- 실제 지원 판정은 단계별 근거로만 한다: AIK-022(실제 인증), AIK-082(사전 probe), AIK-086/044(runtime·도구 실행).

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
| 오류 의미 | 401/403/404/429/5xx의 의미, 초 단위 `Retry-After`, 429 시 미실행 |

시험의 응답 본문은 이 가정에 맞춘 **합성 protocol fixture**이며, vendor 응답의 사본이 아니다.

## 10. 검증

```powershell
python -m unittest discover -s tests -p 'test_adapter_openai.py' -v
```

| 항목 | 상태 |
|---|---|
| 합성 fake transport·`FakeVaultBackend`(장애 주입 하위 클래스 포함)로 등록·재연결 거절·인증 확인(vault 접근 거부 포함)·모델 확인·호출·관측(401 포함)·중단·교체(다중 인스턴스, vault 실패)·해제(재시도) 검사 | 단위 시험 작성. 이 문서 작성 시점의 실행 결과 기록 없음 |
| 성공/실패 fixture, `GuardedAdapter` 결합, 식별자 키 반향, 상한(`MAX_STAGED`·`MAX_MODELS`·출력 항목·텍스트 길이), 부분 usage, 평문 비검출 | 위와 같음 |
| 실제 vendor 왕복(`UrllibTransport`) | **NOT_RUN** |
| 실제 키 인증(AIK-022), runtime/도구 실행(AIK-082/086/044) | **NOT_RUN** |
| Windows DPAPI vault와 결합한 실행 | **NOT_RUN** (Linux 통과는 Windows 지원 증거가 아님) |

## 11. 한계

- 상태(연결·작업·중복 기록)는 프로세스 메모리에만 있다. 재시작 뒤 영속 reconciliation은 AIK-031/033/035/038 범위다.
- adapter는 호출 중 하나의 잠금을 유지하므로, 같은 인스턴스의 요청은 직렬화된다.
- **다중 인스턴스**
  - 교체로 이전 참조가 삭제되면, 다른 인스턴스의 이전 binding은 다음 vault 조회(인증 확인·호출·관측) 때 fail-closed된다.
  - 다만 그 인스턴스의 `auth_status`/`executable_models` 캐시는 TTL 또는 다음 조회 전까지 이전 광고를 유지할 수 있다. 실행은 vault 조회 단계에서 차단된다.
  - 활성 작업 검사는 인스턴스별이다. 다른 인스턴스의 미종료 작업은 교체·해제를 막지 않는다. 그 작업의 관측·중단은 이전 참조 삭제 뒤 `AUTH_REQUIRED`로 실패한다.
- 결과가 불명확한 시작(5xx/transport 실패)은 응답 ID가 없어 관측·중단할 수 없으므로 활성 작업으로 세지 않는다. 교체·해제는 로컬 vault 작업이며, 이미 vendor에 전달된 요청을 멈추지 않는다. 이 경우의 차단은 `GuardedAdapter`의 호스트 fence가 맡는다.
- 교체 중 이전 참조 삭제가 불명확하고 새 참조 폐기마저 실패하면, 새 키가 vault에 남을 수 있다(관리 도구 없음).
- 키 → vendor 계정 대응, 조직·프로젝트 범위, 과금 한도는 관측하지 않는다.
- 해제는 로컬 vault 삭제이며 vendor 쪽 키 폐기가 아니다.
- streaming, 파일·이미지 입력, 내장(hosted) 도구, 추론 토큰 세부 usage는 다루지 않는다.

## 구현 메모

- 엔드포인트·헤더·응답 필드·상태 문자열·오류 의미는 모두 공식 문서로 확인하지 못한 가정이며, 문서 9절에 표시했다. 실제 형식이 다르면 `_STATUS`, `_turn_from`, `_turn_payload`, 경로 문자열을 고쳐야 한다.
- 시험은 이 환경에서 실행하지 않았으므로 결과 PASS를 주장하지 않는다. `FakeVaultBackend`의 경로 검증 때문에, 임시 디렉터리 경로에 symlink가 있는 환경(예: macOS `/tmp`)에서는 setUp이 실패할 수 있다.
- 키 형식은 인쇄 가능 ASCII 12~256자로 제한했다. 헤더 주입 방지와 `OutputRedactor.add_secret`의 길이 제약을 함께 맞추기 위한 가정이다.
- `account_fingerprint`는 slot 식별값이라 vendor 계정 변경을 탐지하지 못한다. 교체 시 참조·revision·fingerprint가 모두 바뀌고 이전 참조가 삭제되는 것으로 이전 binding을 무효화한다.
- `auth_status`는 캐시 기반이다. TTL 동안 vendor 쪽 키 폐기는 다음 401 응답 전까지 반영되지 않는다.
- `isolation`은 근거가 없어 `unknown`으로 두었다. API 키 경로에서는 `GuardedAdapter`의 실행 차단 조건에 영향이 없다.
- `UrllibTransport`는 환경 변수의 프록시 설정을 따른다. 실행 근거가 없다(NOT_RUN).
