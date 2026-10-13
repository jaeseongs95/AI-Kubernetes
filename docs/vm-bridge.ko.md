# AIK-031 — VM EngineApplication command/query bridge (부분 구현)

`src/aik_vm_bridge/`는 GUI·총괄이 VM의 공개 façade(`EngineApplication`)에 Goal 준비·수정·인가·실행·관측·일시정지·취소·상태·재계획·최종 보고를 요청할 때 쓰는 AIK 쪽 typed bridge다. Python 표준 라이브러리와 저장소의 `aik_runtime`(식별자 규칙·Pool/Placement/Lease 모델), `aik_provider.contracts`(인증 종류)만 사용한다.

**현재 상태: 구현·fake port 단위시험까지.** 실제 VM 설치물 호출, Windows 실행, provider 연동은 모두 **NOT_RUN**이다. 이 문서와 테스트 통과를 qualified·완료 판정으로 해석하지 않는다. 장부 상태는 변경하지 않는다.

## 경계와 소유권

- VM 내부 모듈·SQLite 저장 형식을 import·복제하지 않는다. 사용하는 근거는 `contracts/upstream/vm-public-surface.json`(AIK-001 소스 projection)과 `integration-contract.json`, `task-execution-binding.schema.json`의 선언된 표면뿐이다.
- 모든 Goal/Plan/Task 변경은 주입된 port의 공개 command 호출로만 한다. bridge 소스에는 SQL·DB 연결이 없다(테스트로 확인).
- VM의 Task/Attempt/verdict를 복제하지 않는다. 응답의 ID(project/goal/goal revision/plan revision/task/attempt/job/authorization), execution spec digest, `history_cursor`는 `VmRefs`에 그대로 복사한다. 응답 전체는 VM 원본과 분리된 snapshot(`BridgeReceipt.payload`)으로 보존한다. 중첩 mapping은 `MappingProxyType`, 중첩 list는 일반 list와 같다고 비교되지만 `append`·항목 대입 등 일반 변경 메서드를 거절하는 읽기 전용 list다. `list.append(x, …)`처럼 기반 타입 메서드를 직접 부르는 우회까지 막지는 않는다. `copy.copy`·pickle 결과는 일반 list다.
- job의 `terminal` 단계는 provider/job 종료일 뿐 VM 완료 판정이 아니다.

## 구성

| 모듈 | 역할 |
|---|---|
| `surface.py` | 선언 표면 로딩, capability preflight, 버전 handshake |
| `binding.py` | `TaskExecutionBinding`(schema v1 24개 필드), 정규 digest, AIK Placement/Lease/Pool 대조 |
| `bridge.py` | typed request/receipt, port Protocol, `VmBridge` |
| `errors.py` | `BridgeCode`, `EffectOutcome`, `VmBridgeError` |

## Port

`VmApplicationPort`는 설치된 `EngineApplication`을 감싸 **평범한 mapping**을 반환하는 adapter의 계약이다. 메서드 이름과 인자는 공개 façade와 같다(`prepare/revise/authorize/run_once/observe/pause/cancel/status/replan/final_report/attempt_detail/model_binding_status`). 추가로 `handshake()`가 설치물의 `surface_revision`, `engine_schema_revision`, 메서드 signature, 선언 extension을 설명해야 한다. 실제 VM에는 `handshake`가 없으므로 adapter가 설치물 introspection으로 만들어야 하며, 그 adapter는 아직 구현·검증되지 않았다(NOT_RUN).

`VmBindingExtensionPort`(`bind_execution`, `read_execution_binding`)는 효과 전 binding을 VM 쪽에 내구적으로 결속·조회하는 **upstream extension 가정**이다. 현재 VM 공개 표면에는 없다.

## Capability preflight와 handshake

- 생성 시 선택한 command마다 선언 표면에 메서드와 bridge가 사용하는 인자(예: `run_once`의 `resume`, `cancel`의 `reason`)가 있는지 확인한다. 없으면 `CAPABILITY_MISSING`(details에 `명령:사유`)으로 port 호출 전에 실패한다. port에 해당 메서드가 없어도 같은 오류다.
- 첫 호출 때 handshake를 한 번 수행한다. surface revision 불일치·지원하지 않는 engine schema revision(현재 `4`만)은 `VERSION_MISMATCH`, 메서드의 인자 이름 목록 불일치는 `CONTRACT_MISMATCH`, 형식 오류는 `INVALID_RESPONSE`다. 타입 annotation은 비교하지 않는다(한계).
- **한계:** handshake 결과는 bridge 인스턴스 수명 동안 캐시한다. 실행 중 VM 설치물이 교체·업그레이드되면 새 bridge 인스턴스를 만들어야 다시 검증된다. extension 메서드(`bind_execution`/`read_execution_binding`)는 handshake의 extension 선언과 callable 여부만 확인하며 signature·버전은 비교하지 않는다. 이 extension의 signature는 선언된 공개 표면에 아직 없기 때문이다.

