# AIK-029 — 총괄·worker 도구 권한과 실행 인가 연결 (부분 구현)

상태: **구현과 fake port 단위시험까지 진행했다.** 실제 VM 설치물의 인가·효과 권위 연동, Windows 실행, 실제 provider·broker 연동은 모두 **미검증(NOT_RUN)**이다. 이 문서와 시험 통과는 qualified나 완료 판정이 아니다. 장부(`planning/`)의 AIK-029 상태와 완료 판정도 바꾸지 않는다.

`src/aik_authority/`는 scoped tool gateway와 authorization mapper다. Python 표준 라이브러리와 아래 기존 패키지를 import해서 재사용하며, 이 패키지들은 수정하지 않는다.

- `aik_vm_bridge`: `BridgeReceipt`·`Command.AUTHORIZE`(VM 인가 결과 = 승인 근거 참조), `TaskExecutionBinding`(Attempt·lease 결속)
- `aik_profiles`: `ExecutionDecision`·`ExecutionChoice`·`rebuild_profile`(역할·workspace·binding된 도구·connection)
- `aik_broker`: `Grant`(workspace·task·lease·connection 범위)
- `aik_runtime`: 식별자 규칙, `RevisionConflict`

## 권한 계산 (기본 거부)

도구 호출은 아래 조건 모두의 **교집합** 안에서만 허용한다.

1. `ToolPolicy`의 선언 목록과 역할별 허용 목록(`director_tools`/`worker_tools`/`reviewer_tools`). 선언되지 않은 도구와 역할 목록에 없는 도구는 거절한다. 검토자 목록에는 `read_only` 도구만 넣을 수 있다.
2. 세션의 `ExecutionDecision`(status `bound`)이 binding한 도구.
3. VM 인가 결과에서 매핑한 `AuthorizationScope`의 도구·대상(target)·효과 종류·입력 revision·만료.
4. worker/검토자 세션은 총괄이 위임한 도구 부분집합.
5. 자격증명이 필요한 도구는 `Grant`의 connection 범위.

## 사용 흐름

1. 신뢰된 backend가 `set_policy(policy, expected_revision)`로 workspace 정책을 직렬화해 저장한다.
2. VM `authorize` 결과(`BridgeReceipt`)를 `map_vm_authorization(receipt, workspace_id=..., tools=..., targets=..., effect_kinds=..., expires_at=...)`로 `AuthorizationScope`에 매핑한다. 그 결과를 `register_authorization(scope, expected_revision)`로 기록한다.
   - receipt가 `AUTHORIZE`가 아니거나 `authorization_id`·`plan_revision_id`가 없으면 `AUTHORIZATION_EVIDENCE_MISSING`이다.
   - **gateway는 승인 근거를 만들지 않고, 사용자 대신 승인하지도 않는다.**
   - scope의 도구·대상·효과 종류 값은 신뢰된 backend가 VM 결과에서 옮겨 넣는다. VM payload의 scope 형식 파싱은 구현하지 않았다(NOT_RUN).
3. `open_director_session(binding=, vm_authorization_id=, authorization_revision=, decision=, grant=None)`은 총괄 세션 handle(`ToolSession`)을 반환한다. 이때 다음을 확인한다.
   - decision 역할이 director인지(`ROLE_MISMATCH`), binding이 완료됐는지(`PROFILE_NOT_BOUND`)
   - scope·decision·grant의 workspace가 일치하는지(`WORKSPACE_MISMATCH`)
   - binding의 project(`BINDING_MISMATCH`), 프로필이 고른 connection과 binding의 `connection_id`가 일치하는지(`BINDING_MISMATCH`, subject `connection_id`), plan revision(`STALE_INPUT_REVISION`)
   - grant의 task·lease·fencing token·connection(`BROKER_SCOPE_MISMATCH`). gateway는 bearer인 `grant_id`를 보관하지 않고 connection 범위만 보관한다.
   - 인가 revision·만료·lease 활성 여부
4. 총괄은 `delegate(director_session, decision=worker_decision, tools=(...))`로 worker/검토자 세션을 만든다. **기존 범위를 그대로 전달하므로 새 승인을 요구하지 않는다(완료 기준 1).** 총괄 범위를 넘는 도구는 `SCOPE_ESCALATION`(escalation=True)으로 거절하며 부여하지 않는다. 위임 세션의 connection 일치 검사는 총괄 세션에서만 수행한다. worker decision의 connection은 비교하지 않는다.
5. 세션 소유자가 `request(session, ToolCall(...))`를 호출하면 `ToolCallAuthorization`을 반환한다.

## 거부 사유 코드

