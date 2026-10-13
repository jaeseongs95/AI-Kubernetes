# AIK-040 — 예산·쿼터·동시성 admission과 정산

`src/aik_budget/`는 다음 세 기능을 제공한다.

- 신규 실행 직전에 쓰는 admission guard
- 사용량 예약/정산
- quota 상태 조회

총괄(director), worker, 검토자(reviewer) 호출이 모두 대상이며, 표준 라이브러리만 사용한다.

다음 기존 패키지는 import해서 재사용하며 수정하지 않는다.

- `aik_pool.RouteStatus`: pool 사용 여부, 신선도, 관측 용량, `configured_max_parallel`
- `aik_profiles`: `DirectorProfile`/`RoleProfile`의 `max_calls`, `budget_limit`, `budget_unit`, 그리고 `rebuild_profile`
- `aik_config.models.Revisioned`: revision으로 감싼 profile도 받는다. 내부 profile만 꺼내 재검증한다
- `aik_runtime.paths`: 경로 규칙과 프로세스 잠금

이 패키지는 provider를 호출하지 않는다. credential을 읽지 않으며 실행 권한도 부여하지 않는다. admission은 AIK 내부의 회계 guard다.

**provider가 외부에서 청구하는 금액의 절대 상한을 보장하지 않는다.** 지연 청구가 들어오면 기록된 합계가 한도를 넘을 수 있다. 이 경우 상태를 `over_limit`로 표시하고 신규 배치를 멈춘다.

## 단위와 basis

| 단위 | 필드 | 의미 |
| --- | --- | --- |
| API 비용 | `api_cost_cents` | 정수 최소 화폐 단위 |
| 토큰 | `input_tokens`, `output_tokens` | provider가 보고한 토큰 수 |
| 구독 quota | `quota_used` + `quota_unit` | provider가 보고한 구독 quota 소비량. 단위는 보고된 식별자 그대로 쓴다 |

모든 값은 `Amount(value, basis)`로 기록한다. basis는 다음 셋 중 하나다.

- `confirmed`: 확정값
- `estimated`: 추정값
- `unknown`: 미보고. 이 경우 value는 반드시 `None`이다

`Usage()`의 모든 필드는 기본값이 `unknown`이다. 제공된 값만 기록하고, 미제공 항목을 0이나 추정값으로 채우지 않는다. `Amount(0)`처럼 unknown에 값을 넣으면 `BudgetError`로 거절한다.

단위 간 환산은 하지 않는다. 토큰, 가격, 계정 사용률을 구독 quota 차감량으로 바꾸지 않는다. 구독 quota 잔여량은 provider 관측(`record_quota`)으로만 갱신한다.

## 정책(scope)

`BudgetPolicy(scope_id, ...)`는 자원이나 목표 단위의 상한이다. `set_policy(policy, expected_revision)`로 revision과 함께 저장한다.

**자원 정책**은 `pool:<pool_id>`, `profile:<profile_id>` 형식이다.

- 이 정책이 저장되어 있으면 호출자가 `scopes`에 넣지 않아도 admission이 자동으로 검사한다.
- 집계 대상은 해당 pool·profile의 모든 예약이다. 명시적으로 그 scope를 붙인 예약도 포함한다.
- 같은 scope를 `scopes`에 직접 넣어도 한 번만 검사한다.

**목표 정책**(`goal-…` 등)은 다르게 동작한다.

- 요청의 `scopes`에 넣은 경우에만 검사한다. **호출자가 목표 scope를 생략하면 그 목표의 상한은 검사되지 않는다.** 총괄 호출을 포함한 모든 호출에서 목표 scope를 넣는 것은 호출자의 책임이다.
- 명시한 scope에 정책이 없으면 `SCOPE_POLICY_MISSING`으로 거절한다(fail-closed).

| 필드 | 성격 |
| --- | --- |
| `max_calls` | 결정적 hard stop. admission된 예약 수를 센다. 정산·release 여부와 무관하다 |
| `deadline` | 절대 시각 hard stop. 다음 둘을 거절한다 — 현재 시각 ≥ deadline, 또는 현재 시각 + 요청 `max_seconds` > deadline |
| `max_concurrency` | 활성(`reserved`) 예약 수 상한. 초과 시 대기(wait) |
| `max_api_cost_cents` | 비용 상한. 요청에 `api_cost_estimate_cents`가 반드시 있어야 하며, 그 값을 hold로 예약한다 |
| `token_stop` | 사용자가 선택하는 관측량 기반 best-effort 중단. 보고된 토큰 합계만 비교하며 unknown은 세지 않는다 |