## 효과 전 binding (run_once)

`RunOnceRequest`는 binding과 AIK `Placement`·`Lease`·`Pool`을 함께 받는다. 순서는 다음과 같다.

1. handshake가 `aik-task-execution-binding-v1` extension을 선언하지 않거나 port에 extension 메서드가 없으면 `SEAM_UNAVAILABLE`로 **handshake 외 호출 없이** 거절한다(fail-closed). 현재 VM 배포물에서는 이 경로로 차단된다.
2. `verify_assignment`로 placement/lease/pool과 binding의 VM ID·pool·connection·provider·fencing token을 대조하고, lease 상태(`reserved/active`)·만료·pool 사용 설정을 확인한다. 이 검사는 로컬 사전 검사이며 내구적 fencing 전이나 실행 인가가 아니다.
3. 이 bridge의 미해결 attempt·미해결 project·같은 attempt의 진행 중 run_once가 있으면 `RECONCILIATION_REQUIRED`로 거절한다(subject `vm_attempt_id`/`vm_project_id`/`attempt_in_flight`).
4. owner query `attempt_detail(vm_attempt_id)`로 정확한 Attempt와 Task를 읽는다. project/task/attempt/goal revision/plan revision/execution spec digest **6개 필드가 모두 있어야** 하며, 하나라도 없으면 `INVALID_RESPONSE`, 다르면 `BINDING_MISMATCH`(둘 다 known failure)로 효과 전에 멈춘다. 필드 부재를 일치로 간주하지 않는다.
5. `read_execution_binding(vm_attempt_id)`로 기존 결속을 조회한다.
   - 결속이 없으면 `bind_execution`을 호출하고, 같은 binding digest가 돌아와야 한다(다르면 `BINDING_MISMATCH`, outcome `unknown`).
   - 다른 digest로 이미 결속돼 있으면 `BINDING_MISMATCH`(subject `persisted_binding`, known failure)로 거절하며 재결속·재실행하지 않는다.
   - 같은 digest로 이미 결속돼 있으면, 이 bridge가 그 binding으로 해당 attempt의 `terminal` job을 관측한 경우에만 재결속 없이 진행한다. 그렇지 않으면 `RECONCILIATION_REQUIRED`(subject `attempt_bound`)다. `resume=True`도 이 조건을 우회하지 않는다.
   - 조회 실패는 `QUERY_FAILED`, 형식 오류는 `INVALID_RESPONSE`이며 효과 전에 멈춘다.
6. `run_once(project_id, resume=…)`는 job 예약·관측 후 신속히 반환한다고 가정한다. 응답에 `task_id`·`attempt_id`가 **반드시 있어야** 하며, 없으면 `INVALID_RESPONSE`, binding과 다르면 `BINDING_MISMATCH`이다. 두 경우 모두 outcome은 `unknown`이고 해당 attempt를 미해결로 기록한다.

관측·취소·inventory(`model_binding_status`)는 VM에 보존된 **원래 binding digest**와 요청 binding이 같아야 한다. connection revision 회전 등으로 다르면 port의 관측·취소를 호출하지 않는다. binding 없는 project 단위 관측·취소(준비 단계)도 허용하며, 가짜 Task/Attempt를 만들지 않는다. binding 없는 취소의 결과가 불명확하면 아래처럼 project 전체의 run_once를 막는다.

**인가 결속 한계:** `authorize`는 `source`, `selected_plan_revision_id`만 전달한다. 선언 signature의 `operating_policy`·`capability`·`authorization_target`은 표현할 수 없어 VM 기본값이 적용되며, 그 의미는 확인하지 않았다. `run_once`는 authorization ID나 인가된 plan revision을 별도로 대조하지 않는다. 기존 인가의 보존은 VM의 판정에 위임된다.

## 오류 의미 보존

| 상황 | 결과 |
|---|---|
| port 예외에 안전한 `code` 속성(대문자·숫자·`_`) | `VM_REJECTED`, `vm_code`에 원래 코드 그대로 |
| 효과성 호출(명령·`bind_execution`·`run_once`·`cancel`)의 코드가 `STALE_EXECUTION_INPUT`·`CORE_CAPABILITY_DENIED`·`MODEL_BINDING_CHANGED` | outcome `known_failure`(효과 전 거절로 **가정**, 실제 VM 오류 목록과 대조하지 않음) |
| 효과성 호출의 그 밖의 모든 코드(`EXTERNAL_UNKNOWN`, `ECONNRESET`, `TIMEOUT` 등) | outcome `unknown` |
| 조회의 코드 | `UNKNOWN` 토큰이 있으면 `unknown`, 아니면 `known_failure`(조회는 효과가 없음) |
| 코드 없는 예외, 명령 | `OUTCOME_UNKNOWN`(outcome `unknown`) |
| 코드 없는 예외, 조회 | `QUERY_FAILED`(known failure) |
| mapping이 아닌 응답 | `INVALID_RESPONSE`(명령은 `unknown`) |

