# AIK-039 — 재시작·unknown 결과·재시도 reconciliation (부분 구현)

`src/aik_recovery/`는 앱·sidecar·네트워크 장애 뒤 재시작한 프로세스가 **새 dispatch 전에 한 번** 호출하는 복구 reconciler와 별도 결정 기록(journal)을 제공한다. Python 표준 라이브러리와 저장소 내부 패키지(`aik_runtime`, `aik_lease`, `aik_observer`)만 사용한다.

이 패키지는 provider job을 새로 시작하지 않고, tool 효과를 다시 실행하지 않으며, retry를 만들지 않는다. VM 원장의 attempt 상태와 unknown/retry 계약이 권위이고, 이 패키지는 그 결과를 조회해 소비만 한다. **exactly-once 실행이나 전달을 주장하지 않는다.**

## 구성

| 구성 | 역할 |
| --- | --- |
| `RecoveryReconciler(runtime_store, lease_manager, observer, *, vm_port, job_port, effect_port, journal, clock=None)` | 저장된 lease·provider job·in-flight 효과를 VM 상태와 대조하고 결정을 기록 |
| `vm_port.attempt_state(vm_attempt_id)` | `"active"`·`"terminal"`·`"unknown"`·`"absent"` 중 하나. 예외나 다른 값은 `unavailable`로 기록하고 fail-closed로 처리 |
| `job_port.observe(request, receipt)` | `observer.poll(lease_id, job_port)`를 통해 호출. observer에 저장된 request/receipt(같은 connection·account)를 그대로 사용하며 다른 연결로 묵시 routing하지 않음 |
| `effect_port.inflight()` / `mark_unknown(ref)` / `resend_result(ref)` | in-flight tool call 조회·VM unknown 절차 인계·같은 결과 재전송 |
| `ToolEffectAdapter(dispatch_journal, *, resend, is_cancelled=None)` | API agent loop 경로의 실제 effect port. `aik_dispatch.DispatchJournal`의 tool event를 정본으로 읽는다(아래 절) |
| `EffectRef` | `vm_attempt_id`, `tool_call_id`, `operation`, `has_receipt`, `model_result_returned`, `idempotency`(`read_only`/`idempotent`/`non_idempotent`), 선택 `cancelled`, `receipt_ref` |
| `RecoveryJournal(path)` | 별도 SQLite append-only 결정 기록 |

`lease_manager`는 `runtime_store`와 같은 runtime DB를 관리해야 한다. reconciler는 동시에 하나만 실행한다고 가정하며 여러 reconciler 간 조정은 구현하지 않는다.

## lease 판단

`reserved`·`active`·`draining`·`unknown` lease를 모두 조회한다.

| 상황 | 동작(action) | 결과 |
| --- | --- | --- |
| provider job 기록 있음, 같은 계정 관측 결과 terminal | `release_provider_terminal` | `release(dispatched=True, provider_terminal=True)`, unknown이면 `resolve_unknown(provider_terminal=True)` |
| provider job 실행 중 | `keep_running` | 변경 없음. VM attempt가 terminal/absent이면 이유 코드로 불일치만 기록 |
| 관측 실패·observer 기록 없음·job/attempt 식별 불일치 | `observe_failed_keep_unknown` | lease를 unknown으로 둠(이미 unknown이면 유지). 재실행 없음 |
| dispatch 기록 없는 `reserved` + VM attempt terminal/absent | `release_undispatched` | `release(dispatched=False)` |
| dispatch 기록 없는 `reserved` + VM active/unknown/조회 불가 | `await_vm` | 변경 없음. TTL 만료는 `LeaseManager.expire_due` 책임 |
| job ID 없는 `active`/`draining`/`unknown`(dispatch 의도만 기록) | `handoff_vm_unknown` | unknown으로 두고 VM unknown 계약으로 인계. 재실행 없음 |

live→unknown 전이는 `LeaseManager.mark_unknown(lease_id, fencing_token, expected_revision=...)`으로 한다. 읽은 뒤 lease가 바뀌었으면(`LEASE_CHANGED`) 기록은 `failed`로 남고 상태는 바뀌지 않는다. unknown은 용량과 예산을 계속 점유하는 상태(`HOLDING_STATES`)이며 반환이 아니다. 모든 lease 전이는 `LeaseManager`의 fencing 검사를 거친다.

