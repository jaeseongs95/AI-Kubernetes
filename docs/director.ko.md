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
| `stop(session)` | 세션 종료. VM job이나 진행 중 작업을 취소·삭제하지 않는다 |
| `health(session) -> str` | `alive`/`degraded`/`dead`/`unknown`. 모델 turn을 실행하지 않는다 |
| `wake(session, signal)` | 모델 turn을 실행할 수 있는 유일한 경로. director lock 안에서 호출되므로 빠르게 반환해야 한다(신호 전달용) |

`StartRequest`에는 workspace·instance ID, 검증된 binding, `grant_id`만 들어 있다. 비밀 값은 없다. runner/adapter가 broker의 `inject`를 통해 실행 시점에 비밀을 받는다.

## 시작 순서 (fail-closed)

1. `aik_supervisor.launch(workspace_id, component="director")`로 owner를 결정한다. 다른 live owner가 있으면 `already_running`과 기존 `instance_id`를 반환하며, 프로필 검증·probe·runner 시작을 하지 않는다. 같은 `Director`에서 반복 호출해도 `already_running`이다.
2. 프로필을 검증한다. `profile_id=None`(미설정)이면 프로필 저장소를 호출하지 않고 `PROFILE_NOT_CONFIGURED`를 반환한다.
3. runner `probe`를 실행한다.
4. `credential_scope`가 있으면 broker grant를 발급한다. 대상은 binding된 connection 하나다.
5. `runner.start`를 실행하고 관측값을 검사한다. 관측 모델·effort가 binding과 다르면 runner를 정지하고 `failed`(`OBSERVED_BINDING_MISMATCH`)로 끝난다.

2~5 단계 중 하나라도 실패하면 grant를 철회하고 supervisor 잠금을 해제(endpoint 삭제)한다. 그 뒤 구체 항목을 반환한다. 시작 결과는 `started`·`already_running`·`not_configured`·`auth_required`·`unavailable`·`failed` 중 하나다.

worker 프로필·pool·placement가 없어도 총괄을 검증하고 시작할 수 있다(완료 기준 1). `check()`는 2~4 단계만 수행한다. 이때 supervisor 잠금을 잡지 않고, 발급한 grant는 즉시 철회한다.

## 설정 필요 항목 (완료 기준 2)

`SetupIssue(field, code, action, details)`로 반환한다. `details`에는 고정 코드만 들어간다.

| field | code 예 | action |
| --- | --- | --- |
| `director_profile` | `PROFILE_NOT_CONFIGURED`, `PROFILE_NOT_FOUND` | `select_model` |
| `director_profile` | `NOT_A_DIRECTOR_PROFILE`, `PROFILE_SCOPE_MISMATCH`, `PROFILE_REJECTED` | `configure_profile` |
| `director_profile` | `PROFILE_STORE_UNAVAILABLE`, `PROFILE_CHECK_FAILED` | `retry` |
| `model` | `MODEL_NOT_SUPPORTED`(details에 capability 사유) | `select_model` |
| `fallback` | `FALLBACK_APPROVAL_REQUIRED`, `FALLBACK_APPROVAL_STALE_REVISION` 등 | `approve_fallback` |
| `connection` | `CONNECTION_NOT_CONFIGURED`, `CONNECTION_OTHER_WORKSPACE` | `configure_connection` |
| `connection` | `CONNECTION_UNAVAILABLE` | `fix_connection` |
| `login` | `LOGIN_REQUIRED` | `login` |
| `credential` | `AUTH_REQUIRED` | `login` |
| `credential` | `NO_BROKER_SECRET` | `configure_connection` |
| `credential` | `LEASE_INACTIVE` | `retry` |
| `runner`·`supervisor` | `PROBE_FAILED`, `RUNNER_START_FAILED`, `SUPERVISOR_UNAVAILABLE` | `retry` |

상태는 action으로 분류하며, 우선순위는 `not_configured` > `auth_required` > `unavailable`이다. GUI는 action에 따라 모델 선택이나 로그인 흐름으로 이어진다. 그 화면 연결은 범위 밖이다.

## 정지

`stop()`은 다음 순서로 동작한다.