원시 예외 메시지·traceback은 오류 문자열에 넣지 않고 체인도 끊는다(`from None`). 단, 형식(대문자·숫자·`_`, 64자 이내)에 맞는 `code` 값은 그대로 `vm_code`와 오류 문자열에 들어간다. adapter는 이 속성에 VM 오류 코드만 넣어야 한다(한계).

## request ID

- request ID는 **bridge 로컬 식별자**이며 VM port에 전달되지 않는다. 따라서 VM 쪽 상관관계·중복 방지에는 쓰이지 않는다(완료 기준 2의 "request ID 보존"은 로컬 범위로만 충족).
- 작업의 첫 port 호출 직전(handshake 제외)에 request ID를 소비한다. 소비 후에는 실패해도 재사용할 수 없다(`DUPLICATE_REQUEST`). 그보다 앞선 로컬 사전 거절(handshake 실패, `SEAM_UNAVAILABLE`, assignment 대조 실패, `RECONCILIATION_REQUIRED`)은 request ID를 소비하지 않는다.

## UNKNOWN과 재발사 금지

- bridge는 어떤 실패도 자동 재시도하지 않는다.
- run_once·binding·취소 결과가 불명확하거나 job 상태를 인식할 수 없으면 해당 attempt를 `unresolved_attempts`에 넣는다. 이후 `run_once`는 `RECONCILIATION_REQUIRED`로 거절한다. 원래 binding으로 `observe`해 해당 attempt의 **`terminal`** job을 받은 뒤에만 해제한다. `pending`/`running` 관측은 job이 아직 효과를 낼 수 있으므로 해제하지 않는다. 관측된 job ID가 처음 예약한 job과 같은지는 대조하지 않는다(한계).
- project 단위 효과(prepare/revise/authorize/pause/replan/cancel)의 결과가 불명확하면 project를 `unresolved_projects`에 넣는다. 해당 project의 `run_once`는 `RECONCILIATION_REQUIRED`(subject `vm_project_id`)로 거절된다. 해당 project의 `observe`가 `terminal` job을 보고할 때만 해제한다. job이 없는 project(예: prepare 불명확)는 이 bridge 인스턴스에서 해제되지 않는다. 그 job이 원래 효과와 관련된 것인지는 확인하지 않는다(한계).
- 인식하는 job 상태 문자열(`queued/pending/reserved/prepared`, `running/started/in_progress`, `succeeded/completed/failed/cancelled/interrupted/proven_no_effect/expired`)은 실제 `RuntimeJobStatus` 값과 대조하지 않은 **가정**이다. 모르는 값은 `unknown`이다.
- 중복·미해결·진행 중 guard는 **bridge 인스턴스 단위 프로세스 메모리**이며 다른 인스턴스(예: GUI와 총괄이 각자 만든 bridge)와 공유되지 않는다. 같은 인스턴스 안에서는 잠금으로 request 소비와 미해결·진행 중 검사를 원자화하고, 같은 attempt의 동시 `run_once`를 거절한다. 재시작 내구성이나 exactly-once 보장이 아니며, 내구 기록은 VM 원장과 extension의 책임이다. 인스턴스 간 중복 실행 방지는 VM에 보존된 binding 조회(위 5단계)에만 의존한다.

## 완료를 막는 조건

`integration-contract.json`의 미해결 gate `AGS_HOST_INTEGRATION_MANIFEST_MISSING`, `AIK_DURABLE_ROUTE_BINDING_NOT_IMPLEMENTED`(`BLOCKING_GATES`)가 AIK-031을 차단한다. 완료 기준 4의 효과 전 내구 binding과 원래 계정 기반 관측·취소·재시작은 공개 seam만으로 보장되지 않는다. upstream extension과 호환 설치물, 실제 owner query adapter, 설치형 호출 검증(Windows 포함)이 필요하다. AIK가 VM SQL을 직접 수정하거나 별도 재시도 권위를 만들어 우회하지 않는다.

## 검증

```bash
python -m unittest discover -s tests -p 'test_vm_bridge.py' -v
```

fake VM port와 합성 데이터만 사용한다. 선언 표면·binding schema·integration contract 파일이 저장소에 없으면 해당 대조 시험은 `NOT_RUN` 사유와 함께 skip되며, 통과로 간주하지 않는다. Linux 통과는 개발 검증이며 Windows 지원·설치형 호출의 증거가 아니다. 실제 VM 설치·호출, AGS 연동, provider 실행은 **NOT_RUN**이다.
