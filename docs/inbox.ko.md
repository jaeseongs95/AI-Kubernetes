# AIK 총괄 inbox·callback·wake 큐 — AIK-025 (부분 구현)

`src/aik_inbox/`는 사용자 지시와 AGS/VM 이벤트를 총괄에게 전달하기 전에 영속 저장한다. 처리 대기 항목이 있을 때만 총괄을 깨우는 신호를 만든다. 이 패키지는 Python 표준 라이브러리와 `aik_runtime`(경로 검증·잠금)만 사용한다. 총괄 모델 호출과 goal 완료 판정은 구현하지 않는다.

## 저장 위치와 식별

- inbox는 runtime DB와 다른 별도 SQLite 파일을 사용한다. application ID는 `0x41494B49`(`AIKI`)로, runtime(`AIKR`)·`AIKC`와 다르다. schema version은 1이다.
- 기본 위치는 Windows `%LOCALAPPDATA%\AIK\runtime\inbox.sqlite3`이다. 개발용 POSIX fallback은 `$XDG_STATE_HOME/aik/inbox.sqlite3` 또는 `~/.local/state/aik/inbox.sqlite3`다.
- 경로는 `aik_runtime.paths.validate_database_path`로 검사한다. 상대 경로, 저장소·Git 내부, 개발 원장과 그 sidecar 이름, symlink·hardlink, Windows 장치 이름, 원격 드라이브는 거절된다.
- 모든 작업은 `aik_runtime.paths.database_lock`(`<db>.storage.lock`)을 잡는다. 그 안에서 새 연결을 열고 `BEGIN IMMEDIATE` 트랜잭션 하나로 처리한다. `DELETE` journal과 `synchronous=FULL`을 사용한다. 잠금 기한(기본 5초)을 넘기면 `StorageBusy`로 끝나며, 해당 요청은 쓰기를 하지 않는다.
- 다른 application ID, 다른 schema, 빈 파일, 손상 파일, WAL 파일은 `InvalidInboxDatabase`로 거절한다. 이런 파일을 새 inbox로 덮어쓰지 않는다. 새 파일은 같은 디렉터리의 임시 파일에서 schema를 만든 뒤 `os.replace`로 게시한다.
- 기존 inbox 파일에 hot journal이 남아 있으면 SQLite의 표준 rollback 복구가 원본 파일에 적용된다. runtime 저장소처럼 private 사본에서 먼저 검사하지 않는다.

## 메시지 계약

| 필드 | 의미 |
| --- | --- |
| `message_id` | 멱등 키. 같은 ID가 다시 오면 처리하지 않고 `duplicate`를 반환한다 |
| `goal_id` | 목표 외부 ID. 목표의 상태나 판정은 저장하지 않는다 |
| `source` | cursor가 속한 이벤트 흐름(예: `gui`, `vm-events`, `ags-events`) |
| `event_cursor` | source별로 단조 증가해야 하는 양의 정수 |
| `kind` | `user_instruction`·`execution_result`·`error`·`system` |
| `payload` | JSON 객체, 정규화 후 최대 16 KiB, 깊이 8 이하 |

- payload에는 JSON 객체·배열·문자열·유한 숫자·불리언·null만 들어갈 수 있다.
- 비밀로 보이는 필드 이름은 거절한다. `password`, `secret`, `token`, `api_key`, `authorization`, `cookie`, `private_key` 등이 포함되면 대상이다. `max_tokens`처럼 무해한 이름도 거절될 수 있다(fail-closed).
- `Bearer ...`, PEM private key, 일부 알려진 key 접두사 같은 값도 거절한다. 이 휴리스틱은 완전하지 않다. 호출자는 credential 대신 opaque 참조 ID만 넣어야 한다.
- 오류 메시지에는 payload 값이나 필드 이름을 넣지 않는다.
- 같은 `message_id`에 내용이 다른 이벤트가 오면 `MessageConflict`를 반환한다. 이때 기존 기록은 유지되고 새 이벤트는 저장되지 않는다.

## 순서 역전

source마다 마지막 cursor를 기록한다. 새 cursor가 마지막 값보다 크면 `in_order`로 기록하고 마지막 값을 갱신한다. 같거나 작으면 `out_of_order`로 기록한다. out_of_order 항목도 버리지 않고 저장·전달하므로, 총괄은 `StoredMessage.order`로 역전을 확인할 수 있다. cursor가 연속하지 않는 경우(gap)는 정상 흐름으로 보고 별도로 표시하지 않는다. 전달 순서는 수신 순서(`seq`)의 FIFO이며, 종류별 우선순위는 없다.

## 상태와 lease

| 상태 | 의미 |
| --- | --- |
| `received` | 저장 완료, 처리 대기 |
| `leased` | consumer가 lease token과 만료 시각을 가지고 처리 중 |
| `acked` | 총괄이 메시지 처리를 확인함 |
| `failed` | 처리 실패, `retry_after` 이후 다시 lease 가능 |
| `dead` | 최대 시도 횟수 초과 또는 영구 실패. 자동 재처리하지 않는다 |

`lease_next(consumer_id, lease_seconds=60)`가 가장 오래된 처리 가능 항목을 lease하며, lease할 때 `attempts`가 1 증가한다. `ack`와 `fail`에는 현재 lease token이 필요하다. lease가 만료됐거나 token이 다르면 `LeaseLost`가 발생한다. 같은 token으로 `ack`를 다시 호출하면 멱등 처리된다. 최대 시도 횟수는 `max_attempts`(기본 5)다. `fail`의 `error_code`는 식별자만 허용하며, 오류 원문을 저장하지 않는다.

