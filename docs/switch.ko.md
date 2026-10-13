# AIK-028 — 총괄 모델 교체·허용 fallback·handoff (부분 구현)

상태: **구현했고, 합성 데이터로 단위시험을 작성했다.** fake inventory, fake runner, fake profile source, 실제 SQLite 파일(OS 임시 폴더)을 사용한다. 다음 항목은 모두 **미검증(NOT_RUN)**이다.

- 실제 모델 호출과 실제 provider 인증 장애
- 실제 runner의 세션 교체와 관측 model/effort 보고
- Windows 11 실행(`msvcrt` 잠금, `%LOCALAPPDATA%` 경로)

이 문서와 시험은 qualified 판정이나 완료 판정이 아니다. 장부(`planning/`)의 AIK-028 상태(`todo`)와 완료 판정을 바꾸지 않는다. 선행 태스크 AIK-020·AIK-023·AIK-027도 부분 구현 상태다.

`src/aik_switch/`는 총괄(director) 프로필의 교체 상태기계, 허용 fallback 판정, 전환 기록, handoff 전달을 제공한다. 표준 라이브러리와 아래 기존 패키지만 import해서 재사용하며, 기존 패키지는 수정하지 않는다.

| 패키지 | 사용 |
| --- | --- |
| `aik_profiles` | `DirectorProfile`의 primary·fallback 순서, `rebuild_profile`, `FallbackApproval`(승인 형식 검증) |
| `aik_capability` | `inventory.validate`의 `ValidationResult`·`Provenance`, `CapabilityError` 코드 |
| `aik_continuity` | `ContinuityStore.handoff()`의 `HandoffPacket`(기존 checkpoint revision 포함) |
| `aik_director.models` | `DirectorBinding`, `RunnerSession`, `Observation.of` |
| `aik_config.models` | `Revisioned`(profile source 반환 형식) |
| `aik_runtime` | `paths.validate_database_path`·`database_lock`, 식별자 규칙, `StorageBusy` |

## 구성 요소

- `DirectorSwitch`: 상태기계. 총괄 프로필 하나와 project 하나를 대상으로 한다.
- `SwitchPolicy` / `FallbackRule`: 사용자가 설정한 허용 범위.
- `SwitchJournal`: append-only SQLite 기록.
- `evaluate()` / `check_capability()`: 후보 판정(순수 함수).

주입 port(모두 duck typing):

| port | 메서드 | 실제 구현 예 |
| --- | --- | --- |
| profiles | `get(profile_id) -> Revisioned \| None` | `aik_profiles.ProfileStore` |
| inventory | `validate(connection_id, model, effort, tools, *, min_context=None)` | `aik_capability.CapabilityInventory` |
| runner | `activate(binding, handoff) -> RunnerSession` | 미구현(실제 runner 연결 NOT_RUN) |
| continuity | `handoff(project_id) -> HandoffPacket` | `aik_continuity.ContinuityStore` |

profile source는 읽기만 한다. 이 패키지에는 프로필을 저장·삭제하는 코드 경로가 없다.

## 상태기계 (완료 기준 1)

상태는 `active`, `switch_pending`, `reauth_required`, `decision_required` 네 가지다.

1. `begin_call(call_id)`는 진행 중 호출을 등록하고, 그 호출이 쓸 binding을 반환한다. 호출은 끝날 때까지 이 binding을 유지한다.
2. `request_switch(SwitchRequest)`가 후보를 찾으면 요청 기록(`accepted`)을 남긴다.
   - 진행 중 호출이 없으면 즉시 적용한다(`applies="immediately"`).
   - 진행 중 호출이 있으면 `switch_pending`이 된다(`applies="after_calls"`, `waiting_call_ids`). 이 동안 `begin_call`은 `SwitchPending`으로 거절한다. 그래서 적용 시점이 "마지막 진행 중 호출의 종료 관측"으로 하나로 정해진다.
3. `observe(CallEvent(event_id, call_id, kind))`의 `kind`는 `completed`·`failed`·`cancelled`다. 마지막 진행 중 호출의 terminal 관측에서만 대기 중인 전환을 적용한다. 취소는 **취소 완료 관측**만 적용 시점이다. 취소 요청 자체나 응답 타임아웃은 적용 시점이 아니다.
4. 진행 중 호출을 다른 모델로 이어붙이지 않는다. 실패한 호출도 자동 재실행하지 않는다. 새 모델에는 `aik_continuity` handoff 패킷만 전달한다.

이중 처리 방지:

