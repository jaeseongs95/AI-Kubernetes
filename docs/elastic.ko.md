# AIK-041 — 단일 클러스터 탄력 병렬화와 idle 정리 (부분 구현)

`src/aik_elastic/`의 `ElasticityController`는 VM이 준 ready 작업을 검증된 용량 안에서 worker lease로 확보하고, dispatch되지 않은 유휴 lease를 반환한다. 표준 라이브러리만 사용한다. 기존 모듈을 조합하며 자체 영구 상태는 저장하지 않는다.

## 권위 경계

- VM이 Task·Attempt·재시도·검증·GoalVerdict의 권위다. AGS gate는 VM이 집행한다. 이 모듈은 ready 순서를 바꾸지 않고, DAG를 만들지 않으며, 재시도하거나 다음 Task를 고르지 않는다.
- 배치 결정은 AIK-033 `PlacementPlanner`가 담당한다. 프로젝트 직렬 기본값, qualified route, capability 판정도 여기에 포함되며 이 모듈은 이를 다시 구현하지 않는다.
- lease는 반드시 `aik_lifecycle.GatedLeaseManager`를 거친다. 게이트가 없는 manager를 넘기면 `UNGATED_LEASE_MANAGER`로 거절한다. 따라서 pause·drain을 우회할 수 없다.
- 반환하는 `TickResult`는 AIK의 결정 기록이며 완료 판정이 아니다.

## tick 처리 순서

`tick(ready, *, candidates, reserve_options, parallel=None)`의 처리 순서는 다음과 같다.

1. `reclaim_idle()`를 먼저 실행해 반환 가능한 유휴 용량을 확보한다. 결과는 `reclaimed`에 담긴다.
2. ready는 VM이 준 순서대로 처리한다. 같은 `vm_attempt_id`가 반복되면 첫 항목만 처리하고 나머지는 결과에 넣지 않는다.
3. 후보 profile의 pool과 task 프로젝트의 lifecycle gate(`GatedLeaseManager.gate.is_open`)를 planner 호출 전에 확인한다. 닫힌 scope(`SCOPE_PAUSED@…`·`SCOPE_DRAINING@…`)나 읽을 수 없는 gate(`GATE_UNAVAILABLE`)는 후보에서 뺀다. placement는 VM Attempt당 하나이므로, pause 중 만들었다가 해제한 placement는 resume 뒤 같은 Attempt로 다시 만들 수 없기 때문이다.
   이어서 pool마다 점유 lease 수를 센다. 점유 상태는 `aik_lease.HOLDING_STATES`를 따른다.
   - 막힌 pool은 planner 후보에서 제외한다.
   - 모든 후보가 막히면 placement를 만들지 않고 대기시킨다.
4. 남은 후보로 `planner.plan`을 호출한다.
   - `placed`가 아니면 planner의 사유를 그대로 waiting에 넣는다.
5. 선택된 pool의 용량을 다시 확인한다.
6. `leases.reserve(placement_id, **reserve_options)`를 호출한다.
   - `granted`만 granted에 넣는다.
   - wait/rejected는 게이트·lease 사유를 그대로 waiting에 넣는다.
7. 5·6단계에서 lease 없이 남은 placement는 `planner.release_placement(..., "released")`로 정리한다.

`candidates`는 profile ID tuple이거나 `callable(task)`다. `reserve_options`는 kwargs mapping이거나 `callable(task, placement)`다.

## 대기 사유

| 사유 | 의미 |
|---|---|
| `CAPACITY_EXHAUSTED` | pool의 점유 lease 수가 `max_parallel` 이상이다. 용량이 전부 고갈된 상태다. |
| `CONTROL_RESERVE` | 다음 둘 중 하나다. 점유 수가 `max_parallel - reserve` 이상이어서 제어용 여유만 남은 경우, 또는 `max_parallel <= reserve`여서 이 pool에는 worker를 배치하지 않는 경우다. |
| planner 사유 | 직렬 제약, route·capability 불충족 등 `PlacementDecision.reasons`를 그대로 전달한다. |
| gate/lease 사유 | 예: 일시정지된 scope. `GatedLeaseManager`가 반환한 사유를 그대로 전달한다. |
| `LEASE_RESERVE_FAILED` | reserve가 `LeaseError` 또는 `LifecycleError`를 일으켰고 오류 code를 확인할 수 없는 경우다. |
| `LEASE_RESULT_UNREADABLE` | `granted`였지만 lease를 읽지 못했다. lease가 존재할 수 있으므로 placement를 유지한다(fail-closed). |
| `PLACEMENT_RELEASE_FAILED` | lease 없는 placement의 해제가 실패했다. 해당 프로젝트는 계속 점유 상태로 남는다. 과배정 방향이 아닌 차단 방향으로 실패한다. |
| `NO_CANDIDATE_PROFILE` | 후보 profile이 없다. |

