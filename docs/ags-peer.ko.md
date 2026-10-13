# AGS 세션 메시징(PEER) bridge — AIK-025 · AIK-043 기준 3 보강 (부분 구현)

`src/aik_ags_peer/`는 AIK 총괄 세션과 worker 세션 사이의 메시지를 AGS 세션 메시징 공개 CLI로 주고받는다. 이 모듈은 AIK worker 주소를 AGS `{host, sessionId}`에 연결하고, 전달·claim·ACK·result 단계를 `aik_readmodel.from_peer_message` 입력 형태로 내보낸다. 표준 라이브러리만 사용한다. 이 문서는 구현 범위만 설명하며, 실제 환경 검증 완료나 qualified 판정을 뜻하지 않는다.

## AGS 계약의 출처

- 출처는 AGS 2.9.0(commit `56fef8bd`)의 `mcp-server/dist/session-message-cli.mjs`다. 세션 총괄이 실제 CLI로 관측한 공개 계약이며, 이 태스크 문맥에는 관측 일자가 기록되어 있지 않다. 일자는 해당 관측 근거 기록에서 확인한다.
- AGS는 독립 제품이다. AIK는 AGS 내부 모듈이나 저장 형식(`session-messages.sqlite3` 등)을 import하거나 복제하지 않고 CLI만 호출한다.
- 요청 방식: stdin으로 JSON 한 개(`{"operation", "payload"}`)를 보내고 stdout에서 JSON 한 줄(`protocolVersion "1.0.0"`)을 받는다. 상태 디렉터리는 환경변수 `AGENT_GOVERNANCE_SESSION_MESSAGE_STATE_DIR`로 지정한다.
- AIK가 허용하는 operation은 `prepare`, `send`, `claim`, `acknowledge`, `status`, `pending`이다. `wait`와 `reconcile-wake-observation`, 그 밖의 값은 프로세스를 실행하기 전에 `UNSUPPORTED_OPERATION`으로 거절한다.
- AGS 주소는 `{host, sessionId}`뿐이다. AGS에는 worker나 result라는 메시지 유형 개념이 없다.

## AIK 규약과 AGS 계약의 경계

| 구분 | 소유 | 내용 |
|---|---|---|
| `{host, sessionId}`, messageId, ACK | AGS 계약 | broker가 ID를 발급한다. 내용은 불변이다. exactly-once를 보장하지 않는다 |
| `aik-worker:<worker_id>` | AIK 규약 | `AddressBook`이 worker 하나를 AGS 세션 하나에 append-only로 바인딩한다 |
| `aik.peer.v1` 본문 | AIK 규약 | AGS `body` 문자열 안에 넣는 canonical JSON `{"schema","kind","correlation_id","payload"}`. kind는 task/result/control이며 상한은 256 KiB다 |
| `ags_peer_id` | AIK 규약 | `host:sessionId`가 identifier 규칙을 만족하고 host에 `:`가 없으면 그 값을 그대로 쓴다. 그렇지 않으면 `peer-` + sha256(host\0sessionId) 앞 40자다 |

`decode()`는 규약에 맞지 않는 본문(다른 호스트의 일반 메시지 등)에 대해 `None`을 반환하며, 이를 오류로 취급하지 않는다. 같은 worker를 다른 세션에 바인딩하거나 같은 세션을 다른 worker에 바인딩하면 `ADDRESS_CONFLICT`가 발생한다.

## 사용 흐름

```python
cli = AgsSessionCli(node_abs, cli_abs, state_dir_abs)   # 기본 timeout 30초, 재시도 없음
book = AddressBook(addresses_db_abs)
book.bind("worker-1", "codex", "<worker sessionId>", vm_attempt_id="vm-attempt-1")
bridge = PeerBridge(cli, book, journal_db_abs, user_scope="user-local")
rec = bridge.send(("codex", "<orchestrator sessionId>"), "worker-1", encode("task", "corr-1", {...}))
bridge.observe(orchestrator, rec.ags_message_id)        # delivered → claimed → acked 순서 기록
results = bridge.claim_results(orchestrator)            # ACK하지 않음
bridge.acknowledge(orchestrator, [m.message_id for m in results])  # 처리 후 명시적으로 호출
pairs = bridge.events_after(0)                          # (message dict, cursor)
```

- `send`는 prepare 응답의 messageId를 send 호출 전에 journal에 `intent`로 기록한다. 이후 단계는 `sent`, `duplicate`, `send_unknown`, `send_failed`로 append된다.
- 본문은 저장하지 않는다. 저장하는 값은 sha256과 길이뿐이다. 본문은 stdin으로만 전달하며 argv에 넣지 않는다.
- 주소록과 journal은 각각 별도의 SQLite 파일이다. 둘 다 application_id `0x41494B50`(AIKP), schema 1을 쓰며, 헤더·schema·quick_check 검사와 UPDATE/DELETE 거절 trigger를 갖는다. 경로는 `aik_runtime.paths.validate_database_path` 규칙(저장소·`.git`·개발 원장 거절)을 따른다.

