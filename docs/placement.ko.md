# AIK-033 — VM ready Task의 자원 placement (부분 구현)

`src/aik_placement/`는 VM이 실행 가능(ready)·인가했다고 알려준 Task attempt를 AGS/AGENT 역할 요구와 실제 자원 능력에 맞는 pool/프로필에 배치하는 planner·eligibility filter·결정 근거 기록이다. Python 표준 라이브러리와 기존 모듈(`aik_runtime`, `aik_pool`, `aik_capability`)만 사용한다.

## 권위 경계

- 입력 `ReadyTask`는 VM이 제시한 값만 담는다: `vm_instance_id`, `vm_project_id`, `vm_task_id`, `vm_attempt_id`, `vm_authorization_ref`, `role`, `model`, `effort`, `tools`, `purpose`(`operation`|`qualification`), `candidate_route`.
- AIK는 ready/인가를 다시 판정하지 않는다. Task DAG·재시도·다음 Task 선택 기능이 없다. 종료(released/failed/unknown)된 attempt는 다시 배치하지 않고 `ATTEMPT_ALREADY_PLACED`로 거절한다. 재시도는 VM이 새 attempt로 제시해야 한다.
- placement는 AIK 배정 메타데이터다. 실행 권한·lease·fencing은 AIK-034 범위이며 여기서 만들지 않는다. budget 승인(AIK-040 `BudgetStore.admit`)과 A2 gate도 이 모듈이 호출하지 않으므로 호출자가 별도로 적용해야 한다.

## 사용법

```python
from aik_placement import PlacementPlanner, PlacementJournal, ReadyTask, ParallelQualification

planner = PlacementPlanner(store, registry, inventory, PlacementJournal())
decision = planner.plan(ready_task, ("profile-a", "profile-b"))
# decision.status: placed | wait | rejected
planner.release_placement(decision.placement.value.id, decision.placement.revision, "released")
```

`candidates`는 AIK-023 역할 프로필에서 정한 `aik_runtime.ExecutionProfile` ID의 선호 순서다. 순서대로 검사해 첫 eligible 프로필을 배치한다. 비용 추정 등으로 재정렬하거나 가지치기하지 않는다.

## 프로필별 eligibility 검사

각 후보의 결과(`ProfileCheck`: outcome, 모든 사유 코드, capability resource revision)를 결정에 남긴다.

| 검사 | 사유 코드 (거절/대기) |
|---|---|
| 프로필·역할·모델·effort | `PROFILE_NOT_FOUND`, `ROLE_MISMATCH`, `MODEL_MISMATCH`, `EFFORT_MISMATCH`(프로필 effort가 None이면 허용) |
| Pool | `POOL_NOT_FOUND`, `POOL_DISABLED` |
| route (operation) | `ROUTE_NOT_REGISTERED`, `ROUTE_NOT_QUALIFIED`, `ROUTE_NOT_EXECUTABLE`, `ROUTE_NOT_PLACEMENT_ELIGIBLE`, 대기 `ROUTE_NOT_FRESH`·`ROUTE_STATUS_UNAVAILABLE` |
| route (qualification) | `NOT_CANDIDATE_ROUTE`, `ROUTE_NOT_CANDIDATE` |
| capacity | 대기 `CAPACITY_UNKNOWN`, `CAPACITY_EXHAUSTED`, `POOL_PARALLEL_LIMIT`(저장된 pending/assigned 수 ≥ `max_parallel`) |
| capability | `CAPABILITY:<aik_capability 사유>`, `CAPABILITY_RESOURCE_UNKNOWN`, `CAPABILITY_CHECK_FAILED` |
| 같은 프로젝트 | 대기 `SAME_PROJECT_SERIAL`, 거절 `PARALLEL_ROUTE_NOT_QUALIFIED`·`PARALLEL_BASIS_STALE` |

하나라도 eligible이면 `placed`, 아니면 대기 사유만 있는 후보가 하나라도 있을 때 `wait`, 그 외는 `rejected`다. capacity를 0이나 무제한으로 추정하지 않는다.