## 효과 판단

| 상황 | 동작 | 이유 |
| --- | --- | --- |
| receipt 없음(효과 직후 receipt 전 crash 가능, 취소 뒤 늦은 효과 포함) | `handoff_vm_unknown` → `effect_port.mark_unknown` | 효과 발생 여부를 모른다. 재실행하지 않는다. 비멱등은 `NON_IDEMPOTENT_HALT_UNTIL_CONFIRMED`로 확인 또는 VM 판단 전까지 중단 |
| receipt 있음, 모델 응답 전, VM attempt active | `resend_result` → `effect_port.resend_result` | 같은 결과의 재전송. 효과는 재실행하지 않음 |
| receipt 있음, 취소됐거나 VM attempt terminal/absent | `record_late_receipt` | 기록만 하며 재전송·재실행하지 않음 |
| receipt 있음, VM 상태 unknown/조회 불가 | `await_vm` | 기록만 함 |
| receipt와 결과 반환 완료 | `no_action_result_delivered` | 기록만 함 |

## API 경로 effect port (`ToolEffectAdapter`)

`ApiToolLoop`(`aik_dispatch.api_loop`, 문서에서 'API agent loop')은 tool call마다 `DispatchJournal`에 `intent` → `receipt`·`denied`·`effect_unknown` 중 하나 → `returned` 순서로 기록한다. adapter는 이 기록만 공개 메서드(`open_tool_requests`·`record`·`tool_events`·`tool_event`)로 읽고 쓴다.

| 저널 상태 | `EffectRef` | reconciler 판단 |
|---|---|---|
| `intent`만 있음, 또는 `effect_unknown` | `has_receipt=False` | `handoff_vm_unknown`: `mark_unknown`이 `effect_unknown`(`RECOVERY_RECEIPT_MISSING`)을 한 번만 추가한다. 이미 있으면 아무것도 하지 않는다. |
| `receipt` 또는 `denied`, `returned` 없음 | `has_receipt=True`, `model_result_returned=False` | VM attempt가 active면 `resend_result`, terminal·absent·취소면 `record_late_receipt` |
| `returned` 있음 | 목록에 넣지 않음 | 없음 |

- **`resend_result`:** 효과를 실행하지 않고 `returned`도 쓰지 않는다. 호출자의 `resend(request_id, tool_call_id)`에 넘기기만 한다. 실제 재전송과 `returned` 기록은 `ApiToolLoop.resume`가 한다(아래 `LoopResendScheduler`). `resume`은 저널의 결과를 replay하고, 결과 digest가 다르면 `REPLAY_MISMATCH`로 멈춘다.
- **이미 전달된 sibling:** `resume` 한 번은 같은 request의 대기 중인 call을 모두 보낸다. 직전 `inflight()` 스캔에 있던 call이 그 사이 `returned`가 되면 이미 전달된 것으로 보고 다시 넘기지 않는다.
- **`denied`:** 효과가 없었다고 기록된 결과이므로 receipt처럼 다룬다.
- **멱등성 분류:** `read_file`과 지원하지 않는 도구는 `read_only`, 그 밖의 도구는 `non_idempotent`로 분류한다. DevTools의 effect ID replay는 프로세스 메모리에만 있어 재시작 뒤에는 쓸 수 없다.
- **스캔 뒤 상태 변화:** 스캔과 동작 사이에 receipt가 도착하면 `EFFECT_STATE_CHANGED`로 거절한다. reconciler는 이를 실패로 기록하고 다음 reconcile에서 다시 판단한다.
- **여러 request의 같은 tool call:** 같은 Attempt의 같은 tool call ID를 두 request가 가지면 `AMBIGUOUS_EFFECT`로 스캔 전체를 거절한다(fail-closed).
- **취소 여부:** `is_cancelled(vm_attempt_id)`를 주면 그 값을 `cancelled`로 쓴다. 주지 않으면 `False`이며, 취소된 Attempt는 VM 상태(terminal)로 판단한다.
- **hosted runtime 경로:** 이 경로의 tool 효과(`AikEffectJournal`)는 다루지 않는다. 그 경로의 unknown 절차 권위는 VM에 있다.

