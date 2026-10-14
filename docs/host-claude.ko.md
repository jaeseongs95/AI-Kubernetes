# AIK-015 — Claude Code 구독 호스트 인증 bridge (구현·fixture 계약 검증)

상태 요약:
- bridge 코드: **구현됨**(`implementation_state=implemented`)
- route qualification: **`NOT_RUN`**
- 실제 Claude Code 호스트 왕복: **`NOT_RUN`**

이 문서는 `src/aik_host_claude/`의 설계와 한계를 설명한다. 장부(`planning/`)의 AIK-015 상태와 완료 판정은 이 문서에서 바꾸지 않는다. 선행 작업 AIK-008·AIK-011의 장부 상태도 변경하지 않는다.

## 1. 범위

사용자는 공식 Claude Code 호스트(CLI)에서 직접 로그인한다. bridge는 그 로그인 상태를 **관측만** 하고, AIK-011 공통 계약(`aik_provider.ProviderAdapter`)의 자원 등록·재연결 계약으로 연결한다.

| 구분 | 이 태스크에서 한 것 | 판정 위치 |
|---|---|---|
| bridge 코드 경로 | 공식 인증 시작·상태 확인·실행 요청·해제·재연결, host handle, connection capability | 이 패키지 |
| host protocol fixture | 합성 fake runner로 성공/실패·만료·해제·계정 변경 계약 검증 | `tests/test_host_claude.py` |
| 실제 Windows 인증 | **NOT_RUN** | AIK-022 |
| 사전 probe·runtime 실행 | **NOT_RUN** | AIK-082, AIK-086(governed qualification), AIK-044 |

fixture 통과는 실제 실행 성공이 아니다. 그렇다고 unsupported만 반환하는 stub도 아니다. 다섯 코드 경로는 모두 주입형 runner를 통해 실제 명령 사양(`CommandSpec`)을 만들고 그 출력을 해석한다.

## 2. 구성

| 파일 | 역할 |
|---|---|
| `runner.py` | 아래 항목을 제공한다. 표준 라이브러리 `SubprocessRunner`의 실사용은 **NOT_RUN**이다.<br>- 주입형 `CommandRunner` 프로토콜(`source`, `run`, `launch`)<br>- `CommandSpec`/`CommandResult`<br>- `RunnerTimeout`/`RunnerTerminationUnknown`/`RunnerNotStarted`<br>- 환경 변수 allowlist(`build_environment`)<br>- `SubprocessRunner`(실행 경계의 환경 변수 거절 규칙·실행 파일 해석 제한 포함) |
| `aik_platform/process.py` | OS port(AIK-091). 두 호스트 adapter가 함께 쓴다. 실행 중 출력 상한, 프로세스 트리 종료(POSIX process group / Windows Job Object), 종료 재관측을 담당한다 |
| `protocol.py` | **미검증 가정**인 호스트 명령(`HostCommands`)과 순수 parser, `HostState`, `AuthMethod`, `HostStatus`, `account_fingerprint` |
| `bridge.py` | `ClaudeHostBridge`(ProviderAdapter 구현), `HostCall`, `HostResult`, `ConnectionCapability` |

구조는 `aik_adapter_anthropic`을 따른다. 그 구성은 다음과 같다.
- 주입형 경계
- `prepare` → `start_job` 1회 소비
- 동기 terminal receipt
- `OutputRedactor` 감사 기록
- 고정 `ProviderFault`

## 3. 수명주기

```python
from aik_host_claude import ClaudeHostBridge, HostCall, SubprocessRunner
from aik_provider import GuardedAdapter, JobRequest

bridge = ClaudeHostBridge(SubprocessRunner(), catalog=("설정된-모델-ID",))  # 실제 호스트는 NOT_RUN
bridge.start_login()                     # 미로그인이면 공식 로그인 명령만 띄움(입출력 미수집)
binding = bridge.connect("connection-1", expected_account_fingerprint=선택한_계정)  # 선택 계정 검증
guard = GuardedAdapter(bridge, authorize_dispatch=제품_인가_콜백)
guard.auth_status(binding)               # 공식 CLI 상태 재관측
bridge.prepare("req-1", HostCall("..."))
guard.start_job(JobRequest(binding, "req-1", "route-1", "vm-attempt-1", "설정된-모델-ID"))
bridge.result("req-1")                   # HostResult(source="synthetic_fixture"|"live")
binding2 = bridge.reconnect(binding)     # 같은 계정 재검증 후 revision+1
guard.sign_out(binding2)                 # handle 해제(기본값: 호스트 logout 안 함)
```

