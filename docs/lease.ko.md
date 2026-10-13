# AIK-034 — worker lease·동시성 예약·fencing

`src/aik_lease/`는 기간과 작업 범위를 가진 사용권(lease)을 원자적으로 예약하고 중복 점유를 막는다. Python 표준 라이브러리와 기존 패키지(`aik_runtime`, `aik_budget`, `aik_pool`, `aik_profiles`)만 사용한다. 새 DB 테이블을 만들지 않는다. lease는 `aik_runtime.Lease` 레코드로 저장하고, 예산 예약은 `aik_budget.BudgetStore`를 사용한다.

## 연결 구조

| 대상 | 저장 위치 | 연결 방식 |
| --- | --- | --- |
| lease | runtime DB `leases` | `placement_id`(→ VM instance/project/task/attempt), `pool_id`, `worker_id`, `fencing_token`, `expires_at`, `provider_job_id` |
| 예산 예약 | budget DB `reservations` | reservation ID = lease ID |

lease ID는 `lease-<placement_id>-<token>` 형식이다. 이 형식이 식별자 규칙(128자)을 넘으면 `lease:<sha256 앞 40자>:<token>`을 사용한다. token 숫자에는 `-`가 없으므로 두 형식 모두 충돌하지 않는다.

## 예약 (`reserve`)

1. **runtime 트랜잭션**: 하나의 `RuntimeStore.transaction()`(프로세스 잠금 + `BEGIN IMMEDIATE`)에서 아래 순서로 확인하고 lease를 저장한다.
   1. Placement가 `assigned`인지 확인한다.
   2. `route.pool_id`와 placement의 pool이 같은지, 저장된 Pool이 `enabled`인지 확인한다.
   3. 같은 placement의 lease를 확인한다. `unknown`이 있으면 `PREVIOUS_LEASE_UNKNOWN`으로 거절한다. `reserved/active/draining`이 있으면 `LEASE_ALREADY_HELD`로 거절한다.
   4. 같은 pool의 점유 lease(`reserved/active/draining/unknown`) 수가 `Pool.max_parallel` 이상이면 `wait`(`POOL_CAPACITY`)를 반환한다.
   5. fencing token은 같은 placement의 기존 최대 token + 1이다.
   6. `reserved` 상태와 `expires_at = now + TTL`로 저장한다.
2. **예산 승인**: `BudgetStore.admit(AdmissionRequest(reservation_id=lease ID, ...))`를 호출한다.
   - `admitted`가 아니면 lease를 `failed`로 바꾸고 예산 판정의 상태·사유·`retry_at`을 반환한다.
   - 예외가 발생하면 lease를 `failed`로 바꾸고 예외를 다시 올린다.
3. **재확인**: 승인 뒤 lease가 더 이상 `reserved`가 아니면 예산 예약을 release하고 `LEASE_LOST_BEFORE_GRANT`로 거절한다. 승인 도중 `expire_due`나 `reconcile_reservations`가 실행된 경우다.

경쟁 요청이 있어도 capacity 검사와 lease 삽입이 같은 잠금 트랜잭션에서 수행되므로 pool capacity를 초과 점유하지 않는다. 예산 한도는 `BudgetStore.admit`의 잠금 트랜잭션이 지킨다.

## 상태 전이와 fencing

`activate`·`renew`·`release`·`resolve_unknown`는 `(lease_id, fencing_token)`을 받는다. 다음 경우 `StaleLease`로 거절한다.

- 저장된 token과 다른 경우
- 같은 placement에 더 큰 token의 lease가 있는 경우

상태가 맞지 않으면 `InvalidTransition`, 만료 후 갱신·활성화는 `LeaseExpired`로 거절한다.

