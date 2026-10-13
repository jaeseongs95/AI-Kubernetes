# AIK 사용자 workspace 수명주기·VM project·입력 결속 — AIK-084

`src/aik_workspace/`는 사용자 프로젝트의 생성·열기·선택(전환)·보관과, canonical root·VM project·입력/선행 산출물의 결속을 제공하는 공통 backend다. Python 표준 라이브러리와 저장소의 기존 패키지만 사용한다.

| 기존 패키지 | 재사용 대상 |
| --- | --- |
| `aik_runtime` | 경로 규칙, 프로세스 잠금, 식별자 규칙 |
| `aik_config` | `WorkspaceBindingAuthority`, `SettingsStore`, `Workspace` 계약 |
| `aik_vm_bridge` | `VmBridge.status`, `TaskExecutionBinding`, digest 형식 |

기존 파일은 수정하지 않았다.

**현재 상태: 구현과 fake VM port 단위시험까지 완료.** 다음 항목은 모두 **NOT_RUN(미검증)**이다.

- 실제 VM 설치물 호출
- Windows 실행
- GUI 연결
- 설치형 검증

이 문서와 테스트 통과를 qualified 또는 완료 판정으로 해석하지 않는다. `planning/` 장부 상태는 변경하지 않았다.

## 구성

| 모듈 | 역할 |
| --- | --- |
| `roots.py` | canonical root 정규화, Git 내부·개발 원장 거부, 중첩 판정, host cwd 지원 확인, 접근 probe |
| `registry.py` | workspace 결속과 입력 digest의 영속 기록(JSON, 원자적 교체, `aik_runtime` 잠금) |
| `vm_projects.py` | `VmProjectPort` 계약, `VmBridge` 기반 adapter `BridgeProjectPort` |
| `inputs.py` | 입력 상대경로 검증, link 없는 안전한 읽기, sha256 digest |
| `manager.py` | `WorkspaceManager`: 수명주기, `binding_for`(authority), 선택, 입력 결속·manifest |

## 수명주기 API

```python
from aik_config import SettingsStore
from aik_vm_bridge import VmBridge, Command
from aik_workspace import WorkspaceManager, WorkspaceRegistry, BridgeProjectPort

bridge = VmBridge(vm_port, surface, commands=[Command.STATUS])
manager = WorkspaceManager(WorkspaceRegistry(), BridgeProjectPort(bridge, create_project=creator))
settings = SettingsStore(binding_authority=manager)
manager.connect_settings(settings)

manager.create_workspace('ws-a', '프로젝트 A', r'C:\Users\me\Projects\새 프로젝트')
manager.open_workspace('ws-b', '기존', r'C:\Users\me\Projects\기존 폴더')
ctx = manager.select('ws-a')                 # root·VM project 재검증 뒤 선택
```

Task 실행의 cwd는 `select`나 `current_execution()`의 반환값이 아니라, 아래 `verify_task_binding`이 반환하는 `ExecutionContext`에서 얻는다.

### 생성·열기

`create_workspace`는 존재하는 안전한 부모 아래에 새 빈 폴더를 만든다. `open_workspace`는 기존 폴더를 결속하며 내용은 바꾸지 않는다. 두 경우 모두 다음 순서로 진행한다.

1. canonical root를 정규화한다. `create_workspace`는 폴더를 만들기 **전에** 부모가 Git 내부가 아닌지, 새 경로가 개발 원장·AIK 소스 저장소와 겹치지 않는지, host가 지원되는지 확인한다. 실패하면 디렉터리를 만들지 않는다.
2. host 지원과 접근 가능 여부, root identity(st_dev/st_ino)가 기록 가능한 범위(0~2^128-1, NTFS·ReFS file ID 포함)이고 inode가 0이 아닌지 확인한다. 아니면 `root_unsafe`로 거절한다.
3. VM project 생성 전에 project ID를 제외한 workspace 기록 전체를 검증한다.
4. 중복·중첩을 검사한다.
5. VM project를 생성한다.
6. registry에 기록한다.
7. 설정(`aik_config`)에 결속값을 저장한다. 이때 설정은 다시 `binding_for`로 확인한다.