| 메서드 | 동작 |
|---|---|
| `probe` | `--version`과 상태 명령을 실행해 `HostStatus`를 만든다(최근 관측으로 보관). |
| `start_login` / `begin_sign_in` | 공식 로그인 명령을 `runner.launch`로 시작만 한다. 로그인 입출력·비밀은 수집하지 않는다.<br>- 이미 바인딩 계정으로 로그인되어 있으면 실행하지 않고 `authenticated`를 반환한다.<br>- 단, 그 binding이 `reconnect_required` 상태이면 `begin_sign_in`은 이를 해제하지 않고 `STALE_CONNECTION`/`reconnect_required`를 낸다(`reconnect()` 필요).<br>- 그 밖에는 `pending`을 반환한다. 완료 여부는 이후 `auth_status` 관측으로만 판단한다.<br>- `SubprocessRunner.launch`는 `CommandSpec.timeout`(로그인 900초)이 지났을 때 직접 자식이 아직 실행 중이면 그 프로세스 트리를 종료한다(POSIX: 새 session의 process group, Windows: 시작 직후 할당한 Job Object — NOT_RUN).<br>- 그 기한 종료를 확인하지 못하면 `SubprocessRunner.launch_unconfirmed` 카운트가 1 증가한다. 확인된 종료나 기한 전 정상 종료는 세지 않는다.<br>- 기한 타이머는 daemon thread다. 부모 프로세스가 기한 전에 끝나면 종료 처리는 일어나지 않는다.<br>- 기한 전에 정상 종료한 로그인 명령이 남긴 프로세스(예: 브라우저)는 종료하지 않는다. |
| `connect` | 다음 조건을 모두 만족할 때만 `ConnectionBinding`을 발급한다: 호스트가 `authenticated`, 구독 방식, 호스트 제공 계정 ID 관측.<br>- `credential_ref`는 무작위 `host-session://claude-code/<hex>` handle이다.<br>- `expected_account_fingerprint`가 주어지면 일치해야 한다. 불일치 시 `ACCOUNT_CHANGED`/`selected_account_mismatch`다.<br>- 생략하면 호스트에 현재 로그인된 계정이 그대로 바인딩되고, capability reason에 `SELECTED_ACCOUNT_UNVERIFIED`가 표시된다. 제품은 사용자가 선택한 계정의 fingerprint를 전달해야 한다. |
| `auth_status` | 상태를 재관측한다. 바인딩 계정과 비교해 `matched`/`mismatched`/`not_signed_in`/`unknown`을 판정한다. `mismatched`일 때 반환값의 `account_fingerprint`는 관측된 다른 계정의 값이며 binding의 값과 다르다. |
| `prepare` + `start_job` | 실행 직전에 상태를 다시 관측한 뒤, `-p --output-format json --model <id>`에 prompt를 **stdin**으로 전달한다(argv·로그에 넣지 않음).<br>- `start_job`이 준비 항목을 꺼낸 시점에 request_id는 소비된다.<br>- 실행 전 거절이어도 재전송하지 않는다. bridge 단독 호출 시 같은 request_id 재시도는 `DUPLICATE_REQUEST`다.<br>- 준비 항목은 최대 1024개(`prepared_capacity`), 준비+소비된 request_id는 최대 65,536개(`request_record_capacity`)다. |
| `observe_job` | 동기 실행이므로 시작 시 terminal이다. 저장된 receipt와 정확히 같고 binding이 일치할 때만 반환한다. |
| `interrupt_job` | `UNSUPPORTED`. 실행 중단은 runner의 절대 timeout으로만 이루어진다. |
| `reconnect` | 같은 계정으로 다시 인증된 경우에만 revision+1 binding을 발급하고 `reconnect_required`를 해제한다.<br>- 다른 계정이면 `ACCOUNT_CHANGED`/`account_switched`다. 조용히 재바인딩하지 않는다.<br>- 같은 handle의 실행이 진행 중이면 `STALE_CONNECTION`/`job_in_flight`다. |
| `sign_out` / `release` | AIK handle을 제거하고 `unauthenticated`를 반환한다.<br>- 같은 handle의 실행이 진행 중이면 `STALE_CONNECTION`/`job_in_flight`로 거절한다.<br>- 공식 호스트 logout은 `release(binding, host_logout=True)` 또는 생성자 `logout_on_release=True`일 때만 실행한다.<br>- logout 진행 중에는 그 handle의 새 실행을 `STALE_CONNECTION`/`release_in_progress`로 거절한다.<br>- logout이 실패하면 handle을 그대로 남긴다. 이후 상태가 로그아웃으로 확인되지 않으면 `UNKNOWN_OUTCOME`/`logout_unconfirmed`다.<br>- 호스트 logout은 같은 OS 사용자의 다른 binding에도 영향을 줄 수 있다. |
| `capability` | `ConnectionCapability`를 반환한다(아래 5절). |

