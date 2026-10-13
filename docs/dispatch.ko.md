# AIK-035 — native·API dispatch 골격 (부분 구현)

`src/aik_dispatch/`는 VM이 **이미 예약한** RuntimeJob/Attempt를 AIK lease와 provider 연결에 결속해 내보내고, 그 과정을 append-only로 기록하는 골격이다. AIK-035의 완료가 아니며 장부 상태와 완료 판정을 바꾸지 않는다.

권위 경계:

- VM이 Task·Attempt·재시도·완료의 권위다. 이 패키지는 Attempt를 만들거나 재시도·완료를 판정하지 않는다.
- AGS gate는 VM이 집행한다. AIK는 gate를 생략·대체하지 않는다.
- provider job 상태, 모델 루프 종료, journal 기록은 VM 완료 판정이나 도구 효과 완료의 증거가 아니다.

## DispatchIdentity

| 필드 | 의미 / 출처 |
|---|---|
| `request_id` | provider `JobRequest.request_id`와 같은 값 |
| `contract_digest` | VM execution spec digest (`sha256:<64 hex>`) |
| `vm_project_id`·`vm_task_id`·`vm_attempt_id` | VM 외부 ID |
| `lease_id`·`fencing_token`·`pool_id` | AIK-034 lease |
| `route_id`·`binding` | 역할별 route와 정확한 `ConnectionBinding`(connection·revision·account fingerprint) |
| `canonical_cwd` | 호출자가 `aik_workspace`에서 얻은 canonical root (절대 경로) |
| `input_digest` | 입력 manifest digest |
| `path` | `native` 또는 `api` |
| `observed_permission`·`trusted_same_user` | **관측했을 때만** 채운다. 관측이 없으면 `None`이며 추정하지 않는다 |

`identity_digest`는 키 정렬 canonical JSON(binding은 필드 dict)의 SHA-256이다. 같은 `request_id`에 digest가 다른 identity가 오면 `CONFLICT`다. 같은 모델명이나 host라는 이유로 다른 연결로 바꾸지 않는다: 연결이 다르면 digest가 달라진다.

## Journal

별도 SQLite(`application_id = 0x41494B44`, schema 1). `aik_runtime.paths`의 `validate_database_path`·`database_lock`을 재사용하므로 개발 원장, Git 저장소, symlink/hardlink 경로를 거절한다. 테이블:

- `dispatches(request_id, identity_json, identity_digest, created_at)`
- `dispatch_events(seq, request_id, state, data_json, at)` — `(request_id, state)` 유일
- `tool_events(seq, request_id, tool_call_id, state, data_json, at)` — `(request_id, tool_call_id, state)` 유일

세 테이블 모두 UPDATE/DELETE trigger로 거절한다. 연결은 연산마다 열고 `contextlib.closing`으로 닫는다. 이벤트 데이터에는 ID·digest·예외 클래스 이름만 남기며 provider 응답 원문, 예외 메시지, 도구 인자·파일 내용은 남기지 않는다.

## 디스패치 상태 (native)

| 상태 | 의미 |
|---|---|
| `reserved` | lease에 dispatch intent(`activate`, job id 없음)를 남긴 뒤 기록. provider 호출 전 |
| `sent_ack` | provider가 job을 받아 같은 연결의 `JobReceipt`를 반환. job id를 lease에 남기고 observer 시작 |
| `started` | 같은 `GuardedAdapter`로 관측한 상태가 실행 중 또는 terminal |
| `send_unknown` | `start_job` 예외·응답 유실·다른 연결의 receipt. 효과 불명이며 재전송하지 않는다 |
| `rejected` | `GuardedAdapter`가 `AdapterFailure(outcome=known_failure)`로 거절. provider 계약상 job이 시작되지 않았으므로 lease를 `release(dispatched=False)`로 반환하고 `DispatchRejected`를 던진다 |

현재 상태는 마지막 이벤트이고, 전이는 `reserved→sent_ack|send_unknown|rejected`, `sent_ack→started`만 허용한다.

- 같은 `request_id`·같은 digest의 재호출은 기존 기록을 그대로 반환하고 provider를 부르지 않는다(`send_unknown`·`rejected`여도 마찬가지). 재시도 여부는 VM이 새 Attempt·request로 정한다.
- `recover()`는 재시작 뒤 마지막 상태가 `reserved`인 기록을 `send_unknown`으로 바꾸고 그 기록들을 반환한다.
- `send_unknown`의 lease는 `active`+job id 없음으로 남는다. 이 상태의 정리는 AIK-039 복구가 VM unknown/reconciliation 절차로 넘길 대상이며 이 패키지는 처리하지 않는다.
- provider가 receipt를 반환한 뒤 lease 갱신이나 observer 시작이 실패하면 `sent_ack`에 `bookkeeping: failed:<클래스>`를 남기고 원래 예외를 다시 던진다.