설정 저장이 실패하면 registry 기록을 되돌린다. 생성한 폴더도 비어 있으면 제거한다(시험 확인). 이미 생성된 VM project는 삭제 command가 없어 남는다(한계). 같은 ID·겹치는 root로 두 프로세스가 동시에 등록하거나 `rebind_project`를 동시에 호출해 registry 기록 단계에서 충돌(`RevisionConflict`/`RootConflict`)이 나는 경우에도, 이미 생성된 VM project가 고아로 남는다(한계). 이때 먼저 기록된 결속은 덮어쓰지 않는다(동시 등록 모사 시험 확인).

### 선택·전환

`select`는 대상 workspace의 root·identity·접근·VM project를 먼저 검증한 뒤 `SettingsStore.select_workspace`를 호출한다. 설정 쪽도 `binding_for`로 재확인한다. 검증에 실패하면 선택을 바꾸지 않는다.

GUI가 설정을 직접 바꾸려 해도 같은 이유로 거절된다(시험 확인). `current_execution()`은 설정의 현재 선택에서만 실행 root를 도출하고 registry의 결속과 대조한다. 이는 "현재 선택된 workspace"를 보여주기 위한 값이며, 이미 검증된 Task의 실행 cwd로 쓰지 않는다. 검증 후 사용자가 다른 workspace로 전환하면 `current_execution()`은 새 root를 가리키기 때문이다.

### 보관·복원

- `archive_workspace`: 보관된 workspace는 `binding_for`가 `None`을 반환하므로 실행 대상이 아니다.
- `restore_workspace`: 복원할 때 다른 활성 root와의 충돌을 다시 검사한다.

### 재결속

- `rebind_root`: 폴더 이동, 교체, 다른 폴더를 다시 선택한 경우에 쓴다.
- `rebind_project`: VM project가 없음 또는 불일치로 **확인된** 경우에만 새 project를 만든다. VM 조회가 불명확(`vm_project_unknown`)하면 새 project를 만들지 않는다.

## 상태와 재선택·재결속

`status(id)`와 `execution_context(id)`는 매번 실제 파일시스템과 VM을 다시 확인한다. READY가 아니면 `WorkspaceUnavailable(status, action)`으로 실패한다(fail-closed).

| status | 의미 | `action` |
| --- | --- | --- |
| `ready` | 실행 가능 | `none` |
| `archived` | 보관됨 | `restore_workspace` |
| `root_missing` | 폴더 이동/삭제 | `reselect_folder` |
| `root_replaced` | 같은 경로에 다른 폴더(st_dev/st_ino 변경), 또는 inode가 0이라 교체 여부를 확인할 수 없음 | `confirm_rebind_root` |
| `root_unsafe` | symlink/reparse로 바뀜, 정규화 결과 변경, identity가 기록 범위 밖이거나 inode 0 | `reselect_folder` |
| `access_denied` | 목록/읽기/쓰기 권한 상실 | `restore_access_or_reselect` |
| `unsupported_host` | host cwd 결속 미지원 | `not_executable` |
| `vm_project_missing` | VM이 project 없음으로 응답 | `rebind_vm_project` |
| `vm_project_mismatch` | VM ProjectMap root가 AIK root와 다름 | `rebind_vm_project` |
| `vm_project_unknown` | VM 조회 실패·불명확, ProjectMap root 미보고, 응답의 project ID가 요청과 다름 | `retry_vm_query` |

재시작 뒤에는 registry 파일에서 결속을 복원한다. 설정의 선택도 유지되며, 사용 전에 위 검증을 다시 거친다.

폴더를 이동하면 AIK root만 갱신된다. VM ProjectMap의 root를 옮기는 공개 command가 없기 때문이다. VM이 ProjectMap root를 보고하면 `vm_project_mismatch`가 표시되고 `rebind_project`가 필요하다. VM이 root를 보고하지 않으면 project–root 결속을 확인할 수 없으므로 `vm_project_unknown`(실행 불가)이며, 이 상태에서는 새 project도 만들지 않는다. 이전 VM project는 삭제하지 않는다.

inode 0을 보고하는 파일시스템에서는 폴더 교체를 탐지할 수 없으므로 workspace로 등록할 수 없다(fail-closed). 기존 기록에 inode 0이 있으면 `root_replaced`로 표시된다.

## canonical root 규칙

