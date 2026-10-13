# AIK-085 — 개발 worker 파일·검증 도구와 효과 계약 (부분 구현)

상태: **구현과 합성 fixture 단위시험까지.** 다음은 모두 **미검증(NOT_RUN)**이다.

- 실제 `AuthorityGateway`와 VM 효과 권위 extension 연동
- Windows 실행
- 실제 개발 worker(native/API)와의 연결
- 전체 live governed 경로(AIK-086)

이 문서와 시험 통과를 qualified 또는 완료 판정으로 해석하지 않는다. `planning/` 장부의 AIK-085 상태와 완료 판정은 바꾸지 않았다.

## 구성과 재사용

`src/aik_devtools/`는 Python 표준 라이브러리와 아래 기존 패키지만 import한다. `aik_devtools` 밖의 기존 파일은 수정하지 않았다.

| 기존 패키지 | 재사용 대상 |
| --- | --- |
| `aik_authority` | `ToolSession`, `ToolCall`, `ToolCallAuthorization`, `EffectKind`, `ToolDeclaration`, `EffectReceipt`. gateway의 `request`(VM intent)·`forward_receipt`(VM receipt)·`effect_state` |
| `aik_workspace` | `execution_context`로 canonical root 재검증, `normalize_input_path`, `read_verified`, `sha256_digest` |
| `aik_runtime` | 식별자 규칙(`identifier`, `MAX_INTEGER`), `InvalidRecord`(공개 모델·오류 모듈) |
| `aik_vm_bridge` | digest 형식(`DIGEST_PATTERN`) |

경로 구성요소 검사와 디렉터리 fsync는 `aik_runtime`의 비공개 helper를 쓰지 않고 `tools.py` 안의 동등한 구현을 사용한다. 규칙은 `aik_runtime.paths`와 같다(symlink·reparse point·특수 파일·hardlink 거절). 따라서 두 패키지의 규칙이 앞으로 갈라질 수 있다. 바꿀 때는 양쪽을 함께 확인해야 한다.

| 모듈 | 역할 |
| --- | --- |
| `policy.py` | 고정 도구 목록, 효과 분류·계약, 신뢰된 검증 정책(`DevToolPolicy`, `VerificationCommand`) |
| `tools.py` | `DevTools`: 인가 → 효과 → receipt, 재전송 저장, 파일 연산, 검증 명령 실행 |
| `errors.py` | 고정 코드 오류(`DevToolError`, `EffectUnknown`, `InvalidDevToolPolicy`) |

## 도구와 효과 분류

| 도구 | 효과 종류 | 재전송 | 효과 직후 중단 | 취소 |
| --- | --- | --- | --- | --- |
| `dev.read_file` | `read_only` | 재인가 후 다시 읽어 저장 digest와 대조 | 재인가 후 다시 읽음 | 효과 없음 |
| `dev.create_file` | `idempotent` | 저장된 결과 반환 | 실제 파일 digest 관측. 거절이 관측되면 `unknown` | 효과 전에만 |
| `dev.update_file` | `idempotent` | 저장된 결과 반환 | 실제 파일 digest 관측. 거절이 관측되면 `unknown` | 효과 전에만 |
| `dev.run_verification` | `non_idempotent` | 저장된 결과 반환 | `EffectUnknown`, 재실행 금지 | process group 종료 |

`tool_declarations()`는 이 분류로 `aik_authority.ToolDeclaration`을 만든다. 신뢰된 backend는 이것으로 gateway `ToolPolicy`를 구성한다. 검토자 목록에는 `dev.read_file`만 넣을 수 있다(기존 gateway 규칙). gateway가 돌려준 `effect_kind`가 이 분류와 다르면 `AUTHORIZATION_INVALID`로 거절한다. 따라서 정책 쪽 오분류로 비멱등 효과가 멱등처럼 재시도되지 않는다.

삭제·이름 변경·shell·임의 명령·worker 인자·환경/cwd 변경은 지원하지 않는다. `capabilities()["unsupported"]`에 이 목록이 있다.

