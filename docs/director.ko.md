# AIK-024 — 총괄 에이전트 bootstrap·시작·정지 (부분 구현)

`src/aik_director/`는 사용자가 지정한 자원으로 총괄(director)을 검증·시작·정지하고, 상태를 health payload로 제공한다. 표준 라이브러리만 사용하며, 다음 기존 패키지를 import해서 재사용한다. 기존 패키지는 수정하지 않는다.

| 패키지 | 사용하는 기능 |
| --- | --- |
| `aik_supervisor` | `launch()`로 workspace별 단일 owner 결정, attach 감지, `shutdown()`에서 진행 중 lease 보고 |
| `aik_profiles` | `ProfileStore.prepare_execution()`으로 실행 직전 capability 검증, `ExecutionDecision` |
| `aik_broker` | `issue_grant`/`revoke`로 작업 범위 credential grant. runner에는 `grant_id`와 connection ID만 전달 |
| `aik_inbox` | `WakePolicy`로 lease 가능한 항목이 있을 때만 wake |

실제 모델 호출과 provider 실행은 구현하지 않는다. 이 기능은 주입형 `DirectorRunner` 인터페이스 뒤에 있으며, 시험에서는 fake runner를 사용한다.

## Runner 인터페이스

| 메서드 | 계약 |
| --- | --- |
| `probe(binding) -> ProbeResult` | 연결·로그인 확인. `ok`/`auth_required`/`unavailable`. 모델 turn을 실행하지 않는다 |
| `start(request) -> RunnerSession` | 세션 시작. 모델 turn을 실행하지 않는다. 실제로 관측한 `observed_model`/`observed_effort`를 반환한다(모르면 `None`) |
| `stop(session)` | 세션 종료. VM job이나 진행 중 작업을 취소·삭제하지 않는다. 예외를 던지면 "정지 실패"로 취급한다 |
| `health(session) -> str` | `alive`/`degraded`/`dead`/`unknown`. 모델 turn을 실행하지 않는다 |
| `wake(session, signal)` | 모델 turn을 실행할 수 있는 유일한 경로. director lock 안에서 호출되므로 빠르게 반환해야 한다(신호 전달용) |

`StartRequest`에는 workspace·instance ID, 검증된 binding, `grant_id`만 들어 있다. 비밀 값은 없다. runner/adapter가 broker의 `inject`를 통해 실행 시점에 비밀을 받는다.

## 시작 순서 (fail-closed)

1. `aik_supervisor.launch(workspace_id, component="director")`로 owner를 결정한다. 다른 live owner가 있으면 `already_running`과 기존 `instance_id`를 반환하며, 프로필 검증·probe·runner 시작을 하지 않는다. 같은 `Director`에서 반복 호출해도 `already_running`이다. `launch`가 어떤 `Exception`을 던져도 `unavailable`(`SUPERVISOR_UNAVAILABLE`)과 상태 `FAILED`로 끝나며 다시 `start()`할 수 있다. `KeyboardInterrupt` 같은 `BaseException`은 `start_failed`(`UNCLASSIFIED`)를 기록하고 상태를 `FAILED`로 바꾼 뒤 재전파한다.
2. 프로필을 검증한다. `profile_id=None`(미설정)이면 프로필 저장소를 호출하지 않고 `PROFILE_NOT_CONFIGURED`를 반환한다.
3. runner `probe`를 실행한다.
4. `credential_scope`가 있으면 broker grant를 발급한다. 대상은 binding된 connection 하나다.
5. `runner.start`를 실행하고 관측값을 검사한다. 관측 모델·effort가 binding과 다르면 runner를 정지하고 `failed`(`OBSERVED_BINDING_MISMATCH`)로 끝난다.

2~5 단계 중 하나라도 실패하면 grant를 철회하고 supervisor 잠금을 해제(endpoint 삭제)한다. 그 뒤 구체 항목을 반환한다. 예상하지 못한 예외(`BaseException` 포함)가 전파될 때도 `start_failed`(`UNCLASSIFIED`)를 기록하고, 이미 시작한 runner 세션은 정지를 시도한 뒤 재전파한다. 시작 결과는 `started`·`already_running`·`not_configured`·`auth_required`·`unavailable`·`failed` 중 하나다.

**runner 정지 실패 시 예외:** 관측 불일치·잘못된 session 반환·예외 처리 중 `runner.stop`이 실패하면, 세션이 살아 있을 수 있으므로 grant만 철회하고 supervisor ownership(잠금·endpoint)은 유지한다. 상태는 `FAILED`, health의 `runner_stop_pending=True`이고 issues에 `RUNNER_STOP_FAILED`(`retry`)가 추가된다. 이 동안 같은 workspace의 다른 `Director.start()`는 `already_running`이다. 같은 객체의 `start()`는 probe 없이 `failed`(`RUNNER_STOP_FAILED`)를 반환한다. `stop()`을 다시 호출해 runner 정지에 성공해야 ownership이 해제된다.

