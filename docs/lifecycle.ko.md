# AIK-038 — 일시정지·취소·drain·자원 반환 (부분 구현)

`src/aik_lifecycle/`는 **신규 배치를 멈추는 동작**과 **실행 중인 provider job을 끝내는 동작**을 분리해 구현한다. Python 표준 라이브러리와 기존 패키지(`aik_runtime`, `aik_lease`, `aik_observer`, `aik_provider.contracts`)만 사용한다. provider·worker·도구 효과·근거 저장은 주입형 port로만 접근한다. 이 패키지는 provider를 직접 호출하거나 credential을 읽지 않으며, 실행 권한·VM 판정·AGS verdict를 만들지 않는다.

## 구성

| 구성 요소 | 역할 |
| --- | --- |
| `LifecycleGate(path)` | scope(`global`, `pool:<id>`, `project:<vm_project_id>`)의 `pause`/`drain`/`resume`. 현재 상태와 append-only 이벤트를 저장한다. `is_open(pool_id, vm_project_id)`는 `(bool, 사유)`를 반환한다. |
| `GatedLeaseManager(lease_manager, gate, placement_lookup)` | `aik_lease.LeaseManager.reserve()` 전후에 gate를 확인한다. 닫힌 scope에서는 lease를 만들지 않고 `LeaseResult("wait", ...)`와 `SCOPE_PAUSED@<scope>`/`SCOPE_DRAINING@<scope>` 사유를 반환한다. 나머지 메서드는 그대로 위임한다. |
| `WorkerRegistry(path)` | `WorkerRecord(worker_id, ownership, pool_id)`. `owned`는 AIK가 만든 worker, `borrowed`는 빌려 쓰는 기존 호스트다. 한 번 기록한 소유권은 바꿀 수 없다. |
| `LifecycleController(...)` | `cancel`, `settle_cancel`, `teardown`, `view`. 모든 결정은 append-only lifecycle 이벤트로 남는다. |
| `BindingEffectAdapter` | `ExecutionBindingStore`(in-flight·unknown)와 `AikEffectJournal`(receipt 대기·unknown)의 공개 API를 `effect_port.unresolved(vm_attempt_id)`로 감싼다. |

`placement_lookup`은 `runtime_placement_lookup(RuntimeStore)`로 만들 수 있다. 조회 실패나 gate DB 오류는 `GATE_UNAVAILABLE`로 예약을 막는다(fail-closed).

## 신규 예약 차단

`pause`/`drain`이 반환되면 그 뒤에 시작한 `GatedLeaseManager.reserve()`는 lease를 만들지 않는다. 예약 도중 pause가 끼어들어 grant가 이미 저장된 경우에도 grant 직후 gate를 다시 확인하고, 아직 dispatch가 없으므로 `release(dispatched=False)`로 즉시 반환한 뒤 `wait`를 돌려준다. 반환이 실패하면 `LEASE_RELEASE_PENDING` 사유를 붙이며 이 lease는 grant로 전달되지 않는다.

`pause`와 `drain`은 모두 신규 예약을 막는다. 차이는 운영 의미다. drain은 실행 중인 작업이 끝날 때까지 기다린 뒤 scope를 계속 닫아 두는 상태로 기록된다. 어느 쪽도 실행 중인 provider job을 건드리지 않는다. 실행 중인 job 종료는 아래 `cancel`이 맡는다.

`aik_lease.LeaseManager`를 직접 쓰는 호출자는 gate를 우회한다. 제품 경로는 반드시 `GatedLeaseManager`를 사용해야 한다.

## 취소 요청·확인·늦은 성공

`cancel(lease_id, fencing_token, *, capabilities, request=None, receipt=None)`

1. fencing token이 현재 lease와 같지 않으면 아무것도 기록하지 않고 `StaleLease`로 거절한다.
2. `cancel_requested`를 먼저 기록한다.
3. dispatch가 없는 reserved lease는 `release(dispatched=False)`로 반환한다(`cancel_before_dispatch`).
4. 관측 기록이 이미 terminal이면(취소 직전에 결과가 나온 경우) interrupt를 보내지 않고 결과를 그대로 분류한 뒤 lease를 반환한다.
5. `AdapterCapabilities.job_interrupt`가 `Support.SUPPORTED`이고 `implementation_state == "implemented"`일 때만 `job_port.interrupt(request, receipt)`를 호출한다. 응답은 `cancel_interrupt_sent`(보고 상태 포함)로 기록하며 **종료 확인이 아니다**. 호출 예외나 receipt 불일치는 `cancel_interrupt_unknown`이다. 미지원이면 `cancel_unsupported`를 기록하고 provider job을 그대로 둔다. receipt가 없는 dispatch 의도는 `cancel_dispatch_unconfirmed`다.
6. 어느 경우든 lease는 `release(dispatched=True, provider_terminal=False)`로 **draining만** 한다. `unknown` lease는 그대로 점유한다.

