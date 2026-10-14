# AIK-016 — 인증 만료·키 회전·잠금·로그아웃 수명

`src/aik_auth_lifecycle/`은 연결 자격이 바뀔 때 **신규 배치**와 **진행 중 실행**을 구분해 처리하는 인증 상태 기계다. 표준 라이브러리만 사용하며, 다음 기존 패키지를 수정하지 않고 import해서 재사용한다.

- `aik_connection`
  - 오류: `UnknownConnection`, `AuthenticationRequired`, `CredentialRefUnavailable`
  - vault 참조 확인 어댑터: `VaultCredentialRefChecker`
- `aik_vault`: `secret://aik-vault/<id>` 참조 규칙(`is_credential_ref`)
- `aik_provider`: `ConnectionBinding`, `AuthObservation`, `JobReceipt`, `TERMINAL`
- `aik_pool`: `ResourcePoolRegistry`의 `update_binding`·`probe`·`routes`·`operational_inventory`
  - 생성자는 `isinstance` 검사로 이 레지스트리 타입을 강제한다.
- `aik_runtime`: `RevisionConflict`, 식별자 규칙

이 패키지가 하지 않는 일은 다음과 같다.

- 시크릿 값을 저장하지 않는다.
- provider를 직접 호출하지 않는다.
- 실행의 완료나 환불을 판정하지 않는다.

상태는 프로세스 메모리에만 있다.

## 상태

| 상태 | 의미 | 신규 실행 |
| --- | --- | --- |
| `valid` | 현재 binding으로 재검증됨 | 허용 |
| `expired` | 만료 시각 경과, host/provider의 만료·미인증 보고, 또는 `mark_expired` | 거절 |
| `locked` | vault/Windows 세션 잠금 신호가 활성, 또는 host의 잠금 보고 | 거절 |
| `revoked` | 강제 로그아웃 또는 연결 삭제 | 거절(재인증 필요) |
| `rotating` | 키 회전 진행 중 | 거절 |
| `unknown` | 등록 직후, 재인증·회전·잠금 해제 직후, 관측이 불명확하거나 계정이 다를 때 | 거절 |

등록만으로는 `valid`가 되지 않는다. `valid`로 전이시킬 수 있는 것은 `revalidate`뿐이다(fail-closed).

`AuthSnapshot.placeable`과 그 별칭 `auth_valid`는 **인증 측면만** 판정한다. 레지스트리의 probe·검증·qualification과는 무관하다. 따라서 `True`여도 실제 배치 대상이 있다는 뜻이 아니다. 실제 배치 후보는 `placement_candidates()`로 얻는다.

## 재검증

`revalidate(connection_id, expected_revision, observation=None)`의 판단 근거는 다음 둘 중 하나다.

- `observation`: 공식 호스트 어댑터가 관측한 `AuthObservation`
- 레지스트리 probe: 해당 connection의 모든 등록 경로에 대한 `registry.probe`

`observation`은 binding처럼 필드 전체를 재구성해 검증한다. 위조된 값(예: 상태 필드가 enum이 아님)은 고정 코드 `INVALID_OBSERVATION`으로 거절하며, 이때 상태는 바뀌지 않는다.

판정은 다음 순서로 한다.

1. 만료 시각이 지났으면 `expired`.
2. API 연결의 vault 참조를 확인할 수 없으면 `unknown`(`credential_unavailable`).
3. 관측 결과를 다음과 같이 매핑한다.

| 관측 결과 | 판정 |
| --- | --- |
| `authenticated`/`bound` | `valid` |
| `expired`/`unauthenticated` | `expired` |
| `locked` | `locked` |
| 그 외 | `unknown` |

다음 경우는 `valid`가 될 수 없다.

- connection_id나 binding revision이 다른 관측 → `unknown`(`stale_observation`)
- 인증 상태라도 계정 fingerprint가 다름 → `unknown`(`account_changed`)
- `registry.probe`가 예외를 냄 → `unknown`(`probe_failed`). 실패한 재검증이 이전 검증을 유지하지 않으며, 예외 문구는 기록하지 않는다.

다음 경우는 재검증 자체를 거절한다.

- 잠금 신호가 활성
- 회전 중
- `revoked`

`expired`에서 다시 `valid`로 돌아오는 규칙은 경로마다 다르다.

- **시계 만료**: 재인증(`reauthenticate`)으로 새 `expires_at`을 설치해야 한다. 그 전에는 재검증해도 `expired`를 유지한다.
- **보고된 만료**(`mark_expired` 또는 probe/관측의 `expired`): 이후 같은 binding의 관측·probe가 인증됨을 보이면 `revalidate`가 `valid`로 되돌릴 수 있다. host session처럼 재로그인이 AIK 밖의 공식 호스트에서 일어나는 경우를 의도한 것이다.

