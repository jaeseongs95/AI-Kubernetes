# 프로젝트 폴더 선택·생성·전환 GUI (AIK-087)

상태: **구현(부분)**. 이 문서는 AIK-087(프로젝트 폴더 선택·생성·전환 GUI)을 다룬다.

현재 코드가 있는 부분은 다음과 같다.

- 엔진 쪽 workspace 서비스(`src/aik_desktop_engine/workspaces.py`)와 엔진 명령(`workspaces.*`)
- IPC 경계(`src/aik_ipc/workspaces.cjs`)
- main이 띄우는 폴더 대화상자(`apps/desktop/electron/folder-picker.cjs`)
- 설정 화면의 프로젝트 폴더 패널(`apps/desktop/src/workspaces.tsx`, §8)

VM 고정 계약에는 공개 project 생성 command가 없다. 그래서 등록(열기·생성)은 막혀 있다(§7). 흐름은 시험에서 fake VM port로 확인한다.

다음 항목은 NOT_RUN이다(§10).

- 실제 VM project 생성·조회
- 실제 native 대화상자 조작
- Windows 실행
- 한글·공백 경로와 키보드 조작을 포함한 설치본 E2E
- native/API 작업자의 실제 cwd 일치
- Goal 입력·인가 revision 결속

이 문서는 `planning/` 장부의 상태와 완료 판정을 바꾸지 않는다.

## 1. 목적과 수용 기준 대응

목적은 두 가지다.

- 사용자가 GUI에서 기존 폴더를 프로젝트로 열고, 새 폴더를 만들고, 프로젝트 사이를 전환할 수 있게 한다.
- 이 과정에서 앱 renderer가 폴더 경로를 보내지 않게 한다. 경로는 main의 native 대화상자에서만 나온다.

수용 기준은 장부 원문이다. "이 구현" 열은 코드·시험 근거이며 원장 판정이 아니다.

| 기준 | 이 구현 | 남은 것 |
| --- | --- | --- |
| 1. GUI에서 기존 프로젝트 폴더 선택·새 폴더 생성·전환을 제공하고 현재 workspace/root·연결 사용 범위를 표시한다. CLI 입력이나 번들 샘플 프로젝트만으로 제공하지 않는다. | `workspaces.request_folder`(mode `open`·`create`·`rebind`)로 main이 native 대화상자를 연다. 전환은 `workspaces.select`로 한다. 현재 선택과 실행 폴더는 `workspaces.list`의 `active`(`workspace_id`, `status`, `cwd`)로, 각 프로젝트의 root는 목록 DTO의 `root`로 받는다. 연결 사용 범위는 `workspaces.scope`로 읽는다(읽기 전용) | VM project port가 없어 실제 등록은 `VM_NOT_CONFIGURED`로 막힌다(§7). 사용 범위 편집은 AIK-088 |
| 2. 선택 결과를 084 backend와 VM project/Goal 입력에 결속하고 실제 native/API cwd·생성 파일 위치와 일치함을 검증한다. 다른 workspace 권한을 묵시 공유하거나 실행 중 Goal의 입력/인가 revision을 우회하지 않는다. | 등록·선택·재결속·보관·복원은 모두 AIK-084 `WorkspaceManager`를 거친다. 설정은 `binding_authority=manager`로 연다. `cwd`는 `ready`일 때만 `current_execution()`에서 나온다. 사용 범위는 연결마다 `ScopeStore.check` 결과를 보여 준다. scope 파일이 없으면 모든 연결이 `NO_SCOPE`(허용 안 됨)다 | 실제 VM project 결속(생성 seam 없음). Goal 입력 결속. native/API 작업자의 실제 cwd·생성 파일 위치 대조(AIK-044). 진행 중 Goal의 입력·인가 revision 연결(AIK-084 미구현 항목) |
| 3. 재시작 뒤 선택을 복원하고 이동/삭제/접근 불가·지원하지 않는 host root는 이유와 재선택 경로를 제공한다. 한글/공백 경로와 키보드 조작을 설치본 E2E에 포함한다. | 선택은 설정의 `active_workspace`에 남는다. 재시작 뒤 `list`가 `WorkspaceManager.status`로 다시 검증한다. 각 상태에 `required_action`이 붙는다(§6.1). 옮긴 폴더는 `request_folder`의 `rebind` 모드로 다시 연결한다. 단위 시험은 한글·공백 경로(`사용자 폴더`, `기존 프로젝트 A` 등)를 쓴다 | 설치본 E2E(한글·공백 경로, 키보드 조작)는 AIK-057·064. `rebind_vm_project` 조치를 수행하는 명령이 없다(§10) |

## 2. 구성

| 위치 | 역할 |
| --- | --- |
| `src/aik_desktop_engine/workspaces.py` | `WorkspaceService`. AIK-084 `WorkspaceManager`, AIK-007 설정 선택, AIK-021 연결 scope 저장소 위에 `workspaces.*` 엔진 명령을 둔다. 자체 수명주기 규칙은 없다. 모든 실패는 고정 코드를 가진 `WorkspaceCommandError`이며 경로를 담지 않는다 |
| `src/aik_desktop_engine/server.py` | 엔진. `WORKSPACE_COMMANDS`(8개)의 payload 키를 검사하고 주입된 `WorkspaceService`로 보낸다. 서비스가 없으면 `WORKSPACES_NOT_CONFIGURED`로 답한다 |
| `src/aik_ipc/workspaces.cjs` | renderer IPC. renderer 명령 6개의 payload를 필드별로 검사하고, 결과를 동결 DTO로 복사한다. 고정 코드와 그 문구(`WORKSPACE_CODES`)를 둔다 |
| `apps/desktop/src/workspaces.tsx`, `workspaceText.ts` | 설정 화면의 프로젝트 폴더 패널과 한국어 상태·조치·오류 문구(§8) |
| `apps/desktop/electron/folder-picker.cjs` | `FolderPicker`. main이 띄우는 native 폴더 대화상자다. 고른 경로를 엔진 명령 `workspaces.open`·`create`·`rebind_root`로 바로 보낸다 |

