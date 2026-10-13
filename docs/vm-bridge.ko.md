# AIK-031 — VM EngineApplication command/query bridge (부분 구현)

`src/aik_vm_bridge/`는 GUI와 총괄이 VM의 공개 façade(`EngineApplication`)를 호출할 때 쓰는 AIK 쪽 typed bridge다. 호출 대상은 Goal 준비·수정·인가·실행·관측·일시정지·취소·상태·재계획·최종 보고다. 의존성은 Python 표준 라이브러리, 저장소의 `aik_runtime`(식별자 규칙·Pool/Placement/Lease 모델), `aik_provider.contracts`(인증 종류)뿐이다.

**현재 상태: 구현, fake port 단위시험, 실제 upstream 소스(고정 revision `32bb0f9`) 대조 어댑터까지 완료.** 다음 항목은 모두 **NOT_RUN**이다.

- 설치형 VM wheel 호출
- Windows 실행
- provider 연동
- durable binding extension

이 문서와 테스트 통과는 통합 완료나 qualified 판정이 아니다. 장부 상태는 변경하지 않는다.

## 경계와 소유권

- **VM 내부 접근:** VM 내부 모듈과 SQLite 저장 형식을 모듈 import 시점에 import하거나 복제하지 않는다. 근거로 쓰는 것은 다음 선언 표면과 공개 façade 호출뿐이다.
  - `contracts/upstream/vm-public-surface.json`(AIK-001 소스 projection)
  - `integration-contract.json`
  - `task-execution-binding.schema.json`
- **실제 어댑터:** `engine_port.EngineApplicationPort`는 주입된 `EngineApplication` 인스턴스의 공개 메서드만 호출한다. upstream 예외 클래스는 호출이 실패했을 때만 `importlib`로 지연 조회한다.
- **상태 변경 경로:** 모든 Goal/Plan/Task 변경은 port의 공개 command로만 한다. bridge 소스에는 SQL과 DB 연결이 없다(테스트로 확인).
- **VM 기록 복제 금지:** VM의 Task/Attempt/verdict를 복제하지 않는다. 응답에서 다음 값만 `VmRefs`에 그대로 복사한다.
  - ID: project, goal, goal revision, plan revision, task, attempt, job, authorization
  - execution spec digest
  - `history_cursor`
- **응답 snapshot:** 응답 전체는 VM 원본과 분리된 snapshot(`BridgeReceipt.payload`)으로 보존한다. 중첩 mapping은 `MappingProxyType`이다. 중첩 list는 일반 list와 같다고 비교되지만, `append`·항목 대입 같은 일반 변경 메서드는 거절한다.
- **어댑터 정규화의 범위:** payload는 어댑터 정규화 뒤의 snapshot이며 VM 원본과 키 단위로 같지 않다. 다시 쓰는 키는 `engine_port` 모듈 문서에 열거한 것뿐이다(`tasks`→`vm_tasks`, `history_cursor` mapping→정수 `sequence`와 원본 `vm_history_cursor`, 중첩 ID 승격, `job` 추가, 파생 `goal_state`).
- **job 종료의 의미:** job의 `terminal` 단계는 provider/job 종료일 뿐 VM 완료 판정이 아니다.

## 구성

| 모듈 | 역할 |
|---|---|
| `surface.py` | 선언 표면 로딩, capability preflight, 버전 handshake |
| `binding.py` | `TaskExecutionBinding`(schema v1 24개 필드), 정규 digest, AIK Placement/Lease/Pool 대조 |
| `bridge.py` | typed request/receipt, port Protocol, `VmBridge`, `JOB_STATUS_PHASES` |
| `engine_port.py` | 실제 upstream `EngineApplication` → `VmApplicationPort` 어댑터(정규화·오류 분류) |
| `errors.py` | `BridgeCode`, `EffectOutcome`, `VmBridgeError` |

## 실제 upstream(32bb0f9) 대조 결과

고정 revision 소스를 PYTHONPATH로 import하고 합성 임시 프로젝트로 관측한 사실을 기존 가정과 비교한 결과다. 이번 검토 수정 후에는 실제 upstream 시험을 다시 실행하지 않았다(아래 '검증' 참고).