이벤트의 `reauth_required`는 "재인증 또는 외부 재로그인 후 재검증이 필요함"을 알리는 표시다. 이 모듈이 재인증 API 호출 자체를 강제한다는 뜻이 아니다.

## 신규 배치 제외(완료 기준 1)

### 배치 API

- `begin_run(connection_id, run_id)`
  - 잠금 신호와 주입된 시계를 먼저 반영한다.
  - 상태가 `valid`가 아니면 `AuthenticationRequired`로 거절한다.
  - 성공하면 현재 `ConnectionBinding`과 참조 버전(`credential_version`)이 담긴 `RunTicket`을 반환한다.
- `placement_candidates(model=None, require_qualified=True)`
  - 레지스트리의 배치 가능 경로 중에서 인증 상태가 `valid`이고 binding revision이 일치하는 경로만 반환한다.
  - 기본값은 `operational_inventory()`(qualified 경로만)를 사용한다.
  - `require_qualified=False`는 `executable_now` 기준이며 진단·개발 확인용이다.
- `reauthenticate`·`complete_rotation`
  - `registry.update_binding`을 호출해 레지스트리의 관측·검증·qualification을 초기화한다.
  - 따라서 재인증 후에는 다시 probe해야 배치 대상에 돌아온다.

### 우회 경로

**신규 배치 제외는 `begin_run`/`placement_candidates`를 경유하는 소비자에게만 성립한다.**

다음 사건은 레지스트리를 갱신하지 않는다.

- `force_logout`
- `delete_connection`
- 시계·보고 만료
- 잠금

이 사건들 뒤에 `ResourcePoolRegistry.operational_inventory()`나 `routes()`를 직접 조회하면 해당 경로가 여전히 executable/qualified로 보일 수 있다. 즉 레지스트리 직접 조회는 이 차단을 우회한다.

레지스트리를 갱신하는 경우는 binding이 바뀌는 회전·재인증뿐이다. 배치 연동 태스크(AIK-033 등)는 반드시 이 API를 거쳐 후보를 얻어야 한다. 이는 해당 태스크의 연동 조건으로 남긴다. 레지스트리 측 비활성화 hook은 이번 범위에서 구현하지 않았다.

## 키 회전(완료 기준 2)

1. `begin_rotation(id, rev)` → `rotating`. 신규 실행을 거절한다.
2. `complete_rotation(id, new_binding, rev)` → 새 binding을 설치한다.
   - identity(provider·runtime·connection·auth_kind)는 같아야 한다.
   - revision은 더 커야 한다.
   - 계정 fingerprint는 같아야 한다.
   - 새 vault 참조가 존재해야 한다.
   - 성공하면 `credential_version`이 1 증가하고 상태는 `unknown`이 된다.
3. `revalidate` 후의 새 실행은 새 참조·새 버전을 받는다.

회전 중에는 시계 만료가 `rotating` 상태를 바꾸지 않는다. 잠금은 회전을 막지만 취소하지는 않으며, 잠금이 풀리면 `rotating`으로 돌아온다. `cancel_rotation`은 `rotating`을 `unknown`으로 되돌린다. 새 참조 확인에 실패하면 상태는 바뀌지 않는다.

### 이전 실행의 회수

회전 전에 시작한 실행은 `reclaim_required`(`rotated`)로 남는다. 이 실행의 종료는 **원래 binding**으로 관측한 `JobReceipt`(`record_observation`)로만 확정한다.

- 다른 revision·계정의 receipt는 `RECEIPT_MISMATCH`로 거절한다.
- 처음 수용한 receipt의 `job_id`가 그 실행에 결속된다. 이후 다른 `job_id`의 receipt도 `RECEIPT_MISMATCH`로 거절한다.
- terminal receipt를 기록할 때는 상태를 바꾸기 전에 시계를 읽는다. 시계가 실패하면 실행은 바뀌지 않은 채 `CLOCK_UNAVAILABLE`로 끝난다.

`refs_in_use()`는 아직 삭제하면 안 되는 credential 참조 locator를 반환한다. 대상은 다음 두 가지다.

- 삭제되지 않은 연결의 현재 binding
- 종료되지 않은(`active`/`reclaim_required`) 실행의 원래 binding