검사 순서는 아래와 같고, 첫 실패가 결과가 된다.

세션 handle → 요청 형식 → workspace → 인가 revision·철회·만료 → lease → 도구(예약/선언/역할/프로필/인가/위임) → 효과 종류 → 대상 → 입력 revision → 자격증명 범위 → 효과 identity → 재전송 → VM intent

| 코드 | 의미 |
|---|---|
| `UNKNOWN_SESSION` / `SESSION_CLOSED` | 등록된 handle이 아님(위조·직렬화된 원격 요청 포함) / 닫힌 세션 |
| `TOOL_NOT_DECLARED` / `RESERVED_TOOL` | 미선언 도구 / 인가·verdict용 예약 도구 |
| `TOOL_NOT_ALLOWED_FOR_ROLE` / `TOOL_NOT_IN_PROFILE` | 역할 허용 목록 밖 / 실행 프로필 binding 밖 |
| `TOOL_NOT_IN_AUTHORIZATION` / `TARGET_OUT_OF_SCOPE` / `EFFECT_OUT_OF_SCOPE` / `SCOPE_ESCALATION` | 범위 확대. `escalation=True`이며 새 VM 인가가 필요 |
| `STALE_AUTHORIZATION_REVISION` / `STALE_INPUT_REVISION` | 인가 revision 또는 입력(plan) revision 불일치 |
| `AUTHORIZATION_EXPIRED` / `AUTHORIZATION_WITHDRAWN` / `AUTHORIZATION_UNKNOWN` | 만료 / 철회 / 미등록 |
| `WORKSPACE_MISMATCH` / `ROLE_MISMATCH` / `ROLE_CANNOT_DELEGATE` / `PROFILE_NOT_BOUND` | workspace·역할 불일치, worker/검토자의 위임 시도, 미binding 프로필 |
| `BINDING_MISMATCH` | binding과 scope의 project 불일치, 프로필 connection과 binding connection 불일치 |
| `LEASE_INACTIVE` / `CLOCK_UNAVAILABLE` | lease 비활성 / naive 시계 또는 실패한 시계 |
| `CREDENTIAL_SCOPE_MISSING` / `CONNECTION_OUT_OF_SCOPE` / `BROKER_SCOPE_MISMATCH` | broker 범위 없음·범위 밖·불일치 |
| `TOOL_CALL_CONFLICT` / `EFFECT_ID_CONFLICT` / `EFFECT_ALREADY_AUTHORIZED` | identity 재사용 충돌 |
| `EFFECT_NOT_ISSUED` | VM에는 intent가 있지만 gateway가 발행 허가를 낸 적 없는 non_idempotent 효과. 실행 허가 없음, VM 판단 필요 |
| `SEAM_UNAVAILABLE` / `VM_REJECTED` / `INTENT_UNKNOWN` / `RECEIPT_UNKNOWN` / `RECONCILIATION_REQUIRED` | VM 효과 권위 연결 관련 |

`vm_contract`는 VM 공개 계약 코드(`STALE_EXECUTION_INPUT`, `CORE_CAPABILITY_DENIED`)와의 대응을 담는다. 이 대응은 **가정**이며 설치된 VM과 대조하지 않았다(NOT_RUN).

오류 문자열에는 고정 코드와 필드명만 들어간다. 입력 값이나 원시 VM 메시지는 들어가지 않는다. VM port 예외는 검증된 코드만 추출한 뒤 `except` 블록 밖에서 새 오류를 발생시킨다. 따라서 반환된 오류의 `__cause__`와 `__context__`에 원 예외 객체가 남지 않는다.

## revision 규칙

- **도구 정책**: `set_policy`는 `expected_revision`으로 직렬화한다(`RevisionConflict`).
  - 세션은 열 때의 정책 revision을 고정하므로 **진행 중인 실행에 소급 적용하지 않는다.** 권한 추가는 새 세션부터 적용된다.
  - 권한 축소도 기존 세션에는 소급되지 않는다. 즉시 중단이 필요하면 `close_session`(위임 세션까지 연쇄 종료)이나 VM 인가 철회를 사용한다.
- **VM 인가**: `register_authorization`·`withdraw_authorization`도 revision으로 직렬화한다.
  - 이는 VM 권위의 변경이다. 매 호출마다 현재 revision과 세션에 고정된 revision을 비교하고, 다르면 `STALE_AUTHORIZATION_REVISION`이다. 위임 세션에도 같은 규칙이 적용된다.
  - VM이 범위를 넓혀 재인가해도 기존 세션의 범위는 넓어지지 않는다.
  - 철회된 인가는 재등록할 수 없다(`AUTHORIZATION_WITHDRAWN`).