| 항목 | 기존 가정 | 실제(32bb0f9) | 조치 |
|---|---|---|---|
| job 상태 값 | `queued/pending/reserved/prepared`, `started/in_progress`, `succeeded/completed/failed/interrupted/proven_no_effect/expired` 등 | `RuntimeJobStatus`: `scheduled, running, interrupting, provider_terminal, collector_lost, consumed, cancelled` 7개 | **수정**: `JOB_STATUS_PHASES`로 교체. 예전 가정 문자열은 이제 `unknown` |
| 응답 타입 | 모두 mapping | `status/observe/pause/cancel/replan`은 dict. `run_once`(RunOnceResult), `attempt_detail`(AttemptDetail), `final_report`(FinalReport), `prepare/revise/authorize/model_binding_status`는 pydantic | **수정**: 어댑터가 `model_dump(mode="json")` 뒤 JSON 형태 builtins로 변환 |
| 프로젝트 root/ID | 최상위 `project_root`/`project_id` | `status()["project"]["root"]`/`["id"]`. 최상위 키 없음 | **수정**: 어댑터가 `project_id`/`project_root`로 승격. `BridgeProjectPort`는 중첩 형식도 읽음(둘 다 있으면 일치 필수) |
| 없는 project | `PROJECT_NOT_FOUND` 코드 | `EngineLedgerError("프로젝트를 찾을 수 없습니다.")`, 코드 없음 | **수정**: 어댑터가 이 정확한 메시지만 `PROJECT_NOT_FOUND`로 분류. 다른 ledger 오류는 `ENGINE_LEDGER_ERROR`(부재로 간주하지 않음) |
| 없는 attempt | 미정 | `EngineApplicationError("ATTEMPT_NOT_FOUND")` | 코드 보존(조회: known failure) |
| runtime 없음 | 미정 | `observe()`/`run_once()`가 `EngineApplicationError("RUNTIME_REQUIRED")` | 코드 보존. 효과성 호출(`run_once`)에서는 효과 전 거절 목록에 없어 outcome `unknown`(보수적) |
| `history_cursor` | 정수 | `HistoryCursor` mapping(`sequence` 등) | **수정**: 어댑터가 같은 키를 `sequence` 정수로 바꾼다. 원본 mapping은 `vm_history_cursor`에 그대로 보존 |
| attempt_detail 필드 | 최상위 attempt_id/task_id/goal_revision_id/execution_spec_digest | `attempt.{attempt_id, task_id, execution_spec_digest, …}`. `goal_revision_id` 없음 | **수정**: 어댑터가 승격. `goal_revision_id`가 없으므로 run_once owner query는 `INVALID_RESPONSE`로 효과 전에 멈춤(fail-closed, 미해결) |
| run_once job | `job{job_id,status}` | `runtime_job_id/kind/status` 평면 필드 | **수정**: 어댑터가 `job`으로 정규화. bridge도 평면 필드를 직접 읽음 |
| observe job | `job` | `runtime_job`(= `job.model_dump(mode="json")`) | **수정**: `job` 정규화와 attempt/task ID 승격 |
| observe 부수효과 | 순수 조회 | upstream observe 발췌상 `finalize_cancelled_runtime_job`, `supervisor.reattach`, `supervisor.tick`을 호출할 수 있음 | **수정**: 분류 불가 실패는 `OUTCOME_UNKNOWN`/`unknown`(guard 추가는 없음). VM 코드가 있는 실패는 조회로 분류 |
| handshake | port가 제공 | upstream에 없음 | 어댑터가 `inspect.signature`로 메서드 인자 이름을 만든다. surface revision과 engine schema revision은 public façade로 알 수 없어 **호출자가 주입**해야 한다 |
| durable binding (`bind_execution`/`read_execution_binding`) | upstream extension 가정 | **없음** | 변경 없음. 어댑터는 extension을 선언하지도 흉내 내지도 않으며 `run_once`는 `SEAM_UNAVAILABLE` |
| status → controller 형식 | `goal_state`, `tasks[{task_id,state,…}]`, `goal_verdict_id` | `current_stage/control_state/authorization_state`, `tasks`(행 형식이 다름) | **부분 수정**: `goal_state`는 VM 필드가 아니라 **AIK 파생 라벨**이다. `control_state`가 paused/cancelled면 그대로, `authorization_state=required`면 `awaiting_authorization`이다. `goal_state="cancelled"`는 취소 job의 정산 완료를 뜻하지 않는다. `plan_revision_id`는 `project.active_plan_revision_id`에서 파생한다. 실제 `tasks`는 `vm_tasks`로 옮긴다. 실제 task 상태 어휘는 미확인이라 controller task로 매핑하지 않는다 |
| final_report → controller | `goal_verdict{verdict_id,outcome}`, `evidence` | `verdict: GoalVerdict`, `usage`, `execution_summary` 등 | 변경 없음. controller는 `report_unverified`로 판정(완료로 간주하지 않음) |
| VM ID 형식 | AIK identifier 규칙(`[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`) | upstream `_ENTITY_ID_PATTERN`(미확인) | 적합성 시험이 검사한다. 불일치하면 시험이 `CONFORMANCE_FINDING`으로 실패하며 bridge는 해당 요청을 `INVALID_REQUEST`로 거절한다 |