## 4. 비밀 처리와 명령 경계

- bridge는 토큰·세션 파일·키·설정 파일을 **읽지 않는다**. 파일 경로 검사도 하지 않는다. 사용하는 신호는 공식 CLI의 버전·상태 출력뿐이다.
- 구독 인증을 API 키로 재사용하지 않는다.
  - 환경에서 `ANTHROPIC_*`과 `KEY/TOKEN/SECRET/PASSWORD/CREDENTIAL/COOKIE/AUTH`를 포함한 변수는 allowlist를 확장해도 제거한다.
  - `SubprocessRunner`는 같은 거절 규칙을 **실행 경계에서 다시 적용**한다. `build_environment`를 거치지 않고 만든 `CommandSpec`이라도 환경에 `ANTHROPIC_*`·자격 증명처럼 보이는 이름, `=`/NUL을 포함한 이름, NUL을 포함한 값이 있으면 프로세스를 시작하지 않고 `ValueError("ENVIRONMENT_NOT_ALLOWLISTED")`를 낸다(값은 오류에 넣지 않는다).
  - 실행 경계는 거절 규칙만 강제한다. allowlist 구성원 여부는 강제하지 않는다(호출자가 `build_environment`로 allowlist를 확장할 수 있기 때문이다).
  - 상태 출력이 API 키 방식(`authMethod`)이면 `unauthenticated`/`API_KEY_AUTH_NOT_SUBSCRIPTION`으로 실행을 막는다.
- 실행 파일 해석(`SubprocessRunner`):
  - `CommandSpec.env`의 `PATH`로만 해석한다. `PATH`가 없거나 비어 있으면 `RunnerNotStarted`다. 부모 프로세스의 `PATH`나 `os.defpath`(Windows에서는 현재 디렉터리 포함)로 대체하지 않는다.
  - 디렉터리 없는 이름(`claude`)은 해석 결과의 디렉터리가 `PATH`의 **절대 경로 항목** 중 하나와 같아야 한다. Windows의 `shutil.which`가 현재 디렉터리를 먼저 탐색해 cwd의 `claude.exe`를 고르는 경우를 거절하기 위한 것이다(Windows 동작은 NOT_RUN).
  - 디렉터리를 포함한 이름은 절대 경로여야 한다. 상대 경로는 `RunnerNotStarted`다.