데이터 흐름은 두 갈래다.

- 경로가 없는 명령(`list`, `scope`, `select`, `archive`, `restore`): renderer → IPC 검사 → main → 엔진 → `WorkspaceService` → `WorkspaceManager`.
- 경로가 생기는 명령(`request_folder`): renderer → IPC 검사 → main `FolderPicker` → native 대화상자 → main → 엔진 `workspaces.open`·`create`·`rebind_root` → `WorkspaceService` → `WorkspaceManager`.

### 2.1 WorkspaceService

- 생성 인자는 다음과 같다. 엔진에는 `Engine(nonce, workspaces=service)`로 주입한다.
  - `settings_path`: 설정 DB 경로
  - `registry_path`: workspace registry(`workspaces.json`) 경로
  - `projects`: VM project port. 없으면 `UnconfiguredProjects`가 대신 들어가며, 그 `create_project`·`lookup_project`는 항상 `VmProjectUnavailable`을 낸다.
  - `host_check`: `WorkspaceManager`에 넘기는 host 검사
  - `new_id`: workspace ID 생성기. 기본값은 `ws-` + 16 hex다.
- 엔진 프로세스 하나에 인스턴스 하나다.
- `list` 결과의 `vm`은 `projects`가 주입됐으면 `CONFIGURED`, 아니면 `NOT_CONFIGURED`다.
- manager와 설정은 필요할 때 연다.
  - 쓰기 명령은 manager를 만든다. 읽기 명령은 registry 파일이 있을 때만 manager를 만든다.
  - 설정은 쓰기 때, 또는 설정 파일이 이미 있을 때만 붙인다.
  - 설정은 `SettingsStore(settings_path, binding_authority=manager)`로 열고 `manager.connect_settings(settings)`로 연결한다.
- 입력 검사는 다음과 같다.
  - `name`: `Workspace` 이름 규칙에 맞지 않으면 `INVALID_NAME`
  - `folder_name`: `check_new_folder_name`을 통과하지 못하면 `INVALID_FOLDER_NAME`
  - `workspace_id`·`root`·`parent`: 비어 있지 않은 문자열이 아니면 `INVALID_INPUT`
  - `expected_revision`: 1 이상의 정수가 아니면 `INVALID_INPUT`
- DTO의 `status`는 항상 `WorkspaceManager.status`에서 온다. `required_action`은 그 상태의 `aik_workspace.REQUIRED_ACTION` 값이다.

### 2.2 폴더 대화상자

- main이 `dialog.showOpenDialog`로 앱 창의 modal 대화상자를 띄운다.
- 옵션 `properties`는 `["openDirectory", "dontAddToRecent"]`다.
- 한 번에 하나만 연다. 열려 있는 동안 들어온 요청은 `FOLDER_PICKER_BUSY`다. 대화상자가 실패해도 열림 표시는 풀린다.
- 모드별 제목과 버튼은 다음과 같다.

| mode | 제목 | 버튼 |
| --- | --- | --- |
| `open` | 프로젝트 폴더 선택 | 이 폴더 사용 |
| `create` | 새 프로젝트 폴더를 만들 위치 선택 | 여기에 만들기 |
| `rebind` | 옮긴 프로젝트 폴더 다시 선택 | 이 폴더로 다시 연결 |

`create`에서 고르는 폴더는 새 폴더를 만들 부모다. 새 폴더 이름은 renderer가 `folder_name`으로 미리 보낸다.

### 2.3 IPC 경계

- renderer 명령의 payload는 `readWorkspacePayload`가 검사한다. 키는 정확히 일치해야 한다. 값은 필드별 형식 검사와 모드별 조합 규칙(§4.1)을 통과해야 한다. 통과한 payload만 동결 사본으로 넘긴다.
- renderer payload에는 `root`·`path`·`parent`·`cwd` 같은 경로 필드가 없다. 이를 시험으로 확인한다. `request_folder`에 `root`를 덧붙이면 키가 맞지 않아 거절된다.
- 결과는 `WORKSPACE_SNAPSHOTS`의 명령별 함수가 동결 DTO로 복사한다. 형식이나 서로 간 일관성(§4.3)이 맞지 않는 결과는 renderer에 `INVALID_RESPONSE`로 간다.
- IPC 요청의 `workspace_id`는 라우팅 scope `local`로 둔다. 선택한 프로젝트 폴더는 엔진 데이터(`workspaces.select`)다. IPC 라우팅 scope가 아니다. 연결(`connections.*`)도 `local` scope에 그대로 남는다.

## 3. 경로가 지나는 길

폴더 경로는 다음 순서로만 지나간다.