- 절대경로만 허용한다. 상대경로는 거절한다. 프로세스 cwd를 권위로 삼지 않기 위해서다.
- 다음 경로를 거절한다: 네트워크(UNC), `..`, 제어문자, Windows 장치 이름, 파일시스템/드라이브 루트.
- 경로의 모든 구성요소에서 symlink·reparse point를 거절한다(`aik_runtime.paths._inspect` 재사용). 존재하는 디렉터리만 허용한다. `resolve(strict=True)` 결과를 canonical root로 저장한다. Windows에서는 이 결과가 실제 대소문자 표기가 된다.
- 다음 위치는 workspace root가 될 수 없다.
  - `.git` 구성요소, 또는 HEAD/objects/refs를 가진 Git 디렉터리(bare repo 포함)와 그 내부
  - AIK 소스 저장소(`planning/schema.sql` 보유), 또는 상위 경로에 `planning/backlog.sqlite3`가 있는 원장 저장소의 내부·자신·상위
  
  사용자 자신의 Git 작업 트리 루트는 허용한다.
- 비교 기준은 다음과 같다. 대소문자 무시는 Windows에만 적용한다.
  - 경로 구성요소 접두 일치: 같은 root와 중첩 root를 거절한다.
  - 동일 st_dev/st_ino: 대소문자 별칭 등 같은 폴더를 거절한다.
  
  보관된 workspace는 활성 충돌 판정에서 제외한다.
- 한계: root 하위 깊은 곳에 있는 다른 원장 저장소는 전체 트리를 순회하지 않으므로 탐지하지 않는다.

## 입력·선행 산출물 결속

방식은 **단일 root 공유(`shared_root`)**만 구현했다. 별도 projection(사본 배치)은 구현하지 않았다.

- `bind_input(ws, '상대/경로', producer_task_id=..., expected_digest=...)`
  - root 아래의 일반 파일을 link 없이 읽어 sha256 digest·크기·입력 revision을 기록한다.
  - 선행 Task 산출물(`producer_task_id` 지정)은 VM이 선언한 digest를 `expected_digest`로 **반드시** 넘긴다. 생략하면 `InvalidRecord`로 거절하고 기록하지 않는다. 값이 다르면 `InputTampered`이며 기록하지 않는다. `accept_input_change`도 같다.
  - 다음 경로는 거절한다(`InvalidInputPath`): 절대경로, `..`, 역슬래시, 드라이브 표기, `.git`, 장치 이름.
  - 다음 파일은 `unsafe`로 차단한다: symlink, reparse point, hardlink 별칭, root 밖으로 해석되는 파일, 크기 한도 초과(기본 64 MiB), 해석된 경로의 구성요소가 요청 경로와 정확히 같지 않은 파일(대소문자가 다른 표기, Windows 8.3 short name 별칭 등).
- `input_states(ws)`: `current`/`stale`/`missing`/`unsafe`를 표시한다. 기록된 digest와 현재 bytes가 다르면 `stale`이다.
- `prepare_inputs(ws, paths=None)`
  - 모든 대상이 `current`일 때만 `InputManifest`를 만든다. 하나라도 아니면 `InputNotCurrent(details=((path, 상태), ...))`로 실패하고 아무것도 준비하지 않는다.
  - manifest에는 workspace ID·VM project·workspace revision·`workspace_root_ref`·항목(path, digest, size, revision, producer)이 들어간다.
  - `digest()`는 `TaskExecutionBinding.input_manifest_digest` 형식(`sha256:`)이다. `workspace_root_ref`(`aik-workspace://ws/<hash>`)는 `workspace_root_ref` 형식이며 로컬 경로를 노출하지 않는다. 두 값이 `TaskExecutionBinding`으로 생성되는 것을 시험으로 확인했다.
- `verify_task_binding(binding, manifest) -> ExecutionContext`: 효과 전 검사다. 다음을 모두 확인하며 실패하면 예외를 낸다.
  - binding의 manifest digest·VM project·root ref가 manifest와 같다.
  - manifest가 현재 workspace 결속(revision 포함)과 같다.
  - 입력을 다시 준비했을 때 같은 digest가 나온다.

  통과하면 manifest의 workspace에 대해 검증된 `ExecutionContext`를 반환한다. **Task 실행 cwd는 반드시 이 반환값(`ctx.subprocess_kwargs()`)을 사용하고 `current_execution()`을 쓰지 않는다.** 검증 뒤 다른 workspace로 전환해도 반환값은 검증한 workspace를 가리킨다(시험 확인).
