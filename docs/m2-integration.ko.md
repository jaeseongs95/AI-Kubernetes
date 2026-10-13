# M2 결합 시험 — AIK-033~043·085

`tests/test_m2_integration.py`는 M2 모듈의 실제 인스턴스를 서로의 입력·출력으로 연결해, 모듈 단위 시험에서는 드러나지 않는 모듈 간 계약 불일치를 찾기 위한 시험이다. 이 시험은 새 기능을 추가하지 않는다. 장부 상태와 완료 판정도 바꾸지 않는다.

## 연결한 실제 모듈

| 단계 | 모듈 (실제 인스턴스) | 다음 단계로 넘기는 값 |
| --- | --- | --- |
| 저장 | `aik_runtime.RuntimeStore` | pool/profile/placement/lease 레코드 |
| 배치 | `aik_placement.PlacementPlanner` + `PlacementJournal` | placement ID·VM attempt ID |
| 게이트·lease | `aik_lifecycle.LifecycleGate`, `GatedLeaseManager`, `aik_lease.LeaseManager`, `aik_budget.BudgetStore` | lease ID(= 예산 reservation ID)·fencing token |
| 탄력 제어 | `aik_elastic.ElasticityController.tick` / `finish` | `TickResult.granted` |
| dispatch 기록 | `aik_dispatch.DispatchIdentity`, `DispatchJournal` | request ID·provider job ID·dispatch 상태 |
| 관측 | `aik_observer.WorkerObserver` (`start`/`ingest`/`poll`) | `ObservationView`(terminal·usage), 예산 정산 |
| 도구 | `aik_devtools.DevTools` (`create_file`, `run_verification`) | 파일 digest·검증 결과 |
| 증거 | `aik_evidence.relay.EvidenceRelay` (`collect`/`verify`) | artifact ID·sha256 |
| 독립 검사 | `aik_validation.IndependentChecker` | `CheckRecord` |
| 완료 보기 | `aik_validation.build_completion_view` | `CompletionView` |
| 읽기 모델 | `aik_readmodel.ReadModel`, `from_bridge_receipt`, `from_lease`, `SourceEvent` | attempt view·projection digest |
| 정리 | `aik_lifecycle.LifecycleController.teardown` (`ElasticityController.finish` 경유) | lease·placement 해제 |
| 복구 | `aik_recovery.RecoveryReconciler`, `RecoveryJournal` | 재시작 후 결정 기록 |

예산 승인 경로는 `LeaseManager.reserve`를 대체하지 않고 실제 `BudgetStore.admit`를 거친다. 합성 `RoleProfile`(테스트 fixture의 `WORKER`)과 신선한 합성 route를 사용한다. `BudgetPolicy` 필드는 공개 STUB에서 확인되지 않아 별도의 scope 한도를 설정하지 않았다.

`LifecycleController`의 `evidence_port`에는 fake가 아니라 실제 `EvidenceRelay`를 그대로 넣는다. `verify(artifact_id)`가 반환하는 `VerifyResult`가 teardown 증거 확인 입력과 맞는지도 이 연결로 확인한다.

## 경계 fake

- provider: `FakeProvider`. `start_job`/`observe_job`/`interrupt`만 제공한다. 실패는 실제 `fault_from_provider`와 `AdapterFailure`로 만든다. `http_status=400`은 known failure, `outcome_unknown=True`는 unknown이다.
- VM 응답: 합성 payload를 담은 실제 `aik_vm_bridge.bridge.BridgeReceipt`(`RUN_ONCE`, `ATTEMPT_DETAIL`, `FINAL_REPORT`)와 `VmRefs`.
- DevTools authority: `tests/test_devtools.py`와 같은 `FakeAuthority`.
- pool registry·capability inventory: `tests/test_placement.py`와 같은 fake.
- worker/effect/VM port: `FakeWorkerPort`, `FakeEffects`, `FakeVm`. `FakeEffects`는 lifecycle용 `unresolved`와 recovery용 `inflight`/`mark_unknown`/`resend_result`를 함께 제공한다.
- 시계: 단일 `FakeClock`(2030-01-01T00:00:00Z). 모든 파일은 OS 임시 디렉터리에 둔다.

## dispatch 경로

dispatch는 실제 `NativeDispatcher`와 실제 `aik_provider.controller.GuardedAdapter`를 거친다. fake provider는 `describe`·`auth_status`·`start_job`·`observe_job`·`interrupt_job`을 갖춘 완전한 `ProviderAdapter`이며 `tests/test_dispatch.py`와 같은 형태다. 따라서 다음은 모두 실제 모듈 코드가 처리한다.

- known/unknown `AdapterFailure`의 분류(`rejected`·`send_unknown`)
- lease intent 기록·job ID 결속
- `WorkerObserver.start`
- `confirm_started`의 `started` 전이

## 시나리오별 단언