## 오류와 불명 처리

| 상황 | 코드 / outcome |
|---|---|
| CLI timeout | `AGS_TIMEOUT` / `unknown` |
| node 실행 실패(FileNotFound/Permission) | `AGS_UNAVAILABLE` / `not_sent` |
| JSON 한 줄이 아님, protocolVersion 불일치, stdout 1 MiB 초과 | `AGS_INVALID_RESPONSE` / `unknown` |
| `ok:false` | `AGS_REJECTED` / `rejected`. AGS error 문자열은 저장하지 않고 `detail_length`와 `detail_digest`(sha256)만 남긴다 |

- send 결과가 `unknown`이면 `SendRecord.state="send_unknown"`으로 기록하고 반환한다. **자동 재전송은 하지 않는다.** 같은 ID를 다시 보내려면 호출자가 `resend_known(message_id)`를 명시적으로 호출해야 한다. AGS는 이를 duplicate로 처리한다. 새 내용을 보낼 때는 새 intent를 준비한다.
- send가 `rejected` 또는 `not_sent`로 끝나면 `send_failed`를 기록한 뒤 예외를 다시 발생시킨다.
- `observe`가 `status: null`을 받으면 아무것도 기록하지 않고 `ObserveResult.status == "UNKNOWN_DELIVERY"`를 반환한다. 재전송은 하지 않는다.
- prepare가 불명으로 끝나면 ID가 없으므로 journal에 기록하지 않는다. prepare만으로는 전달이 일어나지 않는다.

## ACK와 완료는 다르다

AGS의 ACK는 처리 확인일 뿐이며 업무 완료나 승인이 아니다. `claim_results`는 메시지를 ACK하지 않는다. `acked` 이벤트와 `result` 이벤트 모두 VM 판정이나 태스크 완료의 근거가 아니다. read model의 `PeerView`도 `authoritative=False`인 표시용 투영이다.

## read model 연결

`events_after(cursor)`는 `(message, cursor)` 쌍을 반환한다. message는 `event_id`, `state`, `ags_message_id`, `ags_peer_id`와 선택 키 `ags_session_id`, `worker_address`, `worker_id`, `vm_attempt_id`만 갖는 dict다. 값이 없는 선택 키는 생략한다. `ags_session_id`는 sessionId가 identifier 규칙을 만족할 때만 포함한다. 이벤트 cursor는 1부터 빈틈 없이 이어지며, send intent는 cursor를 소비하지 않는다. `from_peer_message(message, cursor, user_scope)`에 그대로 넣거나 `bridge.to_source_events()`를 사용한다.

## 검증과 한계 (NOT_RUN 구분)

```bash
python -m unittest tests/test_ags_peer.py -v
```

- fake runner 시험은 실제 AGS가 아니라 관측된 계약을 흉내 낸 합성 시뮬레이션이다. 개발 검증에 해당한다.
- `RealAgsContractTests`는 `AIK_AGS_ROOT`(기본값 `../agent-governance-suite`) 아래에 CLI가 있고 node v24 이상일 때만 실행한다. 조건이 맞지 않으면 `NOT_RUN` 사유와 함께 skip한다. host 값은 `AIK_AGS_TEST_HOST`(기본값 `codex`)다.
- 2026-10-13 세션 Linux에서 AGS 2.9.0(`56fef8bd`) 실제 CLI와 node v24.21.0으로 이 시험이 통과했다. 검증 범위는 다음과 같다.
  - orchestrator→worker task 전송과 worker 쪽 claim·acknowledge
  - worker→orchestrator result 전송과 `claim_results`
  - `observe`가 delivered·claimed·acked를 기록함
  - 시험이 띄운 broker를 `stop_broker`로 종료하고 프로세스가 남지 않음
- CI에는 AGS checkout이 없어 이 시험이 skip(NOT_RUN)된다.
- 실제 broker 종료(`stop_broker`)는 Linux의 `/proc/<pid>/cmdline` 확인이 될 때만 수행한다. 다른 OS에서는 동작하지 않고 broker가 남는다. 이 정리는 시험 전용이며 제품 경로에서 자동으로 호출하지 않는다.
- **NOT_RUN**: 실제 worker 호스트 세션(Codex/Claude 등)과의 연결, Windows 실행, AGS wake/autoWake/relay(사용하지 않음), `aik_inbox` 연결(후속 태스크), broker 장애나 재시작 상황의 불명 전달 처리.
- 이 구현은 장부 상태와 완료 판정을 바꾸지 않는다. Linux 통과는 Windows 지원의 증거가 아니다.
