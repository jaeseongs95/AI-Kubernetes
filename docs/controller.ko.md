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
- **재시도 권위를 만들지 않는다.** 실패한 명령을 자동으로 재발사하지 않는다. 독립 검증 실패 뒤의 재작업은 VM이 `replan_allowed`로 허용한 경우에만 `replan` 명령으로 요청하며, 같은 실패에 대해 한 번만 요청한다.
- **성공 판정 권위를 만들지 않는다.** Goal 완료는 VM `final_report`에 담긴 GoalVerdict(`passed`)와 비어 있지 않은 evidence가 있을 때만 성립한다. `RunReport.goal_complete`는 이 조건을 dataclass 불변식으로 강제한다.
- **AUTHORIZE를 보내지 않는다.** 사용자 승인은 GUI→VM 경로의 몫이다. `aik_authority`에서도 `vm.authorize`는 예약 도구다. VM이 `awaiting_authorization`이면 루프는 `handoff="authorize"`로 멈춘다. `GatewayCommandAuthority`는 AUTHORIZE 매핑 자체를 거절한다.
- `final_report`·`status`·`observe`는 읽기 조회로 간주해 권한 검사 없이 보낸다. 상태를 바꾸는 명령(`EFFECTFUL_COMMANDS`: prepare/revise/run_once/replan/pause/cancel)은 모두 `CommandAuthority.check`를 통과한 뒤에만 보낸다. authority는 필수 인자다(fail-closed).

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

## tick과 결정 표

한 tick은 다음 순서로 진행한다.

1. `wake.poll()`
2. VM `status`
3. `decide()`
4. VM 명령 하나

결정 표는 위에서부터 먼저 일치하는 행을 적용한다.

| VM 상태 | 행동 |
| --- | --- |
| status 실패 | `observe`(무진전) |
| `cancelled` / `paused` | 정지 (`cancelled` / `paused`) |
| `goal_verdict_ready` + verdict ID | `final_report` 조회 → `completed`/`goal_failed`/`report_unverified` |
| 결과 불명 effect가 남아 있음 | `observe` 우선(새 위임 금지) |
| `awaiting_authorization` | 정지, handoff `authorize` |
| `blocked` | 정지, VM 사유 코드, handoff `resolve_blocker` |
| goal/task 상태 unknown | `observe`(효과 금지) |
| `validation_failed` + `replan_allowed` + 미요청 실패 | `replan` |
| `validation_failed`인데 VM이 재작업 불허 / replan 한도 도달 | 정지 `validation_failed` |
| ready 작업(attempt 있음) | `run_once` (VM이 나열한 첫 ready 작업) |
| running/validating/goal_test | `observe` |
| 다른 진행이 없는데 blocked task가 있음 | 정지 `blocked` |
| 그 외 | `observe` |

## bounded run policy

`RunPolicy(max_ticks=100, max_no_progress=10, max_replans=3, poll_interval_seconds=1.0, deadline=None)`. `run(deadline=...)`으로 절대 시각을 지정할 수 있다. deadline은 timezone-aware datetime이어야 한다.

- tick 수가 상한에 도달하면 `max_ticks`로 끝난다.
- 주입 시계가 deadline 이상이면 `deadline_exceeded`로 끝난다. 시계가 실패하거나 naive 값을 반환하면 `clock_unavailable`로 끝난다(fail-closed).
- VM status의 지문이 연속으로 변하지 않으면 무진전으로 센다. 지문은 goal 상태, verdict ID, history cursor, plan revision, task별 (ID, attempt, 상태, failure)로 구성한다. 한도에 도달하면 `no_progress`로 끝나며, 미해결 effect가 있으면 `reconciliation_required`로 끝난다.
- 이 한도들은 제어권을 호출자에게 돌려주는 경계일 뿐이다. Goal 실패 판정이 아니며, `run()`을 다시 호출하면 이어서 진행한다.

## 거절과 결과 불명(unknown)

- `AuthorityDenied`: 재요청하지 않는다. `escalation=True`이면 `escalation_required`(새 VM 인가 필요), 아니면 `authority_denied`로 정지한다. 위임 port에 `release`가 있으면 확실히 미발송된 요청을 반환한다.
- `EffectOutcomeUnknown`(intent unknown) 또는 bridge의 `outcome=unknown`(`OUTCOME_UNKNOWN`, `RECONCILIATION_REQUIRED` 등), 또는 인식 불가 job 상태: 해당 task를 미해결로 표시하고 재발사하지 않는다. 이후 VM status에서 그 task가 다른 attempt를 갖거나 ready·unknown이 아닌 상태가 되어야 해제한다. 그동안 다른 위임도 하지 않는다(관측 우선).
- bridge의 알려진 실패(`VM_REJECTED` 등): `vm_rejected`로 정지하고 VM 원래 코드를 `vm_code`에 보존한다.
- 위임 port 예외나 binding 불일치는 `delegation_failed`다. `None`을 반환하면 용량이 없다는 뜻이므로 대기한다.