관측이 불완전하면(예: 모델만 관측하고 effort는 `None`, binding effort는 있음) `matches=None`이다. 이때 불일치로 간주하지 않고 시작한다. 다만 `observed` 이벤트의 code를 `OBSERVATION_INCOMPLETE`로 남겨, 일치 확인 기록과 구분한다.

worker 프로필·pool·placement가 없어도 총괄을 검증하고 시작할 수 있다(완료 기준 1). `check()`는 2~4 단계만 수행한다. 이때 supervisor 잠금을 잡지 않고, 발급한 grant는 즉시 철회한다.

## 설정 필요 항목 (완료 기준 2)

`SetupIssue(field, code, action, details)`로 반환한다. `details`에는 고정 코드만 들어간다.

| field | code 예 | action |
| --- | --- | --- |
| `director_profile` | `PROFILE_NOT_CONFIGURED`, `PROFILE_NOT_FOUND` | `select_model` |
| `director_profile` | `NOT_A_DIRECTOR_PROFILE`, `PROFILE_SCOPE_MISMATCH`, `PROFILE_REJECTED` | `configure_profile` |
| `director_profile` | `PROFILE_STORE_UNAVAILABLE`, `PROFILE_CHECK_FAILED` | `retry` |
| `model` | `MODEL_NOT_SUPPORTED`(details에 capability 사유) | `select_model` |
| `model` | `OBSERVED_BINDING_MISMATCH` | `select_model` |
| `fallback` | `FALLBACK_APPROVAL_REQUIRED`, `FALLBACK_APPROVAL_STALE_REVISION` 등 | `approve_fallback` |
| `connection` | `CONNECTION_NOT_CONFIGURED`, `CONNECTION_OTHER_WORKSPACE` | `configure_connection` |
| `connection` | `CONNECTION_UNAVAILABLE` | `fix_connection` |
| `login` | `LOGIN_REQUIRED` | `login` |
| `credential` | `AUTH_REQUIRED` | `login` |
| `credential` | `NO_BROKER_SECRET` | `configure_connection` |
| `credential` | `LEASE_INACTIVE`, `CREDENTIAL_SCOPE_FAILED` | `retry` |
| `runner`·`supervisor` | `PROBE_FAILED`, `PROBE_INVALID`, `RUNNER_START_FAILED`, `RUNNER_SESSION_INVALID`, `RUNNER_STOP_FAILED`, `SUPERVISOR_UNAVAILABLE` | `retry` |

상태는 action으로 분류하며, 우선순위는 `not_configured` > `auth_required` > `unavailable`이다. GUI는 action에 따라 모델 선택이나 로그인 흐름으로 이어진다. 그 화면 연결은 범위 밖이다.

## 정지

`stop()`은 `RUNNING` 상태이거나, runner 정지가 보류된 `FAILED` 상태(`runner_stop_pending=True`)일 때 다음 순서로 동작한다.

1. 상태를 `STOPPING`으로 바꾼다. 이후 `poll()`은 wake를 만들지 않는다.
2. `runner.stop`을 호출한다.
3. inbox의 `leased` 항목 ID를 보고한다. 이 항목은 삭제하거나 상태를 바꾸지 않는다.
4. runner 정지에 성공한 경우:
   - grant를 철회한다.
   - `supervisor.shutdown()`을 호출한다. 이 단계에서 신규 placement를 거절하고, 진행 중 runtime lease를 삭제하지 않은 채 ID를 보고하며, endpoint와 잠금을 해제한다.
   - 결과는 `stopped`다.
5. runner 정지에 실패한 경우:
   - grant는 철회한다.
   - supervisor ownership(잠금·endpoint)은 유지한다. 상태는 `FAILED`, `runner_stop_pending=True`가 된다.
   - `runner_stop_failed`·`stop_failed` 이벤트를 남긴다.
   - 결과는 `StopResult("stop_failed", (), False, inbox ids, inbox scan 여부, False)`다. supervisor의 lease 스캔은 실행하지 않았으므로 `scan_complete=False`다.
   - 같은 workspace에서 두 번째 총괄 시작은 차단된다. `stop()`을 다시 호출해 재시도한다.

`scan_complete=False`는 진행 중 job이 "없음"이 아니라 "알 수 없음"을 뜻한다. 실행 중이 아닐 때의 `stop()`은 아무것도 스캔·정지하지 않으므로 `StopResult("not_running", (), False, (), False, False)`를 반환하고 `stop_rejected_not_running` 이벤트를 남긴다.

**한계:** ownership 유지는 같은 director 프로세스가 살아 있는 동안만 효과가 있다. director 프로세스가 종료되면 OS가 잠금을 해제한다. 따라서 runner가 별도 프로세스로 계속 살아 있다면, 다른 프로세스가 새 총괄을 시작하는 것을 막지 못한다. runner 프로세스 감시는 구현하지 않았다.

## 기록과 health

시작·정지 시도와 결과는 `DirectorEvent`로 남긴다. 이벤트 종류는 다음과 같다.

