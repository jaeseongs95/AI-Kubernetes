# AIK-027 — 총괄 문맥·지시·작업 연속성 (부분 구현)

상태: **구현했고, 합성 데이터와 fake VM read port로 단위시험을 작성했다.** 다음 항목은 모두 **미검증(NOT_RUN)**이다.

- 실제 VM 설치물의 status payload와의 대조
- Windows 11 실행(`msvcrt` 잠금, `%LOCALAPPDATA%` 경로)
- 실제 총괄 모델의 문맥 축약 동작
- 강제 종료 후 hot journal 복구

**미구현:** AGS 원장 참조는 **미구현**이다. `Checkpoint`·`TaskRef`에는 AGS session ID 같은 AGS 참조 필드가 없고, AGS 원장에서 문맥을 회수하는 경로도 없다. 따라서 현재 복원 대상은 VM 원장 참조뿐이다.

이 문서와 시험 결과는 qualified 판정이나 완료 판정이 아니며, 장부(`planning/`)의 AIK-027 상태(`todo`)를 바꾸지 않는다. 선행 태스크 AIK-025·AIK-026도 부분 구현 상태다.

`src/aik_continuity/`는 총괄의 사용자 지시·정정, 설정 revision, VM 원장 참조 checkpoint를 영속 저장한다. 재기동이나 문맥 축약 뒤에는 저장된 참조를 VM 읽기 명령으로 다시 확인해 기존 Goal을 이어간다. 표준 라이브러리와 아래 기존 패키지만 import해서 재사용하며, 기존 패키지는 수정하지 않는다.

| 패키지 | 사용 |
| --- | --- |
| `aik_runtime` | `paths.validate_database_path`·`database_lock`(경로 규칙·프로세스 잠금), 식별자 규칙, `StorageError` 계층 |
| `aik_inbox` | `canonical_payload`(16 KiB 한도·비밀 값 휴리스틱), `fingerprint` |
| `aik_controller` | `VmView`·`refs`로 VM status payload 해석 |
| `aik_vm_bridge` | `BridgeStatusPort`에서 `ProjectQuery`로 `status` 조회(지연 import) |

## 권위 경계

- **VM/AGS 원장이 권위다.** Goal·Plan·Task·Attempt의 상태, verdict, evidence 내용은 저장하지 않는다. checkpoint에는 `project_id`, `goal_id`, `goal_revision_id`, `plan_revision_id`, `authorization_revision_id`, task/attempt ID, evidence ID만 들어간다(모두 VM 참조). `TaskRef`에는 상태 필드가 없다.
- **모델 요약은 보조 값이다.** `ContextSummary`는 항상 `authority="model_reported"`다. `Resumer`는 요약 텍스트를 읽지 않으므로 요약 내용은 재개 판단이나 미완료 목표 결정의 입력이 아니다. 시험의 "요약이 task-b 완료를 주장해도 VM 기준" 단언은 이 구조의 결과를 보여줄 뿐, 요약을 해석해 무시하는 별도 논리를 검증하는 것은 아니다.
- **재개는 새 Goal/Attempt를 만들지 않는다.** `Resumer`는 VM port의 `status`만 호출하며, prepare/revise/run_once/replan 같은 효과 명령을 호출하는 코드 경로가 없다.
- 사용자 원문 지시와 handoff에는 credential 필드가 없다. 지시·요약 텍스트는 `aik_inbox`의 비밀 값 휴리스틱(`Bearer`, PEM private key, 알려진 key 접두사)을 통과해야 저장된다. 오류 메시지에는 텍스트를 넣지 않는다.

## 저장 위치와 형식

- 전용 SQLite 파일. application ID `0x41494B4E`(`AIKN`)로 runtime(`AIKR`)·inbox(`AIKI`)·`AIKC`와 구분한다. schema version은 1이다.
- 기본 위치: Windows `%LOCALAPPDATA%\AIK\runtime\continuity.sqlite3`. 개발용 POSIX fallback은 runtime DB와 같은 디렉터리의 `continuity.sqlite3`다.
- 경로 규칙은 `aik_runtime`과 같다. 상대 경로, 저장소·Git 내부, 개발 원장과 sidecar, symlink·hardlink, Windows 장치 이름, 원격 드라이브는 거절한다(`UnsafeStoragePath`).
- 모든 작업은 `<db>.storage.lock`을 잡고 새 연결에서 `BEGIN IMMEDIATE` 트랜잭션 하나로 처리한다. `DELETE` journal, `synchronous=FULL`이다.
- 네 테이블(`directives`, `summaries`, `config_revisions`, `checkpoints`)은 UPDATE·DELETE trigger로 append-only를 강제한다.
- 열 때 schema·integrity·외래 키를 검사하고, 모든 행을 다시 decode해 fingerprint, 정정 대상의 선후·프로젝트, checkpoint revision 연속성, 참조 존재, 역행 여부를 확인한다. 위반하면 `InvalidContinuityDatabase`로 거절하고 파일을 덮어쓰지 않는다.