1. renderer가 `workspaces.request_folder`를 보낸다. payload는 `mode`, `name`, `folder_name`, `workspace_id`, `expected_revision`이다. 경로 필드는 없다.
2. main이 payload를 검사한 뒤 `FolderPicker.pick`을 호출한다.
   - 이미 대화상자가 열려 있으면 `FOLDER_PICKER_BUSY`다.
   - 알 수 없는 mode면 `FOLDER_PICKER_UNAVAILABLE`이다.
3. main이 native 대화상자를 연다. 사용자는 대화상자에서 폴더를 고른다.
4. main이 결과를 확인한다.
   - 다음 경우에는 `{outcome: "cancelled", workspace: null}`을 돌려준다. 엔진에는 아무것도 보내지 않는다.
     - 사용자가 취소했다.
     - 경로 목록이 비었다.
     - 결과가 없다.
     - 그 사이 요청 기한이 지났다(`signal.aborted`).
   - 대화상자를 열지 못했으면 `FOLDER_PICKER_UNAVAILABLE`이다.
   - 경로가 정확히 하나가 아니거나, 문자열이 아니거나, 절대 경로가 아니어도 `FOLDER_PICKER_UNAVAILABLE`이다.
5. main이 모드에 맞는 엔진 명령을 만들어 보낸다.
   - `open` → `workspaces.open` `{name, root: 고른 폴더}`
   - `create` → `workspaces.create` `{name, parent: 고른 폴더, folder_name}`
   - `rebind` → `workspaces.rebind_root` `{workspace_id, root: 고른 폴더, expected_revision}`
6. 엔진이 payload 키를 검사하고 `WorkspaceService`를 호출한다. `create`는 `Path(parent) / folder_name`을 새 폴더 경로로 쓴다.
7. 성공하면 main이 `{outcome: "saved", workspace}`를 renderer에 돌려준다. 엔진 실패는 `FolderPicker`가 바꾸지 않고 그대로 던진다.

renderer는 결과 DTO의 `root`와 `cwd`로 경로를 받는다. 이는 표시용이다. renderer가 경로를 엔진에 보내는 길은 없다. 실패 응답에는 경로가 들어가지 않는다. 시험은 `workspaces.open` 실패 응답에 폴더 경로가 없는지 확인한다.

## 4. 명령표

### 4.1 renderer 명령

| renderer 명령 | 구분 | payload | 처리 | 결과 |
| --- | --- | --- | --- | --- |
| `workspaces.list` | 읽기 | 없음 | 엔진 같은 이름 | 목록 DTO |
| `workspaces.scope` | 읽기 | `workspace_id` | 엔진 같은 이름 | 사용 범위 DTO |
| `workspaces.select` | 쓰기 | `workspace_id` | 엔진 같은 이름 | 선택 DTO |
| `workspaces.archive` | 쓰기 | `workspace_id`, `expected_revision` | 엔진 같은 이름 | workspace DTO |
| `workspaces.restore` | 쓰기 | `workspace_id`, `expected_revision` | 엔진 같은 이름 | workspace DTO |
| `workspaces.request_folder` | 쓰기 | `mode`, `name`, `folder_name`, `workspace_id`, `expected_revision` | main이 폴더 대화상자를 연다. 엔진 명령이 아니다 | `outcome`(`saved`·`cancelled`), `workspace` |

`request_folder`의 조합 규칙은 다음과 같다. 나머지 필드는 `null`이어야 한다.

- `open`: `name`만 준다.
- `create`: `name`과 `folder_name`을 준다.
- `rebind`: `workspace_id`와 `expected_revision`을 준다.

renderer 쪽 `folder_name` 검사(`isFolderName`)는 다음을 요구한다.

- 1~255자
- `.`·`..`이 아니다.
- 끝이 공백이나 점이 아니다.
- `<>:"/\|?*`와 제어 문자가 없다.

### 4.2 엔진 명령

엔진은 명령마다 payload 키가 정확히 맞는지 검사한다. 키가 남거나 모자라면 `INVALID_REQUEST`다.

| 엔진 명령 | 보내는 곳 | payload | 결과 |
| --- | --- | --- | --- |
| `workspaces.list` | renderer 경유 | 없음 | 목록 DTO |
| `workspaces.scope` | renderer 경유 | `workspace_id` | 사용 범위 DTO |
| `workspaces.open` | main `FolderPicker`만 보낸다 | `name`, `root` | workspace DTO |
| `workspaces.create` | main `FolderPicker`만 보낸다 | `name`, `parent`, `folder_name` | workspace DTO |
| `workspaces.select` | renderer 경유 | `workspace_id` | 선택 DTO |
| `workspaces.rebind_root` | main `FolderPicker`만 보낸다 | `workspace_id`, `root`, `expected_revision` | workspace DTO |
| `workspaces.archive` | renderer 경유 | `workspace_id`, `expected_revision` | workspace DTO |
| `workspaces.restore` | renderer 경유 | `workspace_id`, `expected_revision` | workspace DTO |

`workspaces.open`·`create`·`rebind_root`는 renderer 명령 집합에 없다. 시험은 renderer의 `workspaces.*` 명령이 `PAYLOADS`의 6개와 같고 `open`·`create`·`rebind_root`를 포함하지 않는지 확인한다.

각 명령의 동작은 다음과 같다.