native 경로는 기존 VM runtime이나 `aik_vm_bridge.AikHostedRuntime`을 대체하지 않는다. 이 브리지는 이미 예약된 Attempt가 어느 연결로 전송됐는지를 기록할 뿐이다. 실제 OS 권한·same-user 여부의 관측 코드는 포함하지 않으며 identity에 전달된 관측값만 저장한다.

## API 도구 루프

`ApiToolLoop(model_port, tools, journal, *, max_model_calls, max_tool_calls, budget_ok, cancel=None)`.

- 각 모델 호출과 도구 호출 전에 `cancel` → `budget_ok()` → 상한 순서로 확인한다. `budget_ok()`가 정확히 `True`가 아니면 `budget_stop`이다.
- 종료 이유는 `end_turn | max_model_calls | max_tool_calls | budget_stop | cancelled | effect_unknown`이다.
- 도구는 `read_file`·`create_file`·`update_file`·`run_verification`만 허용하며 AIK-085 `DevTools`로 실행한다. DevTools가 AIK-029 gateway 인가를 거친다. 허용 밖 도구나 잘못된 인자는 효과 없이 오류 결과로 모델에 돌려준다(`denied`).
- 도구 순서는 다음과 같다.
  1. `intent`: 인자 SHA-256 digest와 effect id만 기록한다.
  2. DevTools를 호출한다.
  3. `receipt`: 결과 종류, 결과 digest, `replayed`를 기록한다.
  4. 결과를 다음 모델 메시지에 넣는다.
  5. 다음 모델 호출 직전에 `returned`를 기록한다.
- `Invocation`의 effect id는 읽기면 `None`, 쓰기·검증이면 `effect-<tool_call_id>`다.
- `EffectUnknown`이면 `effect_unknown`을 기록하고 루프를 멈춘다. 재시도는 없으며 VM unknown/reconciliation으로 넘긴다.
- 그 밖의 `DevToolError`는 효과 없는 거절(`denied`)로 처리한다.
- 예상하지 못한 예외는 `effect_unknown`을 기록한 뒤 `DispatchOutcomeUnknown`으로 끝난다.
- `LoopResult.unresolved_tool_calls`에는 receipt가 없는 모든 호출이 담긴다. 루프 종료가 모든 도구 효과의 완료라는 뜻이 아니다.

### 재생 규칙 (`resume`)

- `intent`만 있고 `receipt`가 없는 호출은 실행하지 않는다. `effect_unknown`을 기록하고 멈춘다.
- `receipt`가 있고 `returned`가 없는 호출은 다음 순서로 처리한다.
  1. 호출자가 넘긴 메시지에서 해당 assistant tool call을 찾는다.
  2. 인자 digest와 revision이 `intent` 기록과 같은지 확인한다.
  3. 같은 `Invocation`으로 DevTools를 다시 부른다.
  4. DevTools 재생 결과가 `replayed=True`이고 결과 digest가 `receipt`와 같아야 재전송한다. 아니면 `REPLAY_MISMATCH`로 멈춘다.
- `denied`는 저장된 오류 코드로 다시 보낸다.
- 재생은 DevTools의 재생 계약에 의존한다. DevTools가 결과를 보존하지 않는 새 프로세스에서의 재생 동작은 검증하지 않았다(NOT_RUN).

## 오류

`DispatchError.code`의 값은 다음과 같다.

- `IDENTITY_MISMATCH`, `CONFLICT`, `INVALID_STATE`, `REPLAY_MISMATCH`, `INVALID_JOURNAL`
- `DispatchOutcomeUnknown`(`OUTCOME_UNKNOWN`)

메시지는 고정 코드와 고정 필드명만 담는다.

## 검증과 한계

```bash
python -m unittest tests/test_dispatch.py -v
```

시험은 합성 데이터와 임시 디렉터리만 사용한다. 실제 RuntimeStore·BudgetStore·LeaseManager·WorkerObserver, `GuardedAdapter`로 감싼 fake provider, 실제 DevTools와 fake authority, fake model port를 쓴다. Linux 통과는 개발 검증이며 Windows 지원 증거가 아니다.

NOT_RUN 항목:

- 실제 provider 실행
- 실제 API 모델 호출(API client 코드 없음)
- VM `run_once`·RuntimeJob과의 결합
- AGS gate 실행
- native OS 권한·same-user 관측
- 프로세스 재시작 뒤 DevTools 재생
- Windows 실행

API 경로의 lease 활성화·budget 정산 연결도 구현하지 않았다. 전체 live governed qualification은 AIK-086 범위다. qualified를 주장하지 않는다.
