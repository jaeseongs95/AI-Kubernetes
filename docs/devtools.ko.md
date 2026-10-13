# AIK-085 — 개발 worker 파일·검증 도구와 효과 계약 (부분 구현)

상태: **구현과 합성 fixture 단위시험까지.** 다음은 모두 **미검증(NOT_RUN)**이다.

- 실제 `AuthorityGateway`와 VM 효과 권위 extension 연동
- Windows 실행
- 실제 개발 worker(native/API)와의 연결
- 전체 live governed 경로(AIK-086)

이 문서와 시험 통과를 qualified 또는 완료 판정으로 해석하지 않는다. `planning/` 장부의 AIK-085 상태와 완료 판정은 바꾸지 않았다.

## 구성과 재사용

`src/aik_devtools/`는 Python 표준 라이브러리와 아래 기존 패키지만 import한다. 기존 파일은 수정하지 않았다.

| 기존 패키지 | 재사용 대상 |
| --- | --- |
| `aik_authority` | `ToolSession`, `ToolCall`, `ToolCallAuthorization`, `EffectKind`, `ToolDeclaration`, `EffectReceipt`. gateway의 `request`(VM intent)·`forward_receipt`(VM receipt)·`effect_state` |
| `aik_workspace` | `execution_context`로 canonical root 재검증, `normalize_input_path`, `read_verified`, `sha256_digest` |
| `aik_runtime` | 경로 구성요소 검사(`_inspect`), 디렉터리 fsync, 식별자 규칙 |
| `aik_vm_bridge` | digest 형식(`DIGEST_PATTERN`) |

| 모듈 | 역할 |
| --- | --- |
| `policy.py` | 고정 도구 목록, 효과 분류·계약, 신뢰된 검증 정책(`DevToolPolicy`, `VerificationCommand`) |
| `tools.py` | `DevTools`: 인가 → 효과 → receipt, 재전송 저장, 파일 연산, 검증 명령 실행 |
| `errors.py` | 고정 코드 오류(`DevToolError`, `EffectUnknown`, `InvalidDevToolPolicy`) |

## 도구와 효과 분류

| 도구 | 효과 종류 | 재전송 | 효과 직후 중단 | 취소 |
| --- | --- | --- | --- | --- |
| `dev.read_file` | `read_only` | 저장된 결과 반환 | 재인가 후 다시 읽음 | 효과 없음 |
| `dev.create_file` | `idempotent` | 저장된 결과 반환 | 실제 파일 digest 관측 | 효과 전에만 |
| `dev.update_file` | `idempotent` | 저장된 결과 반환 | 실제 파일 digest 관측 | 효과 전에만 |
| `dev.run_verification` | `non_idempotent` | 저장된 결과 반환 | `EffectUnknown`, 재실행 금지 | process group 종료 |

`tool_declarations()`는 이 분류로 `aik_authority.ToolDeclaration`을 만든다. 신뢰된 backend는 이것으로 gateway `ToolPolicy`를 구성한다. 검토자 목록에는 `dev.read_file`만 넣을 수 있다(기존 gateway 규칙). gateway가 돌려준 `effect_kind`가 이 분류와 다르면 `AUTHORIZATION_INVALID`로 거절한다. 따라서 정책 쪽 오분류로 비멱등 효과가 멱등처럼 재시도되지 않는다.

삭제·이름 변경·shell·임의 명령·worker 인자·환경/cwd 변경은 지원하지 않는다. `capabilities()["unsupported"]`에 이 목록이 있다.

## 호출 순서 (fail-closed)

1. **세션**: `ToolSession`이고 workspace가 일치해야 한다(`WORKSPACE_MISMATCH`).
2. **`Invocation` 검사**: `tool_call_id`, `input_revision`(VM plan revision), `authorization_revision`, `effect_id`를 확인한다. effect_id는 read 도구에서 금지이고, 그 외 도구에서는 필수다.
3. **로컬 인자 검사**: 경로, 보호 경로, 내용 크기, digest 형식, 명령 ID를 확인한다. 여기서 실패하면 인가 요청조차 하지 않는다.
4. **재전송 확인**: 같은 `tool_call_id`의 저장 결과나 오류가 있으면 그대로 반환한다. 세션이나 요청 fingerprint가 다르면 `CALL_CONFLICT`다.
5. **workspace root 재검증**: `WorkspaceManager.execution_context`가 READY가 아니면 `WORKSPACE_UNAVAILABLE(subject=status)`다.
6. **gateway `request`**: 범위·인가 revision·입력 revision·lease·만료를 검사하고, 효과가 있는 도구는 VM intent를 기록한다. 거부되면 `AUTHORIZATION_DENIED(authority_code=…, escalation=…)`이고 효과는 없다.
7. **취소 확인**: 효과 전에 취소되면 receipt `cancelled`를 전달하고 `CANCELLED`를 반환한다.
8. **효과 실행**.
9. **`forward_receipt`로 관측 전달**: `succeeded`/`failed`/`cancelled`/`unknown`을 전달한다. `receipt_ref`는 파일 digest 또는 검증 `output_digest`다.

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
- 각 구성요소는 `aik_runtime.paths._inspect`로 검사한다. symlink·reparse point·특수 파일·hardlink 별칭은 `PATH_UNSAFE`다. 읽기는 `read_verified`(O_NOFOLLOW, root 밖 해석 거절)를 사용한다.
- 보호 경로(`analysis_only_paths`와 검증 명령의 oracle 파일)는 읽을 수만 있다. 생성·수정은 `PATH_PROTECTED`다. 비교는 모든 OS에서 대소문자를 무시한다.
- 크기 한도(`max_file_bytes`, 기본 4 MiB)를 넘는 내용이나 파일은 `TOO_LARGE`다.