- `read_input(manifest, path)`: 한 번 읽은 bytes의 digest가 manifest와 같을 때만 그 bytes를 반환한다. 이 함수를 통해 읽는 소비자에게만 bytes–digest 일치가 보장된다.

  | 상황 | 결과 |
  | --- | --- |
  | 변조 | `InputTampered` |
  | 누락·변경 수락 후의 옛 manifest | `InputNotCurrent` |
  | 다른 workspace·목록에 없는 경로·바뀐 결속 | `InputBindingMismatch` |

- `confirm_inputs_after_effect(manifest)`: 효과 후 재검사다. cwd에서 파일을 직접 읽는 native/API 소비자처럼 `read_input`을 거치지 않는 경우, 효과 뒤에 호출해 manifest의 bytes·revision이 그대로인지 확인한다. 불일치하면 `InputNotCurrent(details=...)`를 내며, 호출자는 그 효과의 입력을 `unknown`/실패로 분류해야 한다.
- `accept_input_change(ws, path, expected_revision)`: 사용자가 바뀐 bytes를 명시적으로 수락할 때 쓴다. 입력 revision이 증가하고 이전 manifest는 무효가 된다.

**shared_root의 한계:** 파일을 고정하지 않으므로 `verify_task_binding` 이후의 변경을 막지 않는다. native 소비자가 그 사이 바뀐 bytes를 읽을 수 있다. `confirm_inputs_after_effect`는 효과 후 남아 있는 변경만 탐지하며, 효과 도중 바뀌었다가 원래 bytes로 되돌아간 경우는 탐지하지 못한다. 이를 막으려면 projection(사본 배치)이 필요하나 구현하지 않았다. 또한 `read_verified`는 구성요소 검사 후 경로 기반으로 파일을 연다. 같은 사용자 권한의 프로세스가 그 사이 중간 디렉터리를 root 밖 링크로 바꾸고 다시 되돌리는 경합은 막지 못한다(cwd 결속이 OS 격리가 아니라는 아래 범위와 같은 한계).

**진행 중 Goal 규칙(미구현):** 진행 중 Goal의 입력 변경이나 workspace 전환은 VM의 기존 revision·인가 규칙(예: `revise`, `authorize`)을 따라야 한다. 이 패키지의 `select`·`accept_input_change`는 VM revision·인가를 확인하지 않으며 그 연결은 구현되지 않았다. 이 패키지는 Goal·Task 상태를 추적하지 않고 새 manifest digest와 검증된 실행 문맥만 제공한다. registry는 결속 메타데이터와 digest만 저장하며, 별도의 Task/입력 권위 원장이 아니다.

## VM project port

`VmProjectPort`의 메서드는 다음 두 가지다.

- `create_project(workspace_id, canonical_root, name) -> project_id`
- `lookup_project(project_id) -> ProjectInfo | None`

`BridgeProjectPort`는 조회를 `aik_vm_bridge.VmBridge.status`로만 수행한다. 응답 처리 방식은 다음과 같다.

- 응답의 `project_root`를 VM ProjectMap root로 간주해 AIK root와 비교한다. `project_root`가 없으면 불명확(`VmProjectUnavailable` → `vm_project_unknown`)으로 처리한다. 다른 port가 `ProjectInfo(root=None)`을 반환해도 manager는 `vm_project_unknown`으로 판정한다.
- 응답에 `project_id`가 있고 요청 ID와 다르면 불명확으로 처리한다.
- VM 오류 코드 `PROJECT_NOT_FOUND`/`NOT_FOUND`/`UNKNOWN_PROJECT`는 "없음"으로 해석한다. 이 코드 목록은 실제 VM과 대조하지 않은 **가정**이다.
- 그 밖의 실패는 불명확으로 처리한다.

실제 VM `status` 응답이 `project_root`를 포함하는지는 **NOT_RUN**이다. 포함하지 않는다면 모든 workspace가 실행 불가가 되며, 이는 의도된 fail-closed 결과다.

선언된 VM 공개 표면에는 project 생성 command가 없다. 따라서 생성은 주입한 `create_project` callable(upstream seam)로만 수행한다. 주입하지 않으면 `VmProjectUnavailable`로 실패한다(fail-closed). 실제 생성 seam과 설치물 adapter는 **NOT_RUN**이다.

## host cwd 결속과 격리 범위

`host_cwd_support`의 판정은 다음과 같다.