## 호출 순서 (fail-closed)

1. **세션**: `ToolSession`이고 workspace가 일치해야 한다(`WORKSPACE_MISMATCH`).
2. **`Invocation` 검사**: `tool_call_id`, `input_revision`(VM plan revision), `authorization_revision`, `effect_id`를 확인한다. effect_id는 read 도구에서 금지이고, 그 외 도구에서는 필수다.
3. **로컬 인자 검사**: 경로, 보호 경로, 내용 크기, digest 형식, 명령 ID를 확인한다. 여기서 실패하면 인가 요청조차 하지 않는다.
4. **재전송 확인**: 같은 `tool_call_id`의 저장 결과나 오류가 있으면 그대로 반환한다. 세션이나 요청 fingerprint가 다르면 `CALL_CONFLICT`다. 이 인스턴스가 보관 중인 다른 `tool_call_id`가 같은 `effect_id`를 쓰고 있으면 `CALL_CONFLICT(subject=effect_id)`로 인가 요청 전에 거절한다.
5. **workspace root 재검증**: `WorkspaceManager.execution_context`가 READY가 아니면 `WORKSPACE_UNAVAILABLE(subject=status)`다.
6. **이전 효과 상태 조회**: 효과가 있는 도구는 gateway에 `effect_state`가 있으면 인가 전에 그 effect의 상태를 읽는다. `intent_recorded`·`receipt_forwarded`·`outcome_unknown`이거나 조회가 실패하면, 이전 시도가 효과를 냈을 수 있는 "재관측" 호출로 취급한다. 같은 인스턴스의 중단 표시가 있어도 마찬가지다.
7. **gateway `request`**: 범위·인가 revision·입력 revision 검사와 효과 도구의 VM intent 기록은 gateway 책임이다. 거부되면 `AUTHORIZATION_DENIED(authority_code=…, escalation=…)`이고 효과는 없다. lease·만료 검사도 gateway 책임으로 가정한다. 시험용 fake는 lease·만료를 검사하지 않으므로 이 부분은 실제 gateway 결합 시 확인이 필요하다(NOT_RUN). `DevTools`는 결정의 `request_digest`·lease·`expires_at`을 대조하지 않는다. 긴 검증 명령 중 lease가 만료되어도 receipt 전에 재확인하지 않는다.
8. **취소 확인**: 효과 전에 취소되면 receipt `cancelled`를 전달하고 `CANCELLED`를 반환한다. 단 재관측 호출에서는 이미 효과가 있었을 수 있으므로 receipt `unknown`과 `EffectUnknown`이다.
9. **효과 실행**.
10. **`forward_receipt`로 관측 전달**: `succeeded`/`failed`/`cancelled`/`unknown`을 전달한다. `receipt_ref`는 파일 digest 또는 검증 `output_digest`다.

ToolCall 인자에는 내용 원문 대신 경로·내용 sha256·크기·expected digest(검증은 command_id·정의 digest)만 넣는다. 따라서 gateway request digest가 실제 요청과 결속된다.

### 인가 대상(target) 형식

| 대상 | 형식 |
| --- | --- |
| 파일 | `workspace://<ws>/files/<segment>/…` |
| 검증 | `verification://<ws>/<command_id>` |

gateway의 target 문법은 ASCII만 허용한다. 그래서 `[A-Za-z0-9][A-Za-z0-9._-]*`가 아닌 구성요소(한글·공백·`:` 포함)는 `u.<UTF-8 hex>`로 바꾼다. 범위 prefix는 `files_scope_target(ws, "docs")`처럼 같은 helper로 만든다.

target이 256자를 넘으면 `INVALID_REQUEST`다. 깊거나 긴 한글 경로는 이 한도에 걸릴 수 있다.

## 경로 규칙

- `normalize_input_path`(aik_workspace)를 통과한 상대경로만 허용한다. 다음은 `PATH_REJECTED`다.
  - 절대경로, 드라이브 표기, `..`, 빈 구성요소, 역슬래시
  - `.git`, Windows 장치 이름, 제어문자