## control reserve의 의미

`control_reserve={pool_id: n}`은 같은 pool을 총괄·취소·관측 경로가 쓸 때 worker가 점유하지 않고 남겨 둘 슬롯 수다. worker lease는 `max_parallel - n`개까지만 확보한다.

예를 들면 다음과 같다.

- `max_parallel=2`, `n=1`이면 worker는 1개까지 확보된다.
- `max_parallel=1`, `n=1`이면 worker를 배치하지 않는다.

이 값은 AIK의 배정 상한일 뿐이다. 실제 provider 쪽 여유 용량이나 제어 경로의 성공을 보장하지 않는다.

## idle 정리와 완료 후 정리

- `reclaim_idle()`은 다음 조건을 모두 만족하는 lease만 반환한다.
  - `state == "reserved"`이다.
  - `provider_job_id`가 없다.
  - 이 컨트롤러가 그 revision을 처음 본 시점(grant 또는 관찰)부터 `idle_seconds` 이상 지났다.
- 반환은 `release(lease_id, fencing_token, dispatched=False)`로 한다. 이어서 해당 placement를 해제한다.
- active·draining·unknown 상태는 건드리지 않는다.
- 기준 시각은 메모리에만 있다. 재시작 뒤에는 다시 관찰한 시점부터 센다. 그래서 늦게 회수될 수는 있어도 일찍 회수되지는 않는다.
- `finish(lease_id, token, lifecycle, *, required_artifacts)`는 `LifecycleController.teardown`에 위임한다.
  - evidence 검증, usage 정산, provider 종료 관측, 자체 생성 worker 종료 규칙은 teardown의 규칙을 그대로 따른다.
  - 미해결 효과가 있으면 아무것도 정리하지 않는다.
  - teardown이 완료되고 lease가 더 이상 점유 상태가 아닐 때만 placement를 해제한다.

## 검증

```bash
python -m unittest tests/test_elastic.py -v
```

시험 범위는 다음과 같다.

- 같은 프로젝트 직렬 처리
- 다른 프로젝트의 제한 병렬 처리와 `CAPACITY_EXHAUSTED`
- control reserve 동작 (`max_parallel` 2/1)
- pause 중 대기와 resume 후 grant
- 미전송 reserved lease만 회수
- 미해결 효과가 있을 때 teardown이 정리하지 않음

시험 환경은 다음과 같다.

- 실제 RuntimeStore, BudgetStore, PlacementPlanner, LifecycleGate, GatedLeaseManager, WorkerObserver를 사용한다.
- 주입 시계와 합성 데이터만 쓴다.
- LeaseManager는 `reserve()`만 시험용 grant double로 바꿨다. 따라서 예산 승인 경로는 이 시험에서 실행하지 않았다.

## 한계 (NOT_RUN)

- 복수 worker의 **실제 동시 실행**은 실기로 검증하지 않았다(NOT_RUN). 실제 provider, 실제 worker 프로세스, VM/AGS 설치를 쓰지 않았다. 완료 기준 1의 "최소 한 지원 시나리오 실제 동시 실행"은 충족했다고 주장하지 않는다.
- planner 호출과 reserve 사이에 pause나 용량 경합이 끼어들면 placement를 해제한다. 그 Attempt는 AIK에서 다시 배치할 수 없으며(`ATTEMPT_ALREADY_PLACED`), VM이 새 Attempt를 줄 때까지 대기한다.
- 프로세스 생성과 종료는 `worker_port`의 범위다. 이 모듈은 프로세스를 직접 만들거나 죽이지 않는다.
- Linux 통과는 개발 검증일 뿐이다. Windows 동작은 미검증(NOT_RUN)이다.
- qualified 판정, 장부 상태, 완료 판정은 바꾸지 않는다.