## 재전송 스케줄러 (`LoopResendScheduler`)

`LoopResendScheduler(dispatch_journal, contexts)`는 `ToolEffectAdapter`의 `resend`로 넘기는 호출 가능 객체다. 받은 request에 대해 실제 `ApiToolLoop.resume`을 부른다.

- **resume 입력은 호출자 소유:** `contexts(request_id)`는 `ResumeContext(loop, identity, session, input_revision, authorization_revision, messages)`를 돌려준다. messages에는 assistant `tool_calls`가 들어 있어야 한다. dispatch journal은 결과 본문이나 메시지를 저장하지 않으므로 스케줄러가 만들어 내지 않는다. 없으면 `RESUME_CONTEXT_MISSING`, request ID가 다르면 `RESUME_CONTEXT_MISMATCH`다.
- **request당 한 번:** `resume`은 request의 대기 call을 한꺼번에 처리하고 다음 모델 호출로 이어간다. 그래서 한 스케줄러 안에서 request마다 한 번만 실행하고, 같은 request의 다른 call은 그 결과를 재사용한다. 모델은 다시 호출되지 않는다.
- **전달 확인:** 해당 call에 `returned`가 기록돼야 성공이다. `resume`이 `effect_unknown`으로 멈추거나 예산·취소·한도 때문에 `returned` 전에 멈추면 `RESEND_NOT_DELIVERED`다. `DispatchError`는 `RESEND_<코드>`, `DispatchOutcomeUnknown`은 `RESEND_OUTCOME_UNKNOWN`이다. 모두 `RecoveryError`이므로 reconciler는 `applied`가 아니라 `failed`와 그 코드를 기록한다.
- **프로세스 재시작 한계:** DevTools effect-ID replay는 프로세스 메모리에만 있다. 새 DevTools에서 receipt가 있는 call을 replay하면 `RESEND_REPLAY_MISMATCH`로 실패하고 `returned`를 쓰지 않는다. 이때 멱등 도구(`create_file`)는 DevTools가 기존 파일을 다시 관측하며, 권한 계층에 receipt가 한 번 더 전달된다. 새 intent·파일 변경은 없다. 영속 replay 저장소는 구현하지 않았다.

## 결정 기록

`RecoveryJournal`은 `aik_runtime.paths` 규칙을 따른다. 개발 원장·Git 내부·symlink·hardlink 경로를 거절한다. application ID는 `0x41494B59`(`AIKY`), schema version은 1이다. `UPDATE`/`DELETE`는 trigger가 거절한다. 다른 앱 SQLite 파일이나 SQLite가 아닌 파일은 수정하지 않고 `InvalidDatabase`로 거절한다.

각 행에는 다음 값을 저장한다.

- `decision_id`, `subject_kind`(lease/effect/scan), `subject_id`
- `action`, `outcome`, `reasons`(고정 코드)
- `observed`: `vm_state`, `job_state`, `lease_state`, `lease_revision`, fencing 값, 적용 뒤 `result_state`/`result_revision` 등
- `evidence_refs`: lease·placement·attempt·provider job·tool call·receipt ID
- `decided_at`

원래 evidence·receipt·lease 기록은 덮지 않고 ID로만 참조한다. raw 출력·비밀·provider 원문은 기록하지 않는다.

- **상태를 바꾸는 동작:** `planned` 행을 먼저 쓰고 동작을 적용한 뒤 `applied` 또는 `failed`(실패 코드 포함) 행을 쓴다. planned 기록이 실패하면 동작은 수행하지 않는다.
- **효과 port 동작:** 같은 subject·action의 `applied` 행이 있으면 다시 호출하지 않는다. 실패한 호출은 다음 reconcile에서 재시도한다.
- **변경 없는 판단:** 같은 subject의 마지막 행과 내용 fingerprint가 같으면 다시 쓰지 않는다. 두 번째 이후 reconcile에서 중복 동작과 중복 기록을 막기 위한 장치다.