## 지시와 정정 (완료 기준 1)

`Directive(directive_id, project_id, kind, text, target_id=None, scope=None)`

- `kind="instruction"`: 사용자 원문 지시. target·scope가 없어야 한다.
- `kind="correction"`: 후속 정정. 같은 프로젝트의 **먼저 저장된** 지시나 정정을 `target_id`로 가리키고, `scope`로 적용 범위를 밝힌다.
  - `replace`: 대상은 더 이상 적용되지 않고 이 정정이 대신 적용된다.
  - `supplement`: 대상과 이 정정이 함께 적용된다.
  - `withdraw`: 대상은 더 이상 적용되지 않는다. 이 정정 자체는 `applied`로 표시된다.
- 이미 `replace`·`withdraw` 정정을 받은(즉 `replaced`·`withdrawn` 상태인) 지시는 다시 정정할 수 없다. scope와 관계없이 `InvalidContinuityRecord`로 거절한다. 같은 대상을 두 번 replace하여 상충하는 두 지시가 모두 `active`로 남는 것을 막기 위해서다. 계속 바꾸려면 현재 유효한 대체 정정을 대상으로 지정한다.
- 순서는 저장 순서(`seq`)다. 원문은 그대로 보존되며 정정이 원문을 지우지 않는다.
- 같은 `directive_id`와 같은 내용은 `duplicate`, 다른 내용은 `ContinuityConflict`다. 이미 저장된 정정을 재전송하면 대상 상태와 관계없이 `duplicate`다.
- `effective_directives()`·`handoff()`는 이력 전체를 순서대로 반환하고 각 항목의 `status`(`active`/`replaced`/`withdrawn`/`applied`), `superseded_by`, `supplemented_by`를 계산한다.
- **한계:** 정정을 `withdraw`해도 그 정정이 대체했던 이전 지시는 자동으로 되살아나지 않는다(fail-safe). 대체·철회된 지시의 재정정 거절은 저장 시점 검사다. 이 규칙 도입 전에 이미 저장된 행은 열기 검증에서 거절하지 않으며, 그런 이력은 이전과 같이 해석된다. 지시 의미 해석(정정 간 내용 충돌 판단)은 총괄과 사용자 확인에 맡긴다.

## 설정 revision

`ConfigRevision(config_id, revision, digest)`는 설정 내용 대신 SHA-256 hex digest만 저장한다(설정에 비밀이 섞일 수 있으므로). 같은 `config_id`의 revision은 엄격히 증가해야 한다.

- 같은 revision·같은 digest: `duplicate`
- 같은 revision·다른 digest: `ContinuityConflict`
- 마지막 값 이하의 새 revision: `RevisionRegression`

## checkpoint (완료 기준 2)

`save_checkpoint(checkpoint, expected_revision)`는 프로젝트별 revision `expected_revision + 1`을 추가한다. `0`이면 첫 checkpoint다.

- 마지막 성공 저장을 같은 내용으로 반복하면 저장된 revision을 그대로 반환한다(응답 유실 후 재시도).
- 기대 revision이 현재와 다르면 `ContinuityConflict`.
- `directive_seq`는 같은 프로젝트에 실제 저장된 지시 위치여야 하고, 이전 checkpoint보다 작아지면 `RevisionRegression`.
- `summary_id`와 `config_refs`는 저장된 값이어야 하며, 설정 revision이 이전 checkpoint보다 낮아지면 `RevisionRegression`.

문맥을 줄인 뒤에는 `handoff(project_id)`로 지시 이력, 마지막 checkpoint 참조, 참조된 설정 revision, 보조 요약을 한 snapshot으로 받는다. 미완료 task와 검증 근거의 **현재 값**은 이 패킷이 아니라 `Resumer.resume()`의 VM 재조회 결과(`open_task_ids`, `vm_evidence_refs`, `vm_task_attempts`)로 회수한다. 허용 범위는 인가 revision 비교만 수행한다. checkpoint에 `authorization_revision_id`가 기록되지 않았으면 비교하지 않으며, VM이 보고한 현재 인가 revision은 읽을 수 있을 때 `vm_authorization_revision_id`로 함께 반환한다. 인가 내용 자체는 회수하지 않는다. 패킷의 `authority_note`도 VM 재확인이 필요함을 표시한다.