- 각 구성요소는 로컬 구성요소 검사로 확인한다. symlink·reparse point·특수 파일은 `PATH_UNSAFE`다. 읽기는 `read_verified`(O_NOFOLLOW, root 밖 해석 거절)를 사용한다.
- `update_file`은 대상 파일의 링크 수가 1이 아니면(hardlink 별칭) `PATH_UNSAFE`로 거절한다. `read_file`의 hardlink 처리는 `read_verified`의 동작을 따른다.
- 보호 경로는 읽을 수만 있다. 생성·수정은 `PATH_PROTECTED`다. 비교는 모든 OS에서 대소문자를 무시하고 하위 경로까지 포함한다. 보호 경로는 다음 세 가지다.
  - `analysis_only_paths`
  - 검증 명령의 oracle 파일
  - 검증 명령의 `protected_paths`
- 크기 한도(`max_file_bytes`, 기본 4 MiB)를 넘는 내용이나 파일은 `TOO_LARGE`다.

## 파일 효과와 stale 검사

| 도구 | 동작 |
| --- | --- |
| `read_file(…, expected_digest=None)` | 실제 bytes와 sha256을 반환한다. expected digest가 다르면 `STALE_INPUT`이다. |
| `create_file` | 같은 디렉터리의 임시 파일에 쓰고 fsync한 뒤 `os.link`로 원자적 비덮어쓰기 게시를 한다. 이미 같은 bytes가 있으면 `already_applied`, 다른 bytes면 `ALREADY_EXISTS`다. |
| `update_file(…, expected_digest=)` | compare-and-swap이다. 현재 digest가 expected와 다르면 `STALE_INPUT`이고 쓰지 않는다. 이미 새 bytes라면 `already_applied`다. 임시 파일 → 재확인 → `os.replace` → 디렉터리 fsync 순서로 진행한다. |

성공 판정은 게시 후 실제 파일을 다시 읽어 digest를 대조한 결과다. 대조에 실패하면 receipt `unknown`과 `EffectUnknown`이다.

create/update는 관측 전에 `.aik-devtools-*.tmp` 중 대상과 같은 파일(같은 inode)인 잔여 임시 파일을 정리한다. `os.link` 직후·임시 파일 삭제 전에 프로세스가 죽은 경우를 위한 것이다. 정리에 실패하면 receipt `unknown`이다. 대상과 다른 inode의 잔여 임시 파일(예: `os.replace` 전 중단)은 정리하지 않는다.

## 재전송·중단·unknown

- **같은 `tool_call_id` 재전송**
  - create/update/검증의 성공 결과는 `replayed=True`로 그대로 돌려준다. 효과도 gateway 요청도 다시 실행하지 않는다.
  - read 결과는 메모리 상한을 위해 내용을 보관하지 않고 digest만 보관한다. 재전송 시 다시 인가받고 파일을 다시 읽는다. digest가 다르면 `STALE_INPUT`이다.
  - 저장되는 실패는 효과 전 거절(`_Refused` 계열: 경로·stale·크기·명령 불가 등)과 효과 전 취소뿐이다. 이 오류는 그대로 다시 돌려준다.
  - 인가 거절(`AUTHORIZATION_DENIED`/`AUTHORIZATION_INVALID`)과 `EffectUnknown`은 저장하지 않는다. 같은 `tool_call_id`를 다시 보내면 gateway `request`를 다시 호출하고, 그 결과에 따라 멱등 도구는 파일 관측을 다시 수행한다.
- **보관 상한**
  - 기본 `max_entries=4096`, `max_stored_bytes=64 MiB`(검증 stdout/stderr 문자열의 UTF-8 크기 합)이며 생성자 인자로 바꿀 수 있다.
  - 초과하면 오래된 종료 entry부터 축출한다. 중단 표시는 축출하지 않는다. 한 결과가 바이트 상한보다 크면 반환 직후 축출된다.
  - 축출된 결과는 `verify_result`가 거짓이 된다. 축출된 `tool_call_id`를 재전송하면 gateway 경로로 간다. 비멱등 명령은 gateway의 `EFFECT_ALREADY_AUTHORIZED`로 `EffectUnknown`이 되고 재실행하지 않는다. 멱등 도구는 다시 관측하며, 그 사이 파일이 바뀌어 거절이 관측되면 `unknown`이다.
  - effect_id 1:1 검사도 보관 중인 entry에 대해서만 한다.
