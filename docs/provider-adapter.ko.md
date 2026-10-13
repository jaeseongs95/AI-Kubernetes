# AIK-011 — 인증·실행 adapter 공통 계약 (독립 구현·회귀 시험)

## 목적과 범위

AIK-011은 공급자별 **API 키**와 공식 **구독 호스트 세션**을 동일한 상위 인터페이스로 다루되, 인증 근거·작업 실행 능력·OS 계정 문맥·실제 운영 적합성은 **서로 다른 항목**으로 관리한다. 이 문서의 `src/aik_provider/`는 표준 라이브러리만 사용한 Python 공통 계약 및 신뢰 경계이다. 실제 OpenAI/Anthropic 호출, Codex/Claude Code 호스트 인증 및 VM/AGS 제품 통합은 이 커밋의 구현 범위에 포함하지 않는다.

관련 개발 원장 정본은 `planning/backlog.sqlite3`의 **AIK-011**이다. 기존 선행 작업 AIK-001·AIK-010 중 **AIK-010은 미완료**이므로 원장상의 상태를 임의로 완료 처리하지 않는다. 기존 완료 기준 및 후행 의존성도 변경하지 않는다.

## 버전형 산출물

- `src/aik_provider/contracts.py`: Python 3.14/표준 라이브러리의 immutable `ConnectionBinding`, `AdapterCapabilities`, `AuthObservation`, `JobRequest`, `JobReceipt`, `ProviderFault`, `ProviderAdapter` Protocol.
- `src/aik_provider/controller.py`: 안전한 ID·계정 바인딩·revision 검증, 호스트 인증 문맥 충돌 감지, 원래 연결로 observe/interrupt, 중복 request ID 및 불명확한 외부 결과 재실행 차단.
- `contracts/provider-adapter/v1.schema.json`: 이력/IPC 표현을 위한 **v1 비밀 없는** capability/auth observation/job receipt/fault 공통 JSON 스키마. 원문 provider 응답·token·RAW 프롬프트·VM verdict를 포함하지 않는다.
- `tests/test_provider_contract.py`: synthetic fake adapter 회귀·반례 검증.

`ProviderAdapter`는 `describe / auth_status / begin_sign_in / sign_out / start_job / observe_job / interrupt_job` 메서드를 갖는다. 인증의 `authenticated`는 실행 작업의 `job_start=supported` 또는 `job_observe=supported`를 뜻하지 않는다. `supported/unsupported/unknown`은 기능 공개 상태로서 **qualified**와 다르며, 현재 adapter가 직접 보고할 수 있는 `qualification_status`는 `NOT_RUN`뿐이다.

## 연결과 계정의 안전 경계

`ConnectionBinding`은 `provider, runtime, connection_id, revision, auth_kind, credential_ref, account_fingerprint`만 보유한다. `auth_kind=api_key`는 `secret://` 참조, `auth_kind=native_subscription`은 `host-session://` 참조를 강제하며 서로 변환하지 않는다. `account_fingerprint`는 관측 계정의 비민감·고정 길이 식별 다이제스트다. 실제 인증 토큰/계정 암호는 포함하면 안 된다.

시작 전에는 기존 연결 revision과 실제 인증 상태 및 관측 계정 fingerprint를 일치시켜야 한다. `account_isolation=single_active`인 호스트에서는 이미 시작한 미종료 작업이 있을 때 **다른 connection/revision/fingerprint의 동시 실행과 계정 전환을 차단**한다. `unknown`인 native 호스트 문맥에서는 작업 시작을 거절한다. `isolated`가 실증된 경로만 독립 계정의 동시 실행 후보로 처리한다. 이전 작업의 observe/interrupt는 현재 GUI에서 선택한 신규 연결로 바꾸지 않고 원래 request·binding을 사용한다.

`GuardedAdapter.start_job`에는 **외부로부터 명시적으로 주입한 `authorize_dispatch(request)` 콜백이 없으면 실행을 거절**하는 기본값을 적용했다. 이 콜백은 **기존 VM/AGS 인가·AIK lease/fencing 검증**을 제품 작업에서 연결하기 위한 의존성 주입점이다. 회귀 시험의 `lambda request: True`는 허가된 synthetic fixture이며 운영 인가로 대체할 수 없다. `ProviderAdapter`와 `GuardedAdapter`의 성공 응답 자체는 VM의 Goal/Task/Attempt 완료 판정이 아니다.

작업 실행 뒤 응답 수신이 끊기거나 request/account/job identity가 어긋나면 `UNKNOWN_OUTCOME` 또는 `INVALID_RECEIPT`로 기록하고, 해당 호스트의 신규 dispatch를 보수적으로 차단한다. `interrupt_job`에 대한 요청 수락도 `cancelled`라는 실제 관측 전에는 작업 종료로 간주하지 않는다. 표준 `ProviderFault`는 고정 오류 종류와 제한된 vendor code, HTTP 상태, retry-after만 보존하며 원시 provider 오류 문자열·응답 본문을 그대로 전달하지 않는다.

**중요한 제한:** 여기의 중복 요청·계정 충돌 기록은 현재 Python 프로세스 메모리에만 존재한다. 프로세스 재시작·외부에서 직접 수행한 같은 호스트 로그인/작업·계정 충돌을 완전히 통제하지 못한다. 재시작 뒤 같은 계정/route/lease를 안전하게 대조하고 불명확한 외부 효과를 회복하는 **영속 binding 및 reconciliation은 AIK-031/033/035/038**의 별도 완료 조건이다. 본 계약만 설치했다고 안전한 멀티계정 운영이 증명되는 것은 아니다.

## 검증·소유권

```bash
python -m unittest discover -s tests -p test_provider_contract.py -v
```

합성 fake adapter로 API/구독 자격증명 분리, auth vs execute/observe/interrupt, 알 수 없는 account isolation, 동일 호스트의 계정 변경·revision 경합, 중단 ACK의 비종료, 결과 불명/재시도 차단, vendor 429와 retry-after/unknown, 안전한 ID·데이터 타입·v1 JSON enum의 정합성, 지원 matrix의 미검증 상태 유지 등을 검사한다. CI는 Windows Server 2022와 Ubuntu 24.04에서 동일 suite를 실행한다.

**미검증:** 공식 호스트 프로토콜·실계정 인증/전환·실제 API·Windows 11 일반 사용자 설치본·도구 effect/VM Attempt 권위/AGS A2 gate·복구형 영속 routing·실제 운영 적합성. 따라서 AIK-011의 자체 구현 회귀가 PASS해도 AIK-010 미완료 선행 관계를 우회하거나 AIK-022/082/086의 live 증거로 전환하지 않는다.

지원 matrix `contracts/upstream/support-matrix.json`은 revision 1과 기존 `not_implemented/NOT_RUN`의 정본 관측을 그대로 둔다. 추후 실제 provider adapter 구현/실행 증거가 등록되면 올바른 결정 및 검증 절차를 거쳐서만 지원 상태를 갱신한다.