- 시작: `start_requested`, `start_rejected_duplicate`, `start_rejected_stop_pending`, `start_failed`, `observed`, `started`
- runner 정지 실패: `runner_stop_failed`
- 정지: `stop_requested`(details `retry`), `stop_rejected_not_running`, `stop_failed`, `stopped`
- 검증: `check_passed`, `check_failed`
- wake: `wake_delivered`, `wake_failed`
- 해제 실패: `grant_revoke_failed`, `supervisor_shutdown_failed`

각 이벤트는 순번, 주입 시계 시각, 고정 코드, instance ID, ID·모델명·코드만 담은 details를 가진다. `observed` 이벤트에는 요청/관측 모델·effort와 일치 여부가 기록된다(완료 기준 3). code는 일치 시 없음, 불일치 시 `OBSERVED_BINDING_MISMATCH`, 불완전 관측 시 `OBSERVATION_INCOMPLETE`다.

이벤트는 메모리 deque(기본 1024건)와 선택적 `event_sink`로 내보낸다. 예외 원문은 기록하지 않는다. **`event_sink`를 지정하지 않으면 기록은 비영속이다.** 프로세스가 재시작되면 사라지고, deque 용량을 넘으면 오래된 항목부터 삭제된다. 영속 감사는 sink 책임이다. sink가 예외를 던진 횟수는 `sink_failures`와 health의 `event_sink_failures`로 확인할 수 있다. 프로필 검증 결정은 `ProfileStore.execution_records`에 별도로 영속 기록된다.

`health()`는 프로세스 내부 payload(dict)를 반환하며 모델 turn을 일으키지 않는다. 포함 항목은 다음과 같다.

- state, accepting_work, instance_id
- profile/revision, bound model·effort, 관측값
- runner 상태
- `runner_stop_pending`, `event_sink_failures`
- 마지막 issues

runner 상태는 `RUNNING`이거나 runner 정지가 보류된 경우에만 runner에 조회한다. IPC·socket endpoint 서버는 구현하지 않았다.

## 유휴 시 모델 미호출

`poll()`은 `RUNNING`일 때만 `WakePolicy.poll()`을 호출한다. inbox에 lease 가능한 항목이 없거나 이전과 같은 큐 상태이면 `runner.wake`를 호출하지 않는다. 시험에서는 빈 큐에 대해 50회 poll해도 wake가 0회임을 확인한다.

## 시험

```bash
python -m unittest tests/test_director.py -v
```

supervisor와 inbox는 실제 패키지를 OS 임시 폴더에서 사용한다. profile gate·runner·broker는 fake이며 `ProfileStore.prepare_execution`·`CredentialBroker.issue_grant/revoke`와 같은 시그니처를 따른다. 시계는 주입한다. 검사 항목은 다음과 같다.

- 검증만 수행(`check`)
- worker 없이 시작
- 설정·연결 문제:
  - 미설정·프로필 없음·비총괄 프로필
  - 프로필 범위 불일치, 참조 누락·다른 workspace 참조
  - 프로필 저장소 장애·검사 예외
  - capability 거절
  - 로그인 필요·broker 인증 필요
  - 연결 불가(probe·broker), lease 비활성
  - probe 예외·잘못된 probe 결과
  - grant 철회 실패
- 중복 시작:
  - 동일 객체
  - 두 번째 `Director`
  - 별도 프로세스의 owner. 이 owner가 강제 종료되면 재시작이 가능한지도 확인한다
- 관측:
  - 관측 기록
  - 미관측·불완전 관측 code
  - 모델·effort 불일치 시 fail-closed
- runner 실패:
  - 잘못된 session
  - 시작 실패 시 예외 원문 비노출
- runner 정지 실패 시 ownership 유지와 재시도(시작 실패 경로·`stop()` 경로)
- `launch`의 예상 밖 예외와 `BaseException` 후 상태 복구
- 중단된 시작의 `start_failed` 기록과 세션 정지
- `not_running` 정지 결과
- 정지 시 inbox·runtime lease 보존
- 유휴 wake 0회
- health·sink 실패 횟수
- 설정 거절

## 미검증 (NOT_RUN)·한계

- 실제 Windows 11 동작 전체:
  - `msvcrt` 잠금으로 같은 프로세스 안 두 `Director`와 별도 프로세스의 중복 시작을 차단하는지
  - `%LOCALAPPDATA%` 경로

  Linux 통과는 개발 검증이며 Windows 지원 증거가 아니다.
- 실제 `ProfileStore`·`CapabilityInventory`·`CredentialBroker`·provider runner와의 결합. 시험은 fake를 사용한다.
- 실제 모델·effort 관측의 신뢰성. 관측값은 runner가 보고한 값이다.
- broker grant TTL(최대 3600초)보다 긴 세션의 grant 갱신은 구현하지 않았다.
- runner 정지 실패 후 director 프로세스가 종료되는 경우의 이중 실행 방지(위 "정지"의 한계 참조).
- runner 프로세스 감시·자동 재시작, health endpoint 서버, GUI 로그인·모델 선택 화면 연결.
- 기록 영속성은 `event_sink`에 의존한다.

이 문서와 시험 결과는 AIK-024의 완료 판정이 아니다. 선행 태스크(AIK-006·018·023)도 부분 구현 상태이며, 장부 상태는 변경하지 않는다.