- 모든 명령은 `CommandSpec`로 실행하며 다음을 갖는다.
  - **절대 timeout**: 버전 10초, 상태 15초, 로그인 launch 900초, logout 30초, 실행 기본 600초·최대 3600초.
    - `SubprocessRunner.run`은 timeout이 지나면 명령의 프로세스 트리 전체를 종료한다. 로그인 launch는 기한에 직접 자식이 실행 중일 때 트리를 종료한다.
    - POSIX는 `start_new_session=True`로 시작한 process group에 `SIGKILL`을 보낸다.
    - Windows는 `CREATE_NEW_PROCESS_GROUP`으로 시작한 뒤 즉시 전용 Job Object에 할당하고 `TerminateJobObject`로 종료한다. Job 할당에 실패하면 `%SYSTEMROOT%\System32\taskkill.exe /T /F /PID`로 종료를 시도하지만 결과는 항상 미확인이다. Windows 경로는 **NOT_RUN**이다.
    - **정리 유예**: timeout 뒤 트리 종료·재관측·pipe 정리에 최대 약 `4 × GRACE_SECONDS`(20초)가 더 걸릴 수 있다. timeout은 명령의 실행 시간 상한이며, 호출 전체의 벽시계 상한이 아니다.
    - 주입형 runner는 각자 이 값을 적용해야 한다.
  - **출력 상한**: 상태 64KiB, 실행 4MiB.
    - `SubprocessRunner`는 실행 중 stdout·stderr를 pipe로 읽어 각각 상한까지만 메모리에 기록한다. 임시 파일을 쓰지 않는다.
    - 어느 한쪽이라도 상한을 넘으면 즉시 프로세스 트리를 종료하고 `truncated=True`를 반환한다. 이때 `exit_code`는 종료 신호에 따른 값이다.
    - pipe 읽기 오류로 출력 수집이 끝나지 않은 경우는 `truncated`가 아니다. 결과를 반환하지 않고 `RunnerTerminationUnknown`을 낸다.
    - bridge는 `truncated`를 해석 불가로 처리한다. 상태는 `AUTH_STATUS_UNOBSERVED`, 실행은 `malformed_output`이다.
  - **allowlist 환경**: PATH, HOME/USERPROFILE, APPDATA/LOCALAPPDATA, TEMP, LANG, `CLAUDE_CONFIG_DIR` 등.
- 정상 종료 뒤에도 같은 process group(POSIX) 또는 같은 Job Object(Windows, NOT_RUN)에 남은 자손은 종료한다. 남은 자손이 pipe를 붙잡아 결과 수집이 멈추는 것을 막는다.
- 종료 후에는 트리 생존 여부를 재관측한다.
  - POSIX: 직접 자식(group leader)은 group 재관측이 끝난 **뒤에** 회수(reap)한다. 회수되지 않은 zombie가 PID와 PGID를 점유하므로, 신호·재관측 중에 그 번호가 무관한 프로세스에 재할당되지 않는다. 종료 여부는 `waitid(WNOWAIT)`로 회수 없이 관측한다.
  - Linux는 `/proc`(zombie 제외)으로 group을 관측한다. 그 밖의 POSIX는 `killpg(pgid, 0)`을 사용하는데, 회수 전 leader zombie도 group 구성원으로 세므로 결과는 미확인이 된다(지원 대상 OS 아님).
  - leader가 이미 회수된 경우(`waitid` 미지원 플랫폼 — 예: Python 3.12의 macOS — 또는 다른 호출자가 회수)에는 group에 신호를 보내거나 재관측하지 않고 미확인으로 처리한다.
  - Windows: Job Object의 `ActiveProcesses`가 0일 때만 종료 확인이다. Job으로 관측하지 못하면 `taskkill`이 성공해도 미확인이다.
  - 확인할 수 없거나 pipe·stdin 전달이 끝나지 않으면 결과를 반환하지 않는다.
  - 대신 `RunnerTerminationUnknown`(`RunnerTimeout` 하위 클래스)을 낸다. 성공으로 처리하지 않는다.
- prompt는 stdin(별도 쓰기 thread)으로만 전달한다. argv에는 넣지 않는다.
- `CommandSpec.env`·`stdin`, `HostCall.prompt`, `HostResult.text`, `CommandResult` 출력은 `repr`에서 제외한다.
- stderr는 해석·기록하지 않는다.
- runner 예외는 연결하지 않는다(`__context__` 없음).
- 감사 기록은 `aik_security.redaction.OutputRedactor.output("log", ...)`로만 남긴다.
- binding에는 호스트 계정 ID 원문을 보관하지 않는다. `account_fingerprint`만 보관한다.
  - `account_fingerprint`는 **호스트가 제공한 계정 ID**를 도메인 분리 SHA-256으로 변환한 값이다(AIK-011 계약의 64자 hex 형식).
  - label·e-mail·토큰에서 유도하지 않는다.
  - 계정 ID가 `@`를 포함하거나 토큰처럼 보이거나 누락되면 `unknown`/`ACCOUNT_NOT_OBSERVED`로 둔다.

## 5. 상태 구분과 connection capability

`HostStatus`와 `ConnectionCapability`는 아래 상태를 구분한다.

