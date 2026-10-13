# AIK-021 — workspace별 연결 사용 범위와 안전한 설정 이동

상태: **구현(부분) — 실제 Windows·다른 Windows 사용자/기기 복원 NOT_RUN**. 이 문서는 `src/aik_scope/`의 동작과 한계를 설명한다. `planning/` 장부의 AIK-021 상태와 완료 판정은 바꾸지 않는다.

`aik_scope`는 다음 기존 패키지를 import해서 재사용한다. 기존 패키지는 수정하지 않는다.

- `aik_config.SettingsStore`: workspace·연결 참조·프로필·한도·권한, export/import
- `aik_connection.ConnectionStore`: 연결 상세 등록과 vault 참조 확인
- `aik_profiles.ProfileStore`: 실행 프로필 문서
- `aik_runtime.paths`: 경로 검증과 프로세스 잠금

표준 라이브러리만 사용한다.

## 1. 연결 사용 범위 정책 (완료 기준 1)

`ConnectionScope(workspace_id, allowed_connection_ids, shared_connection_ids)`는 workspace가 사용할 연결을 **명시적으로** 나열한다.

`ScopeStore`의 저장 위치와 식별자는 다음과 같다.

- 파일: settings 파일 옆의 `scopes.sqlite3`
- application ID: `0x41494B57`(`AIKW`)
- revision으로 갱신을 확인한다. `0`은 새로 만들 때만 쓴다.

`check(workspace_id, connection_id)`는 아래 순서로 판정한다. 하나라도 걸리면 `ConnectionNotInScope`를 발생시키며, `.reason`에 고정 코드가 들어간다(fail-closed).

| 순서 | 조건 | reason |
| --- | --- | --- |
| 1 | workspace에 저장된 scope가 없음 (기본값은 전부 거부) | `NO_SCOPE` |
| 2 | allowlist에 없음 | `NOT_ALLOWED` |
| 3 | 설정 이동 후 재연결 대기 중 | `RECONNECTION_REQUIRED` |
| 4 | settings에 연결이 없음 | `UNKNOWN_CONNECTION` |
| 5 | 다른 workspace 소유인데 소유 workspace가 `shared_connection_ids`로 공유하지 않음 | `NOT_SHARED` |

**공유 규칙.** 여러 프로젝트가 연결 하나를 공유하려면 두 조건이 모두 필요하다.

- 사용하는 workspace가 그 연결을 allowlist에 둔다.
- 소유 workspace가 그 연결을 shared 목록에 둔다.

소유자는 settings `ConnectionProfile.workspace_id`로 정한다. 따라서 남의 연결을 자기 shared 목록에 넣어도 효과가 없다.

**보조 API.**

- `require(workspace_id, connection_ids)`: broker grant 발급 전 등의 사전 검사에 쓴다.
- `violations(workspace_id, profile_store)`: allowlist 밖의 연결을 가리키는 저장된 `AgentProfile`과 실행 프로필(primary·fallback 전부)을 보고한다.

**guard가 하지 않는 일.** 이 guard는 사용 범위만 판정한다. 다음 검사는 각 소유 패키지가 실행 직전에 따로 수행해야 한다.

- 인증 확인: `aik_connection.verify_auth_context`
- workspace 결속: AIK-084
- credential 주입: `aik_broker`

`aik_config`의 `AgentProfile`은 DB 제약상 같은 workspace의 연결만 가리킬 수 있다. 다른 workspace 연결의 공유 판정은 실행 경로(broker·orchestrator)가 이 guard를 호출할 때 적용된다. 현재 broker·orchestrator가 이 guard를 자동으로 호출하도록 연결되어 있지는 않다.

## 2. 설정 export (완료 기준 2)

`SettingsTransfer(settings, connections, scopes, profiles=None).export(workspace_ids=None)`가 만드는 문서는 다음과 같다.