## Job 상태 해석 (`JOB_STATUS_PHASES`)

| RuntimeJobStatus | JobPhase | 의미 |
|---|---|---|
| `scheduled` | pending | 예약됨 |
| `running`, `interrupting` | running | provider가 아직 효과를 낼 수 있음 |
| `provider_terminal` | terminal | provider turn 종료. Core가 소비하지 않았고 **외부 효과 완료·확정이 아니다**(`core_consumed=False`). **guard를 해제하지 않는다** |
| `consumed` | terminal | Core가 결과를 소비함(`core_consumed=True`). attempt guard 해제의 유일한 근거 |
| `collector_lost` | unknown | collector가 turn을 잃음. provider가 여전히 동작할 수 있음 |
| `cancelled` | unknown | Core가 아직 settle하지 않은 취소일 수 있음(`unsettled_cancelled_runtime_job`). guard를 해제하지 않음 |
| 그 밖의 값 | unknown | 알 수 없는 값 |

`JobPhase.terminal`은 표시용 단계다. 재실행 guard 해제는 phase가 아니라 `core_consumed`로 판단한다.

**attempt guard 해제 조건.** 다음을 모두 만족하는 binding 기반 관측에서만 해제한다.

- 같은 attempt의 job이 `consumed`로 관측됨
- 그 attempt의 run_once가 진행 중이 아님
- 관측을 시작한 뒤 이 bridge가 같은 attempt에 새 run_once를 발사하지 않음(발사 generation 비교)
- 마지막 발사 응답이 job ID를 알려 준 경우, 관측된 job ID가 그 값과 같음

**project guard 해제 조건.** project guard는 관측으로 해제하지 않는다. project 단위 command(authorize/replan/pause/cancel 등)의 효과를 특정 job과 대조할 수 없기 때문이다. 호출자가 VM 상태를 직접 확인한 뒤 `VmBridge.reconcile_project(project_id)`로 명시적으로 해제한다. 이 메서드는 아무것도 검증하지 않으며 attempt guard에는 영향이 없다. guard는 원래 인스턴스 메모리이므로, 이 해제는 새 bridge 인스턴스를 만드는 것보다 권한이 넓지 않다.

어떤 해제도 외부 효과의 확정이 아니며, 효과 판정은 계속 VM Core 소유다.

## Port와 어댑터 사용법

`VmApplicationPort`는 설치된 `EngineApplication`을 감싸 평범한 mapping을 반환하는 adapter의 계약이다. 메서드 이름과 인자는 공개 façade와 같다. `handshake()`는 surface revision, engine schema revision, 메서드 signature, extension 선언을 설명해야 한다.

```python
from aik_vm_bridge import (EngineApplicationPort, VmBridge, PINNED_SURFACE_REVISION,
                           SUPPORTED_ENGINE_SCHEMA_REVISIONS, ProjectQuery)
from aik_workspace.vm_projects import BridgeProjectPort

# app: 호출자가 만든 upstream EngineApplication 인스턴스 (EngineApplication(service))
port = EngineApplicationPort(
    app,
    surface_revision=PINNED_SURFACE_REVISION,                    # 설치물 revision 선언(호출자 책임)
    engine_schema_revision=SUPPORTED_ENGINE_SCHEMA_REVISIONS[0], # 원장 schema revision 선언(호출자 책임)
)
bridge = VmBridge(port, "contracts/upstream/vm-public-surface.json")
info = BridgeProjectPort(bridge).lookup_project(project_id)   # status()["project"]의 id/root 대조
project = port.lookup_project(project_id)                      # EngineProject 또는 None(PROJECT_NOT_FOUND)
```