1. **정상 경로**: 서로 다른 VM 프로젝트의 ready Task 2개가 `tick`에서 모두 granted된다. 다음이 각 단계를 거쳐 그대로 이어지는지 단언한다.
   - lease의 placement·pool·worker가 배치 결과와 일치하고, lease ID가 예산 reservation ID로 존재한다.
   - dispatch 기록의 lease ID·provider job ID가 runtime lease(active, provider_job_id)와 일치한다.
   - observer terminal·usage(`confirmed`)와 예산 `settlement`이 기록된다.
   - DevTools 파일 digest와 evidence sha256이 같다.
   - 고정 명령의 독립 검사가 PASS다.
   - completion view의 `dispatch_ack`·`provider_state`·`check_verdict`가 기대값이다. `vm_verdict_recorded`는 final_report에 `goal_verdict_id`가 있을 때만 True다.
   - read model `attempt_view`가 같은 attempt의 placement·lease·request·provider job을 연결하고, 다른 attempt의 lease를 섞지 않는다.
   - `finish` 뒤 lease와 placement가 `released`이고, owned worker가 종료된다.
2. **pause 경합**: 프로젝트 pause 중 `tick`은 granted가 없고, 활성 placement·보유 lease도 남기지 않는다. resume 후 같은 Attempt가 대기 사유 없이 granted된다.
3. **known 실패**: dispatch는 `rejected`로 기록되고 lease는 `released`된다. placement를 `failed`로 해제한 뒤 같은 Attempt는 `ATTEMPT_ALREADY_PLACED`로 대기·거절되고, 같은 프로젝트의 새 Attempt만 granted된다. provider 재호출은 없다.
4. **unknown + 재시작**: dispatch는 `send_unknown`으로 기록되고 lease는 보유 상태로 남는다. 같은 파일을 새 인스턴스로 다시 연 뒤 다음을 단언한다.
   - `RecoveryReconciler.reconcile`가 해당 lease에 release가 아닌 결정을 남긴다.
   - 미해결 효과에도 결정 또는 실패 기록이 남는다.
   - provider 재시작과 효과 재실행이 없다.
   - teardown은 미해결 효과 때문에 완료되지 않고, lease와 worker를 정리하지 않는다.
5. **독립성·oracle**: worker와 같은 세션 ID의 reviewer는 `NOT_INDEPENDENT`로 거절된다. 이때 명령은 실행되지 않고 이력도 남지 않는다. pinned oracle 변경은 `oracle_ok=False`이며 `ORACLE_CHANGED`로 판정된다. 두 경우 모두 `vm_verdict_recorded`를 바꾸지 않는다. 이 값은 VM final_report의 `goal_verdict_id`로만 정해진다.
6. **read model 재구축**: 정상 경로 이벤트를 하나씩 역순으로 적용한 뒤 다시 정순 중복으로 적용한다. 그 결과 duplicates는 전체 수, conflicts는 0이고, projection_digest는 정순 적용과 같다. `rebuild()` 결과도 같은 digest다.

## 실행 결과와 발견 사항

세션 Linux(Python 3.13)에서 `python -m unittest tests/test_m2_integration.py`로 6개 시나리오가 모두 통과했다. Windows는 NOT_RUN이다.

- **초안의 시험 입력 오류(수정함)**: pause 사유에 공백이 든 문장을 넣어 `INVALID_REASON`이 났다. `LifecycleGate`는 사유를 식별자 형식으로 받는다. 모듈 결함이 아니므로 시험 입력을 식별자로 고쳤다.
- **placement 재사용 규칙(불일치 없음)**: 시나리오 2(pause 뒤 같은 Attempt granted)와 시나리오 3(`failed` placement가 남은 같은 Attempt 거절)이 함께 통과한다. `ElasticityController`가 planner 호출 전에 lifecycle gate를 확인해 pause 중에는 placement를 만들지 않기 때문이다(AIK-041).
- **재시작 뒤 dispatch(불일치 없음)**: 같은 파일로 다시 연 `NativeDispatcher.recover()`는 `send_unknown` 기록을 바꾸지 않는다. 같은 request ID로 다시 dispatch해도 저장된 기록을 돌려주며 provider를 호출하지 않는다.
- **recovery effect port의 실제 adapter 부재(알려진 한계)**: `LifecycleController`의 effect port(`unresolved`)에는 실제 adapter `aik_lifecycle.BindingEffectAdapter`가 있다. 반면 `RecoveryReconciler`의 effect port(`inflight`·`mark_unknown`·`resend_result`)에는 실제 `ExecutionBindingStore`/`AikEffectJournal` adapter가 없어 이 시험도 fake를 쓴다. AIK-039 근거에 기록된 한계와 같다.
- **runtime 변경 피드 부재(알려진 한계)**: `aik_readmodel.from_lease`의 cursor는 호출자가 공급해야 한다. runtime 저장소에 변경 피드가 없어 시험이 cursor를 수동으로 증가시킨다(AIK-043 한계).

## NOT_RUN

- 실제 provider·credential·네트워크 호출(GuardedAdapter·NativeDispatcher는 실제 코드지만 provider는 fake)
- 실제 VM 설치(EngineApplication)·VM History·AGS PEER 연결
- Windows 11 실행(클린 설치 포함). Linux에서 통과하더라도 개발 검증일 뿐 Windows 지원 증거가 아니다.
- 다중 프로세스 동시 실행, 전원 장애

이 문서와 시험은 어느 경로도 qualified로 주장하지 않으며, AIK-033~043·085의 장부 상태를 바꾸지 않는다.