## 한계와 알려진 crash 구간

- 동작 적용 후 `applied` 행을 쓰기 전에 crash가 나는 경우:
  - lease: 다음 실행에서 이미 종료된 lease는 조회 대상이 아니므로 `planned` 행만 남는다.
  - 효과: `mark_unknown`(멱등 가정) 또는 `resend_result`가 한 번 더 호출될 수 있다. 결과 재전송은 효과 재실행이 아니지만 중복 전달이 0회라고 보장하지 않는다.
- 효과 상태 정본은 effect journal/VM에 있다. `EffectRef`는 adapter가 보고한 값이며 이 패키지가 검증할 수 없다.
- API 경로 effect port는 `ToolEffectAdapter`로 실제 `DispatchJournal`에 연결했다. hosted runtime 경로의 `ExecutionBindingStore`/`AikEffectJournal` adapter와 VM 공개 조회 adapter는 아직 없고 주입형 port 계약만 있다.
- 취소된 provider job의 interrupt·cancel은 수행하지 않는다. 이 패키지는 cancel 판단을 기록만 한다.

## 검증

```powershell
python -m unittest discover -s tests -p 'test_recovery*.py' -v
```

시험은 임시 폴더의 실제 `RuntimeStore`·`BudgetStore`·`LeaseManager`·`WorkerObserver`와 fake VM/job/effect port, 주입 시계를 사용한다. 다음 경우를 확인한다.

- dispatch 경계 crash
- terminal 관측 뒤 해제와 같은 route·계정 사용
- 실행 중 유지
- 관측 실패 시 unknown 유지와 재발사 없음
- receipt 전 crash 시 `mark_unknown`
- receipt 뒤 모델 응답 전 `resend_result`
- 취소 뒤 늦은 receipt·효과
- append-only 기록과 외부 파일 거절
- 재부팅 뒤 두 번째·세 번째 reconcile의 멱등성
- `tests/test_recovery_adapter.py`: 실제 `DispatchJournal`의 crash 상태(intent만·receipt·denied·effect_unknown·returned)를 실제 `RecoveryReconciler`로 대조한다. `mark_unknown` 1회 기록, resend 인계만 하고 `returned`를 쓰지 않음, 스캔 뒤 상태 변화 거절, 스캔 뒤 `returned`된 call 재전송 생략, 모호한 tool call 거절, 두 번째 reconcile에서 재전송 반복 없음을 확인한다.
- `tests/test_resend_resume.py`: 실제 `ApiToolLoop`을 crash시킨 뒤 새 `RecoveryReconciler`·`ToolEffectAdapter`·`LoopResendScheduler`가 실제 `resume`을 부르는 8개 시험이다. receipt 뒤 crash는 같은 결과를 1회 재전송하고 `returned`를 기록한다(효과 intent 1회, 두 번째 reconcile 무동작). denied는 DevTools 없이 재전송한다. 두 대기 call은 resume 1회로 처리한다. intent만 있는 sibling은 `RESEND_NOT_DELIVERED`로 `failed`가 된다. 새 DevTools는 `RESEND_REPLAY_MISMATCH`, context 없음·불일치는 journal 변화 없이 `failed`이고, 같은 스케줄러는 나중에 등록된 context로 다시 성공한다.

**미검증(NOT_RUN):**

- 실제 provider·VM/AGS 설치와 연결
- hosted runtime 경로(`AikEffectJournal`) effect adapter
- 실제 실행 프로세스에서 resume context(메시지 기록)를 보관·복원하는 저장소. 시험에서는 호출자가 메모리로 넘긴다.
- 프로세스 재시작 뒤 DevTools 결과 replay(영속 replay 저장소 없음)
- Windows 11 실행(잠금·경로 동작 포함)
- 실제 프로세스 강제 종료·전원 차단 상태의 복구

Linux 통과는 개발 검증이며 Windows 지원 증거가 아니다. qualified를 주장하지 않으며, 장부의 AIK-039 상태와 완료 판정은 변경하지 않는다.