- **효과 후 receipt 전 중단(파일)**
  - 같은 인스턴스나 재시작한 인스턴스에서 같은 요청을 다시 보내면 gateway가 멱등 결정을 재반환한다.
  - 실제 파일 digest가 이미 새 bytes이면 `already_applied`이고, 쓰기 없이 receipt `succeeded`를 전달한다. 시험에서 inode 불변으로 확인했다.
  - 그 사이 다른 호출이 파일을 바꿔 재관측이 `STALE_INPUT`·`ALREADY_EXISTS` 같은 거절이 되면, "효과 없음(`failed`)"으로 보고하지 않는다. 대신 receipt `unknown`과 `EffectUnknown(subject=reobserved)`이다.
- **효과 후 중단(검증 명령)**
  - 같은 인스턴스는 중단 표시로 `EffectUnknown`을 반환한다.
  - 재시작 인스턴스에서는 gateway가 `EFFECT_ALREADY_AUTHORIZED`를 반환한다. 이때 `effect_state`가 `intent_recorded`(receipt 없음)이면 receipt `unknown`을 전달하고 `EffectUnknown`을 반환한다. 이후에는 gateway의 `RECONCILIATION_REQUIRED`(VM `read_effect`로만 해제)를 따른다.
  - **비멱등 효과는 자동 재실행하지 않는다.** 다시 실행하려면 새 tool_call/effect ID로 새 인가를 받아야 한다.
- **intent 결과 불명확**(`EffectOutcomeUnknown`): 효과를 실행하지 않고 `EffectUnknown`을 반환한다.
- **receipt 전달 실패**: 효과 결과는 보존한다. 결과의 `receipt.forwarded=False`와 `reconciliation_required=True`로 표시한다.

새 Task·재시도 엔진은 없다. 재시도·취소·unknown의 최종 판단은 기존 gateway/VM 권위를 따른다.

## 검증 명령

`VerificationCommand(command_id, argv, timeout_seconds, max_output_bytes, oracle_files, protected_paths=())`는 신뢰된 backend가 정책에 고정한다. worker는 `run_verification(session, invocation, command_id)`로 ID만 고른다.

**실행 조건**

- `argv`는 tuple이고 실행 파일은 절대경로여야 한다. PATH 탐색을 하지 않는다.
- shell(`sh`, `bash`, `cmd`, `powershell`, `pwsh`, `env` 등)과 `.bat`/`.cmd`/`.ps1` 등 script는 `UNSUPPORTED_CAPABILITY`로 정책 생성 단계에서 거절한다.
- 실행 파일(`argv[0]`)이 workspace root 안에 있으면 실행 시점에 `COMMAND_NOT_ALLOWED(subject=argv)`로 거절하고 실행하지 않는다(receipt `failed`). 판정 기준은 다음 세 가지다. 따라서 프로젝트 안 `.venv`의 인터프리터는 사용할 수 없고, root 밖 인터프리터를 정책에 지정해야 한다.
  - 지정 경로
  - 부모 디렉터리의 실제 경로
  - 실제 경로
- `shell=False`로 실행한다.
- `cwd`는 workspace canonical root, stdin은 DEVNULL이다.
- 환경은 PATH·임시 폴더·locale 등 허용 목록만 전달한다. 나머지 변수(비밀 포함)는 전달하지 않는다.
- `PYTHONSAFEPATH=1`을 설정한다(Python 3.11+). Python 자식은 cwd(root)를 `sys.path` 앞에 넣지 않으므로, root에 만든 `pathlib.py` 같은 shadow 모듈이 판정 코드의 import를 가로채지 못한다. Python이 아닌 실행 파일에는 효과가 없다.
- POSIX에서는 새 session을 만든다. timeout·취소 시 process group 전체를 SIGKILL하고, 정상 종료 후에도 남은 자손을 정리하려고 group SIGKILL을 시도한 뒤 oracle을 재검사한다.

