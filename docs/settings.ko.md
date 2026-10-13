# AIK 설정 저장소 — AIK-007

`src/aik_config/`는 workspace별 설정 schema와 UI가 사용할 typed settings API를 제공한다. Python 표준 라이브러리만 사용하며, 경로 검증·프로세스 잠금은 `aik_runtime.paths`를 재사용한다. 저장 파일은 runtime 메타데이터 DB(AIK-005)와 **별도 파일**이며 application ID도 다르다(`0x41494B43`, `AIKC`; runtime은 `AIKR`). 기존 `aik_runtime` 코드는 수정하지 않았다.

## 저장하는 설정과 구분

| 모델 / 테이블 | 내용 | 키 |
| --- | --- | --- |
| `Workspace` / `workspaces` | workspace ID·표시 이름·AIK-084 결속(`canonical_root`, `vm_project_id`) | `id` |
| `ConnectionProfile` / `connections` | 연결 참조 검증용 최소 모델: workspace·provider·`auth_mode`·`needs_authentication` | `id` |
| `AgentProfile` / `agent_profiles` | 총괄(`director`)·worker 프로필: connection·model·effort | `id` |
| `WorkspaceLimits` / `workspace_limits` | `max_parallel_workers`, `budget_limit`, `budget_unit`, `budget_period` | `workspace_id` |
| `PermissionDefaults` / `permission_defaults` | `file_write`·`shell_command`·`network` 기본 결정(`deny`/`ask`/`allow`) | `workspace_id` |
| (선택 상태) / `active_workspace` | 현재 선택된 workspace ID 하나 | 고정 slot |

모든 모델은 필드가 고정된 frozen dataclass다. API key·token·비밀번호를 담는 필드는 없으며 추가 필드는 `TypeError`, 지원하지 않는 객체·하위 클래스·`object.__setattr__`로 변조한 객체는 `InvalidSetting`으로 거절한다. ID·provider 값도 실제 비밀이 아닌 식별자여야 한다.

연결의 상세 CRUD·credential 결속은 AIK-010 범위다. 여기서는 프로필이 같은 workspace의 연결을 가리키는지 검증하는 최소 필드만 둔다. 동시성·예산은 값 검증(0·음수·비정수·bool·문자열 거부)만 하며 admission·예산 집행은 구현하지 않는다. 권한 기본값도 저장값일 뿐 실행 권한을 부여하지 않는다. 기본값은 `file_write=ask`, `shell_command=ask`, `network=deny`이다.

## 사용법

```python
from aik_config import (SettingsStore, Workspace, ConnectionProfile, AgentProfile,
                        WorkspaceLimits, PermissionDefaults, RevisionConflict)

store = SettingsStore()  # 기본 위치, 또는 SettingsStore(절대경로)
with store.transaction() as tx:
    tx.save(Workspace('ws-example', '예시 작업공간'))
    tx.save(ConnectionProfile('conn-example', 'ws-example', 'synthetic', auth_mode='api_key'))
    tx.save(AgentProfile('director-example', 'ws-example', 'director', 'conn-example', 'synthetic-model'))
    tx.save(AgentProfile('worker-example', 'ws-example', 'worker', 'conn-example', 'synthetic-small'))
    tx.save(WorkspaceLimits('ws-example', max_parallel_workers=2, budget_limit=1000))

view = store.workspace_settings('ws-example')   # UI용 묶음 조회
perm = view.permissions                          # 저장 전이면 revision 0인 기본값
store.save(PermissionDefaults('ws-example', file_write='allow'), expected_revision=perm.revision)
```

- `save(record, expected_revision=0)`: 0은 새 키 생성, 갱신은 마지막으로 읽은 양의 revision이 필요하다. 불일치 시 `RevisionConflict`.
- `get(Model, key)` / `list(Model, workspace_id=None, limit=100, after_key=None)` / `delete(Model, key, expected_revision)`.
- `workspace_settings(id)`는 workspace·연결·총괄/worker 프로필·한도·권한을 `Revisioned(value, revision)`으로 반환한다. 저장되지 않은 한도·권한은 revision 0의 기본값이다.
- `select_workspace(id, expected_revision)`, `active_workspace()`, `resolve_execution_target()`.
- `export_settings(workspace_ids=None)`, `import_settings(document)`.
- 여러 변경은 `transaction()` 하나로 묶으면 전부 commit되거나 전부 rollback된다. 각 `save`/`delete`/import는 savepoint로 원자적이다.

## 검증 단계에서 차단하는 설정

- 존재하지 않는 workspace를 가리키는 연결·프로필·한도·권한 → `InvalidReference`.
- 존재하지 않는 연결을 쓰는 프로필 → `InvalidReference`, **다른 workspace의 연결**을 쓰는 프로필 → `CrossWorkspaceReference`. DB도 `(connection_id, workspace_id)` composite foreign key로 같은 조합을 거절한다.
- 갱신 시 레코드의 `workspace_id` 변경(소유 workspace 이동) → `InvalidSetting`.
- 참조 중인 workspace·연결, 선택된 workspace의 삭제 → `InvalidReference`.
- 모델 값 오류(알 수 없는 role·auth_mode·권한값, 인증 없는 연결의 인증 필요 표시, 상대·traversal·네트워크 root 등) → `InvalidSetting`.

오류 메시지는 분류만 담고 입력 값은 포함하지 않는다.