## 재개 (완료 기준 3)

`Resumer(store, vm_port).resume(project_id)`:

1. `store.resume_snapshot(project_id)`로 마지막 checkpoint와 프로젝트의 최대 지시 `seq`를 한 트랜잭션에서 읽는다. checkpoint가 없으면 `no_checkpoint`(handoff `start_goal`). VM도 호출하지 않으며, Goal 시작은 호출자·사용자가 명시적으로 결정한다.
2. 최대 지시 `seq`가 checkpoint의 `directive_seq`보다 크면, 즉 checkpoint 이후 새 지시·정정이 저장되었으면 `DIRECTIVES_AFTER_CHECKPOINT`를 기록한다. 이 경우 VM 참조가 모두 일치해도 `continue`가 될 수 없다. 이 코드는 아래 `unavailable`·`unverifiable` 결과에도 함께 포함된다. 새 지시를 반영한 checkpoint를 저장하면 해소된다.
3. `vm_port.status(project_id)`가 예외를 던지면 `unavailable`(handoff `retry`). 예외 원문은 결과에 넣지 않는다.
4. payload를 읽을 수 없으면 `unverifiable`(handoff `inspect_vm`): `VM_STATUS_INVALID`, `VM_STATE_UNKNOWN`, `GOAL_REFERENCE_UNREADABLE`, `GOAL_REVISION_UNREADABLE`, `PLAN_REVISION_UNREADABLE`, `AUTHORIZATION_UNREADABLE`, `PROJECT_REFERENCE_UNREADABLE`, `EVIDENCE_UNREADABLE`.
5. 참조가 다르면 `confirmation_required`(handoff `confirm_with_user`). 새 Goal을 만들지 않고 멈춘다.
   - `DIRECTIVES_AFTER_CHECKPOINT`
   - `PROJECT_MISMATCH`, `GOAL_MISMATCH`, `GOAL_REVISION_CHANGED`, `PLAN_REVISION_CHANGED`, `AUTHORIZATION_CHANGED`, `GOAL_CANCELLED`
   - task별 `TASK_MISSING`, `ATTEMPT_CHANGED`, `EVIDENCE_MISSING`
6. 모두 일치하면 `continue`(handoff `continue_existing`). `continuation`에 기존 checkpoint revision, Goal/Plan/인가 revision ID(checkpoint 값), task/attempt ID가 들어 있다. task의 attempt는 **VM이 관측한 값**(`vm_task_attempts`)을 사용한다. 다음 checkpoint는 같은 revision 체인에 이어서 저장한다.

checkpoint 시점에 attempt가 없던 task가 이후 attempt를 얻었거나, task 상태가 진행된 것은 VM의 정상 진행으로 보고 불일치로 세지 않는다. 이때 `continuation["tasks"]`에는 새로 얻은 attempt ID가 들어가므로, 호출자가 attempt가 없다고 오인해 새 Attempt를 만들 근거가 되지 않는다. 기록된 attempt가 바뀐 경우만 `ATTEMPT_CHANGED`다.

`verify_checkpoint(stored, payload, *, latest_directive_seq=None)`는 같은 비교를 수행하는 순수 함수다. `latest_directive_seq`를 주지 않으면 지시 위치 비교는 하지 않는다.

## VM read port

- `VmReadPort.status(project_id) -> Mapping` 프로토콜. 시험은 fake를 사용한다.
- `BridgeStatusPort(bridge)`: `aik_vm_bridge`의 `VmBridge.status(ProjectQuery(...))`를 호출하고 `receipt.payload`를 반환하는 adapter. 실제 VM과는 시험하지 않았다(NOT_RUN). receipt의 성공·실패 표시는 확인하지 않는다. payload가 없으면 `VM_STATUS_INVALID`(`unverifiable`)로 끝나며, 일시 장애도 `retry`가 아닌 `inspect_vm`으로 분류될 수 있다.
- 가정한 payload 계약(NOT_RUN): `aik_controller`의 status 계약(`goal_state`, `tasks[]`, `plan_revision_id`)에 `project_id`(선택), `goal_id`, `goal_revision_id`, `authorization_revision_id`, `evidence[]`(선택)을 더한다. 인식하지 못한 값은 `unverifiable`로 처리한다. task별 상태·attempt 값의 정규화는 `aik_controller.VmView`에 따른다.

## 사용 예 (합성 값)