- `open`: 기존 폴더를 결속한다. 폴더 내용은 바꾸지 않는다.
- `create`: 기존 `parent` 아래에 `folder_name` 폴더를 만들고 결속한다.
- `select`: 등록되지 않은 ID는 `UNKNOWN_WORKSPACE`다. 그 밖에는 `WorkspaceManager.select`가 root·VM project를 검증한 뒤 선택한다. 검증에 실패하면 `WORKSPACE_<STATUS>`로 답하며, AIK-084 규칙에 따라 선택은 바뀌지 않는다.
- `rebind_root`: 옮겼거나 바뀌었거나 다시 고른 폴더를 같은 workspace에 다시 결속한다.
- `archive`·`restore`: AIK-084 `archive_workspace`·`restore_workspace`를 호출한다.

`expected_revision`은 workspace 기록의 `revision`이다. 선택 DTO의 `selection_revision`은 설정 선택의 revision이며 이 값과 다르다. `select`는 revision을 받지 않는다.

### 4.3 결과 DTO

**workspace DTO**의 필드는 `workspace_id`, `name`, `root`, `state`, `status`, `required_action`, `revision`, `active`다.

- `state`는 `active`·`archived` 중 하나다.
- `state`가 `archived`인 것과 `status`가 `archived`인 것은 항상 함께 성립해야 한다.
- `root`는 1~4096자이고 제어 문자가 없어야 한다.
- `active`는 이 workspace가 현재 선택인지를 나타낸다.

**선택 DTO**(`active`)의 필드는 `workspace_id`, `registered`, `status`, `required_action`, `cwd`, `selection_revision`이다.

- `cwd`는 `registered`가 `true`이고 `status`가 `ready`일 때만 값이 있다. 그 밖에는 `null`이다.
- 등록되지 않은 선택은 다음 값을 가진다. 예를 들어 연결 서비스가 만든 설정 전용 workspace `local`이 선택된 경우다.
  - `registered: false`
  - `status: null`
  - `required_action: "reselect_folder"`
  - `cwd: null`
- 등록된 선택이 `ready`여도 `current_execution()`이 실패하면 `status`는 `null`, `required_action`은 `reselect_folder`가 된다.

**목록 DTO**의 필드는 `vm`, `registry`, `active`, `workspaces`다.

- `vm`은 `CONFIGURED`·`NOT_CONFIGURED`, `registry`는 `ABSENT`·`READY` 중 하나다.
- `active`는 선택 DTO 또는 `null`이다.
- `workspaces`는 workspace DTO 배열이며 IPC 상한은 256개다.
- `registry`가 `ABSENT`이면 목록은 비어 있어야 한다.
- 같은 ID가 두 번 나오면 안 된다.
- 각 항목의 `active`는 `active.workspace_id`와 같은지 여부와 일치해야 한다.

**사용 범위 DTO**의 필드는 `workspace_id`, `scope`, `connections`다.

- `scope`는 `ABSENT`·`READY` 중 하나다.
- `connections`의 각 행은 `connection_id`, `provider`, `owner_workspace_id`, `allowed`, `reason`을 가진다. IPC 상한은 256개다.
- `allowed`는 `reason`이 `null`일 때만 `true`다.
- `scope`가 `ABSENT`이면 모든 행의 `reason`은 `NO_SCOPE`여야 한다.

**폴더 결과**는 `{outcome: "cancelled", workspace: null}` 또는 `{outcome: "saved", workspace: workspace DTO}`다.

## 5. 오류 코드

renderer에 문구가 정의된 코드는 `src/aik_ipc/workspaces.cjs`의 `WORKSPACE_CODES`다. 엔진 쪽 코드 집합은 `src/aik_desktop_engine/workspaces.py`의 `WORKSPACE_CODES`다.