이전 참조를 삭제하기 전에 호출자가 이 목록을 확인해야 한다. 이 모듈이 `vault.delete`를 차단하지는 않는다. 회수 대기 목록은 `runs(status=RunStatus.RECLAIM_REQUIRED)`로 조회한다.

**기준 2의 회수 구현 범위**는 회수 대상 표시와 원 binding·job의 receipt 수용까지다. 다음은 미구현 또는 NOT_RUN이다.

- 이 모듈은 `observe_job`을 호출하지 않는다. 실제 상태 회수는 외부가 `record_observation`을 호출할 때만 일어난다. 실제 관측 루프는 미구현이다.
- 회수 대상 목록의 영속화는 미구현이다. 재시작하면 목록이 사라진다.
- 이전 참조 보존 정책의 강제는 미구현이다.

## 잠금 정책·강제 로그아웃(완료 기준 3)

### 정책과 신호원

- `LockPolicy(vault_lock_blocks, session_lock_blocks, reclaim_running_on_lock)`는 사용자 설정이며 기본값은 모두 `False`다.
- 차단 정책을 켜려면 신호원(인자 없이 `bool`을 반환하는 callable)을 반드시 주입해야 한다. 없으면 생성 시 `LOCK_SOURCE_REQUIRED`로 거절한다.
- 신호원이 예외를 내거나 `bool`이 아닌 값을 반환하면 **잠김**으로 처리한다.

### 잠금 범위와 효과

- vault 잠금은 vault 참조를 쓰는 `api_key` 연결에만 적용한다.
- Windows 세션 잠금은 모든 연결에 적용한다.
- 잠금은 이전 검증을 무효화한다. 해제 후 상태는 `unknown`이며 재검증해야 한다.
- 진행 중 실행은 기본적으로 그대로(`active`) 둔다. `reclaim_running_on_lock=True`이면 `reclaim_required`로 표시한다.

### 폴링 사이의 잠금

잠금 신호는 API 호출 시점에 표본으로 읽는다. 두 호출 사이에 잠갔다가 다시 푼 경우, 신호의 현재 수준만으로는 잠금을 알 수 없다.

이를 보완하기 위해 신호원은 잠금마다 단조 증가하는 정수 `generation` 속성을 제공할 수 있다. 직전 폴링과 값이 다르면, 현재 해제 상태여도 잠금이 발생한 것으로 처리한다.

- 이전 검증을 무효화(`valid`→`unknown`)한다.
- `locked` 이벤트를 사유 `lock_missed:<종류>`로 남긴다.
- 정책에 따라 진행 중 실행을 회수 대상으로 표시한다.

`generation`을 읽을 수 없거나 정수가 아니면 잠김으로 처리한다.

### 제공하는 신호원

- `ManualLockSignal`
  - 앱의 "vault 잠금" 설정 같은 사용자 스위치다.
  - `lock()`마다 `generation`이 증가한다.
- `os_session_locked`: 플랫폼 세션 잠금 신호(`aik_platform.session`). Windows는 입력 데스크톱 전환 가능 여부, Linux는 systemd-logind `LockedHint`(`loginctl`, `$XDG_SESSION_ID`)다. 답할 수 없으면 예외를 내고 AuthLifecycle은 잠김으로 처리한다.
- `windows_session_locked`: 이전 이름. Windows에서만 위 신호를 쓰고 그 밖에서는 예외를 낸다.
  - Windows 전용 휴리스틱(`OpenInputDesktop`/`SwitchDesktop`)이다.
  - 비 Windows에서는 예외를 내므로 잠김으로 처리된다.
  - `generation`이 없다. 따라서 **두 폴링 사이에 시작하고 끝난 세션 잠금은 놓친다.**

### 강제 로그아웃

- `force_logout(id, rev)` → `revoked`.
  - 진행 중 실행은 `reclaim_required`(`forced_logout`)가 되고 `outcome=None`(불명)이다.
- 강제 로그아웃을 성공·취소·환불로 간주하지 않는다.
  - 이후 관측이 `running`이면 계속 회수 대상으로 남는다.
  - terminal 관측만 `closed`로 바꾸며, 기록하는 값은 관측된 job state 그대로다. `refunded` 같은 판정 값은 없다.
  - 환불·과금 판단은 VM/과금 소유자의 책임이다.
- `revoked`에서 복귀하려면 `reauthenticate` 후 새 binding으로 `revalidate`해야 한다.

## 동시성·삭제 경합