| 관측 | host_state | auth_state | 대표 reason |
|---|---|---|---|
| 실행 파일 없음 | `absent` | `unknown` | `HOST_ABSENT` |
| 버전 해석 불가 / 최소 버전 미만 | `unsupported` | `unknown` | `HOST_VERSION_UNRECOGNIZED` / `HOST_VERSION_UNSUPPORTED` |
| 상태 명령 미지원(종료 코드 2·해석 불가) | `unsupported` | `unknown` | `AUTH_STATUS_UNSUPPORTED` |
| 상태 timeout·잘림·해석 불가 | `present` | `unknown` | `AUTH_STATUS_UNOBSERVED` / `AUTH_STATUS_UNRECOGNIZED` |
| 상태 명령 비영 종료 코드인데 로그인(또는 불명)을 주장 | `present` | `unknown` | `AUTH_STATUS_UNRECOGNIZED` |
| `loggedIn: true`와 `expired: true` 동시 출력 | `present` | `unknown` | `AUTH_STATUS_CONTRADICTORY` |
| 인증 만료 | `present` | `expired` | `AUTH_EXPIRED` |
| 미로그인 | `present` | `unauthenticated` | `AUTH_REQUIRED` |
| API 키 방식 | `present` | `unauthenticated` | `API_KEY_AUTH_NOT_SUBSCRIPTION` |
| 구독 로그인 + 계정 ID (종료 코드 0) | `present` | `authenticated` | — |

비영 종료 코드는 `authenticated`의 근거가 되지 않는다. 미로그인·만료 출력은 실행을 막는 방향으로만 쓰이므로, 비영 종료 코드여도 그대로 사용한다. 종료 코드 값 자체는 미검증 가정이다.

`ConnectionCapability`는 **설정 catalog와 runtime 실측을 분리**한다.

- `configured_models`: 생성자 `catalog`(설정값). 실행 요청의 모델은 여기에 있어야 한다. 없으면 `INVALID_CONTRACT`/`model_not_configured`이며 호스트를 실행하지 않는다.
- `observed_models`: 실제 실행 결과에서 호스트가 보고한 모델이다.
  - **binding의 계정 fingerprint별로** 분리해 보관한다. 다른 계정 binding에는 표시되지 않는다.
  - 요청 모델로 대체하지 않는다.
  - `observation_source`가 `synthetic_fixture`이면 측정값이 아니다.
- `account_binding`: `matched` / `mismatched` / `not_signed_in` / `unknown`.
- `SELECTED_ACCOUNT_UNVERIFIED`: `connect()`에 선택 계정 fingerprint를 주지 않은 binding. 기존 공개 동작을 유지하기 위해 이 reason만으로 `executable`을 바꾸지 않는다.
- `reconnect_required`가 켜지는 경우:
  - 외부 로그인 변경(다른 계정, 로그아웃, 만료)을 관측했을 때
  - 그 뒤에는 원래 계정으로 돌아와도 `STALE_CONNECTION`/`reconnect_required`가 유지된다. `begin_sign_in`이 같은 계정을 관측해도 해제하지 않는다.
- `reconnect_required`가 해제되는 경우:
  - `reconnect()`
  - 같은 binding에 대해 bridge가 시작한 공식 로그인 이후 같은 계정이 관측됐을 때
    - 그 사이 다른 계정이 관측되면 대기 중인 로그인(`LOGIN_PENDING`)은 취소되며 `reconnect()`가 필요하다.
    - 미로그인·만료 관측은 대기 상태를 취소하지 않는다(로그인 진행 중의 정상 관측).
- 계정 격리 상태:
  - 기본 `isolation=single_active`(가정)이며 `ISOLATION_SINGLE_ACTIVE`를 표시한다. 같은 호스트에서 서로 다른 구독 계정의 동시 실행은 `GuardedAdapter`가 차단한다.
  - `unknown`으로 설정하면 `ISOLATION_UNKNOWN`을 표시하고 실행이 불가하다.
- `executable`은 아래 조건을 모두 만족할 때만 `true`다. 등록은 되어도 실행 불가인 경로는 `executable=false`와 reason으로 표시된다.
  - 호스트 `present`
  - 계정 `matched`
  - 재연결 불필요
  - 격리 상태가 `unknown`이 아님
  - catalog가 비어 있지 않음