| 코드 | 뜻 | 사용자 조치 | 내는 곳 |
| --- | --- | --- | --- |
| `WORKSPACES_NOT_CONFIGURED` | 엔진에 프로젝트 폴더 관리가 구성되지 않았다 | 사용자가 고칠 수 없다. 설치·개발 설정을 확인한다 | 엔진 |
| `INVALID_NAME` | 프로젝트 이름이 규칙에 맞지 않는다 | 이름을 고쳐 다시 시도한다 | 서비스 |
| `INVALID_FOLDER_NAME` | 새 폴더 이름이 이식 가능하지 않다 | 금지 문자, 끝의 공백·점, `.`·`..`를 빼고 다시 입력한다 | 서비스 |
| `ROOT_INVALID` | 이 폴더는 프로젝트 폴더가 될 수 없다. 상대 경로, 이미 있는 폴더에 대한 생성, AIK-084 root 규칙 위반 등이다 | 다른 폴더를 고른다 | 서비스 |
| `ROOT_UNAVAILABLE` | 폴더가 없거나 검사할 수 없다 | 폴더가 있는지 확인하고 다시 고른다 | 서비스 |
| `ROOT_CONFLICT` | 다른 프로젝트 폴더와 같거나 그 안팎에 중첩된다 | 겹치지 않는 폴더를 고른다 | 서비스 |
| `UNKNOWN_WORKSPACE` | 등록되지 않은 프로젝트 폴더다 | 목록을 다시 읽는다 | 서비스 |
| `VM_NOT_CONFIGURED` | VM이 연결되지 않아 프로젝트 폴더를 아직 등록할 수 없다 | 지금은 해결 방법이 없다(§7) | 서비스 |
| `VM_PROJECT_UNAVAILABLE` | VM project를 만들거나 확인하지 못했다 | 다시 시도한다 | 서비스 |
| `BINDING_MISMATCH` | 저장된 선택이 검증된 프로젝트 결속과 다르다 | 목록을 다시 읽고 다시 선택한다 | 서비스 |
| `WORKSPACE_ERROR` | 프로젝트 폴더 작업이 실패했다 | 다시 시도한다 | 서비스 |
| `WORKSPACE_ARCHIVED` | 보관된 프로젝트 폴더다 | 복원한다 | 서비스 |
| `WORKSPACE_ROOT_MISSING` | 프로젝트 폴더가 이동되었거나 삭제되었다 | 폴더를 다시 고른다 | 서비스 |
| `WORKSPACE_ROOT_REPLACED` | 같은 위치의 폴더가 다른 폴더로 바뀌었다 | 그 폴더로 다시 연결할지 확인한다 | 서비스 |
| `WORKSPACE_ROOT_UNSAFE` | 프로젝트 폴더 경로를 더 이상 안전하게 쓸 수 없다 | 폴더를 다시 고른다 | 서비스 |
| `WORKSPACE_ACCESS_DENIED` | 프로젝트 폴더에 접근할 수 없다 | 권한을 되돌리거나 폴더를 다시 고른다 | 서비스 |
| `WORKSPACE_UNSUPPORTED_HOST` | 이 위치는 이 컴퓨터에서 작업 폴더로 쓸 수 없다 | 실행할 수 없다. 다른 위치를 쓴다 | 서비스 |
| `WORKSPACE_VM_PROJECT_MISSING` | 이 폴더의 VM project가 더 이상 없다 | VM project 재결속이 필요하다(§10) | 서비스 |
| `WORKSPACE_VM_PROJECT_MISMATCH` | VM project가 다른 폴더를 가리킨다 | VM project 재결속이 필요하다(§10) | 서비스 |
| `WORKSPACE_VM_PROJECT_UNKNOWN` | VM project 상태를 확인하지 못했다 | 잠시 뒤 다시 조회한다 | 서비스 |
| `FOLDER_PICKER_BUSY` | 폴더 대화상자가 이미 열려 있다 | 열린 대화상자를 마치거나 닫는다 | main |
| `FOLDER_PICKER_UNAVAILABLE` | 폴더 대화상자를 열지 못했거나 결과가 잘못됐다 | 다시 시도한다 | main |

엔진 코드 집합에는 있지만 `workspaces.cjs`의 `WORKSPACE_CODES`에는 없는 코드가 있다.

| 코드 | 서비스에서의 뜻 |
| --- | --- |
| `INVALID_INPUT` | 빈 문자열, 1 미만 revision, `InvalidRecord`·`InvalidSetting` |
| `REVISION_CONFLICT` | 그 사이 workspace 기록이 바뀌었다 |
| `STORE_BUSY` | 저장소 잠금이 사용 중이다 |
| `STORE_UNAVAILABLE` | `StorageError` 또는 `OSError` |

이 네 이름은 연결 오류 코드(`CONNECTION_CODES`, [connections-gui](connections-gui.ko.md) §5)에 있다. `desktop.cjs`의 `knownCode`는 `CONNECTION_CODES`와 `WORKSPACE_CODES`를 모두 알려진 코드로 받으므로, workspace 명령에서도 이 코드와 연결 쪽 문구가 renderer까지 그대로 간다. 두 집합에 없는 코드는 `BACKEND_UNAVAILABLE`로 바뀐다.

서비스의 예외 → 코드 대응(`_code`)은 다음과 같다. 위에서부터 차례로 검사한다.

| 예외 | 코드 |
| --- | --- |
| `WorkspaceUnavailable` | `WORKSPACE_` + 상태 대문자 |
| `VmProjectUnavailable` | VM port가 있으면 `VM_PROJECT_UNAVAILABLE`, 없으면 `VM_NOT_CONFIGURED` |
| `RootConflict` | `ROOT_CONFLICT` |
| `RootUnavailable` | `ROOT_UNAVAILABLE` |
| `InvalidWorkspaceRoot` | `ROOT_INVALID` |
| `WorkspaceNotFound` | `UNKNOWN_WORKSPACE` |
| `RevisionConflict` | `REVISION_CONFLICT` |
| `WorkspaceBindingError` | `BINDING_MISMATCH` |
| `InvalidRecord`·`InvalidSetting` | `INVALID_INPUT` |
| `StorageBusy` | `STORE_BUSY` |
| `StorageError`·`OSError` | `STORE_UNAVAILABLE` |
| 그 밖의 `WorkspaceError` | `WORKSPACE_ERROR` |

여기에 해당하지 않는 예외는 코드로 바꾸지 않고 다시 던진다.

renderer는 오류 코드와 함께 `WORKSPACE_CODES`의 문구를 받는다. 시험은 `WORKSPACE_ROOT_MISSING`의 `message`가 그 문구와 같은지 확인한다. 문구는 영어다. 화면은 코드별 한국어 문구를 따로 둔다(§8).

## 6. 상태 값

### 6.1 workspace `status`와 `required_action`

`status`는 `WorkspaceManager.status`가 매번 파일시스템과 VM을 다시 확인해 정한다. `required_action`은 `REQUIRED_ACTION`의 값이다. 상태별 의미는 [workspace](workspace.ko.md)의 "상태와 재선택·재결속"을 따른다.