## 자기 권한 부여·완료 판정 덮어쓰기 금지 (완료 기준 3)

- worker/검토자는 `ToolSession` handle만 받는다.
  - handle에는 `session_id`·`role`·`workspace_id`만 있고, scope·정책·grant·binding 객체는 없다.
  - handle의 직렬화와 복사는 거절된다.
  - 같은 ID로 새로 만든 handle이나 dict 요청은 `UNKNOWN_SESSION`이다.
- worker/검토자는 위임할 수 없다(`ROLE_CANNOT_DELEGATE`). 정책·인가 변경 API는 신뢰된 backend가 소유한 gateway 객체에만 있다.
- 다음 이름은 선언 자체가 거절된다(`RESERVED_TOOL`).
  - `authority.`·`approval.`·`verdict.`·`vm.verdict`·`vm.authorize`·`vm.final_report` 접두어
  - `.`/`:`/`/`로 나눈 구간 중 `approve`·`set_verdict`·`complete_task`·`grant` 등 예약 이름이 있는 경우
  - `._:/-`로 나눈 토큰 중 `verdict`·`authoriz`·`authority`·`approv`로 시작하는 토큰이 있는 경우
- **이 이름 필터는 best-effort다.** 판정·인가 덮어쓰기를 실제로 막는 것은 VM 권위이며, gateway는 그 판정을 만들거나 쓰지 않는다.

## tool_call/effect identity와 효과 종류 (완료 기준 4)

- request digest는 정규 JSON을 SHA-256으로 해시한 값이다. 정규 JSON에는 다음 항목이 들어간다.
  - 도구·효과 종류·effect_id·대상·입력 revision·workspace·connection·인수 SHA-256
  - **vm_project/task/attempt, vm_authorization_id·인가 revision, lease_id·fencing token**
- tool_call_id는 digest에 포함하지 않는다. 인수 원문은 저장·감사·intent 어디에도 넣지 않는다.
- `ToolCallAuthorization.status`는 항상 `authorized`다. 이는 gateway의 발행 허가일 뿐 **효과 완료나 verdict가 아니다.**

| 효과 종류 | effect_id | VM intent | 같은 tool_call_id 재요청 | 같은 effect_id 다른 tool_call |
|---|---|---|---|---|
| `read_only` | 금지 | 없음 | 저장된 결정 반환 | 해당 없음 |
| `idempotent` | 필수 | 최초 1회 | 저장된 결정 반환(intent 재기록 없음) | digest가 같으면 허용, 다르면 `EFFECT_ID_CONFLICT` |
| `non_idempotent` | 필수 | 최초 1회 | `EFFECT_ALREADY_AUTHORIZED`(재발행 금지) | `EFFECT_ID_CONFLICT` |

재요청도 인가 revision·만료·lease 검사를 다시 통과해야 한다. lease fencing이나 인가 revision이 바뀌면 digest가 달라진다. 따라서 같은 effect_id는 `EFFECT_ID_CONFLICT`로 막힌다(fail-closed).

### 효과 journal port (`VmEffectAuthorityPort`)

`VmEffectAuthorityPort`(`record_intent`, `record_receipt`, `read_effect`)는 처음에 VM 공개 확장으로 가정했다. 그러나 VM 설계(vibemarshal 32bb0f9 코드와 설계 문서)에는 tool call 단위 intent API나 `read_effect` 조회가 없다. VM은 로컬 도구 효과를 diff/hash evidence와 governance gate의 C0/C1 스냅샷으로 검증한다. 그래서 이 port는 AIK 소유 durable journal인 `AikEffectJournal`(아래 절)이 구현한다. port가 없으면 효과가 있는 도구는 `SEAM_UNAVAILABLE`로 거절되고 `read_only`만 동작한다. 아래 목록의 "VM"은 이 port 구현을 뜻한다.

- **intent 기록**: VM은 effect_id·digest를 그대로 echo해야 한다.
  - 안전한 VM 코드가 있는 예외는 `VM_REJECTED`이며 `vm_code`를 보존한다.
  - `UNKNOWN` 토큰 코드, 코드 없는 예외, 잘못된 echo는 `INTENT_UNKNOWN`이다. 이때 해당 effect는 미해결로 표시된다.
- **receipt 전달**: `forward_receipt(session, tool_call_id, status)`는 실행자의 관측(`succeeded/failed/cancelled/unknown`)을 VM에 전달한다.
  - 반환되는 `vm_state`는 VM 응답 문자열을 그대로 담는다. gateway는 이를 완료로 해석하지 않으며, 로컬 상태는 `receipt_forwarded`다.
  - `unknown` 보고, 인식할 수 없는 응답, 코드 없는 예외는 미해결(`outcome_unknown`)이다.