- revision 두 값을 생략하면 handshake 형식이 불완전해 bridge가 모든 호출을 `INVALID_RESPONSE`로 거절한다. 선언값이 실제 설치물과 맞는지는 어댑터가 검증할 수 없다(한계).
- 어댑터 오류는 `EnginePortError(code, category)`다. 원문 메시지는 포함하지 않는다. upstream 예외는 `__cause__`와 `__context__`에도 남기지 않는다. bridge의 `VmBridgeError`도 port 예외를 두 속성에 남기지 않는다.
  - `EngineApplicationError`는 선행 코드를 보존한다(예: `GOAL_REVISION_ACTIVE_PLAN: …` → `GOAL_REVISION_ACTIVE_PLAN`).
  - 코드가 없으면 `ENGINE_APPLICATION_ERROR`다.
  - 분류할 수 없는 예외는 `code=None`이며, bridge에서 조회는 `QUERY_FAILED`, 명령은 `OUTCOME_UNKNOWN`이 된다.
  - JSON 형태로 바꿀 수 없는 응답은 `INVALID_ENGINE_RESPONSE`다.
  - 어댑터가 만든 분류 코드(`ADAPTER_CODES`: `INVALID_ENGINE_RESPONSE`, `ENGINE_APPLICATION_ERROR`, `ENGINE_LEDGER_ERROR`, `ENGINE_METHOD_ABSENT`, `PROJECT_MISMATCH`)는 bridge에서 `vm_code`로 보고하지 않는다. 응답 형식 오류는 `INVALID_RESPONSE`, 나머지는 코드 없는 실패와 같게 처리한다. `PROJECT_NOT_FOUND`는 VM의 "없음" 결과를 옮긴 것이므로 `vm_code`로 유지한다.
- `port.calls`는 호출한 façade 메서드 이름만 기록한다(진단용).
- `port.upstream_binding_methods_present`는 upstream에 durable binding 메서드가 있는지 보고한다(현재 False).
- **메시지 기반 분류 한계:** `PROJECT_NOT_FOUND` 분류는 upstream 메시지 문자열 일치에 의존한다. upstream 문구가 바뀌면 `ENGINE_LEDGER_ERROR`(조회 불가)로 떨어진다. 이는 fail-closed 방향이지만 "없음"을 인식하지 못하게 된다.
- **선행 코드 한계:** `EngineApplicationError`의 선행 토큰이 대문자 식별자 형식이면 코드로 보존한다. upstream 코드 allowlist가 없어, 그 형식을 가진 다른 선행 토큰도 코드로 전달될 수 있다.

## Capability preflight와 handshake

- **preflight:** 생성 시 선택한 command마다 선언 표면에 메서드와 bridge가 쓰는 인자가 있는지 확인한다. 없으면 port 호출 전에 `CAPABILITY_MISSING`으로 실패한다.
- **handshake:** 첫 호출 때 한 번 수행하고 결과를 bridge 인스턴스 수명 동안 캐시한다.

| handshake 결과 | 오류 |
|---|---|
| surface revision 불일치, 지원하지 않는 engine schema revision(현재 `4`만) | `VERSION_MISMATCH` |
| 인자 이름 목록 불일치 | `CONTRACT_MISMATCH` |
| 형식 오류 | `INVALID_RESPONSE` |

- **실제 대조 결과:** 적합성 시험은 12개 command에서 실제 `inspect.signature`, 선언 표면, `REQUIRED_PARAMETERS`가 일치하는지 대조한다. 타입 annotation은 비교하지 않는다. handshake의 revision 두 값은 시험이 주입한 **가정**이므로, 그 검사는 설치물의 revision을 검증하지 않는다.

## 효과 전 binding (run_once)

순서는 다음과 같다.

1. **extension 확인:** handshake가 `aik-task-execution-binding-v1` extension을 선언하지 않거나 port에 extension 메서드가 없으면 `SEAM_UNAVAILABLE`로 거절한다. handshake 외에는 아무것도 호출하지 않는다. **실제 upstream 어댑터는 항상 이 경로로 차단된다**(적합성 시험 (e)).
2. **assignment 대조:** `verify_assignment`로 placement/lease/pool을 binding과 대조한다. 이는 로컬 사전 검사이며 내구적 fencing 전이나 실행 인가가 아니다.
3. **로컬 guard:** 미해결 attempt, 미해결 project, 진행 중 run_once가 있으면 `RECONCILIATION_REQUIRED`로 거절한다.
4. **owner query:** `attempt_detail`로 6개 필드(project/task/attempt/goal revision/plan revision/execution spec digest)가 모두 있고 일치해야 한다. 실제 `AttemptDetail`에는 `goal_revision_id`가 없어, extension이 생겨도 이 단계에서 `INVALID_RESPONSE`로 멈춘다. upstream 응답 확장 또는 별도 조회가 필요하다(미해결).
5. **기존 결속 조회:** `read_execution_binding`/`bind_execution`으로 결속을 조회·생성한다. 이미 같은 binding으로 결속된 attempt는 마지막 job이 `consumed`로 관측된 뒤에만 다시 실행한다(`resume`도 우회하지 못함).
6. **실행:** `run_once` 응답에 `task_id`·`attempt_id`가 반드시 있어야 하며, 없거나 다르면 outcome `unknown`이다. 응답의 job ID는 이후 관측 대조용으로 기록한다.