- Windows: `GetDriveTypeW`가 로컬 드라이브로 판정할 때만 지원으로 본다(`aik_runtime.paths._require_local_drive` 재사용).
- POSIX: 개발용으로 허용한다.
- 그 외 OS: 미지원이다.

미지원 host에서는 생성·열기를 거절하고(생성은 폴더를 만들기 전에 거절), 기존 workspace는 `unsupported_host`(실행 불가)로 표시한다. cwd를 root로 지정하는 것은 실행 위치 결속일 뿐이다. 프로세스가 root 밖을 읽고 쓰지 못하게 하는 OS 격리를 제공하거나 가정하지 않는다.

## 파일 위치·동시성

- registry 위치
  - Windows 기본: `%LOCALAPPDATA%\AIK\workspace\workspaces.json`
  - Linux: `$XDG_STATE_HOME/aik/workspaces.json`. 개발용 fallback이며 Linux 제품 지원을 뜻하지 않는다.
- 경로 검증은 `aik_runtime.paths.validate_database_path`와 같은 규칙을 쓴다. 따라서 저장소 내부·개발 원장·symlink·hardlink는 거절한다.
- 동시 접근은 `workspaces.json.storage.lock`의 OS 잠금으로 직렬화한다.
- 기록은 임시 파일 작성 → fsync → `os.replace` 순서로 원자적으로 교체한다.
- 손상되었거나 알 수 없는 형식의 registry는 `InvalidDatabase`로 거절하며 덮어쓰지 않는다.
- registry 잠금을 쥔 채 설정 transaction을 호출하지 않는다. 설정 transaction 안에서 `binding_for`가 registry 잠금을 다시 잡기 때문이다.

## 검증

```bash
python -m unittest tests.test_workspace -v
```

임시 디렉터리와 fake VM port(`VmBridge` + 합성 `status`/`create_project`)만 사용한다. 검사 범위는 다음과 같다.

- 한글·공백 경로
- 실제 subprocess cwd
- 기존 폴더 열기와 전환 시 재검증, GUI 단독 선택 거절
- 상대·traversal·네트워크·Git 내부·원장·드라이브 루트 거절, symlink root·부모 거절
- Git 내부·원장 부모 아래 생성과 미지원 host 생성의 mkdir 전 거절, 기존 폴더에 대한 생성 거절
- 중복·중첩 root 거절과 VM 효과 전 차단
- 기록 범위 밖 identity의 VM 효과 전 거절, inode 0 identity 불신
- 설정 저장 실패 시 registry·새 폴더 원복, 동시 등록 모사에서 기존 결속 보존
- 재시작 복원
- 폴더 이동·삭제·교체, 권한 상실(mock, 그리고 비 root POSIX의 실제 chmod)
- VM 조회 불명확, ProjectMap root 미보고·다른 project ID 응답, 미지원 host, 보관·복원 충돌, 생성 seam 부재
- 손상 registry 보존
- 생산 산출물의 후속 소비(동일 bytes·digest, `TaskExecutionBinding` 결속), producer digest 누락 거절
- 잘못 선언된 digest, stale·변조·누락 입력의 효과 전 차단, 변경 수락 후 옛 manifest 무효화
- `verify_task_binding` 반환 문맥의 cwd로 실행한 별도 subprocess가 결속 파일을 읽은 digest 대조, 검증 후 변경의 사후 탐지
- 검증 후 workspace 전환 시 반환 문맥 유지, binding의 VM project ID 불일치 거절
- 경로 탈출·symlink·hardlink·다른 workspace 참조 거절, 대소문자·short name 별칭의 해석 불일치(mock) 거절

**미검증(NOT_RUN):**

- Windows 실행: 드라이브 판정, reparse point, 대소문자 정규화, 8.3 short name의 실제 `resolve` 결과, `st_ino` 범위(NTFS/ReFS file ID; 상한을 SQLite 정수 범위에서 2^128-1로 넓힌 뒤 Windows CI `aik-modules.yml`에서 재검증 필요), ACL 권한 상실, `%LOCALAPPDATA%` 경로
- 실제 VM 설치물의 `status` 응답 형식(`project_root`·`project_id` 포함 여부)과 not-found 코드
- VM project 생성 seam
- 실제 GUI·native 실행기의 cwd 적용과 `confirm_inputs_after_effect` 연결
- 진행 중 Goal의 VM revision·인가 연결(미구현)
- 대규모 입력

Linux 통과는 개발 검증이며 Windows 지원 증거가 아니다.