- 같은 call `event_id` 재전송 → `duplicate`(효과 없음). 같은 event ID를 다른 call에 쓰면 `SwitchConflict`.
- 이미 끝난 call에 대한 다른 terminal 이벤트 → `already_terminal`(효과 없음).
- 등록되지 않은 call → `unknown_call`.
- 같은 switch `event_id` 재전송 → `duplicate`. 저장된 요청·적용 기록을 그대로 반환하고 runner를 다시 호출하지 않는다. switch event ID는 journal에 기록되므로 재기동 뒤에도 중복으로 판정된다.
- 대기 중에 들어온 다른 전환 요청 → `rejected`(`SWITCH_ALREADY_PENDING`). 그 event ID도 소비된다.

적용 시점에는 다음을 다시 확인한다.

- 프로필 revision이 요청 시점과 같은지. 다르면 `recheck_failed`(`PROFILE_CHANGED`).
- 정책·inventory 판정. 실패하면 `recheck_failed`(`RECHECK_REJECTED`와 이유).
- handoff를 읽을 수 있는지(`CONTINUITY_UNAVAILABLE`·`INVALID_HANDOFF`), `require_checkpoint`일 때 checkpoint가 있는지(`CHECKPOINT_REQUIRED`).
- runner 활성화 성공 여부(`RUNNER_UNAVAILABLE`·`INVALID_RUNNER_RESULT`). 이 경우 `activation_failed`.

위 실패에서는 binding을 바꾸지 않고 `decision_required`로 멈춘다.

## 허용 fallback 판정 (완료 기준 2)

후보는 `aik_profiles` 총괄 프로필의 순서(`primary`, `fallback:0`, `fallback:1`, …) 안에서만 고른다. 그중 `SwitchPolicy.rules`에 위치가 있는 것만 허용한다.

| 필드 | 의미 |
| --- | --- |
| `profile_id`, `profile_revision` | 정책이 묶이는 프로필과 revision. 다르면 `POLICY_STALE` |
| `approval_ref` | 사용자 승인 참조. fallback 전환 기록에 `FallbackApproval` 형식으로 남는다 |
| `data_scope` | 이번 작업의 데이터 범위 label(예: workspace/connection scope) |
| `rules` | `FallbackRule(position, quality, estimated_cost, data_scopes)` |
| `min_quality` | 품질 하한(1~100, 사용자 지정 등급) |
| `required_tools` | 필요 도구. 후보 choice의 tools에 모두 있어야 한다 |
| `min_context` | inventory의 context 요구치(선택) |
| `max_cost_per_call` | 호출당 비용 상한(선택) |
| `fallback_on_auth` | 인증 장애 시 fallback 허용 여부. 기본 `False` |
| `require_checkpoint` | 적용 시 checkpoint 필수 여부. 기본 `True` |

후보 판정 순서와 거절 코드:

1. 정책 없음 `NO_FALLBACK_POLICY`, 정책 revision 불일치 `POLICY_STALE`, 규칙 없음 `NOT_ALLOWED_BY_POLICY`, 프로필에 없는 위치 `POSITION_OUT_OF_RANGE`
2. `SAME_AS_CURRENT`, 인증 장애 시 같은 connection `SAME_CONNECTION_AUTH_FAILED`
3. `QUALITY_BELOW_FLOOR`, `TOOL_NOT_CONFIGURED:<tool>`, `DATA_SCOPE_NOT_ALLOWED`
4. 예산: `OVER_COST_LIMIT`, 남은 예산을 모르면 `BUDGET_UNKNOWN`, 초과 `BUDGET_EXCEEDED`(비용 0인 후보는 예산 검사 생략)
5. 위가 모두 통과한 경우에만 inventory를 조회한다. `provider_observed`·`configured_catalog` 근거로 수락되고 요청 provenance가 `requested`일 때만 통과한다. 그 밖에는 `CAPABILITY_REJECTED`와 `CAPABILITY:<inventory 이유>`, `NO_SUPPORT_EVIDENCE`, `CAPABILITY_ERROR:<코드>`, `INVENTORY_UNAVAILABLE`, `VALIDATION_RESULT_MISMATCH`로 거절한다.

요청 이유(`reason`):

- `user_request`: `target` 위치 하나만 판정한다. 실패하면 `decision_required`를 반환하지만 현재 상태는 유지한다(현재 binding은 정상이므로).
- `provider_unavailable`, `capability_lost`: 현재 위치를 뺀 순서대로 판정하고, 처음 통과한 후보로 전환한다. 통과 후보가 없으면 자동 전환하지 않고 `decision_required` 상태로 멈춘다. 새 호출은 `SwitchBlocked`로 거절한다.
- `auth_expired`, `auth_locked`: 인증 장애다.
  - `fallback_on_auth=False`(기본) → `reauth_required`(`REAUTH_REQUIRED`). 같은 프로필을 다시 로그인해야 한다는 뜻이며 전환하지 않는다.
  - `fallback_on_auth=True` → 같은 connection을 쓰는 후보를 제외하고 fallback을 판정한다. 후보가 없으면 `reauth_required`(`NO_QUALIFYING_CANDIDATE`, `REAUTH_REQUIRED`).