- **미해결 effect**: `RECONCILIATION_REQUIRED`로 막힌다. `reconcile(effect_id)`가 VM의 `read_effect`에서 인식 가능한 상태를 받을 때만 해제된다.
- **`read_effect`가 None(VM에 intent 없음)을 반환할 때**: gateway가 그 effect에 발행 허가를 낸 적이 있는지에 따라 처리가 다르다.
  - **발행 허가를 낸 적이 없는 경우**(intent 불명확으로 끝난 경우)에만 로컬 기록을 지우고 새 intent를 허용한다.
  - 이미 발행 허가를 낸 경우(intent echo 성공)에는 None이 로컬 근거와 모순된다. 이때는 VM 재시작, 다른 instance 조회, adapter 결함 등을 가정한다. effect를 미해결로 유지하고 `RECONCILIATION_REQUIRED`를 발생시키며, 감사에 `effect_reconcile_blocked`를 남긴다. non_idempotent 효과의 이중 허가를 막기 위한 처리다.
- **intent 불명확 후 VM에 intent가 남은 경우**: `INTENT_UNKNOWN` 뒤 `reconcile`이 VM에 intent가 있음을 확인한 경우다. 로컬 상태는 `vm:<state>`가 되지만 실행 허가는 발행되지 않은 상태다.
  - non_idempotent 효과의 재요청은 `EFFECT_NOT_ISSUED`로 거절된다.
  - 이 intent의 취소나 실행 여부는 **VM 쪽 판단**이다. gateway에는 취소 API가 없고, receipt 전달 대상도 아니다(`TOOL_CALL_UNKNOWN`).
  - 이후 VM이 intent를 없애 `read_effect`가 None을 반환하면 로컬 기록을 지우고 새 요청을 허용한다.
- 취소·unknown의 판단은 VM 상태를 따른다. 자동 재시도는 하지 않는다.

### AIK 효과 journal과 VM 외부 효과 제출 (`effect_journal.py`)

VM 설계는 효과 기록을 두 갈래로 나눈다. AIK도 그 경계를 따른다.

- **tool call 효과: `AikEffectJournal(path, *, binding_reader, attempt_reader=None)`**
  - `VmEffectAuthorityPort`를 구현하는 stdlib SQLite journal이다. 전용 application ID(`AIKE`)를 쓰고, 개발 원장·Git 내부 경로를 거절하는 `aik_runtime.paths` 규칙을 따른다. 재시작 뒤에도 같은 상태를 읽는다.
  - `record_intent`는 다음 조건을 모두 만족할 때만 기록한다.
    - `binding_reader(vm_attempt_id)`가 확인된 execution binding(`{"binding_digest"}`)을 돌려준다.
    - 그 digest가 intent의 `binding_digest`와 같다.
    - `attempt_reader`가 주어졌다면 Attempt 상태가 `running`이다.

    hosted 경로에서는 `ExecutionBindingStore.read_execution_binding`이 reader다. 조건을 어기면 `code`가 있는 `EffectJournalError`(`BINDING_NOT_CONFIRMED`, `BINDING_MISMATCH`, `ATTEMPT_NOT_RUNNING` 등)를 낸다. gateway는 이를 `VM_REJECTED`로 처리한다.
  - 같은 intent·receipt를 다시 기록하면 같은 결과를 돌려준다(멱등). 내용이 다르면 `INTENT_CONFLICT`/`RECEIPT_CONFLICT`로 거절한다.
  - `read_effect`는 `intent_recorded` 또는 receipt 상태(`succeeded/failed/cancelled/unknown`)를 돌려준다.
  - journal 상태는 **AIK 관측**이다. VM 권위, VM evidence, 완료 판정이 아니다. Task 완료는 VM Core가 자기 evidence로 판정한다.
- **외부 효과: `ExternalEffectSubmitter(service, *, approved_identities)`**
  - VM `EngineService.record_effect_receipt(...)`에 Task 단위 typed receipt(`ExternalEffectReceipt`, `EffectIdentity`)를 그대로 넘긴다.
    - `EffectIdentity`는 provider, system, target, account, operation, scope, `idempotency_key`(16자 이상), `checkpoint_policy`(`none`/`before_irreversible`/`always`)로 이루어진다.
  - 그 Task에 승인된 identity가 아니면 제출 전에 거절한다.
  - 같은 `(provider, system, provider_operation_id)`를 다른 Attempt로 제출하려 하면 로컬에서도 거절한다.
  - VM 거절 코드(`EFFECT_ADAPTER_OPERATION_REPLAY_CONFLICT`, `EFFECT_ADAPTER_RECEIPT_CONFLICT` 등)는 그대로 보존한다. 코드 없는 예외나 이상 응답은 결과 불명으로 분류하고 자동 재시도하지 않는다.
  - 제출 결과는 `target_reobservation_required` 상태다. 외부 효과는 VM이 대상 재관측(`ExternalValidationObservation`)을 받은 뒤에만 확정된다. provider terminal이나 receipt만으로는 완료가 아니다.