## workspace identity와 실행 root (AIK-084 인터페이스)

AIK-084(workspace 수명주기·VM project 결속)는 미구현이다. 이 패키지는 다음 인터페이스와 차단 검증만 제공한다.

- `Workspace.canonical_root`와 `vm_project_id`는 함께 있거나 함께 없다. 한 번 결속된 뒤에는 이 API로 변경할 수 없다(재결속은 AIK-084 소관). root 문자열은 문법만 검사하며 정규화·존재 확인은 AIK-084가 한다. 비교는 정확한 문자열 일치다.
- `WorkspaceBindingAuthority.binding_for(workspace_id) -> WorkspaceBinding | None`을 AIK-084가 구현해 `SettingsStore(binding_authority=...)`로 전달한다.
- **실행 root를 별도로 저장하지 않는다.** `resolve_execution_target()`은 현재 선택된 workspace에서만 root와 VM project를 도출한다. 따라서 GUI 선택이 바뀌면 이전 root는 더 이상 반환되지 않는다. 선택된 workspace가 미결속이거나, authority가 없거나, authority가 응답하지 못하거나(예외), 결과가 다르면 `WorkspaceBindingRequired`/`WorkspaceBindingMismatch`로 실패한다(fail-closed).
- authority가 있으면 결속 workspace를 선택하거나 새로 결속할 때도 일치를 확인하고, 불일치면 선택·저장을 거절한다.

## 설정 export와 전체 제품 백업의 구분

| 항목 | 설정 export (이 패키지) | 전체 제품 상태 백업 (AIK-058 등 별도) |
| --- | --- | --- |
| 포함 범위 | workspace ID·이름, 연결 provider·인증 방식, 프로필, 한도, 권한 기본값 | runtime DB, VM/AGS 원장, 사용자 artifact 등 제품 상태 |
| 제외 | 비밀 값·credential·인증 세션·canonical root/VM project 결속·현재 선택·revision 이력·runtime 상태 (`excluded` 목록에 명시) | 해당 범위에서 정의 |
| 재인증 | 인증이 필요한 연결은 `requires_authentication=true`로 내보내고, import 후 `needs_authentication=True`로 저장·`ImportReport.connections_requiring_authentication`으로 표시 | 별도 정의 |
| 재결속 | import한 workspace는 미결속 상태이며 `workspaces_requiring_binding`으로 표시. AIK-084 결속 전에는 실행 대상이 되지 않음 | 별도 정의 |
| 복구 가능 이력 | 없음. 모든 레코드를 revision 1로 새로 생성 | 별도 정의 |

import는 문서 전체(정확한 필드 집합, 형식·버전, `contains_secret_values=false`, 문서 내 ID 중복, 같은 workspace 안의 연결 참조, 모든 값)를 먼저 검증한 뒤 하나의 savepoint에서 생성한다. 알 수 없는 필드(예: 비밀 값 필드)가 있으면 `InvalidSettingsDocument`, 기존 ID와 충돌하면 `RevisionConflict`로 전체가 원복된다.

## 파일 위치·동시성·무결성

- Windows 기본 위치: `%LOCALAPPDATA%\AIK\config\settings.sqlite3`. Linux의 `$XDG_STATE_HOME/aik/settings.sqlite3`(또는 `~/.local/state/aik/settings.sqlite3`)는 개발·시험용 fallback이며 Linux 제품 지원을 뜻하지 않는다.
- 경로 규칙은 `aik_runtime.paths.validate_database_path`와 같다. 저장소 내부·Git 메타데이터·개발 원장 이름·symlink·hardlink·장치 이름·상대 경로를 거절한다.
- 협력 프로세스는 `settings.sqlite3.storage.lock`의 OS 잠금으로 직렬화되며, 기한 초과 시 `StorageBusy`로 쓰지 않고 끝난다.
- SQLite `DELETE` journal·`FULL` 동기화·외래 키를 쓴다. 기존 파일은 SQLite로 열기 전에 header의 application ID를 확인하므로 runtime DB 등 다른 파일은 열거나 바꾸지 않고 `InvalidDatabase`로 거절한다. 시작 시 schema·무결성·참조·레코드 계약을 전수 검사하고, 각 transaction은 schema 일치를 다시 확인한다. 파일이 사라지면 빈 DB로 몰래 재생성하지 않는다.
- schema version은 1이며 migration 경로는 아직 없다. 알 수 없는 버전은 거절한다.

## 검증과 원장

```powershell
python -m unittest tests.test_config -v
```

시험은 OS 임시 폴더의 합성 데이터만 사용하며 재시작 유지, 잘못된 값·참조, 다른 workspace 참조, revision 충돌, rollback, 잠금 대기 시간, export/import의 비밀 제외·재인증 표시·원자성, 결속 authority 일치/불일치, runtime DB와의 분리, 경로 거절, 손상·외부 파일 보존을 검사한다.

- Linux 통과는 개발 검증이며 Windows 지원 증거가 아니다. Windows 기본 경로(`%LOCALAPPDATA%\AIK\config`)와 Windows 잠금 동작은 **미검증(NOT_RUN)**이다.
- AIK-084 실제 구현과의 연동, AIK-010 연결 CRUD, GUI 연결, admission은 이 태스크의 범위 밖이다.
- `planning/` 장부의 상태와 완료 판정은 변경하지 않았다.
