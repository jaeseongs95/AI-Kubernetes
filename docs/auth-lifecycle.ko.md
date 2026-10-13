# AIK-016 — 인증 만료·키 회전·잠금·로그아웃 수명

`src/aik_auth_lifecycle/`은 연결 자격이 바뀔 때 **신규 배치**와 **진행 중 실행**을 구분해 처리하는 인증 상태 기계다. 표준 라이브러리만 사용한다. 다음 기존 패키지를 수정하지 않고 import해서 재사용한다.

- `aik_connection`: `UnknownConnection`, `AuthenticationRequired`, `CredentialRefUnavailable`, 그리고 vault 참조 확인 어댑터(`VaultCredentialRefChecker`)
- `aik_vault`: `secret://aik-vault/<id>` 참조 규칙(`is_credential_ref`)
- `aik_provider`: `ConnectionBinding`, `AuthObservation`, `JobReceipt`, `TERMINAL`
- `aik_pool`: `ResourcePoolRegistry`의 `update_binding`·`probe`·`routes`·`operational_inventory`
- `aik_runtime`: `RevisionConflict`, 식별자 규칙

이 패키지는 시크릿 값을 저장하지 않는다. provider를 직접 호출하지 않고, 실행의 완료나 환불도 판정하지 않는다. 상태는 프로세스 메모리에만 있다.

## 상태

| 상태 | 의미 | 신규 실행 |
| --- | --- | --- |
| `valid` | 현재 binding으로 재검증됨 | 허용 |
| `expired` | 만료 시각 경과, host/provider가 만료·미인증을 보고, 또는 `mark_expired` | 거절 |
| `locked` | vault/Windows 세션 잠금 신호가 활성, 또는 host가 잠금을 보고 | 거절 |
| `revoked` | 강제 로그아웃 또는 연결 삭제 | 거절(재인증 필요) |
| `rotating` | 키 회전 진행 중 | 거절 |
| `unknown` | 등록 직후, 재인증·회전·잠금 해제 직후, 관측이 불명확하거나 계정이 다를 때 | 거절 |

등록만으로는 `valid`가 되지 않는다. `revalidate`만 `valid`로 전이시킬 수 있다(fail-closed).

## 재검증

`revalidate(connection_id, expected_revision, observation=None)`의 판단 근거는 다음 둘 중 하나다.

- `observation`(공식 호스트 어댑터가 관측한 `AuthObservation`)
- 레지스트리에 등록된 해당 connection의 모든 경로에 대한 `registry.probe`

판정은 다음 순서로 한다.

1. 만료 시각이 지났으면 `expired`.
2. API 연결의 vault 참조를 확인할 수 없으면 `unknown`(`credential_unavailable`).
3. 관측 결과를 매핑한다.
   - `authenticated`/`bound` → `valid`
   - `expired`/`unauthenticated` → `expired`
   - `locked` → `locked`
   - 나머지 → `unknown`

connection_id·binding revision이 다른 관측은 `unknown`(`stale_observation`)이다. 인증 상태라도 계정 fingerprint가 다르면 `unknown`(`account_changed`)이다. 잠금 신호가 활성이거나, 회전 중이거나, `revoked`이면 재검증 자체를 거절한다.

## 신규 배치 제외(완료 기준 1)

- `begin_run(connection_id, run_id)`는 잠금 신호와 주입된 시계를 먼저 반영한 뒤, `valid`가 아니면 `AuthenticationRequired`로 거절한다. 성공하면 현재 `ConnectionBinding`과 참조 버전(`credential_version`)이 담긴 `RunTicket`을 반환한다.
- `placement_candidates(model=None, require_qualified=True)`는 레지스트리의 배치 가능 경로 중에서 인증 상태가 `valid`이고 binding revision이 일치하는 경로만 반환한다.
  - 기본값은 `operational_inventory()`(qualified 경로만)를 사용한다.
  - `require_qualified=False`는 `executable_now` 기준이며 진단·개발 확인용이다.
- `reauthenticate`·`complete_rotation`은 `registry.update_binding`을 호출해 레지스트리의 관측·검증·qualification을 초기화한다. 따라서 재인증 후에는 다시 probe해야 배치 대상에 돌아온다.

## 키 회전(완료 기준 2)

1. `begin_rotation(id, rev)` → `rotating`. 신규 실행을 거절한다.
2. `complete_rotation(id, new_binding, rev)` → 새 binding을 설치한다.
   - 새 binding은 identity(provider·runtime·connection·auth_kind)가 같아야 하고, revision은 더 커야 하며, 계정 fingerprint가 같아야 한다.
   - 새 vault 참조가 존재해야 한다.
   - 성공하면 `credential_version`이 1 증가하고 상태는 `unknown`이 된다.
3. `revalidate` 후 새 실행은 새 참조·새 버전을 받는다.

회전 전에 시작한 실행은 `reclaim_required`(`rotated`)로 남는다. 이 실행의 종료는 **원래 binding**으로 관측한 `JobReceipt`(`record_observation`)로만 확정한다. 다른 revision·계정의 receipt는 `RECEIPT_MISMATCH`로 거절한다. `cancel_rotation`은 `rotating`을 `unknown`으로 되돌린다. 새 참조 확인에 실패하면 상태는 바뀌지 않는다.

