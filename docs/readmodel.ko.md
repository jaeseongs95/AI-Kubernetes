# AIK-043 — VM·AGS·AIK 이벤트 상호 참조 read model (부분 구현)

`src/aik_readmodel/`은 GUI가 상태를 한곳에서 읽도록 각 원장의 이벤트를 ID·revision·cursor로 연결하는 **재구축 가능한 projection**이다. 표준 라이브러리만 사용한다.

## 권위가 아니다

이 저장소는 **non-authoritative**다. 권위는 다음 원장에 남는다.

| 대상 | 권위 |
|---|---|
| Goal·Task·Attempt·재시도·검증·GoalVerdict | VM |
| workflow·peer·session, gate 계약 | AGS(집행은 VM) |
| placement·lease·dispatch·lifecycle·evidence | 각 AIK 원장 |

DB의 `meta` 테이블에는 `authority=non-authoritative`가 기록되고, 스키마 주석과 모든 view(`AttemptView`, `PeerView`)의 `authoritative=False`도 같은 의미다. view의 상태는 표시용 사본이며 완료·준비·인가를 판정하지 않는다. 이 projection을 VM의 권위 원장으로 사용하면 안 된다.

## 이벤트와 cursor 규칙

`SourceEvent(source, cursor, event_id, user_scope, kind, refs, payload)`의 `digest`는 나머지 필드의 canonical JSON sha256이다. `source`는 `vm`, `aik_runtime`, `dispatch`, `lifecycle`, `evidence`, `ags_peer` 중 하나다. `cursor`는 source별로 1부터 연속 증가하는 번호다. VM은 `history_cursor`를 사용한다.

`ReadModel.apply(events)`는 한 트랜잭션에서 다음을 수행한다.

- 같은 `(source, cursor)`와 같은 digest: duplicate로 무시한다.
- 같은 `(source, cursor)`와 다른 digest: `CURSOR_CONFLICT`를 append-only `conflicts`에 기록한다. 같은 `event_id`가 다른 cursor로 오면 `EVENT_ID_CONFLICT`를 기록한다. 기존 이벤트와 projection은 바꾸지 않는다.
- 연속 다음 cursor가 아닌 이벤트: `event_log`에는 저장하지만 projection에는 연속 구간까지만 적용한다. 빈 구간은 `gaps`에 열린 gap으로 기록하고, 해당 source는 `stale`로 표시한다.
- 늦게 온 이벤트가 gap을 메우면 이어지는 저장 이벤트까지 적용하고 gap을 닫는다(순서 역전 복원). gap 일부만 메우면 기존 gap을 닫고 남은 구간을 새 gap으로 연다.

`rebuild()`는 projection 테이블(`source_cursor`, `projected_events`, `event_refs`, `links`, `attempts`)을 비우고, `event_log`를 source·cursor 순으로 다시 적용한다. 각 이벤트의 digest도 다시 검증한다. projection은 적용 순서와 무관하게 결정되므로 `projection_digest()`는 rebuild 전후와 정순 적용 결과가 같다.

## 링크 규칙

`refs`에는 `vm_project_id`, `vm_goal_id`, `vm_task_id`, `vm_attempt_id`, `placement_id`, `lease_id`, `request_id`, `provider_job_id`, `artifact_id`, `ags_workflow_id`, `ags_session_id`, `ags_peer_id`, `ags_message_id`, `worker_address`, `worker_id`만 허용한다. 한 이벤트에 함께 나온 ref 쌍을 `links`에 출처 source·cursor와 함께 기록한다.

`attempt_view(vm_attempt_id)`는 attempt 고유 ID(`vm_attempt_id`, `placement_id`, `lease_id`, `request_id`, `provider_job_id`, `artifact_id`, `ags_session_id`, `ags_message_id`)를 통해서만 그래프를 확장한다. project·goal·task·workflow·peer·worker ID와 worker 주소는 결과에 포함하지만 확장하지 않는다. 따라서 같은 worker를 다시 쓰는 다른 attempt의 lease는 섞이지 않는다. 결과에는 키별 ID tuple, source별 최신 cursor의 `kind`/`state`, source별 cursor·gap·conflict 상태가 포함된다. 서로 다른 source의 상태는 합치지 않는다. 하나의 AGS session을 여러 attempt가 공유하면 그 attempt들은 한 view로 연결된다. 이 경우 session이 attempt 고유 ID라는 가정이 성립하지 않는다.