관측·취소·inventory는 VM에 보존된 원래 binding digest와 같아야 한다. binding 없는 project 단위 관측·취소도 허용한다.

**인가 결속 한계:** `authorize`는 `source`와 `selected_plan_revision_id`만 전달한다. `operating_policy`·`capability`·`authorization_target`은 VM 기본값이 적용되며, 그 의미는 확인하지 않았다.

## VM 설계 seam 위의 hosted runtime binding (`aik-hosted-runtime-v1`)

VM 설계(vibemarshal 32bb0f9 코드와 설계 문서)에는 `bind_execution`/`read_execution_binding` 확장이 없고, 앞으로 만들 계획도 없다. 대신 VM은 두 가지를 제공한다.

- `EngineApplication(service, runtime=...)`은 주입된 `CodexRuntimePort` 구현으로 `RuntimeJobSupervisor`를 만든다.
- runtime 메서드를 호출하는 동안 `current_operation_trace_scope()`로 Attempt, runtime intent, provider call을 알 수 있다.

AIK는 이 두 seam과 owner query(`attempt_detail`·`status`)만으로 효과 전 binding을 AIK 쪽에 남긴다. 위의 확장 기반 경로(`run_once`·`RunOnceRequest`)는 바꾸지 않았고, 실제 VM에서는 여전히 `SEAM_UNAVAILABLE`로 차단된다.

**구성 요소**

- `AikHostedRuntime(runtime_for, store, assignment_source)`: VM에 주입하는 runtime이다. 메서드 이름·인자·기본값이 실제 `CodexRuntimePort` 9개 메서드와 같다(실제 upstream 적합성 시험 (f)).
  - `runtime_for(connection_id)`는 연결별 inner runtime을 돌려준다. 예를 들어 계정 설정을 고른 VM 기존 runtime이다. AIK는 provider를 새로 만들지 않는다.
  - `assignment_source(cwd, trace)`는 현재 placement/lease/route를 담은 `HostedAssignment`를 돌려준다.
- `ExecutionBindingStore(path)`: AIK 소유 SQLite journal(`aik_runtime.binding_journal`)이다.
  - Attempt binding과 intent binding은 불변이다.
  - thread→connection 경로를 저장한다.
  - owner 확인이 끝난 `TaskExecutionBinding` v1과 결과 불명 표시도 저장한다. 재시작 뒤에도 같은 상태를 읽는다.
- `build_hosted_application(EngineApplication, service, hosted_runtime)`: VM application과 port를 같은 hosted runtime으로 함께 만든다.
  - 이렇게 만든 `EngineApplicationPort(hosted_runtime=...)`는 handshake에 `aik-hosted-runtime-v1`을 선언하고, store의 `read_execution_binding`을 제공한다.
  - `bind_execution`은 제공하지 않는다.
  - 주입된 application이 실제로 이 runtime을 쓰는지는 VM 비공개 속성을 읽지 않으므로 증명할 수 없다. 이 helper로 함께 만드는 것이 호출자 책임이다.
- `VmBridge(..., binding_store=store)`: `run_once_hosted(HostedRunOnceRequest)`와 `confirm_attempt(project_id, attempt_id)`를 제공한다.
  - port가 `hosted_runtime`을 노출하면, 그 runtime의 store와 같은 journal 파일이어야 한다. 다르면 `INVALID_REQUEST`다(검토 R1-F5).
  - 노출하지 않는 port는 호출자가 연결을 책임진다.
- binding journal은 SQLite로 열기 전에 기존 파일의 헤더(magic·application ID)를 확인한다. 빈 파일에 rollback journal이 함께 있으면 거절한다. 다른 SQLite 파일을 열다가 수정하는 일을 막기 위해서다(검토 R1-F7).

**provider 효과 전 순서** (`create_thread`·`start_turn`·`resume`·`interrupt`)

1. 호출 시점에 trace scope를 읽는다. VM trace 모듈은 이때만 lazy import한다. 모듈이나 `current_operation_trace_scope`가 없으면 `SEAM_UNAVAILABLE`로 거절한다(fail-closed). 그렇게 하지 않으면 Task 호출이 binding 없는 준비 호출로 처리되기 때문이다(검토 R1-F1).
2. assignment를 정하고, `binding.check_assignment`로 placement/lease/pool을 확인한다. 만료·비활성이면 `LEASE_NOT_USABLE`, fencing·ID가 다르면 `BINDING_MISMATCH`다.
3. store에 durable하게 기록한다. 같은 트랜잭션에서 in-flight 행도 남긴다(write-ahead, 검토 R1-F3).
4. 그 다음에만 원래 connection의 inner runtime을 부른다.
5. inner가 정상 반환하면 그 in-flight 행만 지운다. `create_thread`는 thread 경로 기록까지 성공한 뒤에 지운다.