- 모든 변경 API는 `expected_revision`을 받는다.
- 동시 회전, 회전과 삭제, 재인증과 삭제가 경합하면 하나만 성공하고 나머지는 `RevisionConflict`로 끝난다.
- 잠금 신호나 만료 반영도 revision을 올린다. 그 사이 상태가 바뀌었다면 오래된 revision의 호출은 충돌한다.
- `delete_connection`은 tombstone을 남긴다.
  - 이후 신규 실행·회전·재인증은 현재 revision이어도 `UnknownConnection`이다. 오래된 revision의 호출은 `RevisionConflict`다.
  - 같은 ID의 재등록은 `DUPLICATE_CONNECTION`이다.
  - 진행 중 실행은 `reclaim_required`(`connection_deleted`)로 남고, 계속 원래 binding의 관측으로 회수할 수 있다.
  - 레지스트리 경로 자체는 지우지 않는다(레지스트리에 삭제 API가 없음). 이 경로는 `placement_candidates`에서만 제외된다. 위 "우회 경로"를 참고한다.

## 이벤트

`events()`는 최근 이벤트(기본 4096개)를 반환한다. 이벤트 종류는 다음과 같다.

`registered`, `revalidated`, `revalidation_failed`, `expired`, `locked`, `unlocked`, `rotation_started`, `rotation_cancelled`, `rotated`, `reauthenticated`, `logout_forced`, `deleted`, `reclaim_required`, `run_started`, `run_closed`

- `reauth_required`는 상태가 `expired`/`revoked`일 때 참이다.
- `listener`는 잠금 밖에서 호출한다.
  - listener의 예외는 상태 변경을 되돌리지 않으며 `listener_failures`에 집계한다.
  - 여러 스레드가 연산을 연달아 끝내면 **listener 전달 순서는 보장되지 않는다.** 정본 순서는 `events()`의 순서와 각 이벤트의 revision이다.
- 이벤트·스냅샷에는 참조와 ID만 있고 시크릿 값은 없다.

## 검증

```powershell
python -m unittest discover -s tests -p 'test_auth_lifecycle.py' -v
```

시험은 다음만 사용한다.

- 합성 binding
- 주입 시계
- fake adapter(효과 메서드 호출 금지 확인)
- test-only `FakeVaultBackend`

검사 항목은 다음과 같다.

- 등록 후 재검증 전 배치 제외
- 시계 만료 → 재인증 → 재검증
- 보고 만료(`mark_expired`) → 재인증 → 재검증
- host 보고 만료·잠금·계정 변경
- probe 예외 시 이전 검증 철회
- 위조 관측 거절
- 회전 후 새 참조 버전 사용, 이전 실행 회수, `refs_in_use`
- receipt의 job 결속과 terminal 기록 시 시계 실패
- 회전 중 만료·잠금
- 스레드 동시 회전과 삭제 경합의 revision 직렬화
- 삭제된 연결의 재인증 거절
- vault/세션 잠금 정책, 폴링 사이 잠금(`generation`), 기본 정책의 실행 유지, 신호 실패 fail-closed
- 강제 로그아웃의 비완료·비환불, 재인증 후 복귀
- vault 참조 확인 실패, listener 실패 집계, 입력 검증

## 한계·미검증(NOT_RUN)

- 실제 Windows 세션 잠금 신호와 실제 Linux 데스크톱 세션의 logind `LockedHint`는 둘 다 **NOT_RUN**이다(Linux는 가짜 `loginctl` 단위 시험만 있다). 이 신호원은 폴링 사이의 잠금을 놓친다. Linux 단위시험 통과는 Windows 지원 증거가 아니다.
- 실제 공식 호스트·provider의 만료/잠금 관측과 실제 DPAPI vault와의 회전 연동은 **NOT_RUN**이다.
- 상태·실행 기록은 메모리에만 있어 재시작하면 사라진다. 닫힌 실행 기록도 정리하지 않는다. 영속화와 영속 lease 연동은 후속 범위다.
- 이전 실행의 실제 관측 루프(`observe_job` 호출)는 이 모듈에 없다. 회수는 외부의 `record_observation` 호출에 의존한다.
- 신규 배치 제외는 이 API를 경유하는 소비자에게만 성립한다. 레지스트리를 직접 조회하면 우회된다.
- 레지스트리 probe는 이 객체의 잠금을 잡은 채 실행된다.
- vault 잠금은 `aik_vault`에 잠금 개념이 없으므로 주입 신호로만 표현한다.
- 구현은 장부의 선행 작업(AIK-009/010/011)에 없는 **AIK-019(`aik_pool.registry`) 의존**을 가진다. AIK-019의 완료 상태와 API 안정성은 이 문서에서 확인하지 않았다.
- 장부 상태와 완료 판정은 변경하지 않는다.