| status | 뜻 | `required_action` | GUI에서 쓰는 명령 |
| --- | --- | --- | --- |
| `ready` | 실행 가능 | `none` | — |
| `archived` | 보관됨 | `restore_workspace` | `workspaces.restore` |
| `root_missing` | 폴더 이동·삭제 | `reselect_folder` | `workspaces.request_folder`(`rebind`) |
| `root_replaced` | 같은 경로에 다른 폴더가 있거나, 교체 여부를 확인할 수 없음 | `confirm_rebind_root` | `workspaces.request_folder`(`rebind`) |
| `root_unsafe` | symlink·reparse로 바뀜, 정규화 결과 변경 등 | `reselect_folder` | `workspaces.request_folder`(`rebind`) |
| `access_denied` | 권한 상실 | `restore_access_or_reselect` | 권한 복구 뒤 `workspaces.list`, 또는 `request_folder`(`rebind`) |
| `unsupported_host` | host cwd 결속 미지원 | `not_executable` | 없음 |
| `vm_project_missing` | VM이 project 없음으로 응답 | `rebind_vm_project` | 없음(§10) |
| `vm_project_mismatch` | VM ProjectMap root가 AIK root와 다름 | `rebind_vm_project` | 없음(§10) |
| `vm_project_unknown` | VM 조회 실패·불명확 | `retry_vm_query` | `workspaces.list` 다시 읽기 |

READY가 아닌 workspace를 `select`하면 `WORKSPACE_<STATUS>`로 실패한다.

### 6.2 선택(`active`)

- 선택은 설정의 `active_workspace`에 저장된다. `selection_revision`은 그 선택의 revision이다.
- 설정 파일이 없으면 선택이 없다. 이때 `active`는 `null`이다.
- 선택된 workspace가 registry에 없으면 `registered: false`다(§4.3).
- 등록된 선택의 `cwd`는 `status`가 `ready`이고 `current_execution()`이 성공할 때만 채운다. 이 값은 "현재 선택된 workspace"를 보여 주기 위한 것이다. AIK-084 규칙에 따라 Task 실행 cwd로 쓰지 않는다.

### 6.3 저장소 상태

| 필드 | 값 | 뜻 |
| --- | --- | --- |
| `list.vm` | `CONFIGURED` | VM project port가 주입됐다 |
| `list.vm` | `NOT_CONFIGURED` | VM project port가 없다. 등록은 `VM_NOT_CONFIGURED`다 |
| `list.registry` | `ABSENT` | registry 파일이 없다. 목록은 비어 있다 |
| `list.registry` | `READY` | registry를 읽었다 |
| `scope.scope` | `ABSENT` | 설정 파일이 없거나(연결 목록 비어 있음), scope 파일이 없다(모든 연결 `NO_SCOPE`) |
| `scope.scope` | `READY` | scope 파일이 있어 연결마다 `ScopeStore.check`를 했다. 허용되지 않은 연결의 `reason`은 `ConnectionNotInScope.reason`이다(예: `NOT_SHARED`) |

## 7. 설계 결정

- **VM 없이는 등록을 막는다(사용자 결정, 2026-10-14).**
  - VM 고정 계약에는 공개 project 생성 command가 없다. 그래서 등록은 막힌 채로 둔다.
  - GUI, 엔진 명령, 폴더 대화상자는 만들어 둔다. VM project port가 없으면 모든 등록(`open`, `create`)이 `VM_NOT_CONFIGURED`로 답한다. 이때 폴더, registry 기록, 설정 값을 남기지 않는다.
  - 시험은 `create`가 만든 새 폴더가 다시 지워지고 `workspaces.json`이 생기지 않는지 확인한다.
  - AIK-084의 설계와 설정 불변식("root와 VM project는 함께 저장된다")은 바꾸지 않는다. 스키마도 바꾸지 않는다.
  - 흐름은 시험에서 fake VM port로 확인한다.
- **renderer는 경로를 보내지 않는다.**
  - 경로는 main의 native 대화상자에서만 나온다.
  - 경로를 받는 엔진 명령(`open`, `create`, `rebind_root`)은 renderer 명령이 아니다.
  - 취소했거나 기한이 지난 요청은 엔진에 아무것도 보내지 않는다.
- **IPC 라우팅 scope와 선택한 프로젝트는 별개다.**
  - IPC 요청의 `workspace_id`는 `local`로 남는다. 선택한 프로젝트는 엔진의 설정 데이터다.
  - 연결은 계속 `local` scope에 있다.
- **읽기는 파일을 만들지 않는다.**
  - registry가 없으면 `registry: "ABSENT"`다. manager도 만들지 않는다.
  - 설정이 없으면 선택이 없다(`active: null`).
  - scope 파일(설정 DB 옆의 `scopes.sqlite3`)이 없으면 모든 연결을 `NO_SCOPE`로 보고하고 파일을 만들지 않는다.
  - 시험은 `list`와 `scope` 호출 전후에 임시 디렉터리 내용이 같은지 확인한다.
- **설정은 manager를 결속 권위로 해서 연다.** `SettingsStore(..., binding_authority=manager)`이므로 설정 쪽 선택도 AIK-084 `binding_for`로 다시 확인된다.
- **서비스는 수명주기 규칙을 갖지 않는다.** 상태는 `WorkspaceManager.status`, 조치는 `REQUIRED_ACTION`에서만 온다.
- **사용 범위 표시는 읽기 전용이다.**
  - `workspaces.scope`는 연결마다 허용 여부와 이유만 보여 준다. 편집은 AIK-088이다.
  - 연결 목록은 설정의 연결 중 처음 100개(`limit=100`)만 읽는다.