| 구성 | 내용 |
| --- | --- |
| `settings` | `SettingsStore.export_settings`의 결과. 비밀·결속·선택·이력은 제외된다 |
| `connections` | `ConnectionSpec` 필드만: provider, label, auth/execution kind, 공식 호스트·프로필, 비민감 `account_binding`, opaque `credential_ref` |
| `scopes` | workspace별 allowlist·shared 목록 |
| `execution_profiles` | profile store를 준 경우에만 포함 |

다음 항목은 포함하지 않는다(`excluded` 목록에 명시).

- API 키, 공식 호스트 토큰, 인증 세션
- `verified_at`·`binding_revision`·`provenance_id`
- canonical root·VM project 결속, 현재 선택, revision 이력
- runtime 상태, Goal·근거, execution record, VM/AGS 원장

`credential_ref`(`secret://aik-vault/<32 hex>`)는 비밀 값이 아니라 그 사용자 vault 안의 위치를 가리키는 무작위 참조다. 다른 사용자·기기에서는 해석되지 않는다.

export가 거절되는 경우(`TransferBlocked`):

- 재연결 대기 레코드가 남아 있음
- settings 연결과 연결 상세가 서로 다름 (예: 상세 없는 settings 연결)

profile store 없이 export하면 실행 프로필은 빠진다. 이 경우 문서의 `execution_profiles`는 빈 목록이다.

## 3. import와 재인증 필요 상태 (완료 기준 3)

`import_document(document)`는 쓰기 전에 다음을 먼저 확인한다.

1. 최상위 필드 집합과 형식·버전을 확인한다. `contains_secret_values=false`, `restores_goals_and_evidence=false`가 아니면 거절한다.
2. settings 부분은 `SettingsStore.import_settings`를 transaction 안에서 실행한 뒤 rollback하는 dry-run으로 검증한다. 이 단계에서 정확한 필드·참조와 기존 ID 충돌을 확인한다.
3. 연결 상세와 settings 연결의 일치를 확인한다. scope·실행 프로필의 workspace 소속과 연결 참조도 확인한다.
4. 대상 저장소의 ID와 credential 참조가 충돌하지 않는지 확인한다.

검증에 실패하면 결과는 다음과 같다. 어느 경우든 아무것도 쓰지 않는다.

- 알 수 없는 필드(예: `api_key`, `token`): `InvalidTransferDocument`. 오류 메시지에 입력 값을 넣지 않는다.
- 기존 ID·참조와 충돌: `RevisionConflict`

복원은 다음 순서로 진행한다.

1. **workspace**·한도·권한을 settings import로 만든다. 항상 **미결속**이다.
2. **연결**을 `ConnectionStore.register`로 등록한다. 등록된 연결은 항상 미확인(`verified_at=None`)이고 settings `needs_authentication=True`다.
3. API 연결의 vault 참조를 이 사용자·기기에서 해석할 수 없으면(`CredentialRefUnavailable`) 연결을 등록하지 않는다. 대신 비민감 필드만 `pending_restores`에 남긴다. 이 상태가 **재연결(재인증) 필요**다. 그 연결을 쓰는 `AgentProfile`·실행 프로필도 대기 레코드로 남는다.
4. scope를 복원한다. 대기 중인 연결은 guard가 `RECONNECTION_REQUIRED`로 거부한다.

`TransferReport`에는 다음이 들어간다.

| 필드 | 의미 |
| --- | --- |
| `workspaces_requiring_binding` | 재결속이 필요한 workspace |
| `connections_requiring_authentication` | 인증이 필요한 모든 연결 |
| `connections_requiring_reconnection` | 그중 참조를 해석하지 못한 연결 |
| `pending_profile_ids` | 대기 중인 프로필 |
| `required_actions` | 위 항목에 대한 조치 목록 |

## 4. 재연결·재결속과 복원 범위 (완료 기준 4)