`peer_view(ags_peer_id)`는 `ags_peer` source만 읽는다. 결과에는 worker 주소·worker ID·session과 cursor 순 전달 이력(`delivered`/`claimed`/`acked`/`result`), 메시지별 최신 상태가 포함된다.

## 어댑터

순수 함수 어댑터는 원문 값을 refs/state로 옮기기만 한다. 상태를 추론하거나 누락된 ID를 만들지 않는다.

- `from_bridge_receipt(receipt, user_scope)`: `aik_vm_bridge.BridgeReceipt`만 받는다(그 밖은 `INVALID_BRIDGE_RECEIPT`). `receipt.refs.history_cursor`가 없으면 `None`을 반환한다. ID는 `receipt.refs`의 `project_id`/`goal_id`/`task_id`/`attempt_id`를 그대로 `vm_*` 키로 옮기고, kind는 command 이름, state는 bridge가 관측한 VM RuntimeJob status(`receipt.job.vm_status`)가 있을 때만 쓴다. bridge `request_id`는 payload의 `vm_request_id`로 보관하며, dispatch의 `request_id`와 연결하지 않는다.
- `from_lease(versioned_lease, placement, user_scope, *, cursor=...)`: source cursor가 전역 단조여야 하므로 cursor는 호출자가 지정한다. lease revision은 payload에 넣는다. placement·pool이 일치하지 않으면 거절한다.
- `from_peer_message(message, cursor, user_scope)`: 알려진 필드만 받는다. message 본문 같은 미지 필드와 네 단계 밖의 상태는 거절한다.

dispatch·lifecycle·evidence 이벤트는 호출자가 `SourceEvent`를 직접 만든다.

## 같은 사용자 범위

`ReadModel(path, user_scope=...)`의 범위는 DB `meta`에 한 번 기록되고 바뀌지 않는다. 다른 범위로 다시 열거나 다른 범위의 이벤트를 하나라도 포함한 batch를 적용하면 `ScopeMismatch`로 전부 거절하며 아무것도 기록하지 않는다. `user_scope`는 같은 OS 사용자를 가리키는 호출자 식별자다. 이 모듈은 실제 OS principal을 확인하지 않는다.

## 저장소

경로는 `aik_runtime.paths.validate_database_path`의 규칙을 따른다. 저장소·Git·개발 원장 경로와 symlink·hardlink를 거절하며, 협력 프로세스는 `.storage.lock`으로 직렬화한다. application ID는 `0x41494B4D`(`AIKM`)다. schema·application ID·journal mode가 다른 파일은 `InvalidReadModelDatabase`로 거절하고 수정하지 않는다. `event_log`·`conflicts`·`meta`는 trigger로 append-only를 강제한다. `gaps`는 한 번 닫는 것만 허용한다. 연결은 작업마다 열고 닫는다.

## 한계와 검증 상태

- 실제 VM History·AGS PEER 이벤트 수집기는 없다. 실제 VM·AGS·provider 소스 연결은 **NOT_RUN**이다. 실제 `BridgeReceipt` 필드 이름과의 일치도 검증하지 않았다.
- gap 판정은 source cursor가 1부터 빈틈없이 이어진다고 가정한다. VM history_cursor처럼 receipt 사이에 cursor가 비는 source는 영구 gap(`stale`)으로 표시된다. 이는 숨기지 않는 fail-closed 동작이지만, 그 source의 이후 이벤트가 projection에 반영되지 않는다.
- GUI와 연결되지 않았다. Windows 동작은 **미검증(NOT_RUN)**이며, Linux 단위시험 통과는 개발 검증일 뿐이다.
- 장부 상태와 완료 판정은 변경하지 않는다.

```bash
python -m unittest tests/test_readmodel.py -v
```