차단 상태 해제는 다음 둘 중 하나다.

- 다른 위치로의 전환 성공
- `resume_current(event_id)`: 재로그인이나 사용자 결정 뒤 현재 binding을 inventory로 다시 확인하고 `active`로 돌아간다. 로그인 자체는 이 패키지가 하지 않는다.

## 기록과 handoff (완료 기준 3)

`SwitchJournal`은 전용 SQLite 파일이다.

- 기본 위치: Windows `%LOCALAPPDATA%\AIK\runtime\switch.sqlite3`, 개발용 POSIX fallback은 runtime DB와 같은 디렉터리.
- application ID `0x41494B53`(`AIKS`), schema version 1. `DELETE` journal, `synchronous=FULL`, 작업마다 `.storage.lock` 잠금과 `BEGIN IMMEDIATE`.
- UPDATE·DELETE trigger로 append-only를 강제한다. `(event_id, phase)`는 유일하며 같은 내용 재기록은 `duplicate`, 다른 내용은 `SwitchConflict`.
- 열 때 schema·integrity와 모든 행의 fingerprint·정규 JSON을 검사한다. 위반하면 `InvalidSwitchJournal`로 거절하고 파일을 덮어쓰지 않는다. 경로 규칙은 `aik_runtime`과 같다(저장소 내부·상대 경로·symlink 등 거절).

기록 종류:

- `phase="request"`: `kind` `switch_request` 또는 `resume`. 이유, target, 남은 예산, 이전 binding, 계획된 binding, 승인, 후보별 판정, 대기 call ID, `state_after`, `block_codes`.
- `phase="apply"`: `kind` `switch_apply`. 상태는 `applied`·`recheck_failed`·`activation_failed`.

`applied` 기록의 필드:

| 필드 | 내용 |
| --- | --- |
| `reason` | 전환 이유 |
| `previous`, `new` | 이전/새 binding(connection·model·effort·tools·위치·프로필 revision) |
| `requested_model`, `requested_effort` | 요청값 |
| `observed_model`, `observed_effort` | runner가 실제로 관측해 보고한 값. 관측이 없으면 `null` |
| `observed_model_reason`, `observed_effort_reason` | 관측이 없을 때 `NOT_OBSERVED`. 요청값을 관측값으로 승격하지 않는다 |
| `observation_matches` | `Observation.of` 결과(`true`/`false`/`null`) |
| `checkpoint` | `aik_continuity`의 기존 checkpoint 참조(`project_id`, `revision`, goal/plan revision ID). 없으면 `null`과 `checkpoint_reason="NO_CHECKPOINT"` |
| `handoff` | handoff 패킷의 ID 요약(지시 수, active 지시 ID, summary ID, 설정 참조). 지시·요약 **텍스트는 기록하지 않는다** |
| `approval` | fallback 위치의 승인(`profile_id`, `profile_revision`, `fallback_index`, `approval_ref`) |

관측 model이 요청과 다르면 runner는 이미 전환했으므로 새 binding을 기록하되 `decision_required`(`OBSERVED_BINDING_MISMATCH`)로 멈춘다.

handoff 패킷은 `ContinuityStore.handoff(project_id)`의 결과를 그대로 runner에 넘긴다. 기록된 checkpoint revision은 같은 패킷에서 읽으므로 runner가 받은 패킷과 일치한다. 미완료 task와 검증 근거의 **현재 값**은 이 패킷이 아니라 `aik_continuity.Resumer`의 VM 재조회로 확인해야 한다(AIK-027 문서 참고).

**worker 프로필은 바꾸지 않는다.** `DirectorSwitch`는 `DirectorProfile`만 받는다. worker·reviewer 프로필 ID를 주면 `InvalidSwitch`로 거절한다. 정책의 `profile_id`가 다르면 생성 시 거절한다.

## 재기동

`DirectorSwitch`를 다시 만들면 journal에서 상태를 복원한다.