비용 상한이 나중에 추가되는 경우가 있다. 이전 정책 변경 때문이거나 같은 profile·pool에 정책이 새로 생긴 경우다. 이때 hold 없이 실행된 호출이 이미 있을 수 있으며, 다음과 같이 처리한다.

- **비용이 미보고(unknown)인 정산 호출:** 0으로 보지 않고 `API_COST_UNKNOWN`으로 거절한다. 비용 보고를 `settle`로 추가하면 해제된다.
- **hold 없는 활성 예약:** `API_COST_UNHELD_ACTIVE`로 대기한다.

프로필 한도도 같은 방식으로 `profile:<id>`에 적용된다. `max_calls`가 그대로 쓰이므로 총괄 호출 수도 제한된다.

- `budget_unit`이 `usd_cents`이면 비용 상한, `tokens`이면 best-effort token stop이 된다.
- 그 밖의 단위에 `budget_limit`이 설정되어 있으면 측정할 수 없으므로 `BUDGET_UNIT_UNSUPPORTED`로 거절한다.

## admission 판정

`admit(request, profile, route)`는 한 locked IMMEDIATE 트랜잭션 안에서 카운터를 읽고, 허용될 때만 예약을 추가한다. 따라서 경합하는 두 호출이 마지막 동시성 슬롯이나 마지막 비용 예산을 함께 얻지 못한다.

구독 quota 잔여량은 단위 환산 없이 다음과 같이 보수적으로 막는다.

- 최신 관측 시각 이후 생성된 예약과 현재 활성 예약을 각각 최소 1단위로 센다.
- 그 수가 보고된 `remaining` 이상이면 `QUOTA_REMAINING_UNCONFIRMED`로 대기하며, 새 관측이 필요하다.
- 이 검사는 **호출당 최소 1단위라는 하한 가정**에 기반한다. 한 호출이 여러 단위를 소비하면 실제 잔여량을 넘는 호출이 승인될 수 있다. quota 잔여량 자체를 예약하지는 않는다.

결과는 `AdmissionDecision(status, reasons, retry_at, …)`이다.

- `rejected`: reject 사유가 하나라도 있으면 이 결과다. 한도에 도달했으므로 신규 배치를 멈춘다.
- `wait`: reject 사유는 없고 wait 사유만 있으면 이 결과다. 일시적인 원인이므로 나중에 다시 시도한다. `retry_at`은 알려진 해제 시각 중 가장 늦은 값으로, 참고용 힌트일 뿐이다.
- `admitted`: 사유가 없을 때만 이 결과이며 예약이 생성된다.

사유 코드는 `코드@대상` 형식이다(예: `CONCURRENCY_LIMIT@pool:pool-1`). 모든 결정은 `decisions` 테이블에 append된다.

| 대상 | wait | reject |
| --- | --- | --- |
| pool(`RouteStatus`) | `ROUTE_STATUS_NOT_FRESH`(신선하지 않음, 또는 `last_probe_at`이 기본 300초보다 오래됨·미래), `CAPACITY_UNKNOWN`, `CAPACITY_EXHAUSTED`, `ROUTE_NOT_EXECUTABLE`, `CONCURRENCY_LIMIT`(활성 예약 ≥ `configured_max_parallel`) | `POOL_DISABLED`, `INVALID_POOL_LIMIT` |
| 429 | `RATE_LIMITED`(retry 시각 있음), `RATE_LIMITED_RETRY_UNKNOWN`(`clear_rate_limit` 전까지) | — |
| 구독 quota(`auth_kind=native_subscription`) | `QUOTA_REMAINING_UNKNOWN`(관측 없음 또는 remaining 미보고), `QUOTA_OBSERVATION_STALE`(TTL 초과·시계 역행), `QUOTA_EXHAUSTED`, `QUOTA_REMAINING_UNCONFIRMED`(관측 이후 예약 수 ≥ remaining) | — |
| profile/scope | `CONCURRENCY_LIMIT`, `API_COST_HELD_BY_ACTIVE`(활성 hold가 정산되면 여유가 생길 수 있음), `API_COST_UNHELD_ACTIVE` | `CALL_LIMIT_REACHED`, `DEADLINE_PASSED`, `DEADLINE_INSUFFICIENT`, `API_COST_LIMIT_REACHED`, `API_COST_INSUFFICIENT`, `API_COST_ESTIMATE_REQUIRED`, `API_COST_UNKNOWN`, `TOKEN_STOP_REACHED`, `BUDGET_UNIT_UNSUPPORTED`, `SCOPE_POLICY_MISSING` |

