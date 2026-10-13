# AIK-026 — 총괄 Goal→위임→회수→보고 루프 (부분 구현)

상태: **구현과 fake VM port 단위시험까지 진행했다.** 다음 항목은 모두 **미검증(NOT_RUN)**이다.

- 실제 모델 실행
- 실제 VM 설치물 호출
- 실제 AuthorityGateway 세션과의 결합
- Windows 실행
- 합성 목표 E2E(AIK-044/059 gate)

이 문서와 시험 통과는 qualified 판정이나 완료 판정이 아니다. 장부(`planning/`)에 기록된 AIK-026 상태도 바꾸지 않는다.

`src/aik_controller/`는 총괄이 자연어 목표를 VM 공개 명령에 연결하고, 작업 결과와 검증 상태를 보고하는 bounded control loop다. 이 패키지는 표준 라이브러리와 아래 기존 패키지만 import해서 재사용하며, 기존 패키지는 수정하지 않는다.

| 패키지 | 사용 |
| --- | --- |
| `aik_vm_bridge` | `VmBridge`의 `prepare/revise/run_once/observe/pause/cancel/status/replan/final_report`, typed request, `Command`, `VmBridgeError`·`EffectOutcome` |
| `aik_authority` | `AuthorityError`·`AuthorityDenied`·`EffectOutcomeUnknown`, `ToolCall`·`EffectKind` (`GatewayCommandAuthority` adapter) |
| `aik_director` | 주입된 `wake` 객체로 `Director.poll()` 호출(inbox wake는 `aik_inbox.WakePolicy`가 처리) |
| `aik_runtime` | 식별자 규칙 |

## 권위 경계

- **별도 DAG를 만들지 않는다.** Goal·Plan·Task의 생성과 변경, 선행 관계, 준비 여부(ready) 판정은 VM이 소유한다. 총괄은 VM `status`가 ready라고 보고한 작업만 `run_once`로 요청한다.
- **재시도 권위를 만들지 않는다.** 실패한 명령을 자동으로 재발사하지 않는다. 독립 검증 실패 뒤의 재작업은 VM이 `replan_allowed`로 허용한 경우에만 `replan` 명령으로 요청한다. VM에 보냈거나 결과가 불명인 replan은 같은 실패에 대해 다시 보내지 않는다. 확실히 보내지 않은 replan(권한 거절, VM의 확정 거절)은 표시를 남기지 않으므로 원인을 해결한 뒤 `run()`을 다시 호출하면 요청한다.
- **성공 판정 권위를 만들지 않는다.** Goal 완료는 VM `final_report`에 담긴 GoalVerdict(`passed`)와 비어 있지 않은 evidence가 있을 때만 성립한다. `RunReport.goal_complete`는 이 조건을 dataclass 불변식으로 강제한다.
- **AUTHORIZE를 보내지 않는다.** 사용자 승인은 GUI→VM 경로의 몫이다. `aik_authority`에서도 `vm.authorize`는 예약 도구다. VM이 `awaiting_authorization`이면 루프는 `handoff="authorize"`로 멈춘다. `GatewayCommandAuthority`는 AUTHORIZE 매핑 자체를 거절한다.
- `final_report`·`status`·`observe`는 읽기 조회로 **가정**해 권한 검사 없이 보낸다. 이 가정은 실제 VM과 대조하지 않았다(NOT_RUN). 시험용 fake VM의 `observe`는 상태 전이를 함께 수행하지만, 이는 fixture의 단순화이며 실제 VM `observe`의 효과 분류 근거가 아니다. 실제 VM `observe`가 상태를 바꾼다면 effect로 재분류해야 한다. 상태를 바꾸는 명령(`EFFECTFUL_COMMANDS`: prepare/revise/run_once/replan/pause/cancel)은 모두 `CommandAuthority.check`를 통과한 뒤에만 보낸다. authority는 필수 인자다(fail-closed).

## 구성

| 모듈 | 역할 |
| --- | --- |
| `models.py` | `VmView`/`TaskView`(status 해석), `Decision`, `Memory`, `RunPolicy`, `RunReport`, `interpret_final_report` |
| `policy.py` | 순수 결정 함수 `decide(view, memory, policy)` |
| `controller.py` | 실행 루프 `Controller` |
| `ports.py` | `DelegationPort`, `CommandAuthority`, `WakeSource` 프로토콜 |
| `authority.py` | `GatewayCommandAuthority`: 명령을 `AuthorityGateway.request(session, ToolCall)`로 변환 |

## 사용 흐름