- `qualification_status`는 항상 `NOT_RUN`이다. `live_evidence_tasks=("AIK-022","AIK-082","AIK-086")`로 실제 근거 위치를 연결한다. 다른 값은 생성자가 거절한다.

`describe()`의 `AdapterCapabilities`는 최근 관측 기준으로 계산한다.
- 호스트가 `absent`/`unsupported`이면 `job_start`·`sign_in`은 `unsupported`다.
- 격리 상태는 별도 필드 `isolation`으로 보고하며 `job_start`에 합쳐 표시하지 않는다. 격리 `unknown`의 실행 불가는 `isolation` 필드와 `capability().executable`로 판단한다.
- `job_interrupt`는 항상 `unsupported`다.
- 상태가 바뀐 뒤에는 `probe()`/`auth_status()`로 갱신해야 한다.

## 6. 오류 매핑

| 상황 | FaultKind / outcome | native_code |
|---|---|---|
| 호스트 실행 파일 없음(연결·로그인·logout) | `UNSUPPORTED` / known | `host_absent` |
| 호스트 버전·상태 프로토콜 미지원 | `UNSUPPORTED` / known | `host_unsupported` |
| 호스트 상태 불명 | `PROVIDER_FAILURE` / known | `host_unknown` |
| 미로그인 / 만료 / API 키 방식 / 계정 미관측 | `AUTH_REQUIRED` / known | `auth_required` / `auth_expired` / `api_key_auth_not_subscription` / `account_not_observed` |
| 다른 계정으로 전환됨 | `ACCOUNT_CHANGED` / known | `account_switched` |
| 선택 계정과 다른 계정 | `ACCOUNT_CHANGED` / known | `selected_account_mismatch` |
| 재연결 필요 / 해제·이전 revision·타 bridge의 binding | `STALE_CONNECTION` / known | `reconnect_required` / — |
| 실행 중인 handle의 해제·재연결 / logout 진행 중 handle의 실행 | `STALE_CONNECTION` / known | `job_in_flight` / `release_in_progress` |
| 실행 timeout | `UNKNOWN_OUTCOME` / unknown | `timeout` |
| runner의 프로세스 트리 종료 미확인 또는 출력 수집 미완료(`RunnerTerminationUnknown`) | `RunnerTimeout` 하위 예외. timeout과 같은 경로로 처리된다(실행: `UNKNOWN_OUTCOME`, 상태: `AUTH_STATUS_UNOBSERVED`) | 실행 시 `timeout` |
| 실행 출력이 다음 중 하나: 해석 불가, 잘림(출력 상한 초과로 트리 종료 포함), 종료 코드와 결과 불일치, 오류 없음인데 성공 subtype 아님 | `UNKNOWN_OUTCOME` / unknown | `malformed_output` |
| 실행 시점 실행 파일 없음(미시작) | `PROVIDER_FAILURE` / known | `host_absent` |
| 로그인 launch 결과 불명 | `UNKNOWN_OUTCOME` / unknown | `login_launch_unknown` |
| 호스트 logout 미확인 / 실패 | `UNKNOWN_OUTCOME` / `PROVIDER_FAILURE` | `logout_unconfirmed` / `logout_failed` |
| 미등록 모델 / 준비 없는 실행 | `INVALID_CONTRACT` / known | `model_not_configured` / `call_not_prepared` |
| 준비 항목 한도 / request 기록 한도 | `PROVIDER_FAILURE` / known | `prepared_capacity` / `request_record_capacity` |
| 격리 불명 상태의 실행 | `ACCOUNT_CONFLICT` / known | `isolation_unknown` |

`RunnerTerminationUnknown` 행은 bridge가 `RunnerTimeout`을 isinstance로 처리한다는 전제다. 이번 변경에서 `bridge.py`는 수정하지 않았다. 출력 수집 미완료는 이전에는 `truncated=True`(실행: `malformed_output`)로 반환되었고, 이번 변경 뒤에는 위 행으로 처리된다. 두 경우 모두 unknown outcome이다.

`SubprocessRunner`의 실행 경계 환경 거절(`ValueError("ENVIRONMENT_NOT_ALLOWLISTED")`)은 bridge가 `build_environment`로 환경을 만드는 한 발생하지 않는다. bridge가 이 예외를 어떤 fault로 바꾸는지는 이번 변경에서 확인하지 않았다.