**결과 필드**: `exit_code`, `stdout`/`stderr`(한도까지 UTF-8 해석), `*_bytes`(전체 크기), `*_truncated`, `output_limit_exceeded`, `timed_out`, `duration_ms`, `output_digest`.

receipt 상태는 실행 관측이다(완료 → `succeeded`, timeout → `failed`, 실행 중 취소 → `cancelled`). 검증 통과 판정과는 별개다. **주의:** timeout·실행 중 취소의 `failed`/`cancelled`는 프로세스가 시작된 뒤의 결과이므로 일부 부작용이 이미 있었을 수 있다. "효과 없음"을 뜻하지 않는다. VM이 이 상태값을 어떻게 해석하는지는 이 패키지가 확인하지 않았다(NOT_RUN). 실행 전 거절(`oracle_mismatch`, `COMMAND_NOT_ALLOWED`, `COMMAND_UNAVAILABLE`)의 `failed`만 명령 미실행이다.

### 자기 성공 방지 (완료 기준 4 — 부분 충족)

이 패키지가 보장하는 범위는 **열거된 oracle의 변조 탐지**와 **worker 도구로 보호 경로·실행 파일을 바꾸는 경로의 차단**뿐이다. 완료 기준 4를 충족했다고 판정하지 않는다.

- `passed`는 실제 종료 코드 0이면서 oracle 파일 digest가 실행 전후 모두 정책 고정값과 같을 때만 참이다.
- oracle이 실행 전에 바뀌었으면 명령을 실행하지 않는다(`oracle_mismatch`, receipt `failed`). 실행 중에 바뀌었으면 `oracle_changed`이고 `passed=False`다.
- worker는 도구로 oracle·분석 전용 경로·명령의 `protected_paths`를 수정할 수 없다. 명령 정의(argv·한도·보호 경로)도 바꿀 수 없다. 정의 digest가 요청 인자에 들어가므로 다른 정의로 같은 tool_call을 재사용하면 충돌이다. `protected_paths`는 비어 있지 않을 때만 정의 digest에 포함되므로 기존 정의의 digest는 그대로다.
- `verify_result(result)`는 이 인스턴스가 실제로 만들어 아직 보관 중인 결과일 때만 참이다. 모델이 설명하거나 조작해 만든 `VerificationResult`는 거짓이다.

**남는 한계**

- 판정은 worker가 쓴 코드를 실행해 얻는다. 판정에 관여하는 파일(test runner 설정 `conftest.py`, 시험 보조 모듈, argv에 적힌 script, unittest discover가 `sys.path`에 넣는 디렉터리의 모듈 등)을 정책 작성자가 `oracle_files`나 `protected_paths`에 열거하지 않으면 worker가 그 파일을 바꿔 결과에 개입할 수 있다.
- `protected_paths`는 도구 쓰기만 막고 내용을 고정하지 않는다. 내용 고정이 필요하면 `oracle_files`에 digest로 넣어야 한다.
- native worker는 도구 밖에서 OS 권한으로 이 파일들을 수정할 수 있다(아래 표).

## native 실제 권한과 API 제한 도구의 보장 범위

| 경로 | 보장 |
| --- | --- |
| API 제한 도구(이 패키지) | 고정 도구 목록·인자, root 안 경로, CAS, 인가·intent·receipt, 고정 검증 명령. 이 범위는 **worker가 이 도구만 사용할 때** 성립한다. |
| native worker(동일 Windows 사용자 프로세스) | OS 권한이 실제 권한이다. root 밖 파일, 임의 명령, oracle·보호 파일 직접 수정이 OS 차원에서 가능하다. 이 패키지는 그것을 막지 않는다. pinned oracle digest가 열거된 oracle의 변경을 **탐지**해 성공 판정을 막을 뿐이다. |