호출자는 `ResourcePoolRegistry.status()`로 방금 받은 `RouteStatus`를 넘겨야 한다. 오래된 snapshot은 `last_probe_at`을 기준으로 거른다. 용량이 unknown이면 0이나 무제한으로 바꾸지 않고 대기한다. API key route에는 구독 quota 조건을 적용하지 않는다.

저장된 관측 문서는 읽을 때 다시 검증한다. 음수 잔여량, 문자열 수치, 잘못된 단위 같은 손상은 잔여량으로 해석하지 않으며 `InvalidDatabase`로 중단한다.

## 정산(append-only)

- **`settle(reservation_id, usage)`:** `usage`는 그 호출의 **누적 총사용량**이다. 증분이 아니다.
  - 활성 예약이면 첫 기록을 `settlement`로 남기고 상태를 `settled`로 바꾼다.
  - 이미 `settled`/`released`인 예약에 대한 보고는 `late` 기록으로 **추가만** 하며 기존 상태는 바꾸지 않는다.
- **유효값 산정:** 집계는 호출별·필드별로 유효값 하나만 쓴다. 비용, 입력·출력 토큰, quota 소비량이 각각 해당한다.
  - 가장 최근의 `confirmed` 보고를 쓴다.
  - `confirmed` 보고가 없으면 가장 최근의 `estimated` 보고를 쓴다.
  - 둘 다 없으면 unknown이다. 나중에 들어온 unknown 보고는 이미 알려진 값을 지우지 않는다.
  - 따라서 다음 경우에 이중 합산이 발생하지 않는다.
    - 추정값을 확정값으로 바꾸는 지연 청구(감액 포함)
    - unknown hold를 실제 비용으로 대체하는 보고
    - 같은 보고의 재전송
  - 차액을 별도로 기록하지 않는다. 증분으로 보고하면 과소 집계되므로 반드시 총량으로 보고해야 한다.
- **`release(reservation_id)`:** provider 호출이 발송되지 않았음을 호출자가 확인한 경우에 쓴다. hold와 슬롯을 해제하며, 호출 수는 그대로 센다. release 이후 `late` 보고가 들어오면 발송된 호출로 간주한다. 그 보고의 비용이 unknown이면 hold 없는 미보고 비용으로 센다.
- **비용 unknown 정산:** 어떤 보고에도 비용이 없으면 예약 시점의 추정 hold를 `unknown_held_cents`로 계속 반영한다. 이 값을 실제 비용으로 표시하지는 않는다. 비용이 보고되면 hold는 그 값으로 대체된다.
- **DB 수준 보호:** `usage_records`, `observations`, `decisions`, `reservation_scopes`는 DB trigger로 UPDATE/DELETE를 거절한다. `reservations`도 삭제할 수 없고, 상태는 `reserved → settled|released` 한 번만 바꿀 수 있다.

## 상태 조회

- **`scope_status(scope_id)` / `profile_status(profile)`:** 상태(`open`/`limit_reached`/`over_limit`/`unconfigured`)와 사유를 다음 항목과 함께 반환한다.
  - 호출 수, 활성 수
  - 비용 basis별 합계(`CostTotals`)
    - `confirmed`/`estimated`/`reserved`/`unknown_held` 금액
    - 비용 미보고 호출 수 `unknown_records`
    - 그중 hold가 없는 수 `unknown_unheld_records`
    - hold 없는 활성 예약 수 `unheld_active_records`
  - 관측 토큰 합계와, 토큰 보고가 불완전한 호출 수. 이 수가 0보다 크면 합계는 하한값이다
  - 구독 quota 소비량(단위별)
- **`pool_status(pool_id)`:** 활성 예약 수, 429 상태와 retry 시각, 단위별 최신 quota 잔여량과 신선도를 반환한다.
- **`reservation(id)`, `usage_records(id)`:** 예약 한 건과 그 사용량 기록을 조회한다.
- **`decisions(limit=100, after_seq=None, *, newest_first=False)`:**
  - 각 결정 dict에는 `seq`가 들어 있다.
  - `after_seq`로 오래된 순서로 페이지를 넘긴다.
  - `newest_first=True`이면 최근 결정부터 반환한다. 두 옵션은 함께 쓸 수 없다.

## 저장