- **workspace 명령은 연결 명령과 같은 줄에서 처리한다.**
  - 엔진은 `workspaces.*`를 `connections.*`와 함께 작업 스레드에서 답한다(`LANE_COMMANDS`).
  - 그래서 긴 연결 명령이 진행 중이면 workspace 명령이 차례를 기다릴 수 있다.
  - 기한은 엔진 클라이언트 150초, router 155초다. `workspaces.request_folder`의 기한은 1,800,000ms(30분)다.
- **재시작 뒤 선택을 복원한다.**
  - 선택은 설정의 `active_workspace`에 남는다.
  - 새 엔진의 `list`는 `WorkspaceManager.status`와 `current_execution`으로 다시 검증한다. `cwd`는 `ready`일 때만 돌려준다.
- **오류는 고정 코드만 보낸다.** `WorkspaceCommandError`는 `WORKSPACE_CODES`에 없는 코드로 만들 수 없다. 응답에 경로를 넣지 않는다.

## 8. 화면

설정 route 맨 위에 `#workspaces-panel`(제목 "프로젝트 폴더")을 둔다. 화면은 `workspaces.list`를 읽어 다음을 보여 준다.

- `data-state`: `LOADING`(첫 응답 전), `NOT_CONFIGURED`(`WORKSPACES_NOT_CONFIGURED`), `UNAVAILABLE`(엔진 없음 등 읽기 실패, 5초마다 다시 시도), `EMPTY`(등록 0개), `READY`. `data-vm`에는 `list.vm`을 둔다.
- VM이 없으면(`vm: NOT_CONFIGURED`) "VM이 연결되지 않았습니다" 안내를 띄운다. 등록 버튼은 그대로 두고 엔진의 실제 답(`VM_NOT_CONFIGURED`)을 보여 준다. 성공한 것처럼 꾸미지 않는다.
- **현재 프로젝트**: 선택 없음, 등록되지 않은 선택(`registered: false`), 등록된 선택의 이름·상태·프로젝트 폴더·실행 폴더(cwd)·필요한 조치를 구분해 보여 준다. cwd는 `ready`일 때만 표시한다.
- **등록 폼**: 프로젝트 이름(필수)과 새 폴더 이름(새 폴더 만들기에만)을 받는다. 경로 입력란은 없다. "기존 폴더 연결"과 "새 폴더 만들기"가 `request_folder`의 `open`·`create`를 보낸다. 이름·폴더 이름은 보내기 전에 IPC 규칙과 같은 기준으로 검사하고, 문제는 입력란 아래에 `aria-describedby`로 연결한 문구로 보여 준다.
- **목록 행**: 이름, 상태(한국어와 `status` 코드), 폴더, 필요한 조치를 보여 준다. 버튼은 상태에 따라 "이 폴더로 전환"(`ready`만), "폴더 다시 선택"(`reselect_folder`·`confirm_rebind_root`·`restore_access_or_reselect`), "보관", "복원", "연결 사용 범위 보기"다. 전환할 수 없는 행은 이유를 버튼 설명으로 연결한다.
- **사용 범위**: 행에서 펼치면 `workspaces.scope`를 읽어 연결마다 허용 여부와 이유(`NO_SCOPE`, `NOT_SHARED` 등의 한국어 문구)를 보여 준다. 편집 기능은 없다(AIK-088).
- 마지막 목록을 받은 뒤 새로 읽기가 실패하면 목록을 지우지 않고 "마지막으로 받은 목록" 표시와 오류 코드를 붙인다.
- 모든 동작은 버튼과 입력란만 쓰므로 키보드로 조작할 수 있다. 버튼에는 프로젝트 이름이 든 `aria-label`을 단다.
- 왼쪽 메뉴의 앱 이름 아래(`#nav-project`)에 현재 프로젝트 이름을 보여 준다. 화면 아래쪽에는 IPC 라우팅 scope(`IPC 범위 local`)를 표시한다.

smoke(§9)는 엔진이 있을 때 이 패널이 `EMPTY`·`vm NOT_CONFIGURED`이고, 엔진이 없을 때 `UNAVAILABLE`인지 확인한다. 설정 화면 스크린샷은 [근거](../evidence/implementation/2026-10-14-workspaces-gui/README.ko.md)에 있다.

## 9. 시험 결과

실행 명령(저장소 루트 기준):

```bash
python -m unittest tests.test_desktop_workspaces -v
node --test tests/test_desktop_ipc_workspaces.cjs
node --test apps/desktop/tests/folder-picker.test.cjs
(cd apps/desktop && npm run build)
```

시험은 임시 디렉터리, 합성 DTO·경로, fake VM port, 가짜 대화상자·엔진만 쓴다.