`settle_cancel(lease_id, fencing_token)`은 `WorkerObserver.poll`로 provider 상태를 관측한다. terminal이 아니면 아무것도 바꾸지 않는다. terminal이면 `CANCELLED`는 `cancel_confirmed`, `SUCCEEDED`는 `late_success`, `FAILED`는 `terminal_failed`로 **서로 다른 이벤트**로 기록하고, 그 뒤에만 lease를 `released`(또는 `unknown`이면 `resolve_unknown`)로 반환한다. usage 정산은 observer가 terminal 최초 관측 시 수행한 것을 그대로 둔다. 이 패키지는 usage를 0이나 추정치로 정산하지 않는다.

## teardown과 worker 소유권

`teardown(lease_id, fencing_token, *, required_artifacts)`는 다음 순서를 모두 충족할 때만 자원을 반환한다. 하나라도 미충족이면 해당 대기 이벤트를 기록하고 멈춘다. 다시 호출하면 이어서 진행한다.

1. `effect_port.unresolved(vm_attempt_id)`가 비어 있어야 한다. in-flight·unknown 효과나 receipt 대기(늦은 receipt) 효과가 있거나 조회가 실패하면 `awaiting_effect_resolution`.
2. `required_artifacts` 각각에 대해 `evidence_port.verify`가 `intact`여야 한다. 아니면 `awaiting_evidence_handoff`(누락 artifact ID 기록).
3. dispatch된 job은 observer 기록이 terminal이어야 한다. receipt 없는 dispatch 의도는 종료를 확인할 수 없으므로 `awaiting_provider_terminal`.
4. lease를 반환한다. 같은 worker가 다른 lease를 아직 점유하면 `worker_in_use`로 멈춘다. `owned`면 `worker_port.terminate`, `borrowed`면 terminate하지 않고 `worker_returned`만 기록한다. 그 뒤 `worker_port.cleanup`으로 임시 데이터를 제거하고 `teardown_complete`를 기록한다.

lease가 이미 반환됐더라도 불명확한 효과나 미보관 근거가 남아 있으면 `teardown_complete`를 기록하지 않고 cleanup도 호출하지 않는다. lease 회수만으로 정상 완료로 처리하지 않는다. 미등록 worker나 pool이 다른 worker는 `worker_unregistered`로 멈추며 terminate하지 않는다.

`view(lease_id)`는 `LifecycleView(lease_id, stage, events)`를 반환한다. `stage`는 마지막 이벤트 종류다. `cancel_outcome`, `teardown_complete` 속성으로 결과를 구분한다.

## 저장소

lifecycle DB는 별도 SQLite 파일(application_id `0x41494B4C`, `AIKL`, schema 1)이다. `aik_runtime.paths` 규칙을 그대로 따르므로 개발 원장(`backlog.sqlite3`와 sidecar), Git 저장소 내부, symlink·hardlink·장치 이름 경로를 거절한다. 기존 파일은 헤더의 application ID가 맞을 때만 연다. 다른 프로그램의 DB는 수정하지 않고 `InvalidDatabase`로 거절한다. 새 파일은 임시 파일에서 schema를 만든 뒤 원자적으로 교체한다. `gate_events`, `workers`, `lifecycle_events`는 trigger로 UPDATE/DELETE를 거절한다. 이벤트에는 고정 코드·ID·provider job 상태만 저장한다.

## 한계와 미검증(NOT_RUN)

- 실제 provider adapter의 interrupt·observe, 실제 worker 종료·정리, 실제 VM/AGS·effect journal 연동은 실행하지 않았다(**NOT_RUN**). 시험은 fake port와 합성 데이터만 사용한다.
- `Support.SUPPORTED` 멤버 이름에 의존한다. 해당 멤버가 없으면 모든 adapter를 미지원으로 처리한다(fail-closed).
- 실행 중 job의 일괄 취소(scope 단위 stop)는 제공하지 않는다. 호출자가 scope를 pause한 뒤 lease별로 `cancel`을 호출한다.
- 시험의 observer 정산 대상은 `settle`만 기록하는 합성 sink다. 실제 `BudgetStore.settle`와의 결합은 이번 시험 범위 밖이다.
- Windows 동작(경로 규칙·잠금·rename 내구성)은 기존 `aik_runtime.paths` 구현에 의존하며 Windows에서 실행하지 않았다(**NOT_RUN**). Linux 통과는 개발 검증일 뿐이다.

## 검증

```powershell
python -m unittest discover -s tests -p 'test_lifecycle.py' -v
```

이 문서와 테스트 결과는 AIK-038의 구현 근거일 뿐 완료 판정이 아니다. 장부 상태는 변경하지 않으며, 실제 provider·worker·VM/AGS 통합과 Windows 11 검증 전에는 qualified로 주장하지 않는다.