## 파일 효과와 stale 검사

| 도구 | 동작 |
| --- | --- |
| `read_file(…, expected_digest=None)` | 실제 bytes와 sha256을 반환한다. expected digest가 다르면 `STALE_INPUT`이다. |
| `create_file` | 같은 디렉터리의 임시 파일에 쓰고 fsync한 뒤 `os.link`로 원자적 비덮어쓰기 게시를 한다. 이미 같은 bytes가 있으면 `already_applied`, 다른 bytes면 `ALREADY_EXISTS`다. |
| `update_file(…, expected_digest=)` | compare-and-swap이다. 현재 digest가 expected와 다르면 `STALE_INPUT`이고 쓰지 않는다. 이미 새 bytes라면 `already_applied`다. 임시 파일 → 재확인 → `os.replace` → 디렉터리 fsync 순서로 진행한다. |

성공 판정은 게시 후 실제 파일을 다시 읽어 digest를 대조한 결과다. 대조에 실패하면 receipt `unknown`과 `EffectUnknown`이다.

## 재전송·중단·unknown

- **같은 `tool_call_id` 재전송**: 저장된 결과를 `replayed=True`로 돌려준다. 실패도 저장된 오류를 그대로 돌려준다. 효과도 gateway 요청도 다시 실행하지 않는다.
- **효과 후 receipt 전 중단(파일)**:
  - 같은 인스턴스나 재시작한 인스턴스에서 같은 요청을 다시 보내면 gateway가 멱등 결정을 재반환한다.
  - 실제 파일 digest가 이미 새 bytes이면 `already_applied`이고, 쓰기 없이 receipt `succeeded`를 전달한다. 시험에서 inode 불변으로 확인했다.
- **효과 후 중단(검증 명령)**:
  - 같은 인스턴스는 중단 표시로 `EffectUnknown`을 반환한다.
  - 재시작 인스턴스에서는 gateway가 `EFFECT_ALREADY_AUTHORIZED`를 반환한다. 이때 `effect_state`가 `intent_recorded`(receipt 없음)이면 receipt `unknown`을 전달하고 `EffectUnknown`을 반환한다. 이후에는 gateway의 `RECONCILIATION_REQUIRED`(VM `read_effect`로만 해제)를 따른다.
  - **비멱등 효과는 자동 재실행하지 않는다.** 다시 실행하려면 새 tool_call/effect ID로 새 인가를 받아야 한다.
- **intent 결과 불명확**(`EffectOutcomeUnknown`): 효과를 실행하지 않고 `EffectUnknown`을 반환한다.
- **receipt 전달 실패**: 효과 결과는 보존한다. 결과의 `receipt.forwarded=False`와 `reconciliation_required=True`로 표시한다.

새 Task·재시도 엔진은 없다. 재시도·취소·unknown의 최종 판단은 기존 gateway/VM 권위를 따른다.

## 검증 명령

`VerificationCommand(command_id, argv, timeout_seconds, max_output_bytes, oracle_files)`는 신뢰된 backend가 정책에 고정한다. worker는 `run_verification(session, invocation, command_id)`로 ID만 고른다.

**실행 조건**

- `argv`는 tuple이고 실행 파일은 절대경로여야 한다. PATH 탐색을 하지 않는다.
- shell(`sh`, `bash`, `cmd`, `powershell`, `pwsh`, `env` 등)과 `.bat`/`.cmd`/`.ps1` 등 script는 `UNSUPPORTED_CAPABILITY`로 정책 생성 단계에서 거절한다.
- `shell=False`로 실행한다.
- `cwd`는 workspace canonical root, stdin은 DEVNULL이다.
- 환경은 PATH·임시 폴더·locale 등 허용 목록만 전달한다. 나머지 변수(비밀 포함)는 전달하지 않는다.
- POSIX에서는 새 session을 만들어 timeout 시 process group 전체를 SIGKILL한다.

**결과 필드**: `exit_code`, `stdout`/`stderr`(한도까지 UTF-8 해석), `*_bytes`(전체 크기), `*_truncated`, `output_limit_exceeded`, `timed_out`, `duration_ms`, `output_digest`.