inner 호출 중 프로세스가 죽어도 재시작 뒤 in-flight 행이 남아 같은 Attempt·thread의 다음 효과를 막는다. 준비 `create_thread`는 Attempt도 thread도 없으므로 그 행이 아무것도 막지 않는다. 중단되면 고아 행으로 남는다.

1~3단계에서 실패하면 inner를 부르지 않는다. 같은 Attempt에 다른 assignment가 오면 `BINDING_MISMATCH`이며 효과는 없다.

**호출 종류별 처리**

- **Task 실행 호출**(trace에 `attempt_id`가 있음): Attempt binding과 intent binding을 기록한다. `intent_id` 없이 `attempt_id`만 있으면 거절한다.
- **준비 호출**(Planning·Goal 준비처럼 Attempt가 없음): `TaskExecutionBinding`을 만들지 않고 thread 경로(`preparation`)만 남긴다. scope가 `None`이면 준비 호출로 보는 것은 "Task 실행 호출은 항상 scope 안에서 일어난다"는 VM 설계에 기댄 가정이다.
- **placement 결정:** `Placement`은 `vm_attempt_id`·`vm_task_id`가 필수다. 그래서 `assignment_source`는 호출 시점 trace의 Attempt로 placement를 정해야 한다.
  - VM은 역할 실행 도중 호스트가 다시 VM을 호출하는 것을 거절한다(`CORE_CAPABILITY_DENIED`).
  - 그래서 효과 시점에는 VM에 Task를 물을 수 없다. 그때 trace에는 Attempt·intent·call ID만 있다.
  - 이 경우 placement의 Task ID에 `HOSTED_TASK_PENDING`을 쓴다. 실제 Task는 owner 확인 때 VM에서 받아 binding에 고정한다.
  - placement에 실제 Task ID가 있으면 owner 확인 때 VM의 Task와 대조하고, 다르면 `BINDING_MISMATCH`다(검토 R1-F6).

**Windows E2E 진단**(`tools/diagnose_hosted_e2e.py`, CI job `hosted-e2e-diagnostic`)

- 고정 VM 소스로 합성 Goal·Plan을 준비하고, VM Windows 전용 RuntimeJob supervisor로 Task를 실행한다. 이때 AIK hosted runtime이 VM의 결정적 `FakeCodexRuntime`을 감싼다.
- 확인하는 항목:
  - 효과가 hosted runtime을 지난다.
  - trace의 Attempt·intent가 기록된다.
  - 효과마다 binding이 먼저 기록된다.
  - in-flight 행이 정리된다.
  - owner 확인이 성공한다.
- 모델·계정·네트워크는 쓰지 않는다.
- VM wheel은 개발자 모듈(`e2e_qualification` 등)을 빼므로, 이 진단은 VM을 소스에서 import한다. 그래서 설치형 wheel 근거가 아니다.
- governance gate는 진단용 허용 gate다. 그래서 통과해도 AGS 적합성, 제품 qualification, 통합 완료가 아니다.
- POSIX에서는 owner lock에서 멈추며 `NOT_RUN`으로 보고한다.

**원래 계정 정책**

- 기존 thread의 `start_turn`·`resume`·`interrupt`·`read`·`read_stored`는 저장된 경로의 connection으로만 간다. `assignment_source`가 다른 계정을 주어도 계정을 바꾸지 않는다.
- 경로 없는 thread는 `THREAD_ROUTE_UNKNOWN`으로 거절한다.
- 같은 thread ID가 서로 다른 connection에 있으면 `THREAD_ID_COLLISION`으로 거절한다(fail-closed).
- Task thread는 trace의 Attempt가 thread의 Attempt와 같아야 한다.

**interrupt 정책**

중단은 비용을 줄이는 안전한 방향이다. 그래서 다음 경우에도 원래 계정으로 중단하는 것을 허용한다.

- lease가 만료됐다. 그 사실(`lease_usable=false`)을 기록한다.
- 결과 불명(unknown)이나 in-flight 차단 상태다. 그 사실(`while_unresolved=true`)을 기록한다(검토 R1-F2).

다만 경로나 placement/lease/fencing 자체가 다르면 거절한다.

**결과 불명**