1. `controller.start_goal(text)`: 권한 검사를 통과하면 VM `prepare`를 보낸다. VM이 Goal·Plan·Task를 만든다.
2. `controller.run()`: VM이 사용자 인가를 기다리면 `awaiting_authorization`으로 멈춘다.
3. 사용자가 GUI에서 승인한다. 이때 VM `authorize`가 호출되며, 총괄이 호출하지 않는다.
4. `controller.run()`을 다시 호출한다. 이후 tick 반복으로 A/B를 위임하고, VM이 C를 ready로 바꾸면 VM이 준 입력 참조와 함께 C를 위임한다. 독립 검증이 실패하면 `replan`을 보내고, VM이 만든 새 attempt를 위임한다. VM이 만든 Goal Test 작업도 위임하고, GoalVerdict가 준비되면 `final_report`를 조회해 `RunReport`로 사용자에게 인계한다.
5. `note_model_reported()`는 모델 응답 종료를 기록하기만 한다. `report.model_reported`에 남지만 완료 판정에는 쓰지 않는다.

`run()`은 실행 동안 controller 잠금을 보유한다. 따라서 다른 스레드의 `controller.pause()`/`cancel()`/`start_goal()`/`revise_goal()`은 `run()`이 반환할 때까지 대기한다. 실행 중인 루프를 멈추려면 GUI→VM 경로로 VM을 paused/cancelled로 만든다. 루프는 다음 tick의 VM status에서 이를 보고 정지한다. port 호출 자체에는 기한이 없으므로 port가 멈추면 정지도 늦어진다.

## tick과 결정 표

한 tick은 다음 순서로 진행한다.

1. `wake.poll()` (실패하면 `ActionRecord(action="wake", result="WAKE_FAILED")`를 trace에 남기고 계속한다)
2. VM `status`
3. `decide()`
4. VM 명령 하나

결정 표는 위에서부터 먼저 일치하는 행을 적용한다.

| VM 상태 | 행동 |
| --- | --- |
| status 실패 | `observe`(무진전) |
| `cancelled` / `paused` | 정지 (`cancelled` / `paused`) |
| `goal_verdict_ready` + verdict ID | `final_report` 조회 → `completed`/`goal_failed`/`report_unverified` |
| 결과 불명 effect(run_once 또는 replan)가 남아 있음 | `observe` 우선(새 위임·replan 금지) |
| `awaiting_authorization` | 정지, handoff `authorize` |
| `blocked` | 정지, VM 사유 코드, handoff `resolve_blocker` |
| goal/task 상태 unknown | `observe`(효과 금지) |
| `validation_failed` + `replan_allowed` + 미요청 실패 | `replan` |
| `validation_failed`인데 VM이 재작업 불허 / 이번 run의 replan 한도 도달 | 정지 `validation_failed` |
| ready 작업(attempt 있음) | `run_once` (VM이 나열한 첫 ready 작업) |
| running/validating/goal_test | `observe` |
| 다른 진행이 없는데 blocked task가 있음 | 정지 `blocked` |
| 그 외 | `observe` |

## bounded run policy

`RunPolicy(max_ticks=100, max_no_progress=10, max_replans=3, poll_interval_seconds=1.0, deadline=None)`. `run(deadline=...)`으로 절대 시각을 지정할 수 있다. deadline은 timezone-aware datetime이어야 한다.

- tick 수가 상한에 도달하면 `max_ticks`로 끝난다.
- 주입 시계가 deadline 이상이면 `deadline_exceeded`로 끝난다. 시계가 실패하거나 naive 값을 반환하면 `clock_unavailable`로 끝난다(fail-closed).
- VM status의 지문이 연속으로 변하지 않으면 무진전으로 센다. 지문은 goal 상태, verdict ID, history cursor, plan revision, task별 (ID, attempt, 상태, failure)로 구성한다. 한도에 도달하면 `no_progress`로 끝나며, 미해결 effect가 있으면 `reconciliation_required`로 끝난다.
- `max_replans`는 **한 번의 `run()`** 안에서 보낼 replan 수의 한도다. `run()`을 시작할 때마다 카운터를 0으로 되돌린다. 이미 VM에 보낸(또는 결과 불명인) 실패 ID는 인스턴스 수명 동안 기억하므로 다시 보내지 않는다.
- 이 한도들은 제어권을 호출자에게 돌려주는 경계일 뿐이다. Goal 실패 판정이 아니며, `run()`을 다시 호출하면 이어서 진행한다.

## 거절과 결과 불명(unknown)