## 잠금 정책·강제 로그아웃(완료 기준 3)

- `LockPolicy(vault_lock_blocks, session_lock_blocks, reclaim_running_on_lock)`는 사용자 설정이며 기본값은 모두 `False`다.
- 차단 정책을 켜려면 신호원(인자 없이 `bool`을 반환하는 callable)을 반드시 주입해야 한다. 없으면 생성 시 `LOCK_SOURCE_REQUIRED`로 거절한다.
- 신호원이 예외를 내거나 `bool`이 아닌 값을 반환하면 **잠김**으로 처리한다.
- 잠금 범위는 다음과 같다.
  - vault 잠금은 vault 참조를 쓰는 `api_key` 연결에만 적용한다.
  - Windows 세션 잠금은 모든 연결에 적용한다.
- 잠금은 이전 검증을 무효화한다. 해제 후 상태는 `unknown`이고 재검증해야 한다.
- 진행 중 실행은 기본적으로 그대로 둔다. `reclaim_running_on_lock=True`이면 `reclaim_required`로 표시한다.
- 신호원 구현은 두 가지를 제공한다.
  - `ManualLockSignal`: 앱의 "vault 잠금" 설정 같은 사용자 스위치
  - `windows_session_locked`: Windows 전용 휴리스틱(`OpenInputDesktop`/`SwitchDesktop`). 비 Windows에서는 예외를 내므로 잠김으로 처리된다.
- `force_logout(id, rev)` → `revoked`. 진행 중 실행은 `reclaim_required`(`forced_logout`)가 되고 `outcome=None`(불명)이다.
  - 강제 로그아웃을 성공·취소·환불로 간주하지 않는다.
  - 이후 관측이 `running`이면 계속 회수 대상으로 남는다.
  - terminal 관측만 `closed`로 바꾸며, 기록하는 값은 관측된 job state 그대로다(`refunded` 같은 판정 값은 없다).
  - 환불·과금 판단은 VM/과금 소유자의 책임이다.

## 동시성·삭제 경합

- 모든 변경 API는 `expected_revision`을 받는다.
- 동시 회전, 회전과 삭제, 재인증과 삭제가 경합하면 하나만 성공하고 나머지는 `RevisionConflict`로 끝난다.
- 잠금 신호나 만료 반영도 revision을 올린다. 따라서 그 사이 상태가 바뀌었다면 오래된 revision의 호출은 충돌한다.
- `delete_connection`은 tombstone을 남긴다.
  - 이후 신규 실행·회전은 `UnknownConnection`, 오래된 revision의 호출은 `RevisionConflict`다.
  - 같은 ID의 재등록은 `DUPLICATE_CONNECTION`이다.
  - 진행 중 실행은 `reclaim_required`(`connection_deleted`)로 남고, 계속 원래 binding의 관측으로 회수할 수 있다.
  - 레지스트리 경로 자체는 지우지 않지만(레지스트리에 삭제 API가 없음), `placement_candidates`에서 제외된다.

## 이벤트

`events()`는 최근 이벤트(기본 4096개)를 반환한다. 이벤트 종류는 `registered`, `revalidated`, `revalidation_failed`, `expired`, `locked`, `unlocked`, `rotation_started`, `rotation_cancelled`, `rotated`, `reauthenticated`, `logout_forced`, `deleted`, `reclaim_required`, `run_started`, `run_closed`다.

`reauth_required`는 상태가 `expired`/`revoked`일 때 참이다. `listener`는 잠금 밖에서 호출한다. listener의 예외는 상태 변경을 되돌리지 않으며 `listener_failures`에 집계한다. 이벤트·스냅샷에는 참조와 ID만 있고 시크릿 값은 없다.

## 검증

```powershell
python -m unittest discover -s tests -p 'test_auth_lifecycle.py' -v
```

시험은 합성 binding, 주입 시계, fake adapter(효과 메서드 호출 금지 확인), test-only `FakeVaultBackend`만 사용한다. 검사 항목은 다음과 같다.

- 등록 후 재검증 전 배치 제외
- 시계 만료 → 재인증 → 재검증
- host 보고 만료·잠금·계정 변경
- 회전 후 새 참조 버전 사용과 이전 실행 회수
- 스레드 동시 회전과 삭제 경합의 revision 직렬화
- vault/세션 잠금 정책과 신호 실패 fail-closed
- 강제 로그아웃의 비완료·비환불
- vault 참조 확인 실패, listener 실패 집계, 입력 검증

## 한계·미검증(NOT_RUN)

- 실제 Windows 세션 잠금 신호(`windows_session_locked`)는 **NOT_RUN**이다. Linux 단위시험 통과는 Windows 지원 증거가 아니다.
- 실제 공식 호스트·provider의 만료/잠금 관측, 실제 DPAPI vault와의 회전 연동은 **NOT_RUN**이다.
- 상태·실행 기록은 메모리에만 있어 재시작하면 사라진다. 닫힌 실행 기록도 정리하지 않는다. 영속화와 영속 lease 연동은 후속 범위다.
- 레지스트리 probe는 이 객체의 잠금을 잡은 채 실행된다.
- vault 잠금은 `aik_vault`에 잠금 개념이 없으므로 주입 신호로만 표현한다.
- 장부 상태와 완료 판정은 변경하지 않는다.