다음 경우 해당 Attempt나 thread를 unknown으로 표시하고 `OUTCOME_UNKNOWN`을 낸다.

- durable 기록 뒤 inner가 예외를 냈다.
- 새 thread ID를 읽을 수 없다.
- 경로 기록에 실패했다.

unknown으로 표시된 대상이나 in-flight 행이 남은 대상의 다음 효과(interrupt 제외)는 `RECONCILIATION_REQUIRED`로 막힌다. 호출자가 VM으로 대조한 뒤 `reconcile_attempt`/`reconcile_thread`로 명시적으로 풀어야 한다. 이때 in-flight 행도 지워진다. 자동 재실행은 하지 않는다.

bridge가 hosted 경로에서 Attempt 결과를 확정하지 못하면 그 Attempt를 journal에 durable하게 unknown으로 기록한다. hosted runtime이 그 기록을 읽고 다음 효과를 막는다. 기록에 실패하면 project guard를 대신 건다(검토 R1-F4).

**owner 확인** (`confirm_attempt`)

1. `attempt_detail`(attempt/task/execution spec digest/plan revision)과 `status`의 활성 goal·plan revision을 join한다. 실제 `AttemptDetail`에 `goal_revision_id`가 없기 때문이다. Plan이 활성인 동안 VM은 Goal revision 변경을 거절한다.
2. plan revision이 다르거나 없으면 `CONTRACT_MISMATCH`다.
3. 저장된 assignment, 첫 intent/call과 합쳐 `TaskExecutionBinding` v1을 완성하고 digest를 고정한다.
4. 효과 binding이 아직 없으면 `binding_pending`을 돌려준다. 이것은 성공 주장이 아니다.
5. 확인된 binding으로 hosted 모드의 `observe`·`cancel`·`inventory`가 동작한다.

**남은 한계**

- provider 호출이 supervisor 스레드에서 일어날 때도 trace scope가 존재한다는 점은 VM 설계 가정이다. 실제 설치형 RuntimeJob 실행으로 확인하지 않았다. RuntimeJob은 VM 1.0에서 Windows 전용이다.
- 실제 upstream 적합성 시험은 VM 표면이 맞는지까지만 확인한다.
  - (f) 시그니처 일치
  - (g) 실제 trace scope 읽기
  - (h) 실제 `EngineApplication`이 hosted runtime을 받아들임(생성만 하며 효과 없음)
- 실제 Task 실행, 실모델, Windows는 NOT_RUN이다.
- AGS governance gate가 A2 전환 전이라 실제 governed dispatch에 도달하지 못한다. 따라서 통합 완료가 아니다.

## 오류 의미 보존

| 상황 | 결과 |
|---|---|
| port 예외에 안전한 `code` 속성 | `VM_REJECTED`, `vm_code`에 원래 코드 |
| 효과성 호출의 코드가 `STALE_EXECUTION_INPUT`·`CORE_CAPABILITY_DENIED`·`MODEL_BINDING_CHANGED` | `known_failure`(**가정**, 실제 오류 목록과 미대조) |
| 효과성 호출의 그 밖의 코드(`RUNTIME_REQUIRED` 포함) | `unknown` |
| 조회의 코드 | `UNKNOWN` 토큰이 있으면 `unknown`, 아니면 `known_failure` |
| 어댑터 분류 코드(`ADAPTER_CODES`) | `vm_code` 없음. 응답 형식 오류는 `INVALID_RESPONSE`, 그 밖은 코드 없는 예외와 같음 |
| 코드 없는 예외 | 명령은 `OUTCOME_UNKNOWN`, 조회는 `QUERY_FAILED` |
| `observe`의 코드 없는 예외 | `OUTCOME_UNKNOWN`/`unknown`(upstream observe의 부수효과 가능성). guard는 추가하지 않음 |
| mapping이 아닌 응답 | `INVALID_RESPONSE` |

`observe`가 VM 코드로 거절된 경우(예: 실제 upstream의 `RUNTIME_REQUIRED`)는 조회 분류(`known_failure`)를 유지한다. 이는 해당 코드가 부수효과 전의 사전 조건 거절이라는 **가정**에 기반한다.

## request ID와 UNKNOWN