**재연결.** 새 기기에서 사용자가 API 키를 vault에 다시 넣은 뒤 `reconnect(connection_id, new_credential_ref)`를 호출한다.

- 새 참조로 연결을 등록하고, 대기 중이던 프로필을 복원한다.
- 해석되지 않는 참조를 넘기면 `CredentialRefUnavailable`로 실패하며 대기 상태가 유지된다.
- 재연결 후에도 연결은 **미확인**이다. 공식 호스트 어댑터의 `mark_verified`가 필요하다.
- 포기하려면 `discard_pending(connection_id)`를 호출한다. 대기 연결과 그 연결에 의존하는 대기 프로필을 제거한다.

**재결속.** import한 workspace는 canonical root·VM project 결속이 없으므로 `resolve_execution_target()`이 `WorkspaceBindingRequired`로 실패한다. 재결속은 AIK-084가 수행한다.

**복원 범위.** `TransferReport.execution_ready`는 항상 `False`이고 `goals_and_evidence_restored`도 항상 `False`다. 이 기능은 완료 Goal·근거·runtime 상태·VM/AGS 원장을 복구했다고 표시하지 않는다.

- 전체 상태 복원은 **AIK-058** manifest와 소유자별 절차를 사용한다.
- GUI 진입점은 **AIK-088**에서 제공한다.

## 5. 원자성과 한계

**여러 파일 사이의 원자성.** import는 settings·연결·프로필·scope 파일에 차례로 쓴다. 따라서 파일 전체를 하나의 transaction으로 묶지 못한다.

- 사전 검증 뒤에도 I/O 실패가 나면 `TransferIncomplete(stage, completed)`를 발생시킨다.
- 이때 남는 레코드는 미결속·미확인이며, 해당 workspace scope가 없으면 guard가 거부한다(fail-closed).
- 자동 복구는 하지 않는다.

**export 시점 일관성.** export도 여러 파일을 따로 읽는다. 동시에 변경이 일어나면 하나의 snapshot이 아닐 수 있다.

**`account_binding`.** 식별자 규칙상 비민감 ID만 허용되며 export에 포함된다. 다른 기기에서는 `rebind`로 바꿀 수 있다.

**host session.** 세션 토큰은 공식 호스트가 소유하며 이 패키지에 전달되지 않는다. 새 기기에서는 항상 인증 필요 상태로 복원된다.

## 6. 검증

```powershell
python -m unittest tests.test_scope -v
```

시험은 OS 임시 폴더, 합성 ID, 시험 전용 `FakeVault`(참조 확인용 checker)만 사용한다. 검사 항목은 다음과 같다.

- 범위 허용·거부, reason별 판정
- 공유 규칙
- 프로필 위반 보고
- scope 검증과 revision
- export의 평문 키·토큰·검증 시각·결속 비포함
- 다른 사용자·기기 시뮬레이션: 별도 디렉터리와 빈 vault
  - 재연결 필요 상태, 미결속 workspace, 재연결 후 프로필 복원
- 같은 사용자 복원
- 잘못된 문서와 충돌의 무기록 거절
- 부분 실패 보고
- 대기 레코드 포기

| 항목 | 상태 |
| --- | --- |
| Linux 단위 시험 | 개발 검증. 이 문서 작성 시점에 실행 결과 기록 없음 |
| 실제 다른 Windows 사용자/기기에서 DPAPI vault 참조가 해석되지 않는 복원 | **NOT_RUN** (빈 fake vault로 모사) |
| Windows 기본 경로·잠금 | **NOT_RUN** |
| 실제 `aik_vault`·공식 호스트 어댑터·broker/orchestrator와 guard의 연결 | **NOT_RUN** / 범위 밖 |
| GUI 진입점(AIK-088), 전체 상태 복원(AIK-058), workspace 결속(AIK-084) | 범위 밖 |

Linux 통과는 Windows 지원 증거가 아니다. 이 구현만으로 AIK-021 완료를 주장하지 않는다.