- 마지막 `applied` 기록의 binding을 복원한다. 프로필 revision이나 해당 위치의 choice가 바뀌었으면 `decision_required`(`PROFILE_CHANGED_SINCE_LAST_SWITCH`).
- 마지막 기록의 `state_after`가 차단 상태면 그 상태와 코드를 복원한다.
- 마지막 기록이 `switch_pending`이면, 대기 요청과 진행 중 호출은 메모리에만 있었으므로 `decision_required`(`PENDING_SWITCH_LOST_ON_RESTART`)로 멈춘다(fail-closed). 그 요청은 같은 event ID로 다시 적용되지 않는다. 새 event ID로 다시 요청해야 한다.

## 사용 예 (합성 값)

```python
from aik_switch import DirectorSwitch, SwitchJournal, SwitchPolicy, FallbackRule, SwitchRequest, CallEvent

policy = SwitchPolicy('director-1', 3, 'approval-1', 'ws-1',
                      rules=(FallbackRule('fallback:0', 80, 20, ('ws-1',)),),
                      min_quality=75, required_tools=('shell',))
switch = DirectorSwitch(profiles=profile_store, profile_id='director-1', inventory=inventory,
                        runner=runner, continuity=continuity_store, project_id='project-1',
                        journal=SwitchJournal(), policy=policy)
binding = switch.begin_call('call-1')
switch.request_switch(SwitchRequest('evt-1', 'user_request', 'fallback:0', budget_remaining=100))  # pending
switch.observe(CallEvent('evt-call-1', 'call-1', 'completed'))                                     # applied
```

## 시험

```bash
python -m unittest discover -s tests -p 'test_switch.py' -v
```

검사 항목:

- 호출 중 변경: 대기, 대기 중 새 호출 거절, terminal 관측 뒤에만 적용, 취소 완료 관측, 대기 중 다른 요청 거절
- 중복 이벤트: 같은 call event·switch event 재전송, 이미 끝난 call, 미등록 call, 재기동 뒤 중복
- 지원 도구 부족: 프로필 tools 부족, inventory의 도구 미지원, 자동 fallback의 순서와 첫 통과 후보
- 통과 후보 없음·inventory 장애에서 사용자 결정 대기와 새 호출 차단, `resume_current`
- 인증 장애: 재인증 필요와 fallback 구분, 같은 connection 제외
- 예산 초과·예산 미상·비용 상한·품질 하한·데이터 범위·정책 미허용·정책 stale·정책 없음
- 대기 중 프로필 변경의 적용 시점 재검사 실패, checkpoint 필수, runner 실패
- 기록: 이유·이전/새 모델·요청/관측 effort·관측 없음의 `null`+이유·관측 불일치·checkpoint revision·승인
- handoff 연속성: runner가 받은 패킷의 checkpoint revision과 지시가 기록과 일치, journal 파일에 지시 텍스트 없음
- worker 프로필 거절·미변경
- 재기동 복원, 대기 유실의 fail-closed, journal append-only, 외부 DB 보존, Git 내부·상대 경로 거절

이 목록은 시험 범위이며 실행 결과 기록이 아니다. Linux 통과는 개발 검증이며 Windows 지원 증거가 아니다.

## 한계·미검증(NOT_RUN)

- 실제 모델 호출, 실제 runner의 세션 교체, 실제 provider의 인증 만료/잠금 신호와의 연결은 미검증이다. 인증 장애 분류(`auth_expired`/`auth_locked`)는 호출자가 요청에 넣는다.
- 품질 등급, 호출당 예상 비용, 데이터 범위 label은 사용자가 정책에 지정한 값이다. 이 패키지는 실제 품질을 측정하거나 예산을 차감하지 않고, 데이터가 실제로 범위 밖으로 나가는지 집행하지도 않는다. `budget_remaining`은 호출자가 준 값이다.
- `approval_ref`는 형식만 검사한다(승인 기록의 실재는 미검증).
- 진행 중 호출, 대기 요청, call event 중복 기억은 프로세스 메모리에만 있다. 재기동 시 대기 요청은 유실로 처리한다. call event 기억은 상한이 없다.
- 적용 기록 저장이 runner 활성화 뒤 실패하면 상태를 `decision_required`(`JOURNAL_WRITE_FAILED`)로 두고 예외를 전파한다. runner는 이미 전환했을 수 있다.
- `resume_current`는 현재 binding의 capability만 다시 확인한다. 관측 불일치 차단도 이 호출로 해제되므로, 해제는 사용자의 명시적 결정으로 취급한다.
- capability 수락 규칙은 `aik_profiles.execution`의 비공개 helper와 같은 규칙을 이 패키지에서 다시 구현했다(fallback을 primary 수락 여부와 무관하게 판정해야 하므로).
- journal은 hot journal을 원본에서 SQLite 표준 rollback으로 복구하며, 강제 종료 시험은 작성하지 않았다. Windows 잠금·기본 경로는 NOT_RUN.
