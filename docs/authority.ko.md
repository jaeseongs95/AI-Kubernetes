# AIK-029 — 총괄·worker 도구 권한과 실행 인가 연결 (부분 구현)

상태: **구현·fake port 단위시험까지.** 실제 VM 설치물의 인가·효과 권위 연동, Windows 실행, 실제 provider·broker 연동은 모두 **미검증(NOT_RUN)**이다. 이 문서와 시험 통과는 qualified·완료 판정이 아니며, 장부(`planning/`)의 AIK-029 상태와 완료 판정은 바꾸지 않는다.

`src/aik_authority/`는 scoped tool gateway와 authorization mapper다. Python 표준 라이브러리와 다음 기존 패키지를 import해서 재사용하며 수정하지 않는다.

- `aik_vm_bridge`: `BridgeReceipt`·`Command.AUTHORIZE`(VM 인가 결과 = 승인 근거 참조), `TaskExecutionBinding`(Attempt·lease 결속)
- `aik_profiles`: `ExecutionDecision`·`ExecutionChoice`·`rebuild_profile`(역할·workspace·binding된 도구)
- `aik_broker`: `Grant`(workspace·task·lease·connection 범위)
- `aik_runtime`: 식별자 규칙, `RevisionConflict`

## 권한 계산 (기본 거부)

도구 호출은 다음 모두의 **교집합** 안에서만 허용한다.

1. `ToolPolicy` 선언 목록과 역할별 허용 목록(`director_tools`/`worker_tools`/`reviewer_tools`). 선언되지 않은 도구, 역할 목록에 없는 도구는 거절한다. 검토자 목록에는 `read_only` 도구만 넣을 수 있다.
2. 세션의 `ExecutionDecision`(status `bound`)이 binding한 도구.
3. VM 인가 결과에서 매핑한 `AuthorizationScope`의 도구·대상(target)·효과 종류·입력 revision·만료.
4. worker/검토자 세션은 총괄이 위임한 도구 부분집합.
5. 자격증명이 필요한 도구는 `Grant`의 connection 범위.

## 사용 흐름

1. 신뢰된 backend가 `set_policy(policy, expected_revision)`로 workspace 정책을 직렬화해 저장한다.
2. VM `authorize` 결과(`BridgeReceipt`)를 `map_vm_authorization(receipt, workspace_id=..., tools=..., targets=..., effect_kinds=..., expires_at=...)`로 `AuthorizationScope`에 매핑하고 `register_authorization(scope, expected_revision)`로 기록한다. receipt가 `AUTHORIZE`가 아니거나 `authorization_id`·`plan_revision_id`가 없으면 `AUTHORIZATION_EVIDENCE_MISSING`이다. **gateway는 승인 근거를 만들거나 사용자 대신 승인하지 않는다.** scope의 도구·대상·효과 종류 값은 신뢰된 backend가 VM 결과에서 옮긴다(VM payload의 scope 형식 파싱은 미구현·NOT_RUN).
3. `open_director_session(binding=, vm_authorization_id=, authorization_revision=, decision=, grant=None)`이 총괄 세션 handle(`ToolSession`)을 반환한다. 다음을 확인한다.
   - decision 역할이 director(`ROLE_MISMATCH`), binding 완료(`PROFILE_NOT_BOUND`)
   - scope·decision·grant의 workspace 일치(`WORKSPACE_MISMATCH`)
   - binding의 project(`BINDING_MISMATCH`)·plan revision(`STALE_INPUT_REVISION`)
   - grant의 task·lease·fencing token·connection(`BROKER_SCOPE_MISMATCH`). gateway는 bearer인 `grant_id`를 보관하지 않고 connection 범위만 보관한다.
   - 인가 revision·만료·lease 활성
4. 총괄은 `delegate(director_session, decision=worker_decision, tools=(...))`로 worker/검토자 세션을 만든다. **기존 범위를 그대로 전달하므로 새 승인을 요구하지 않는다(완료 기준 1).** 총괄 범위를 넘는 도구는 `SCOPE_ESCALATION`(escalation=True)으로 거절하며 부여하지 않는다.
5. 세션 소유자가 `request(session, ToolCall(...))`를 호출하면 `ToolCallAuthorization`이 반환된다.

## 거부 사유 코드

검사 순서(첫 실패가 결과): 세션 handle → 요청 형식 → workspace → 인가 revision·철회·만료 → lease → 도구(예약/선언/역할/프로필/인가/위임) → 효과 종류 → 대상 → 입력 revision → 자격증명 범위 → 효과 identity → 재전송 → VM intent.