| 호출 | 전이 |
| --- | --- |
| `activate(id, token)` | provider 호출 **전에** `reserved → active`(dispatch 의도 기록, job ID 없음) |
| `activate(id, token, provider_job_id)` | 영수증을 받은 뒤 같은 active lease에 job ID를 채운다. `reserved`에서 바로 job ID와 함께 부를 수도 있다 |
| `renew(id, token)` | 만료 전에만 `expires_at = now + TTL` |
| `release(dispatched=False)` | provider 호출 없음 → `released`, 예산 예약 release (job ID가 기록돼 있으면 거절) |
| `release(dispatched=True, provider_terminal=True)` | `released`. 정산은 관측 모듈이 `BudgetStore.settle`로 별도 수행 |
| `release(dispatched=True, provider_terminal=False)` | `draining`. 종료 관측 전에는 `released`로 가지 않는다 |
| `expire_due()` | dispatch 없는 `reserved` → `expired` + 예산 release. dispatch가 기록된 lease → `unknown` |
| `resolve_unknown(provider_terminal=...)` | 종료가 관측되면 `released`, 아니면 `unknown` 유지 |

불변식: `provider_job_id`가 있으면 dispatch가 기록된 것이다. `dispatched=True`는 job ID가 기록된 lease에만 허용한다. 따라서 job ID가 없는 lease의 만료·release·복구만 예산 hold를 풀 수 있다. 이 모듈은 사용량을 0이나 추정치로 정산하지 않는다.

만료는 provider 종료가 아니다. `unknown` lease는 capacity를 계속 점유한다. 같은 placement의 새 lease도 막으므로, 살아 있을 수 있는 job을 즉시 재발사하지 않는다.

## crash 복구 (`reconcile_reservations`)

- `reserved`이고 job ID가 없으며 예산 예약이 없는 lease를 `failed`로 바꾼다. lease 저장 뒤 예산 단계 전에 프로세스가 죽은 경우이며, 효과가 일어나기 전이다.
- `failed/released/expired`이고 job ID가 없는데 예산 예약이 아직 `reserved`이면 예산 예약을 release한다.

진행 중인 `reserve`와 겹쳐도 3단계 재확인이 불일치를 정리한다.

## 한계

- **dispatch 기록 순서**: provider 호출 전에 `activate(id, token)`으로 의도를 남겨야 한다. 그러면 호출 뒤 영수증 전에 프로세스가 죽어도 lease는 `active`로 남는다. 만료 때는 `unknown`이 되고 예산 hold는 유지된다. 호출자가 이 순서를 지키는지는 이 모듈이 강제하지 못한다. `activate` 없이 provider를 시작하면 만료·복구가 예산을 풀 수 있다.
- **만료 실행**: 만료는 `expire_due()`를 호출할 때만 적용된다. background timer는 없다.
- **lease 조회 비용**: 매 조회마다 lease 전체를 페이지 단위(1000)로 스캔한다. 비용은 lease 수에 비례한다.
- **capacity 범위**: capacity는 같은 runtime DB의 lease만 센다. `RuntimeStore`의 잠금을 따르지 않는 직접 SQLite 쓰기는 보호하지 않는다.
- **원자성 범위**: runtime과 budget은 별도 파일이다. 두 파일을 하나의 원자적 commit으로 묶지 않으며, 보상 처리(`failed` 표시, release, reconcile)로 일관성을 맞춘다.
- **부여하지 않는 것**: lease는 실행 인가가 아니다. provider·VM/AGS 실제 종료를 관측하지 않으며, `qualified` 판정도 만들지 않는다.

## 검증

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest tests/test_lease.py -v
```

시험은 임시 디렉터리의 RuntimeStore·BudgetStore와 주입 시계, 합성 Pool·Profile·Placement·RouteStatus만 사용한다. 실제 provider·credential·네트워크·VM/AGS를 사용하지 않는다. 검증 항목은 다음과 같다.

- 별도 store를 쓰는 6개 스레드의 동시 예약
- TTL 만료(dispatch 전 / job 있음)
- stale·superseded owner 거절
- unknown 재발사 차단
- 예산 wait/rejected/예외 처리
- reconcile
- release 세 분기
- renew 만료
- 승인 중 만료 경합

Linux 실행은 개발 검증일 뿐이다. Windows 동작(파일 잠금·경로)은 **미검증(NOT_RUN)**이다. 장부의 AIK-034 상태와 완료 판정은 변경하지 않으며, 이 문서와 테스트 통과를 증거 기록이나 완료로 주장하지 않는다.