receipt 상태는 실행 관측이다(완료 → `succeeded`, timeout → `failed`, 실행 중 취소 → `cancelled`). 검증 통과 판정과는 별개다.

### 자기 성공 방지 (완료 기준 4)

- `passed`는 실제 종료 코드 0이면서 oracle 파일 digest가 실행 전후 모두 정책 고정값과 같을 때만 참이다.
- oracle이 실행 전에 바뀌었으면 명령을 실행하지 않는다(`oracle_mismatch`, receipt `failed`). 실행 중에 바뀌었으면 `oracle_changed`이고 `passed=False`다.
- worker는 도구로 oracle·분석 전용 경로를 수정할 수 없다. 명령 정의(argv·한도)도 바꿀 수 없다. 정의 digest가 요청 인자에 들어가므로 다른 정의로 같은 tool_call을 재사용하면 충돌이다.
- `verify_result(result)`는 이 인스턴스가 실제로 만든 결과일 때만 참이다. 모델이 설명하거나 조작해 만든 `VerificationResult`는 거짓이다.

## native 실제 권한과 API 제한 도구의 보장 범위

| 경로 | 보장 |
| --- | --- |
| API 제한 도구(이 패키지) | 고정 도구 목록·인자, root 안 경로, CAS, 인가·intent·receipt, 고정 검증 명령. 이 범위는 **worker가 이 도구만 사용할 때** 성립한다. |
| native worker(동일 Windows 사용자 프로세스) | OS 권한이 실제 권한이다. root 밖 파일, 임의 명령, oracle 파일 직접 수정이 OS 차원에서 가능하다. 이 패키지는 그것을 막지 않는다. pinned oracle digest가 변경을 **탐지**해 성공 판정을 막을 뿐이다. |

검증 명령 자체(예: 정책의 Python 인터프리터)는 OS 권한으로 실행된다. cwd 결속은 실행 위치일 뿐 격리가 아니다. OS principal 격리·sandbox는 범위 밖이다.

## 시험

```bash
python -m unittest tests.test_devtools -v
```

**사용한 것**

- 임시 디렉터리의 한글·공백 경로 workspace(실제 `WorkspaceManager` + fake VM project port)
- 실제 파일 연산과 실제 `python -c` 자식 프로세스
- fake gateway: `aik_authority`의 실제 타입을 반환하고 scope·revision·intent·receipt·재전송 규칙을 흉내 냄

**확인한 것**

- read/create/update와 독립 검증 명령의 종료 코드·cwd
- CAS·stale 거절, 경로 탈출·symlink 거절
- 인가 밖 도구·대상·stale 입력 revision의 효과 전 차단
- 재전송 비재실행, 효과 직후 중단(in-process 예외 주입)의 관측/unknown 처리
- 출력 잘림·timeout, shell 정책 거절, oracle 보호·변조 탐지
- 효과 분류와 gateway 정책의 정합, intent unknown·취소·workspace 불가 시 효과 없음

이 문서 작성 시점의 실행 결과 기록은 없다.

## 한계·미검증(NOT_RUN)

- 실제 `AuthorityGateway`·`ExecutionDecision`·`TaskExecutionBinding`과의 결합 시험은 하지 않았다. gateway 계약을 흉내 낸 fake만 사용했다.
- VM 효과 권위 extension은 aik_authority 문서대로 가정된 seam이다.
- 재전송 결과와 중단 표시는 프로세스 메모리에만 있다. 실제 프로세스 종료 뒤에는 gateway/VM 상태와 실제 파일 관측에 의존한다. 중단은 실제 kill이 아니라 예외 주입으로 재현했다.
- 모든 호출이 하나의 잠금으로 직렬화된다. 긴 검증 명령 동안 다른 도구 호출은 대기한다.
- update의 재확인과 `os.replace` 사이에는 짧은 경쟁 창이 남는다. 같은 OS 사용자의 다른 프로세스가 그 사이 파일을 바꾸면 게시 후 대조에서 `unknown`이 된다.
- `create_file`은 hardlink를 지원하는 파일시스템이 필요하다. 지원하지 않으면 `WRITE_FAILED`다. 중간 디렉터리는 효과 실패 시에도 남을 수 있다.
- 자식 출력은 OS 임시 파일에 기록되며 디스크 사용량을 제한하지 않는다. 반환만 한도로 자른다.
- Windows에서는 `proc.kill()`이 자식의 자식을 종료하지 않을 수 있다. Windows의 프로세스 종료, `os.link`, 권한 비트, reparse point 처리는 미검증이다.
- receipt를 이미 전달한 뒤 결과만 잃은 경우, 재전송된 멱등 도구는 receipt를 한 번 더 전달할 수 있다. VM 쪽 receipt 멱등성을 가정한다.
- Linux 통과는 개발 검증이며 Windows 지원 증거가 아니다.