| 코드 | 의미 |
|---|---|
| `UNKNOWN_SESSION` / `SESSION_CLOSED` | 등록된 handle이 아님(위조·직렬화된 원격 요청 포함) / 닫힌 세션 |
| `TOOL_NOT_DECLARED` / `RESERVED_TOOL` | 미선언 도구 / 인가·verdict용 예약 도구 |
| `TOOL_NOT_ALLOWED_FOR_ROLE` / `TOOL_NOT_IN_PROFILE` | 역할 허용 목록 밖 / 실행 프로필 binding 밖 |
| `TOOL_NOT_IN_AUTHORIZATION` / `TARGET_OUT_OF_SCOPE` / `EFFECT_OUT_OF_SCOPE` / `SCOPE_ESCALATION` | 범위 확대. `escalation=True`, 새 VM 인가 필요 |
| `STALE_AUTHORIZATION_REVISION` / `STALE_INPUT_REVISION` | 인가 revision 또는 입력(plan) revision 불일치 |
| `AUTHORIZATION_EXPIRED` / `AUTHORIZATION_WITHDRAWN` / `AUTHORIZATION_UNKNOWN` | 만료 / 철회 / 미등록 |
| `WORKSPACE_MISMATCH` / `ROLE_MISMATCH` / `ROLE_CANNOT_DELEGATE` / `PROFILE_NOT_BOUND` | workspace·역할 불일치, worker/검토자의 위임 시도, 미binding 프로필 |
| `LEASE_INACTIVE` / `CLOCK_UNAVAILABLE` | lease 비활성 / naive·실패 시계 |
| `CREDENTIAL_SCOPE_MISSING` / `CONNECTION_OUT_OF_SCOPE` / `BROKER_SCOPE_MISMATCH` | broker 범위 없음·밖·불일치 |
| `TOOL_CALL_CONFLICT` / `EFFECT_ID_CONFLICT` / `EFFECT_ALREADY_AUTHORIZED` | identity 재사용 충돌 |
| `SEAM_UNAVAILABLE` / `VM_REJECTED` / `INTENT_UNKNOWN` / `RECEIPT_UNKNOWN` / `RECONCILIATION_REQUIRED` | VM 효과 권위 연결 관련 |

`vm_contract`는 VM 공개 계약 코드와의 대응(`STALE_EXECUTION_INPUT`, `CORE_CAPABILITY_DENIED`)을 담는 **가정**이며 설치된 VM과 대조하지 않았다(NOT_RUN). 오류 문자열에는 고정 코드와 필드명만 들어가고 입력 값·원시 VM 메시지는 들어가지 않는다.

## revision 규칙

- **도구 정책**: `set_policy`는 `expected_revision`으로 직렬화한다(`RevisionConflict`). 세션은 열 때의 정책 revision을 고정하므로 **진행 중 실행에 소급 적용하지 않는다.** 권한 추가는 새 세션부터 적용된다. 권한 축소도 기존 세션에는 소급되지 않으므로, 즉시 중단이 필요하면 `close_session`(위임 세션까지 연쇄 종료) 또는 VM 인가 철회를 사용한다.
- **VM 인가**: `register_authorization`·`withdraw_authorization`도 revision으로 직렬화한다. 이는 VM 권위의 변경이므로 매 호출마다 현재 revision과 세션의 고정 revision을 비교하고, 다르면 `STALE_AUTHORIZATION_REVISION`이다. VM이 범위를 넓혀 재인가해도 기존 세션 범위는 넓어지지 않는다.

## 자기 권한 부여·완료 판정 덮어쓰기 금지 (완료 기준 3)

- worker/검토자는 `ToolSession` handle만 받는다. handle에는 `session_id`·`role`·`workspace_id`만 있고 scope·정책·grant·binding 객체가 없으며 직렬화·복사가 거절된다. 같은 ID로 새로 만든 handle이나 dict 요청은 `UNKNOWN_SESSION`이다.
- worker/검토자는 위임할 수 없다(`ROLE_CANNOT_DELEGATE`). 정책·인가 변경 API는 신뢰된 backend가 소유한 gateway 객체에만 있다.
- `authority.`·`approval.`·`verdict.`·`vm.verdict`·`vm.authorize`·`vm.final_report` 접두어와 `approve`·`set_verdict` 등은 선언 자체가 거절된다(`RESERVED_TOOL`).

## tool_call/effect identity와 효과 종류 (완료 기준 4)