- **request ID 범위:** request ID는 bridge 로컬 식별자이며 VM에 전달되지 않는다.
- **request ID 소비:** 첫 port 호출 직전에 소비한다. 그보다 앞선 로컬 사전 거절은 소비하지 않는다.
- **자동 재시도 없음:** bridge는 어떤 실패도 자동 재시도하지 않는다.
- **attempt guard:** 결과가 불명확하거나 job 상태가 `unknown`(위 표의 `collector_lost`, `cancelled` 포함)이면 attempt를 미해결로 기록한다. 위 해제 조건을 만족하는 `consumed` 관측 전까지 재실행을 거절한다. `provider_terminal`, `running` 관측은 guard를 유지한다.
- **project guard:** project 단위 효과가 불명확하면 해당 project의 run_once를 막는다. `reconcile_project` 호출 전까지 유지된다.
- **guard의 범위:** 모든 guard는 bridge 인스턴스 단위 프로세스 메모리이며 내구성이나 exactly-once 보장이 아니다. `cancelled`/`collector_lost`로 남은 attempt는 이후 `consumed` 관측이 없으면 해당 인스턴스에서 계속 막힌다(liveness 한계).
- **job ID:** 실제 job ID가 AIK identifier 규칙에 맞지 않으면 `job_id=None`으로 기록된다. 마지막 발사 응답에 job ID가 없거나 발사 결과가 불명확하면 job ID 대조는 생략된다(발사 generation 대조만 적용, 한계).

## 여전히 남은 차단 사유

1. **durable binding:** 실제 `EngineApplication`에는 `bind_execution`/`read_execution_binding`이 없고, VM 설계에도 없다. 확장 기반 `run_once`는 계속 fail-closed다. VM 설계 seam 위의 hosted 경로(위 절)를 구현했지만, 실제 설치형 VM에서 Task를 실행하는 검증은 NOT_RUN이다(`AIK_DURABLE_ROUTE_BINDING_NOT_IMPLEMENTED`는 그 검증 전까지 유지).
2. **owner query 정보 부족:** 실제 `AttemptDetail`에 `goal_revision_id`가 없다. hosted 경로는 `status` join으로 결속하고, 확장 기반 경로는 여전히 `INVALID_RESPONSE`로 멈춘다.
3. **AGS manifest 부재**(`AGS_HOST_INTEGRATION_MANIFEST_MISSING`).
4. **설치형 검증 NOT_RUN:**
   - 설치형 wheel·Windows 호출 검증이 없다. 적합성 시험은 소스 import 기반이며 Linux 결과는 개발 검증이다.
   - surface/schema revision 선언값을 설치물에서 확인하는 절차가 없다.
5. **controller 연동 미완:** 실제 status의 task/stage 어휘, final report의 verdict 구조와 controller 형식 사이의 매핑이 미확정이다. 실제 VM에 대해 controller는 task 실행이나 완료 판정에 도달하지 않는다(fail-closed).
6. **ID 형식 미확인:** 실제 VM ID 형식과 AIK identifier 규칙의 호환성은 upstream 설치 환경의 적합성 시험 결과로만 확인된다.

## 검증

```bash
python -m unittest discover -s tests -p 'test_vm_bridge.py' -v
python -m unittest discover -s tests -p 'test_vm_real_conformance.py' -v
```

- **`test_vm_bridge.py`:** fake VM port와 합성 데이터만 쓴다. job 상태는 실제 `RuntimeJobStatus` 값이다.
- **`test_vm_real_conformance.py` — `EnginePortAdapterTests`, `ConformanceGateTests`:** 항상 실행한다. fake 앱으로 정규화와 오류 분류를 확인하고, NOT_RUN/required 모드 판정 자체를 확인한다.
- **`test_vm_real_conformance.py` — `RealUpstreamConformanceTests`:** `flowmarshal.engine`이 import 가능할 때만 실행한다. 임시 원장과 합성 프로젝트로 실제 façade를 호출하며 runtime·provider·네트워크는 쓰지 않는다. import할 수 없으면 `NOT_RUN` 사유로 skip되며, skip은 통과가 아니다.
- **선언 표면 파일:** 파일이 없으면 해당 대조는 로컬 실행에서 `NOT_RUN`으로 skip된다. `AIK_REQUIRE_UPSTREAM=1`에서는 skip 대신 **실패**한다.
- **required 모드 commit 확인:** `AIK_REQUIRE_UPSTREAM=1`에서는 설치 메타데이터(`direct_url.json`의 `vcs_info.commit_id`)가 `32bb0f9`로 시작해야 한다. 이 기록이 없는 설치는 commit을 확인할 수 없으므로 실패한다. 버전 문자열(`0.2.0a1`)만으로는 commit을 구분하지 못한다.

Linux 통과는 Windows 지원이나 설치형 호출의 증거가 아니다. 이번 검토 수정 후의 시험 실행 결과(fake 시험 포함)는 이 문서에 기록하지 않았다.