호스트가 보고한 실행 실패(`is_error=true`)는 known `failed` receipt다. 이때 상태를 다시 관측해 만료·계정 변경을 `reconnect_required`로 반영한다.

`is_error=false`인데 `subtype`이 `success`가 아니거나 없으면, 호스트가 작업을 수행했을 수 있다. 따라서 known 실패로 기록하지 않고 `UNKNOWN_OUTCOME`으로 둔다.

## 7. 미검증 가정 (공식 문서로 확인하지 못함)

| 항목 | 코드의 가정 (`HostCommands`로 교체 가능) |
|---|---|
| 실행 파일 | `claude` (전달한 환경의 PATH 절대 경로 항목에서 검색) |
| 버전 | `claude --version` 출력이 `X.Y.Z`로 시작, 최소 `1.0.0` |
| 상태 확인 | `claude auth status --json` → `{"loggedIn": bool, "authMethod": str, "accountId": str, "expired": bool}`, 로그인 상태는 종료 코드 0 |
| 미지원 상태 명령 | 종료 코드 `2`와 해석 불가 출력 |
| 인증 방식 값 | 구독: `claude.ai`, `subscription` / API 키: `api_key`, `apiKey`, `api-key`, `console` |
| 계정 ID | 호스트가 비민감 불투명 ID를 `accountId`로 제공 |
| 로그인 / 로그아웃 | `claude auth login`(대화형, 브라우저, 900초 이내 완료) / `claude auth logout` |
| 실행 | `claude -p --output-format json --model <id>`, prompt는 stdin<br>출력 `{"type":"result","subtype","is_error","result","session_id","model"}`, 성공 시 `subtype="success"` |
| 격리 | OS 사용자당 하나의 활성 로그인(`single_active`), 실측 아님 |
| 자손 프로세스 | 실행·상태 명령이 종료 뒤에도 유지되어야 하는 자손 프로세스를 같은 process group·Job Object에 남기지 않음(남기면 runner가 종료함) |

위 가정이 실제와 다르면 fixture는 통과해도 실제 호스트 왕복은 실패한다. 확인은 AIK-022/082/086의 단계별 근거로만 한다.

## 8. 검증 상태

```bash
python -m unittest discover -s tests -p 'test_host_claude.py' -v
```

| 항목 | 상태 |
|---|---|
| 합성 fake runner host fixture | 개발용 단위 시험. 이 문서 작성 시점에 실행 결과 기록 없음 |
| `SubprocessRunner` 실제 프로세스 실행 | **NOT_RUN** (Claude Code 대상). 아래 시험은 호스트가 아닌 로컬 Python 자식·손자 프로세스 또는 가짜 process 객체만 사용한다. 실행 결과 기록 없음 |
| ↳ 공통 시험 | 실행 파일 부재 판정, launch 기한 종료, stdin 전달·stderr 상한, 실행 경계의 자격 증명 환경 거절, PATH 절대 항목 밖·PATH 없음·상대 경로 실행 파일 거절, launch 기한 종료 미확인 기록, 출력 수집 미완료의 미확인 분류, launch 프로세스의 트리 추적 호출, Windows 분기의 Job 관측 판정(가짜 process 객체와 mock으로 실행하며 실제 Windows Job Object를 만들지 않음) |
| ↳ POSIX 전용 시험 | 출력 상한 초과 시 즉시 트리 종료, timeout 시 pipe를 붙잡은 손자 종료, 정상 종료 뒤 남은 손자 종료, launch 기한의 트리 종료, 종료 미확인의 `RunnerTerminationUnknown` 분류, leader 회수 전 group 재관측, 이미 회수된 leader의 group 신호 생략, launch 관리 목록이 프로세스를 회수하지 않음 |
| 실제 Claude Code 구독 인증 | **NOT_RUN** (AIK-022) |
| 사전 probe·runtime 실행·제품 통합 | **NOT_RUN** (AIK-082/086/044) |
| Windows 동작 | **NOT_RUN**. 대상: `.cmd` 실행, `CREATE_NEW_CONSOLE`/`CREATE_NO_WINDOW`/`CREATE_NEW_PROCESS_GROUP`, Job Object 할당·종료·`ActiveProcesses` 재관측, `taskkill /T /F` fallback, launch 기한 종료, cwd·`PATHEXT` 해석 |
| route qualification | **NOT_RUN** — bridge는 qualified를 주장하지 않는다 |