- request digest는 도구·효과 종류·effect_id·대상·입력 revision·workspace·connection·인수 SHA-256과 **vm_project/task/attempt, vm_authorization_id·인가 revision, lease_id·fencing token**을 정규 JSON으로 묶은 SHA-256이다. tool_call_id는 digest에 포함하지 않는다. 인수 원문은 저장·감사·intent에 넣지 않는다.
- `ToolCallAuthorization.status`는 항상 `authorized`다. 이는 gateway의 발행 허가일 뿐 **효과 완료나 verdict가 아니다.**

| 효과 종류 | effect_id | VM intent | 같은 tool_call_id 재요청 | 같은 effect_id 다른 tool_call |
|---|---|---|---|---|
| `read_only` | 금지 | 없음 | 저장된 결정 반환 | 해당 없음 |
| `idempotent` | 필수 | 최초 1회 | 저장된 결정 반환(intent 재기록 없음) | digest가 같으면 허용, 다르면 `EFFECT_ID_CONFLICT` |
| `non_idempotent` | 필수 | 최초 1회 | `EFFECT_ALREADY_AUTHORIZED`(재발행 금지) | `EFFECT_ID_CONFLICT` |

재요청도 인가 revision·만료·lease 검사를 다시 통과해야 한다. lease fencing이나 인가 revision이 바뀌면 digest가 달라지므로 같은 effect_id는 `EFFECT_ID_CONFLICT`로 막힌다(fail-closed).

### VM 효과 권위 연결 (가정된 extension)

`VmEffectAuthorityPort`(`record_intent`, `record_receipt`, `read_effect`)는 VM 공개 권위 계약을 감싸는 **upstream extension 가정**이며 현재 고정된 VM 공개 표면에 없다. port가 없으면 효과가 있는 도구는 `SEAM_UNAVAILABLE`로 거절되고 `read_only`만 동작한다.

- intent가 effect_id·digest를 그대로 echo해야 한다. 안전한 VM 코드가 있는 예외는 `VM_REJECTED`(`vm_code` 보존), `UNKNOWN` 토큰 코드·코드 없는 예외·잘못된 echo는 `INTENT_UNKNOWN`이며 그 effect는 미해결로 표시된다.
- `forward_receipt(session, tool_call_id, status)`는 실행자의 관측(`succeeded/failed/cancelled/unknown`)을 VM에 전달한다. 반환되는 `vm_state`는 VM 응답 문자열을 그대로 담을 뿐 gateway가 완료로 해석하지 않으며, 로컬 상태는 `receipt_forwarded`다. `unknown` 보고나 인식 불가 응답은 미해결이다.
- 미해결 effect는 `RECONCILIATION_REQUIRED`로 막히고, `reconcile(effect_id)`가 VM의 `read_effect` 결과를 받을 때만 해제된다. VM에 intent가 없으면(None) 로컬 기록을 지워 새 intent를 허용한다. 취소·unknown의 판단은 VM 상태를 따른다. 자동 재시도는 하지 않는다.

## 시험

```bash
python -m unittest discover -s tests -p 'test_authority.py' -v
```

합성 ID·fake 시계·fake lease checker·fake VM effect port만 사용한다. 거부 경로(미허용 도구, 만료 인가, 다른 workspace, 역할 불일치, lease 비활성, 범위 확대, stale revision, 위조 handle, 예약 도구, broker 범위)와 동일 인가 재사용, 정책 비소급, 효과 종류별 규칙, intent unknown·reconcile, receipt 비판정, 감사 기록의 인수 비노출을 확인한다. 이 문서 작성 시점의 실행 결과 기록은 없다.

## 한계·미검증(NOT_RUN)

- 실제 VM `authorize` 결과의 scope 형식, VM 효과 권위 extension, 공개 계약 코드 대응은 가정이며 설치형 VM으로 검증하지 않았다.
- 정책·인가 revision, 세션, 재전송·미해결 guard는 프로세스 메모리에만 있다. 재시작 내구성이나 exactly-once 보장이 아니다.
- gateway 잠금 안에서 VM port를 호출하므로 port가 멈추면 다른 요청도 대기한다. port 기한은 adapter 책임이다.
- lease 판정은 주입된 checker(예: `aik_broker.RuntimeLeaseChecker`)에 의존하며 fencing 전이는 AIK-033/034 범위다.
- 같은 프로세스 안에서 `ToolSession`을 넘겨받은 코드는 그 세션으로 행동할 수 있다. OS principal 격리는 범위 밖이다.
- AIK-004 IPC·실제 Electron main·제품 GUI 연결은 하지 않았다. Linux 통과는 개발 검증이며 Windows 지원 증거가 아니다.