검증 명령 자체(예: 정책의 Python 인터프리터)는 OS 권한으로 실행된다. cwd 결속은 실행 위치일 뿐 격리가 아니다. OS principal 격리·sandbox는 범위 밖이다.

## 시험

```bash
python -m unittest tests.test_devtools -v
```

**사용한 것**

- 임시 디렉터리의 한글·공백 경로 workspace(실제 `WorkspaceManager` + fake VM project port)
- 실제 파일 연산과 실제 `python -c` 자식 프로세스
- fake gateway: `aik_authority`의 실제 타입을 반환하고 scope·revision·intent·receipt·재전송 규칙을 흉내 냄. lease·만료·effect_id 재사용은 검사하지 않는다.

**확인 대상(시험 항목)**

- read/create/update와 독립 검증 명령의 종료 코드·cwd
- CAS·stale 거절, 경로 탈출·symlink·hardlink 대상 쓰기 거절
- 인가 밖 도구·대상·stale 입력 revision의 효과 전 차단, 조작된 결정(`AUTHORIZATION_INVALID`)
- 재전송 비재실행, read 재전송의 재읽기·stale 거절, 보관 상한과 축출 후 비재실행
- 효과 직후 중단(in-process 예외 주입)의 관측/unknown 처리
  - 중단 뒤 다른 변경이 있으면 `failed` 대신 `unknown`
  - link·unlink 사이 중단 잔여물 정리
- 출력 잘림·timeout·실행 중 취소, 정상 종료 후 자손 프로세스 정리(POSIX)
- shell·script 정책 거절, root 안 실행 파일 거절, shadow 모듈 무력화, oracle·`protected_paths` 보호·변조 탐지
- effect_id 재사용 로컬 거절, receipt 전달 실패 표시, 크기 한도
- 효과 분류와 gateway 정책의 정합, intent unknown·취소·workspace 불가 시 효과 없음

이 문서 작성 시점의 실행 결과 기록은 없다.

## 한계·미검증(NOT_RUN)

- 실제 `AuthorityGateway`·`ExecutionDecision`·`TaskExecutionBinding`과의 결합 시험은 하지 않았다. gateway 계약을 흉내 낸 fake만 사용했다. 실제 gateway의 `effect_state` 상태값이 `intent_recorded`·`receipt_forwarded`·`outcome_unknown`과 같다는 것은 가정이다.
- VM 효과 권위 extension은 aik_authority 문서대로 가정된 seam이다.
- 재전송 결과와 중단 표시는 프로세스 메모리에만 있다. 실제 프로세스 종료 뒤에는 gateway/VM 상태와 실제 파일 관측에 의존한다. 중단은 실제 kill이 아니라 예외 주입으로 재현했다.
- 모든 호출이 하나의 잠금으로 직렬화된다. 긴 검증 명령 동안 다른 도구 호출은 대기한다.
- update의 재확인과 `os.replace` 사이에는 짧은 경쟁 창이 남는다. 같은 OS 사용자의 다른 프로세스가 그 사이 파일을 바꾸면 게시 후 대조에서 `unknown`이 된다.
- `create_file`은 hardlink를 지원하는 파일시스템이 필요하다. 지원하지 않으면 `WRITE_FAILED`다. 중간 디렉터리는 효과 실패 시에도 남을 수 있다.
- 자식 출력은 OS 임시 파일에 기록되며 디스크 사용량을 제한하지 않는다. 반환만 한도로 자른다.
- Windows에서는 `proc.kill()`이 자식의 자식을 종료하지 않을 수 있다. 정상 종료 후 자손 정리도 POSIX에서만 수행한다. Windows의 다음 처리는 미검증이다.
  - 프로세스 종료
  - `os.link`
  - 권한 비트
  - reparse point
  - 링크 수
- receipt를 이미 전달한 뒤 결과만 잃은 경우(축출 포함), 재전송된 멱등 도구는 receipt를 한 번 더 전달할 수 있다. VM 쪽 receipt 멱등성을 가정한다.
- Linux 통과는 개발 검증이며 Windows 지원 증거가 아니다.