```python
from aik_continuity import ContinuityStore, Directive, Checkpoint, TaskRef, Resumer

store = ContinuityStore()
d1 = store.append_directive(Directive('dir-1', 'project-1', 'instruction', '합성 보고서 A를 작성하라')).record
c1 = store.append_directive(Directive('cor-1', 'project-1', 'correction', 'A 대신 B',
                                      target_id='dir-1', scope='replace')).record
store.save_checkpoint(Checkpoint('project-1', 'goal-1', 'goal-rev-2', 'plan-rev-3',
                                 tasks=(TaskRef('task-a', 'attempt-a1'),), directive_seq=c1.seq), 0)
result = Resumer(store, vm_read_port).resume('project-1')
if result.status == 'continue':
    ids = result.continuation   # 기존 Goal/Attempt로 이어간다
else:
    ...                         # result.handoff에 따라 사용자 확인·재시도
```

## 시험

```bash
python -m unittest discover -s tests -p 'test_continuity.py' -v
```

OS 임시 폴더의 합성 DB와 fake VM read port만 사용한다. 검사 항목은 다음과 같다.

- 지시·정정의 순서와 적용 범위(replace/supplement/withdraw)가 재열기 후에도 보존됨, 프로젝트 분리, 페이지 조회
- 정정 대상 규칙(없는 대상·다른 프로젝트·자기 자신·이미 대체/철회된 대상), 중복 멱등과 충돌
- raw SQL UPDATE·DELETE를 trigger가 거절
- 비밀로 보이는 지시·요약 거절, 오류·파일·handoff에 비노출
- 저장되지 않은 지시 위치를 덮는 요약 거절
- 설정 revision의 역행·충돌 거절
- checkpoint의 낙관적 revision, 재시도 멱등, 지시 위치·설정 역행 거절, 참조 존재 검사
- checkpoint가 VM 참조만 담음(상태 필드·AGS 필드 없음), 문서의 AGS 미구현 표기
- 같은 프로세스 안의 문맥 축약·재기동 모사 후 같은 Goal ID·revision으로 재개, VM에는 `status`만 호출
- 별도 Python 프로세스(`subprocess`)에서 `handoff`·`resume` 호출로 같은 Goal 재개
- checkpoint 이후 추가된 지시로 사용자 확인 필요, 다른 프로젝트 지시는 무관
- VM 진행(새 attempt, 완료)은 계속 진행하고 `continuation`에 VM의 attempt 반영
- VM 현재 인가 revision 보고
- stale 참조 9종에서 사용자 확인 필요로 정지, checkpoint 미변경
- 읽을 수 없는 payload(프로젝트·Goal 참조 포함)·port 예외의 fail-closed
- checkpoint 없음
- 손상·외부·변조(잘못된 body, revision 공백) DB 거절과 원본 보존
- Git 저장소 내부·상대 경로 거절
- `BridgeStatusPort`의 `ProjectQuery` 사용(fake bridge)

이 목록은 시험 범위이며 실행 결과 기록이 아니다. Linux 통과는 개발 검증이며 Windows 지원 증거가 아니다. 별도 프로세스 시험은 프로세스 재시작을 모사할 뿐, 실제 총괄 모델의 문맥 축약을 실행한 것이 아니다.

## 한계·미검증(NOT_RUN)

- 실제 VM status payload 형식과의 대조. AGS 원장 참조는 미구현(위 상태 절 참고).
- Windows 11의 잠금·기본 경로·강제 종료 후 hot journal 복구. hot journal은 `aik_inbox`처럼 원본 파일에서 SQLite 표준 rollback으로 복구되며, `aik_runtime`처럼 private 사본에서 먼저 검사하지 않는다. 강제 종료 시험은 작성하지 않았다.
- 비밀 값 휴리스틱의 완전성. 식별자 필드에 비밀을 넣는 오용은 막지 못한다.
- VM이 보고한 task 상태를 읽을 수 없는 경우의 처리와 attempt ID 형식 검증은 `aik_controller.VmView`의 정규화에 의존하며, 이 패키지에서 별도로 fail-closed 처리하지 않는다.
- `handoff()`·`effective_directives()`는 프로젝트의 지시 전체를 읽는다. 대규모 이력의 처리량은 검증하지 않았다. checkpoint 참조 수는 task·evidence 각 1024개, 설정 64개로 제한한다.
- 열 때 전체 행을 검증하므로 비용이 DB 크기에 비례한다.
- `aik_controller.Controller`의 미해결 effect 기억(메모리)은 이 저장소로 옮기지 않았다. Controller·Director와의 자동 연결(언제 checkpoint를 저장할지)은 호출자 책임이며 구현하지 않았다.
- 지시 의미 해석(정정 간 충돌 판단)은 하지 않는다.