- `AuthorityDenied`: 재요청하지 않는다. `escalation=True`이면 `escalation_required`(새 VM 인가 필요), 아니면 `authority_denied`로 정지한다. authority 자체가 예외로 실패하면 `authority_denied`(`AUTHORITY_UNAVAILABLE`)로 정지한다. run_once와 replan이 같은 규칙을 따른다. replan은 VM 명령을 보내기 전에 권한을 먼저 검사하므로, 거절된 replan은 요청 기록을 남기지 않는다.
- 위임 port에 `release`가 있으면 확실히 미발송된 요청을 반환한다. 권한 거절, VM 확정 거절, binding 불일치(`DELEGATION_MISMATCH`)가 여기에 해당한다.
- `EffectOutcomeUnknown`(intent unknown) 또는 bridge의 `outcome=unknown`(`OUTCOME_UNKNOWN`, `RECONCILIATION_REQUIRED` 등), 또는 인식 불가 job 상태:
  - run_once: 해당 task를 미해결로 표시하고 재발사하지 않는다. VM status에서 그 task가 다른 attempt를 갖거나 ready·unknown이 아닌 상태가 되면 해제한다.
  - replan: 해당 실패를 요청됨으로 표시하고(재발송 금지) task를 미해결로 표시한다. VM status의 plan revision이 바뀌거나, 그 task가 더 이상 같은 failure의 `validation_failed`가 아니면 해제한다.
  - 미해결이 남아 있는 동안에는 다른 위임이나 replan을 하지 않는다(관측 우선). 무진전 한도에 도달하면 `reconciliation_required`로 끝나고, `unresolved_task_ids`에 해당 task가 남는다.
- bridge의 알려진 실패(`VM_REJECTED` 등): `vm_rejected`로 정지하고 VM 원래 코드를 `vm_code`에 보존한다. replan이 이렇게 확정 거절되면 요청 표시를 되돌린다.
- 위임 port 예외나 binding 불일치는 `delegation_failed`다. `None`을 반환하면 용량이 없다는 뜻이므로 대기한다.

## VM status/final_report payload (가정 계약, NOT_RUN)

설치된 VM과 대조하지 않은 adapter 계약이다. 인식하지 못한 값은 `unknown`으로 처리하며 효과를 유발하지 않는다.

- status: `goal_state`, `tasks[]`, `goal_verdict_id`, `blocked_reason`, `history_cursor`, `plan_revision_id`
  - `goal_state` 값: `draft`/`awaiting_authorization`/`active`/`goal_test`/`goal_verdict_ready`/`blocked`/`paused`/`cancelled`
  - `tasks[]` 항목: `task_id`, `state`, `attempt_id`, `inputs`, `evidence`, `failure_id`, `replan_allowed`, `blocked_reason`
- final_report: `goal_verdict{verdict_id, outcome: passed|failed}`, `evidence[]`, `deliverables[]`
  - `deliverables`가 식별자 목록 형식이 아니면 `report_unverified`(`DELIVERABLES_INVALID`)로 보고한다. 형식 위반을 빈 인계물로 바꾸지 않는다.

## 권한 adapter

`GatewayCommandAuthority(gateway, director_session, workspace_id=, input_revision=, authorization_revision=, tool_ids={Command: tool_id}, decision_check=None)`는 다음 규칙으로 동작한다.

- 명령을 선언된 도구 호출로 바꾼다. target은 `vm://<project>[/<task>]`다.
- 효과가 있는 도구는 `effect_id=request_id`로 지정한다. READ_ONLY 도구는 `effect_id=None`이다.
- 매핑되지 않았거나 정책에 선언되지 않은 명령은 `TOOL_NOT_DECLARED`, workspace 정책이 없으면 `POLICY_UNKNOWN`으로 거절한다.
- 판단은 gateway 세션이 고정한 범위(정책·프로필·VM 인가·lease)가 내린다.
- effect가 있는 도구는 gateway에 VM effect authority port가 없으면 `SEAM_UNAVAILABLE`로 거절된다.
- gateway는 거절 시 예외를 던지는 것으로 가정한다. 그래도 반환값을 fail-closed로 검사한다. `None`·`False`, 거절을 뜻하는 문자열, `allowed`/`authorized`/`granted`/`permitted`/`approved` 속성이 `True`가 아닌 객체, `denied`가 참이거나 `state`/`status`/`outcome`/`decision`/`verdict`/`result`가 거절을 뜻하는 객체는 `AuthorityDenied(INVALID_REQUEST)`로 거절한다. 실제 gateway 반환 타입(`ExecutionDecision`)의 계약은 확인하지 않았다(NOT_RUN). 실제 gateway가 성공 시 `None`을 반환한다면 이 adapter는 모든 명령을 거절한다(fail-closed). 실제 계약을 고정하려면 `decision_check(result) -> bool`을 주입한다.