1. 상태를 `STOPPING`으로 바꾼다. 이후 `poll()`은 wake를 만들지 않는다.
2. `runner.stop`을 호출한다.
3. inbox의 `leased` 항목 ID를 보고한다. 이 항목은 삭제하거나 상태를 바꾸지 않는다.
4. grant를 철회한다.
5. `supervisor.shutdown()`을 호출한다. 이 단계에서 신규 placement를 거절하고, 진행 중 runtime lease를 삭제하지 않은 채 ID를 보고하며, endpoint와 잠금을 해제한다.

`scan_complete=False`는 진행 중 job이 "없음"이 아니라 "알 수 없음"을 뜻한다. runner 정지에 실패하면 `runner_stopped=False`로 보고하고 `RUNNER_STOP_FAILED`를 기록한다. 이때 runner 프로세스가 아직 살아 있을 수 있다.

## 기록과 health

모든 시도는 `DirectorEvent`로 남긴다. 이벤트 종류는 `start_requested`, `start_rejected_duplicate`, `start_failed`, `observed`, `started`, `stop_requested`, `stopped`, `check_*`, `wake_*`다. 각 이벤트는 순번, 주입 시계 시각, 고정 코드, instance ID, ID·모델명·코드만 담은 details를 가진다. `observed` 이벤트에는 요청/관측 모델·effort와 일치 여부가 기록된다(완료 기준 3).

이벤트는 메모리 deque(기본 1024건)와 선택적 `event_sink`로 내보낸다. 예외 원문은 기록하지 않는다. 영속 감사는 sink 책임이다. 프로필 검증 결정은 `ProfileStore.execution_records`에 별도로 영속 기록된다.

`health()`는 프로세스 내부 payload(dict)를 반환하며 모델 turn을 일으키지 않는다. 포함 항목은 state, accepting_work, instance_id, profile/revision, bound model·effort, 관측값, runner 상태, 마지막 issues다. IPC·socket endpoint 서버는 구현하지 않았다.

## 유휴 시 모델 미호출

`poll()`은 `RUNNING`일 때만 `WakePolicy.poll()`을 호출한다. inbox에 lease 가능한 항목이 없거나 이전과 같은 큐 상태이면 `runner.wake`를 호출하지 않는다. 시험에서는 빈 큐에 대해 50회 poll해도 wake가 0회임을 확인한다.

## 시험

```bash
python -m unittest tests/test_director.py -v
```

supervisor와 inbox는 실제 패키지를 OS 임시 폴더에서 사용한다. profile gate·runner·broker는 fake이며 `ProfileStore.prepare_execution`·`CredentialBroker.issue_grant/revoke`와 같은 시그니처를 따른다. 시계는 주입한다. 검사 항목은 다음과 같다.

- 검증만 수행(`check`)
- worker 없이 시작
- 미설정·프로필 없음·비총괄 프로필·capability 거절·로그인 필요·broker 인증 필요·연결 불가
- 동일 객체 및 두 번째 `Director`의 중복 시작
- 관측 기록·미관측·불일치 시 fail-closed
- runner 시작 실패 시 예외 원문 비노출
- 정지 시 inbox·runtime lease 보존
- 유휴 wake 0회
- health
- 설정 거절

## 미검증 (NOT_RUN)·한계

- 실제 Windows 11 동작 전체: `msvcrt` 잠금으로 같은 프로세스 안 두 `Director`의 중복 시작을 차단하는지, `%LOCALAPPDATA%` 경로. Linux 통과는 개발 검증이며 Windows 지원 증거가 아니다.
- 실제 `ProfileStore`·`CapabilityInventory`·`CredentialBroker`·provider runner와의 결합. 시험은 fake를 사용한다.
- 실제 모델·effort 관측의 신뢰성. 관측값은 runner가 보고한 값이다.
- broker grant TTL(최대 3600초)보다 긴 세션의 grant 갱신은 구현하지 않았다.
- runner 프로세스 감시·자동 재시작, health endpoint 서버, GUI 로그인·모델 선택 화면 연결.

이 문서와 시험 결과는 AIK-024의 완료 판정이 아니다. 선행 태스크(AIK-006·018·023)도 부분 구현 상태이며, 장부 상태는 변경하지 않는다.