Linux 시험 통과는 개발 검증이며 Windows 지원 증거가 아니다. fixture 결과를 실측 증거로 기록하지 않는다.

## 9. 한계

- handle, revision, `reconnect_required`, 소비된 request_id, receipt, 결과, 감사 기록은 프로세스 메모리에만 있다. 재시작 뒤 대조·복구는 AIK-031/033/035/038 범위다.
- 소비된 request_id·receipt는 중복 방어를 위해 삭제하지 않는다.
  - 준비+소비 기록이 65,536개에 이르면 새 `prepare`를 `request_record_capacity`로 거절한다(fail-closed).
  - 계속 사용하려면 bridge를 새로 만들어야 하며, 이때 메모리 중복 방어도 함께 사라진다.
  - 결과(`result`)는 `discard_result`로 지울 수 있지만 기록 한도는 줄지 않는다.
- 관측과 실행 사이에 사용자가 호스트에서 계정을 바꾸는 경합은 막지 못한다(실행 직전 재관측까지만 수행). 실행 중 계정 변경은 실행 실패 후 재관측에서만 반영된다. 같은 bridge 안의 해제·재연결은 실행 중 handle에 대해 거절된다.
- 프로세스 트리 종료 범위는 다음과 같다.
  - POSIX는 같은 process group, Windows는 전용 Job Object 안의 프로세스만 대상이다.
  - `setsid`·새 session으로 빠져나간 자손, Job에서 breakaway한 프로세스는 종료하지 않는다.
  - Windows에서 `Popen` 반환 직후 Job에 할당하기 전에 자식이 만든 프로세스는 Job에 속하지 않으므로 종료·관측 대상이 아니다(`CREATE_SUSPENDED` 재개는 표준 `subprocess`로 지원되지 않음). 이 경합 창은 NOT_RUN이다.
  - Windows에서 Job 할당에 실패하면 `taskkill /T`로 종료를 시도하지만 결과는 항상 미확인(`RunnerTerminationUnknown`)이다.
- 트리 종료를 확인하지 못하면 `RunnerTerminationUnknown`으로 끝난다. 이때 아직 끝나지 않은 읽기·stdin thread와 그 pipe는 정리하지 않고 남긴다. 반복되면 호출마다 thread와 최대 상한만큼의 메모리가 남을 수 있다.
- `waitid`가 없는 POSIX 플랫폼(예: Python 3.12의 macOS)과 `/proc`이 없는 POSIX 플랫폼에서는 트리 종료를 확인하지 못해 `run`이 항상 `RunnerTerminationUnknown`이 된다. 이 플랫폼들은 지원 대상이 아니다.
- POSIX에서 `start_new_session=True`로 launch한 로그인 명령은 제어 터미널을 갖지 않는다. 이 경로는 개발·시험용이며 제품 대상은 Windows다.
- launch 기한 종료의 결과는 `launch_unconfirmed` 카운트로만 남는다. 어느 launch가 미확인인지는 구분하지 않는다. 부모 프로세스가 기한 전에 끝나면 기한 종료는 일어나지 않는다. 로그인 완료는 이후 `auth_status` 관측으로만 판단한다.
- 기한 전에 정상 종료한 로그인 명령이 남긴 프로세스는 종료하지 않는다. 실행·상태·logout 명령(`run`)이 남긴 같은 group·Job의 자손은 종료한다.
- Windows에서 `.cmd` 래퍼 실행은 고정 인자와 정규식으로 검증한 모델 ID만 argv에 넣는 것을 전제로 한다(prompt는 stdin). 실제 동작은 미검증이다.
- Windows에서 `shutil.which`는 확장자 목록(`PATHEXT`)을 부모 프로세스의 환경에서 읽는다(NOT_RUN).
- 대화형 로그인은 화면이 있는 사용자 세션을 가정한다. 서비스·비대화형 환경 동작은 미검증이다.
- `authorize_dispatch`는 제품의 VM/AGS 인가·lease 검증과 연결해야 한다. 시험의 허용 callback은 합성 fixture다.
- 장부의 AIK-015 상태·완료 판정은 변경하지 않았다.