- 1.0 VM 설계에서는 내부 파일·명령 효과와 외부 효과를 같은 Task에 섞지 않는다. 외부 효과 Task는 별도 Task와 dependency로 둔다.
- **남은 한계**
  - VM `EngineApplication`에는 receipt 제출 wrapper가 없어 `EngineService`를 주입해야 한다.
  - 실제 VM service 연결, 설치형 VM, Windows, 실제 외부 서비스 호출은 NOT_RUN이다. 시험(`tests/test_effect_journal.py`)은 fake service·reader로 VM 설계 계약을 흉내 낸다.

## 메모리·감사 한계

- 감사 기록은 최대 `AUDIT_CAPACITY`(4096)개다. 초과하면 가장 오래된 기록부터 삭제된다. 삭제된 건수는 `gateway.audit_dropped`로 확인할 수 있으며, 신호 없이 유실되지는 않는다. 장기 보존이 필요하면 호출자가 주기적으로 `audit_records()`를 수집해야 한다.
- `close_session`은 닫힌 세션(위임 세션 포함)의 `read_only` 결정을 정리한다. 효과가 있는 결정과 effect 기록은 receipt 전달과 identity 보호를 위해 프로세스가 종료될 때까지 유지되며, 그 용량 상한은 없다.

## 시험

```bash
python -m unittest discover -s tests -p 'test_authority.py' -v
```

합성 ID·fake 시계·fake lease checker·fake VM effect port만 사용한다.

- **거부 경로**: 미허용 도구, 만료 인가, 다른 workspace, 역할 불일치, connection 불일치, lease 비활성, 범위 확대, stale revision(위임 세션 포함), 철회 인가와 재등록, 위조 handle, 예약 도구, broker 범위
- **허용 규칙**: 동일 인가 재사용, 정책 비소급, 효과 종류별 규칙(worker non_idempotent 포함), 동시 요청 시 단일 발행
- **불명확·VM 연결**:
  - intent echo 불일치, `UNKNOWN` 토큰 코드, receipt 예외
  - 발행 후 reconcile None 차단, 미발행 intent의 `EFFECT_NOT_ISSUED`
  - receipt 비판정
- **정보 노출·용량**: 오류의 `__context__` 비보존, 감사 overflow 카운트, 닫힌 세션의 read_only 정리, 감사 기록의 인수 비노출

이 문서 작성 시점의 실행 결과 기록은 없다.

## 한계·미검증(NOT_RUN)

- 다음 항목은 가정이며 설치형 VM으로 검증하지 않았다: 실제 VM `authorize` 결과의 scope 형식, VM 효과 권위 extension, 공개 계약 코드 대응.
- `Grant` 범위는 세션을 열 때 한 번만 확인한다. 세션 중에는 broker grant의 만료·철회·revision을 다시 확인하지 않는다(`Grant` 시각 필드의 의미를 확정하지 못했다). grant가 무효화됐을 때 즉시 차단하려면 `close_session` 또는 VM 인가 철회가 필요하다. 요청 시점의 grant 유효성 재검사는 미구현이다.
- 정책·인가 revision, 세션, 재전송·미해결 guard, 감사 기록은 프로세스 메모리에만 있다. 재시작 내구성이나 exactly-once를 보장하지 않는다.
- gateway 잠금 안에서 VM port를 호출한다. port가 멈추면 다른 요청도 대기하며, port 기한은 adapter의 책임이다.
- lease 판정은 주입된 checker(예: `aik_broker.RuntimeLeaseChecker`)에 의존한다. fencing 전이는 AIK-033/034 범위다.
- 같은 프로세스 안에서 `ToolSession`을 넘겨받은 코드는 그 세션으로 행동할 수 있다. 원격 peer는 handle을 직렬화할 수 없다는 점 외에 별도 처리가 없다. OS principal 격리는 범위 밖이다.
- AIK-004 IPC, 실제 Electron main, 제품 GUI는 연결하지 않았다. Linux 통과는 개발 검증이며 Windows 지원 증거가 아니다.