- 기본 위치: runtime DB 옆의 `budget.sqlite3`이다(Windows `%LOCALAPPDATA%\AIK\runtime\budget.sqlite3`).
- application ID: `0x41494B42`(`AIKB`)이다. runtime(AIKR), profiles(AIKP)와 다르다. 다른 application ID의 파일은 `InvalidDatabase`로 거절하며 내용을 바꾸지 않는다.
- 경로 검증과 `.storage.lock` 잠금은 `aik_runtime.paths`를 그대로 사용한다. 따라서 저장소 내부, `.git`, 개발 원장 경로는 거절한다.
- DELETE journal과 `synchronous=FULL`을 사용한다. 시계는 `clock`으로 주입한다.
- 이번 수정은 DB schema(version 1)를 바꾸지 않았다. 기존 파일을 그대로 연다.

## 시험

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest tests.test_budget -v
```

합성 profile, `RouteStatus`, 사용량과 주입한 시계만 사용한다. 단, 두 프로세스 경합 시험의 자식 프로세스는 실제 시계로 합성 route를 만든다. 시험이 검사하도록 작성된 항목은 다음과 같다.

**경합**
- 같은 프로세스의 두 스레드·두 store 인스턴스: 마지막 동시성 슬롯, 마지막 비용 예산, 마지막 quota 잔여 단위를 하나만 획득
- 두 프로세스: 마지막 동시성 슬롯을 하나만 획득

**한도와 정책**
- pool 병렬 한도
- 저장된 `pool:`/`profile:` 정책의 자동 적용(scope 생략 시)
- 총괄 호출을 포함한 호출 수 hard stop
- 절대 deadline
- 비용 hold와 추정값 필수
- 나중에 추가된 비용 상한에서 hold 없는 unknown 비용 거절과 해제
- profile `budget_unit` 매핑(`usd_cents`·`tokens`)과 `BUDGET_UNIT_UNSUPPORTED`
- best-effort token stop

**provider 관측**
- 429 retry와 retry 시각 unknown
- 구독 잔여량 미확인·소진·stale, 관측 이후 예약 수에 따른 대기
- 토큰을 quota로 환산하지 않음

**정산**
- unknown을 0으로 채우지 않음
- 지연 확정 청구가 추정값·unknown hold를 대체(이중 합산 없음)와 `over_limit`
- 재전송 보고의 중복 집계 방지
- release

**저장**
- DB 수준 append-only(`reservation_scopes` 포함)
- 변조된 관측·사용량 문서의 `InvalidDatabase`
- 재열기·정책 revision, 결정 페이지 조회
- 외부·runtime DB와 저장소 경로 거절

## 한계·미검증(NOT_RUN)

- **Windows 미검증:** Windows 기본 경로, Windows 잠금(`msvcrt`), 실제 Windows 11 동작은 **미검증(NOT_RUN)**이다. Linux 통과는 개발 검증이며 Windows 지원 증거가 아니다. 두 프로세스 경합 시험도 Windows에서 실행하지 않았다.
- **provider 연결 없음:** 실제 provider의 사용량·청구·429·quota 응답을 이 API로 옮기는 연결은 없다. 실제 provider와 실계정 검증도 NOT_RUN이며 후행 연결 태스크 범위다.
- **외부 청구 상한 비보장:** provider 외부 청구의 절대 상한은 보장하지 않는다. 지연 청구, 추정 오차, 미보고 비용이 있으면 실제 청구가 정책 상한을 넘을 수 있다.
- **quota 잔여량 하한 가정:** 구독 quota 잔여량 검사는 호출당 최소 1단위라는 하한 가정에 의존한다. 잔여량을 예약하거나 실제 소비량을 예측하지 않는다.
- **목표 scope 누락:** 목표 scope는 호출자가 넣지 않으면 검사되지 않는다. 자동 적용은 `pool:`/`profile:` 자원 정책에만 해당한다.
- **보고 식별자 없음:** 정산 보고에 별도 보고 식별자(report id)는 없다. 재전송 보고도 기록으로는 추가되지만(`usage_records`에 보임) 합계에는 한 번만 반영된다.
- **token stop:** token stop은 보고된 토큰만 비교하는 best-effort 정책이며, 실행 중인 호출을 중단하지 않는다.
- **overdue 예약:** 기한이 지난 예약(`overdue`)은 자동으로 해제하지 않는다. 명시적인 정산·release가 있을 때까지 슬롯을 점유한다(fail-closed).
- **미지원 예산 단위:** `aik_config`의 `BUDGET_UNITS` 중 `usd_cents`·`tokens` 외의 단위는 측정 방법이 없어 admission을 거절한다.
- **처리량:** 정산 기록이 커질수록 상태 계산 비용이 늘어난다. quota 검사는 pool의 예약 전체를 읽는다. 대규모 처리량은 검증하지 않았다.
- **장부 불변:** `planning/` 장부의 상태와 완료 판정은 변경하지 않았다.