## VM status/final_report payload (가정 계약, NOT_RUN)

설치된 VM과 대조하지 않은 adapter 계약이다. 인식하지 못한 값은 `unknown`으로 처리하며 효과를 유발하지 않는다.

- status: `goal_state`, `tasks[]`, `goal_verdict_id`, `blocked_reason`, `history_cursor`, `plan_revision_id`
  - `goal_state` 값: `draft`/`awaiting_authorization`/`active`/`goal_test`/`goal_verdict_ready`/`blocked`/`paused`/`cancelled`
  - `tasks[]` 항목: `task_id`, `state`, `attempt_id`, `inputs`, `evidence`, `failure_id`, `replan_allowed`, `blocked_reason`
- final_report: `goal_verdict{verdict_id, outcome: passed|failed}`, `evidence[]`, `deliverables[]`

## 권한 adapter

`GatewayCommandAuthority(gateway, director_session, workspace_id=, input_revision=, authorization_revision=, tool_ids={Command: tool_id})`는 다음 규칙으로 동작한다.

- 명령을 선언된 도구 호출로 바꾼다. target은 `vm://<project>[/<task>]`다.
- 효과가 있는 도구는 `effect_id=request_id`로 지정한다.
- 매핑되지 않았거나 정책에 선언되지 않은 명령은 `TOOL_NOT_DECLARED`로 거절한다.
- 판단은 gateway 세션이 고정한 범위(정책·프로필·VM 인가·lease)가 내린다.
- effect가 있는 도구는 gateway에 VM effect authority port가 없으면 `SEAM_UNAVAILABLE`로 거절된다.

## 시험

```bash
python -m unittest discover -s tests -p 'test_controller.py' -v
```

실제 `VmBridge`를 fake EngineApplication port 위에서 사용한다. fake VM이 DAG, ready 판정, 독립 검증 실패, GoalVerdict를 소유한다. 위임·authority·wake·시계는 fake이며 데이터는 모두 합성 ID다. 검사 항목은 다음과 같다.

- A/B 선행 산출물이 C 입력으로 전달되는 흐름
- C 독립 실패 뒤 `replan`(1회)과 새 attempt 위임
- Goal Test 위임과 `final_report` 인계(`completed`, evidence·deliverable)
- 총괄이 AUTHORIZE를 보내지 않음
- `model_reported`가 완료로 취급되지 않음
- evidence 없는 보고는 `report_unverified`, 실패 verdict는 `goal_failed`
- 재작업 불허 시 `validation_failed`, VM blocked 시 정지
- run_once unknown: 시작된 경우 관측으로 해제, 증거가 없는 경우 재발사 없이 `reconciliation_required`
- authority escalation·일반 거절·intent unknown 처리
- 무진전 한도, 절대 deadline, naive 시계
- 순수 `decide` 결정, status 해석의 fail-closed, gateway adapter 매핑

이 문서 작성 시점의 실행 결과 기록은 없다.

## 한계·미검증(NOT_RUN)

- 실제 모델·VM 설치물·AGS·provider를 쓰는 E2E는 실행하지 않았다(완료 기준 4의 실제 합성 목표 검증은 AIK-044/059 gate). Linux 통과는 개발 검증이며 Windows 지원 증거가 아니다.
- VM status/final_report payload 형식은 가정이다. VM `run_once`는 다음 ready 작업을 VM이 고르므로, 위임 port가 만든 binding과 VM 선택이 일치하는지는 bridge의 `attempt_detail`·`bind_execution` 대조에 의존한다.
- 실제 `AuthorityGateway` 세션(실제 `ExecutionDecision`)과의 결합은 시험하지 않았다. adapter는 fake gateway로만 검증했다. 실행자 receipt를 VM에 전달하는 `forward_receipt`·`reconcile`은 이 루프 범위 밖이다.
- 미해결·replan 기록은 프로세스 메모리에만 있다. 재시작 내구성이나 exactly-once 보장이 없다.
- binding·placement·lease 생성(용량 예약·fencing)은 주입된 `DelegationPort`의 몫이다(AIK-033/034).
- 루프는 동기식이고 port 호출에 자체 기한이 없다. port 기한은 adapter 책임이다.
