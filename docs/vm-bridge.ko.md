# AIK-031 — VM EngineApplication command/query bridge (부분 구현)

`src/aik_vm_bridge/`는 GUI·총괄이 VM의 공개 façade(`EngineApplication`)에 Goal 준비·수정·인가·실행·관측·일시정지·취소·상태·재계획·최종 보고를 요청할 때 쓰는 AIK 쪽 typed bridge다. Python 표준 라이브러리와 저장소의 `aik_runtime`(식별자 규칙·Pool/Placement/Lease 모델), `aik_provider.contracts`(인증 종류)만 사용한다.

**현재 상태: 구현·fake port 단위시험까지.** 실제 VM 설치물 호출, Windows 실행, provider 연동은 모두 **NOT_RUN**이다. 이 문서와 테스트 통과를 qualified·완료 판정으로 해석하지 않는다. 장부 상태는 변경하지 않는다.

## 경계와 소유권

- VM 내부 모듈·SQLite 저장 형식을 import·복제하지 않는다. 사용하는 근거는 `contracts/upstream/vm-public-surface.json`(AIK-001 소스 projection)과 `integration-contract.json`, `task-execution-binding.schema.json`의 선언된 표면뿐이다.
- 모든 Goal/Plan/Task 변경은 주입된 port의 공개 command 호출로만 한다. bridge 소스에는 SQL·DB 연결이 없다(테스트로 확인).
- VM의 Task/Attempt/verdict를 복제하지 않는다. 응답의 ID(project/goal/goal revision/plan revision/task/attempt/job/authorization), execution spec digest, `history_cursor`는 `VmRefs`에 그대로 복사하며 응답 전체는 읽기 전용 snapshot(`BridgeReceipt.payload`)으로 보존한다.
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

- 생성 시 선택한 command마다 선언 표면에 메서드와 bridge가 사용하는 인자(예: `run_once`의 `resume`, `cancel`의 `reason`)가 있는지 확인한다. 없으면 `CAPABILITY_MISSING`(details에 `명령:사유`)로 port 호출 전에 실패한다. port에 해당 메서드가 없어도 같은 오류다.
- 첫 호출 때 handshake를 한 번 수행한다. surface revision 불일치·지원하지 않는 engine schema revision(현재 `4`만)은 `VERSION_MISMATCH`, 메서드의 인자 이름 목록 불일치는 `CONTRACT_MISMATCH`, 형식 오류는 `INVALID_RESPONSE`다. 타입 annotation은 비교하지 않는다(한계).

## 효과 전 binding (run_once)

`RunOnceRequest`는 binding과 AIK `Placement`·`Lease`·`Pool`을 함께 받는다. 순서는 다음과 같다.

1. handshake가 `aik-task-execution-binding-v1` extension을 선언하지 않거나 port에 extension 메서드가 없으면 `SEAM_UNAVAILABLE`로 **아무 호출 없이** 거절한다(fail-closed). 현재 VM 배포물에서는 이 경로로 차단된다.
2. `verify_assignment`로 placement/lease/pool과 binding의 VM ID·pool·connection·provider·fencing token을 대조하고, lease 상태(`reserved/active`)·만료·pool 사용 설정을 확인한다. 이 검사는 로컬 사전 검사이며 내구적 fencing 전이나 실행 인가가 아니다.
3. owner query `attempt_detail(vm_attempt_id)`로 정확한 Attempt와 Task를 읽고 project/task/attempt/goal revision/plan revision/execution spec digest가 다르면 `BINDING_MISMATCH`로 효과 전에 멈춘다.
4. `bind_execution`이 같은 binding digest를 돌려줘야 한다.
5. `run_once(project_id, resume=…)`는 job 예약·관측 후 신속히 반환한다고 가정한다. 응답의 task/attempt가 binding과 다르면 `BINDING_MISMATCH`(outcome `unknown`).

관측·취소·inventory(`model_binding_status`)는 VM에 보존된 **원래 binding digest**와 요청 binding이 같아야 한다. connection revision 회전 등으로 다르면 port의 관측·취소를 호출하지 않는다. binding 없는 project 단위 관측·취소(준비 단계)도 허용하며, 가짜 Task/Attempt를 만들지 않는다.

## 오류 의미 보존

| 상황 | 결과 |
|---|---|
| port 예외에 안전한 `code` 속성(대문자·숫자·`_`) | `VM_REJECTED`, `vm_code`에 원래 코드 그대로(`STALE_EXECUTION_INPUT`, `CORE_CAPABILITY_DENIED`, `MODEL_BINDING_CHANGED` 등) |
| 코드에 `UNKNOWN` 토큰 포함(`EXTERNAL_UNKNOWN`) | outcome `unknown` |
| 코드 없는 예외, 명령 | `OUTCOME_UNKNOWN`(outcome `unknown`) |
| 코드 없는 예외, 조회 | `QUERY_FAILED`(known failure) |
| mapping이 아닌 응답 | `INVALID_RESPONSE`(명령은 `unknown`) |

원시 예외 메시지·traceback은 오류 문자열에 넣지 않고 체인도 끊는다(`from None`).

## UNKNOWN과 재발사 금지

- bridge는 어떤 실패도 자동 재시도하지 않는다. request ID는 실패 후에도 재사용할 수 없다(`DUPLICATE_REQUEST`).
- run_once·binding·취소 결과가 불명확하거나 job 상태를 인식할 수 없으면 해당 attempt를 `unresolved_attempts`에 넣고, 이후 `run_once`는 `RECONCILIATION_REQUIRED`로 거절한다. 원래 binding으로 `observe`해 해당 attempt의 인식 가능한 상태를 받은 뒤에만 해제한다.
- 인식하는 job 상태 문자열(`queued/pending/reserved/prepared`, `running/started/in_progress`, `succeeded/completed/failed/cancelled/interrupted/proven_no_effect/expired`)은 실제 `RuntimeJobStatus` 값과 대조하지 않은 **가정**이다. 모르는 값은 `unknown`이다.
- 중복·미해결 guard는 프로세스 메모리에만 있다. 재시작 내구성이나 exactly-once 보장이 아니며, 내구 기록은 VM 원장과 extension의 책임이다.

## 완료를 막는 조건

`integration-contract.json`의 미해결 gate `AGS_HOST_INTEGRATION_MANIFEST_MISSING`, `AIK_DURABLE_ROUTE_BINDING_NOT_IMPLEMENTED`(`BLOCKING_GATES`)가 AIK-031을 차단한다. 완료 기준 4의 효과 전 내구 binding과 원래 계정 기반 관측·취소·재시작은 공개 seam만으로 보장되지 않으므로, upstream extension과 호환 설치물, 실제 owner query adapter, 설치형 호출 검증(Windows 포함)이 필요하다. AIK가 VM SQL을 직접 수정하거나 별도 재시도 권위를 만들어 우회하지 않는다.

## 검증

```bash
python -m unittest discover -s tests -p 'test_vm_bridge.py' -v
```

fake VM port와 합성 데이터만 사용한다. Linux 통과는 개발 검증이며 Windows 지원·설치형 호출의 증거가 아니다. 실제 VM 설치·호출, AGS 연동, provider 실행은 **NOT_RUN**이다.