## 시험

```bash
python -m unittest discover -s tests -p 'test_controller.py' -v
```

실제 `VmBridge`를 fake EngineApplication port 위에서 사용한다. fake VM이 DAG, ready 판정, 독립 검증 실패, GoalVerdict를 소유한다. 위임·authority·wake·시계는 fake이며 데이터는 모두 합성 ID다. 일부 시험은 실제 `VmBridge`를 감싸 지정 명령에서 합성 `VmBridgeError`(분류된 outcome·코드)를 던지거나, 고정 status payload를 돌려주는 최소 stub bridge를 사용한다. 검사 항목은 다음과 같다.

- A/B 선행 산출물이 C 입력으로 전달되는 흐름
- C 독립 실패 뒤 `replan`(1회)과 새 attempt 위임
- Goal Test 위임과 `final_report` 인계(`completed`, evidence·deliverable)
- 총괄이 AUTHORIZE를 보내지 않음
- `model_reported`가 완료로 취급되지 않음
- evidence 없는 보고는 `report_unverified`, 실패 verdict는 `goal_failed`, 인식 불가 outcome과 deliverables 형식 위반은 `report_unverified`
- 재작업 불허 시 `validation_failed`, VM blocked·paused·cancelled 시 정지
- run_once unknown: 시작된 경우 관측으로 해제, 증거가 없는 경우 재발사 없이 `reconciliation_required`
- run_once 확정 거절의 `vm_rejected`·`vm_code` 보존·예약 반환, `DELEGATION_FAILED`·`DELEGATION_MISMATCH`(예약 반환)·`NO_CAPACITY`
- replan의 권한 거절·VM 확정 거절 뒤 재실행 시 재요청, authority 장애 시 `AUTHORITY_UNAVAILABLE`, 결과 불명 replan 동안 다른 위임 금지와 VM 변화 후 해제
- replan 한도의 run 단위 동작
- authority escalation·일반 거절·intent unknown 처리, revise/pause/cancel 권한 검사
- status 실패와 final_report 실패 시 대기, wake 실패의 trace 기록
- 무진전 한도, 절대 deadline, naive 시계
- 순수 `decide` 결정, status 해석의 fail-closed, gateway adapter 매핑·정책 없음·READ_ONLY·거절 반환값·`decision_check`

이 문서 작성 시점의 실행 결과 기록은 없다.

## 한계·미검증(NOT_RUN)

- 실제 모델·VM 설치물·AGS·provider를 쓰는 E2E는 실행하지 않았다(완료 기준 4의 실제 합성 목표 검증은 AIK-044/059 gate). Linux 통과는 개발 검증이며 Windows 지원 증거가 아니다.
- VM status/final_report payload 형식은 가정이다. VM `observe`가 읽기 조회라는 분류도 가정이다. VM `run_once`는 다음 ready 작업을 VM이 고르므로, 위임 port가 만든 binding과 VM 선택이 일치하는지는 bridge의 `attempt_detail`·`bind_execution` 대조에 의존한다.
- 실제 `AuthorityGateway` 세션(실제 `ExecutionDecision`)과의 결합은 시험하지 않았다. adapter는 fake gateway로만 검증했다. 반환값 검사는 일반적인 거절 표지를 대상으로 한 방어이며 실제 반환 계약의 확인이 아니다. 실행자 receipt를 VM에 전달하는 `forward_receipt`·`reconcile`은 이 루프 범위 밖이다.
- 시험의 합성 `VmBridgeError`는 실제 bridge 생성자를 거치지 않고 분류 속성(outcome·code·vm_code)만 설정한다. 실제 bridge가 어떤 상황에서 어떤 outcome을 내는지는 이 시험으로 확인하지 않는다.
- 미해결·replan 기록은 프로세스 메모리에만 있다. 재시작 내구성이나 exactly-once 보장이 없다.
- binding·placement·lease 생성(용량 예약·fencing)은 주입된 `DelegationPort`의 몫이다(AIK-033/034).
- 루프는 동기식이고 port 호출에 자체 기한이 없다. port 기한은 adapter 책임이다. `run()` 동안 같은 인스턴스의 다른 호출은 대기한다.