operation 배치는 `registry.operational_inventory(model=...)`에 해당 (pool, model)이 있어야 한다(`placement_eligible`, 즉 live qualification 통과). qualification 실행은 `candidate_route`로 명시한 (pool, model) 한 경로만 허용하며 그 level이 `candidate_for_qualification` 또는 `qualified`이고 `executable_now`여야 한다. qualification PASS를 요구하지 않으므로 순환이 없다. operation 요청에 `candidate_route`가 있으면 `CANDIDATE_ROUTE_NOT_OPERATIONAL`, qualification 요청에 없으면 `QUALIFICATION_ROUTE_REQUIRED`로 거절해 candidate가 운영 자동 배치에 섞이지 않는다.

## 직렬 기본과 병렬 근거

같은 `vm_project_id`에 pending/assigned placement가 있으면 기본은 `SAME_PROJECT_SERIAL` 대기다. `plan(..., parallel=ParallelQualification(evidence_ref, inventory_revision))`가 주어지고, route level이 `qualified`이며, `inventory_revision`이 그 연결의 현재 capability resource revision과 같을 때만 병렬 배치한다. 이 경우 `SAME_PROJECT_PARALLEL_QUALIFIED` 사유와 근거가 결정에 남는다. `evidence_ref`의 외부 내용은 이 모듈이 검증하지 않는다(호출자 책임). 다른 프로젝트의 독립 작업은 capacity 범위에서 병렬 배치된다.

## 원자성·멱등·기록

- 저장소 검사(capacity·직렬·attempt 중복)와 Placement 저장은 RuntimeStore의 한 transaction(프로세스 잠금) 안에서 수행하므로 여러 연결·프로세스가 동시에 plan해도 `max_parallel`을 넘지 않는다. registry/inventory 조회도 그 잠금 안에서 일어난다.
- 같은 `vm_attempt_id`가 이미 활성 placement로 있고 instance/project/task가 같으며 그 프로필이 후보에 있으면 기존 placement를 그대로 반환한다(`IDEMPOTENT_EXISTING_PLACEMENT`). 다르면 `ATTEMPT_ALREADY_PLACED`.
- Placement ID는 `(vm_instance_id, vm_attempt_id)`의 SHA-256 앞 40자로 정한다.
- 모든 결정(wait/rejected 포함)은 `PlacementJournal`(별도 SQLite, application ID `0x41494B4A` "AIKJ")에 canonical JSON과 SHA-256 digest로 append된다. UPDATE/DELETE는 trigger가 거절하고, 읽을 때 digest를 다시 검증한다. 경로 규칙과 잠금은 `aik_runtime.paths`를 재사용해 저장소 내부·개발 원장 경로를 거절한다. 결정에는 matrix revision과 선택 경로의 capability resource revision을 기록한다.
- journal 기록은 store commit 뒤에 수행한다. journal 기록이 실패하면 예외가 전파되며 placement는 남아 있다. 같은 task를 다시 plan하면 멱등 반환과 함께 결정이 기록된다.

## 한계

- 같은 attempt 탐색과 capacity 계산은 placement 전체를 읽으므로 비용이 placement 수에 비례한다.
- registry 상태와 inventory 결과는 호출 시점 스냅샷이다. route의 `available_capacity`는 aik_pool probe 관측값이며 저장된 placement 수와 함께 검사한다.
- 데이터 범위(data scope) 요구는 현재 `ReadyTask`·프로필 필드가 없어 tool/model/effort/role 조합으로만 대신한다. 별도 데이터 범위 계약은 후속 연결 작업이 필요하다.

## 검증과 장부

```bash
python -m unittest tests/test_placement.py -v
```

시험은 합성 Pool·ExecutionProfile을 실제 RuntimeStore에 저장하고, registry·inventory는 같은 공개 메서드를 가진 fake를 주입한다. 실제 provider·credential·네트워크·VM/AGS는 사용하지 않는다. Linux 통과는 개발 검증이며 Windows 동작은 **미검증(NOT_RUN)**이다. 실제 VM/AGS 연결, live qualification 통과 경로, 설치형 제품 검증도 NOT_RUN이며 어떤 route도 qualified라고 주장하지 않는다. 장부(planning/)의 AIK-033 상태와 완료 판정은 변경하지 않는다.