| 시험 | 대상 | 결과 |
| --- | --- | --- |
| `tests/test_desktop_workspaces.py` | `WorkspaceService`와 엔진 `workspaces.*` 명령. 다음을 확인한다. 읽기가 파일을 만들지 않는다. VM 없이 등록하면 `VM_NOT_CONFIGURED`이고 아무것도 남지 않는다. 열기·생성·선택·재시작 뒤 복원이 된다. 옮긴 폴더는 `root_missing`·`reselect_folder`로 보고되고 `rebind_root`로 다시 결속된다(오래된 revision은 `REVISION_CONFLICT`). VM 조회 실패는 `vm_project_unknown`이다. 보관·복원이 된다. 잘못된 이름·폴더 이름·root를 거절한다. scope 파일 없이 연결별 사용 범위를 보고한다. 서비스가 없는 엔진은 `WORKSPACES_NOT_CONFIGURED`, 남는 payload 키는 `INVALID_REQUEST`다. 오류 응답에 경로가 없다 | 9개 OK |
| `tests/test_desktop_ipc_workspaces.cjs` | renderer IPC. 다음을 확인한다. renderer 명령에 경로 필드와 `open`·`create`·`rebind_root`가 없다. payload의 정확한 키·형식·모드별 조합을 검사한다. 결과를 동결 DTO로 복사한다. 일관성이 깨진 결과는 `INVALID_RESPONSE`다. scope 행 규칙을 지킨다. 대화상자 결과와 고정 코드·문구가 renderer에 도달한다 | 5개 pass |
| `apps/desktop/tests/folder-picker.test.cjs` | `FolderPicker`. 다음을 확인한다. 모드별로 맞는 엔진 명령과 payload를 보낸다. 대화상자 옵션이 맞다. 취소·빈 결과·기한 초과 때 엔진에 보내지 않는다. 한 번에 하나만 열린다(`FOLDER_PICKER_BUSY`). 대화상자 실패·상대 경로·경로 여러 개는 `FOLDER_PICKER_UNAVAILABLE`이다. 실패 뒤 열림 표시가 풀린다. 엔진 실패를 그대로 전달한다 | 4개 pass |

전체 회귀(2026-10-14, Linux 컨테이너):

- Python `unittest discover -s tests`: 1584개 OK(skip 18)
- IPC node 시험(`tests/*.cjs`): 53개 pass
- `apps/desktop` node 시험: 19개 pass, `npm run build` 성공

Electron smoke(xvfb, 일반 사용자 계정, 빈 HOME):

| 실행 | 결과 |
| --- | --- |
| 엔진 있음 | `PASS`, exit 0. 패널 `EMPTY`, `vm NOT_CONFIGURED`, `workspaces.list` 답은 `registry ABSENT`·`active null`·목록 0개. 키 입력 창 `PASS` |
| 엔진 없음 | `BLOCKED`, exit 2. 패널 `UNAVAILABLE` |

`tools/check_desktop_smoke.py`는 두 보고서 모두 OK다. smoke 뒤 HOME에 상태 파일이 생기지 않았다.

Linux 통과는 개발 검증이며 Windows 지원 증거가 아니다. Windows 결과는 CI(`desktop-shell`, `desktop-ipc`)로만 기록한다.

## 10. NOT_RUN과 한계

- **실제 VM project 생성·조회**: 생성 seam이 없다. 그래서 VM project port를 주입하지 않으며 실제 등록은 `VM_NOT_CONFIGURED`다. 실제 VM `status` 응답으로 조회한 적도 없다(NOT_RUN).
- **실제 native 대화상자 조작**: smoke는 native 대화상자를 조작할 수 없다. 대화상자는 시험에서 가짜로 대신한다(NOT_RUN).
- **Windows 실행**: CI에서만 실행한다. Windows에서 직접 확인한 결과는 없다.
- **설치본 E2E**: 한글·공백 경로와 키보드 조작을 포함한 설치본 E2E는 AIK-057·064에 남아 있다(NOT_RUN).
- **실제 cwd 일치**: native/API 작업자의 실제 cwd와 생성 파일 위치가 선택한 폴더와 같은지는 확인하지 않았다. VM 연결(AIK-044) 뒤의 일이다(NOT_RUN).
- **Goal 입력·인가 revision**: 진행 중 Goal의 입력 변경이나 전환을 VM의 revision·인가 규칙에 연결하지 않았다. AIK-084의 미구현 항목이다.
- **고아 VM project**: AIK-084가 적은 대로, 등록 실패나 동시 등록 충돌 때 이미 만든 VM project가 남을 수 있다. VM 삭제 command가 없기 때문이다. 이 위험은 VM port가 생기면 그대로 적용된다.
- **VM project 재결속 명령 없음**: `vm_project_missing`·`vm_project_mismatch`의 조치는 `rebind_vm_project`다. 하지만 엔진 명령(`WORKSPACE_COMMANDS`)에 AIK-084 `rebind_project`에 해당하는 명령이 없다. GUI에서 이 상태를 해소할 수 없다.
- **목록 상한**: 엔진 `list`에는 개수 제한이 없다. IPC는 workspace 256개까지만 받는다. 이를 넘으면 `INVALID_RESPONSE`다. 사용 범위는 엔진이 처음 100개 연결만 읽는다.
- **기한이 지난 대화상자**: 기한이 지나도 이미 열린 대화상자는 닫히지 않는다. 사용자가 닫을 때까지 새 요청은 `FOLDER_PICKER_BUSY`다. 그 대화상자에서 고른 폴더는 엔진에 보내지 않는다.
- **READY 행 화면**: 실제 앱에는 VM이 없어 등록이 항상 `VM_NOT_CONFIGURED`다. 그래서 등록된 폴더 행, 전환·다시 선택·보관·복원 버튼, 사용 범위 펼침은 실제 앱 화면에서 본 적이 없다. 이 부분은 IPC 시험의 합성 DTO로만 확인했다(NOT_RUN).