**ACK와 작업 완료는 다르다.** ACK는 "총괄이 이 메시지를 처리했다"는 기록일 뿐이다. goal/task 완료 판정은 VM 소유이며, 이 패키지는 그런 필드나 API를 갖지 않는다. `AckResult.goal_completion`은 항상 `"not_determined"`다. `execution_result`의 ACK도 실행 성공 판정이 아니다.

## outbox

`ack(message_id, token, outbox=[OutboxMessage(...)])`는 ACK와 후속 요청을 같은 트랜잭션에 기록한다. 후속 요청 종류는 `vm_request`·`ags_request`·`user_notice`다. `outbox_id`가 충돌하면 ACK 전체가 rollback되어 메시지는 `leased`로 남는다. 대기 항목은 `pending_outbox()`로 조회하고, 전달 후 `mark_delivered(outbox_id)`를 호출한다. `delivered`는 소유 서비스에 전달했다는 뜻이며, goal 완료를 뜻하지 않는다. 실제 전송 transport는 이번 범위 밖이다.

## 재시작 회수

`InboxStore`는 열릴 때 `recover()`를 실행하고 결과를 `last_recovery`에 남긴다. 만료된 `leased` 항목은 `received`로 돌리고 `last_error="lease_expired"`를 기록한다. 최대 시도에 도달한 항목은 `dead`로 바꾼다. 만료 전 lease는 기본적으로 유지된다. supervisor(AIK-006)가 단일 owner를 보장하는 재시작에서는 `InboxStore(..., reclaim_all_leases=True)` 또는 `recover(release_all_leases=True)`로 모든 lease를 즉시 회수할 수 있다. `lease_next`도 처리 전에 만료 lease를 회수한다.

## wake 정책

`WakePolicy(store, wake_callback, clock=None, min_interval=0.0)`의 `poll()` 규칙은 다음과 같다.

- 지금 lease 가능한 항목이 없으면 callback을 호출하지 않고 `None`을 반환한다. 빈 큐, 모든 항목이 lease 중인 큐, backoff 대기 중인 큐가 여기에 해당한다.
- 처리 가능 항목이 있고 큐 상태가 마지막 신호 이후 변했을 때만 `WakeSignal(available, kinds)`로 callback을 한 번 호출한다. 큐 상태는 변경 카운터, 처리 가능 수, seq·attempts 집계로 판단한다.
- 총괄이 항목을 소비하지 않아도 같은 상태로는 다시 깨우지 않는다. 따라서 polling을 반복해도 무한 model 호출로 이어지지 않는다.
- 중복 수신은 상태를 바꾸지 않으므로 wake를 만들지 않는다.
- `min_interval`은 연속 wake의 최소 간격(초)이다. callback이 예외를 내면 상태를 기록하지 않으므로 다음 poll에서 다시 시도한다.
- 시계는 `InboxStore(clock=...)`와 `WakePolicy(clock=...)`로 주입한다.
- 총괄 프로세스가 재시작되면 `reset()`을 호출하거나 새 policy를 만들어 남은 항목에 대해 다시 깨운다.

## 사용 예 (합성 값)

```python
from aik_inbox import InboxStore, InboxMessage, Consumer, WakePolicy

store = InboxStore()
store.receive(InboxMessage('msg-1', 'goal-1', 'gui', 1, 'user_instruction', {'text': 'synthetic'}))
policy = WakePolicy(store, lambda signal: print('wake', signal.available))
policy.poll()
consumer = Consumer(store, 'director')
leased = consumer.next()
if leased is not None:
    consumer.ack(leased)  # 처리 확인일 뿐 goal 완료가 아니다
```

## 검증

```bash
python -m unittest tests/test_inbox.py -v
```

시험은 OS 임시 폴더의 합성 DB만 사용하며 다음을 확인한다.

- 재열기 후 보존
- ACK 이후를 포함한 중복 멱등 처리와 충돌 거절
- source별 순서 역전 감지
- lease 배타성과 token 검사, ACK와 goal 완료의 분리
- 만료 lease 회수와 재시작 회수
- 실제 자식 프로세스를 강제 종료한 뒤 미처리 항목 회수
- backoff·dead 전이
- 빈 큐 1000회 poll에서 wake 0회, 종류별 반응, 중복 wake 억제와 rate limit
- outbox 원자성
- payload 비밀·크기·타입 거절
- 원장·저장소·상대 경로·외부/손상/빈 DB 거절
- application ID 구분과 기본 위치
- 잠금 시간 초과

Linux 통과는 개발 검증이며 Windows 지원 증거가 아니다.

## 미검증 항목 (NOT_RUN)

- 실제 Windows 11에서의 `msvcrt.locking` 잠금, `%LOCALAPPDATA%` 기본 경로, 프로세스 강제 종료 후 lease 회수, rename 전원 장애 내구성
- AGS/VM 이벤트 수신기와 GUI 지시 입력의 실제 연결, outbox 전송 transport, 총괄 model 호출 루프와 wake callback의 실제 결합
- 대규모 큐의 처리량·지연 시간
- 전원 차단·저장장치 고장
- 비밀 값 휴리스틱의 완전성

이 문서와 시험 결과는 AIK-025의 완료 판정이 아니다. 장부 상태는 변경하지 않는다.
