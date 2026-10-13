# AIK-014 — Codex 구독 호스트 인증 bridge (구현·host fixture 검증)

**상태:** bridge 코드는 구현했다(`implementation_state=implemented`). 실제 호스트 인증·사전 probe·governed 적합성은 모두 `NOT_RUN`이다. 실제 Codex CLI 왕복도 `NOT_RUN`이다.

이 문서는 `src/aik_host_codex/`의 설계와 한계를 설명한다. 장부(`planning/`)의 AIK-014 상태와 완료 판정은 바꾸지 않는다. 선행 AIK-008·AIK-011의 장부 상태도 바꾸지 않는다.

## 1. 범위

사용자가 공식 Codex 호스트(CLI)에서 직접 완료한 로그인을 **관측**한다. 관측 결과는 AIK-011 `ProviderAdapter` 계약(`auth_kind=native_subscription`)으로 변환한다. 실행은 주입된 기존 VM runtime에 위임한다.

| 구분 | 이 태스크에서 한 것 | 판정 위치 |
|---|---|---|
| bridge 코드 경로 | 인증 시작·상태 회수·재연결·해제·취소, 계정 binding revision 대조, VM runtime 위임 | 이 패키지 |
| host protocol fixture | 합성 runner·합성 CLI 출력으로 성공/실패 계약 검증 | `tests/test_host_codex.py` |
| 실제 인증 | **NOT_RUN** | AIK-022 |
| 실행 전 사전 probe | **NOT_RUN** | AIK-082 |
| governed 적합성 | **NOT_RUN** | AIK-086 (실행 통합 AIK-044) |

fixture 통과는 실제 로그인 성공이 아니다. fixture 결과를 실측 증거로 기록하지 않는다.

## 2. 구성

| 파일 | 역할 |
|---|---|
| `runner.py` | 아래 항목을 제공한다. 표준 라이브러리 `SubprocessRunner`의 실사용은 **NOT_RUN**이다.<br>- 주입형 `HostRunner`(`source`, `run`, `spawn`)<br>- `RunResult`<br>- 고정 코드 예외(`ProcessTreeUnconfirmed` 포함)<br>- 환경변수 allowlist<br>- `SubprocessRunner` |
| `_process.py` | 패키지 내부 helper. 실행 중 출력 상한, 프로세스 트리 종료(POSIX process group / Windows Job Object), 종료 재관측을 담당한다 |
| `protocol.py` | 미검증 CLI 가정을 모은 `CodexCliProtocol`. `LoginState`, `LoginMethod`, `ParsedStatus`를 정의한다. 다른 protocol 객체로 교체할 수 있다 |
| `bridge.py` | `CodexHostBridge`(ProviderAdapter 구현), `HostCapability`(connection capability), `HostHandle`, `GUI_COMMANDS` |

## 3. GUI command와 코드 경로

| GUI command | 메서드 | 동작 |
|---|---|---|
| `host.codex.auth.start` | `start_login(connection_id)` / `begin_sign_in(binding)` | 버전을 확인한 뒤 공식 로그인 명령을 `spawn`한다. 출력은 버린다. 상태는 `pending`이다 |
| `host.codex.auth.status` | `probe(binding=None)` / `auth_status(binding)` | `--version`과 `login status`를 실행해 상태를 분리 보고한다 |
| `host.codex.auth.reconnect` | `reconnect(binding)` / `connect(connection_id)` | 관측 계정으로 binding을 확인한다. 변경이 있었으면 revision을 올린다 |
| `host.codex.auth.sign_out` | `sign_out(binding)` | 현재 호스트 계정이 binding과 같은지 확인한 뒤 `logout`을 요청한다. 그 뒤 상태를 관측해 결과를 확인한다 |
| `host.codex.auth.cancel` | `cancel_login()` | 대기 중인 로그인 프로세스의 트리를 종료한다 |

어떤 경로도 unsupported만 반환하는 stub이 아니다.

로그인 프로세스에는 절대 기한(기본 600초, 최대 3600초)이 있다. 기한은 **지연(lazy) 집행**된다.
- 별도 타이머가 없다. `SubprocessRunner.spawn` 자체에는 기한이 없다(아래 §7).
- 기한이 지난 뒤 처음으로 로그인 상태를 확인하는 bridge 호출이 프로세스를 종료하고 상태를 다시 관측한다. 해당 호출은 `probe`, `connect`/`reconnect`, `auth_status`, `start_login`/`begin_sign_in`, `sign_out`이다.
- `probe`는 호스트 버전 확인 결과와 무관하게 기한을 집행한다.
- bridge 호출이 없으면 기한이 지나도 로그인 프로세스는 남아 있다.

`SubprocessRunner`가 반환하는 handle의 `terminate()`는 직접 자식이 실행 중이면 프로세스 트리 전체를 종료한다. POSIX는 group에 `SIGTERM`을 보내고 최대 5초 뒤 `SIGKILL`을 보낸다. Windows는 spawn 직후 할당한 전용 Job Object를 `TerminateJobObject`로 종료하며 **NOT_RUN**이다(Job 할당 실패 시 `taskkill /T /F` fallback, 결과는 항상 미확인). 종료 확인 결과는 handle의 `termination_confirmed`에 남는다.

`describe()`의 capability는 다음과 같다.

- `auth_status`·`sign_in`·`sign_out`: `supported`
- `isolation`: `single_active` (호스트 로그인은 OS 사용자·설정 위치마다 하나라는 가정)
- `job_*`: VM runtime이 주입됐을 때만 `supported`, 아니면 `unsupported`
- `qualification_status`: 항상 `NOT_RUN`

## 4. capability의 분리

`probe()`가 반환하는 `HostCapability`는 아래 항목을 각각 따로 보고한다.

| 필드 | 값 | 의미 |
|---|---|---|
| `presence` | `present` / `absent` / `unknown` | 실행 파일 존재 여부 |
| `protocol` | `supported` / `unsupported` / `unknown` | 버전 출력이 가정한 형식과 맞는지 |
| `login` | `logged_in` / `logged_out` / `expired` / `pending` / `unknown` | 호스트 로그인 상태 |
| `method` | `subscription` / `api_key` / `unknown` | 로그인 방식 |
| `binding` | `bound` / `unbound` / `changed` / `unknown` | 계정 binding 대조 결과 |
| `execution_ready` | bool | 위 조건과 VM runtime 연결이 모두 충족됐는지 |
| `reasons` | 고정 코드 tuple | 아래 목록 참조 |
| `implementation_state` | `implemented` | bridge 구현 상태 |
| `auth_qualification` | 항상 `NOT_RUN` (AIK-022) | 다른 값으로 생성하면 `ValueError` |
| `probe_qualification` | 항상 `NOT_RUN` (AIK-082) | 다른 값으로 생성하면 `ValueError` |
| `governed_qualification` | 항상 `NOT_RUN` (AIK-086) | 다른 값으로 생성하면 `ValueError` |
| `source` | `synthetic_fixture` / `live` | runner가 스스로 선언한 값 |

`reasons` 코드는 다음과 같다: `host_absent`, `host_unsupported`, `host_state_unknown`, `auth_required`, `auth_expired`, `auth_pending`, `non_subscription_login`, `account_unknown`, `account_changed`, `connection_stale`, `connection_unbound`, `vm_runtime_unavailable`.

`execution_ready=True`도 실제 인증 성공이나 governed 적합성을 뜻하지 않는다.

호스트가 없거나 지원되지 않아도 연결 정의는 등록할 수 있다. 다만 그 상태는 실행 불가로 표시한다.

이 프로세스가 발급·확인하지 않은 binding(예: 재시작 뒤 처음 제시된 binding)은 `binding=changed`/`connection_stale`이다. `reconnect()` 전까지 실행할 수 없다.

ProviderAdapter 경로(`connect`/`reconnect`, `auth_status`, `begin_sign_in`, `sign_out`, `start_job`, `interrupt_job`)는 호출마다 `--version`을 다시 실행한다(캐시하지 않는다). 결과 표현은 다음과 같다.

- 호스트 부재: `UNSUPPORTED` / `host_absent`
- 호스트 버전 미지원: `UNSUPPORTED` / `host_unsupported`. VM runtime·로그인·로그아웃 명령을 실행하지 않는다.
- 버전 확인 중 timeout·출력 초과·runner 오류: `PROVIDER_FAILURE` / 해당 코드
- 인증 만료: `AuthState.EXPIRED`
- API 키 로그인: `UNSUPPORTED` / `non_subscription_login`

## 5. 계정 binding과 revision

- `ConnectionBinding.credential_ref`는 호스트 handle `host-session://codex-cli/<profile>`이다. 토큰이나 세션 파일 경로가 아니다.
- `account_fingerprint`는 **호스트가 상태 출력으로 제공한 계정 식별자**에서 계산한다.
  - 계산 방식은 도메인 분리 SHA-256이다. AIK-011 계약의 64자리 hex 형식에 맞추기 위한 것이다.
  - 사용자 label, 토큰, 파일 내용에서 유도하지 않는다.
  - 원래 식별자는 저장하지도 반환하지도 않는다.
- **이 fingerprint는 가명화(pseudonymization)이며 익명화가 아니다.**
  - 비밀 키를 쓰지 않으므로, 식별자 후보(예: 이메일 형태의 값)를 알면 대입으로 일치 여부를 확인할 수 있다.
  - 이 값은 `ConnectionBinding`, `HostHandle`, `JobReceipt`와 VM runtime으로 전달된다. 따라서 민감도가 낮은 값으로 단정하지 않는다.
  - 로컬 비밀 salt를 쓰는 HMAC 전환과 salt 보관 위치는 이 태스크에서 구현하지 않았다(별도 태스크).
- 아래 경우에는 계정을 확인할 수 없다고 보고 `unknown`으로 둔다. 이때 `connect`는 `AUTH_REQUIRED`/`account_unknown`을, `auth_status`는 `unknown`을 반환한다. 실행은 항상 막힌다.
  - 호스트가 식별자를 제공하지 않음
  - 형식이 맞지 않음
  - 토큰처럼 보이는 값
  - 서로 다른 값이 여러 개 있음
- **현재 가정한 CLI가 계정 식별자를 출력하지 않는다면, 이 bridge로는 실행 binding을 만들 수 없다(fail-closed).**
- 실행 전(`start_job`, `interrupt_job`)에는 세 가지를 확인한 뒤 VM runtime을 호출한다.
  - 호스트 버전이 지원되어야 한다.
  - binding의 revision·fingerprint가 bridge가 기억하는 값과 같아야 한다.
  - 다시 실행한 `login status`의 계정도 같아야 한다.
- `connect()`/`reconnect()`가 새 revision을 발급할 때는 항상 더 큰 값을 사용한다. 기준은 bridge가 기억하는 revision과 제시된 `previous`의 revision이다.

**외부 로그인 변경**은 다음과 같이 처리한다.

- bridge 밖에서 일어난 아래 상태를 한 번이라도 관측하면, 해당 connection을 stale로 표시한다: 로그아웃, 만료, 다른 계정 로그인, API 키 로그인, 계정 불명.
- 원래 계정으로 돌아와도(A→B→A, 로그아웃 후 재로그인) `STALE_CONNECTION`/`reconnect_required`가 유지된다.
- `reconnect()`가 새 revision을 발급해야 다시 실행할 수 있다. 이전 revision의 binding은 계속 거절된다.
- 예외는 하나다. bridge가 그 binding으로 직접 시작한 로그인(`begin_sign_in`)이 **종료 코드 0으로 정상 종료**하고 같은 계정이 관측되면, revision을 유지한 채 재검증된 것으로 본다. 이 경로는 GuardedAdapter의 인증 예약 해제와 맞물린다.
  - 다음으로 끝난 로그인은 재검증하지 않으며 stale이 유지된다: 기한 초과로 종료, 0이 아닌 종료 코드, 상태 확인(`poll`) 실패, `cancel_login()`.
  - 이 프로세스가 발급하지 않고 채택한 binding도 이 경로로 재검증되지 않는다.

**로그아웃(`sign_out`)** 전에는 `login status`를 확인한다. 호스트가 하나의 활성 로그인만 갖는다는 가정 때문이다. 아래 경우에는 `logout`을 실행하지 않는다.

| 호스트 상태 | 결과 |
|---|---|
| 다른 계정으로 로그인 | `ACCOUNT_CHANGED`/`account_changed` |
| API 키 로그인 | `UNSUPPORTED`/`non_subscription_login` |
| 구독 로그인이지만 계정 식별 불가 | `PROVIDER_FAILURE`/`account_unknown` |

이 사전 확인 단계의 runner 오류는 아무 효과도 없었으므로 known failure(`PROVIDER_FAILURE`)다.

## 6. 비밀 처리

- 토큰·세션 파일·키를 읽거나 복사하거나 저장하지 않는다. 로그에도 남기지 않는다. 사용하는 신호는 공식 CLI의 종료 코드와 상태 출력뿐이다.
- CLI 출력은 parser가 enum과 계정 식별자 후보만 추출한 뒤 버린다. 오류에는 고정 `ProviderFault` 코드만 넣는다. runner 예외를 `__context__`/`__cause__`로 연결하지 않는다.
- `RunResult.output`과 `HostHandle.environment`는 `repr`에서 제외한다.
- 환경변수는 allowlist에 있는 이름만 전달하고, 고정값 `NO_COLOR=1`을 추가한다. 목록은 `PATH`, `SYSTEMROOT`, `HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `TEMP`, `CODEX_HOME`, `LANG` 등이다.
- `OPENAI_API_KEY`를 포함한 그 밖의 변수는 항상 제외한다. 따라서 구독 인증이 API 키 실행으로 바뀌지 않는다. allowlist 밖의 이름이 들어오면 runner가 실행 전에 거절한다.
- 호스트가 API 키로 로그인된 상태(`Logged in using an API key ...`)는 구독 경로로 쓰지 않는다. 마스킹된 키 문자열도 보관하지 않는다.
- bridge는 감사 로그를 남기지 않는다. 외부 경계로 출력할 때의 정제는 AIK-017 호출부의 책임이다.

## 7. runner 제한

| 항목 | 값 |
|---|---|
| 명령 timeout (`run`) | 기본 15초, 최대 120초, 절대 기한. 지나면 프로세스 트리 전체를 종료하고 `host_timeout`<br>정리 유예: timeout 뒤 트리 종료·재관측·pipe 정리에 최대 약 `4 × GRACE_SECONDS`(20초)가 더 걸릴 수 있다. 예: `MAX_TIMEOUT=120`이면 벽시계로 약 140초까지 걸릴 수 있다 |
| 출력 상한 | 기본 64 KiB, 최대 1 MiB. 실행 중 pipe에서 상한까지만 메모리에 기록하며 임시 파일은 쓰지 않는다. 초과하면 즉시 프로세스 트리를 종료하고 `host_output_limit` |
| 프로세스 트리 | POSIX: `start_new_session=True`로 시작한 process group에 `SIGKILL`<br>Windows: `CREATE_NEW_PROCESS_GROUP`으로 시작한 뒤 전용 Job Object에 할당하고 `TerminateJobObject`. 할당 실패 시 `%SYSTEMROOT%\System32\taskkill.exe /T /F` (**NOT_RUN**)<br>정상 종료 뒤 같은 group·Job에 남은 자손도 종료한다 |
| 종료 재관측 | POSIX: 직접 자식(group leader)은 group 재관측이 끝난 **뒤에** 회수한다. 회수 전 zombie가 PID·PGID 재사용을 막는다. 종료는 `waitid(WNOWAIT)`로 회수 없이 관측한다. group은 Linux `/proc`(zombie 제외)으로 확인한다. 그 밖의 POSIX의 `killpg(pgid, 0)`은 회수 전 leader도 세므로 미확인이 된다. leader가 이미 회수된 경우(`waitid` 미지원 등)에는 신호·재관측 없이 미확인<br>Windows: Job의 `ActiveProcesses`가 0일 때만 확인. Job 없이 `taskkill`만 쓴 경우는 항상 미확인<br>확인하지 못하거나 pipe가 닫히지 않으면 `ProcessTreeUnconfirmed`(code `HOST_TERMINATION_UNKNOWN`)로 끝나며 결과를 반환하지 않는다 |
| stdin | 닫음 |
| stderr | stdout에 병합 (상태 문구가 stderr로 나올 수 있다는 가정) |
| 로그인 프로세스(`spawn`) | 출력은 전부 버림. **자체 기한이 없다**(allowlist만 검사). 호출자가 handle의 `terminate()`를 불러야 하며, handle을 버리면 프로세스가 계속 남는다. bridge는 §3의 지연 기한으로 이를 호출한다 |
| 실행 방식 | `shell=False`. 실행 파일은 전달한 환경의 `PATH`로만 `shutil.which` 해석 |
| Windows | `CREATE_NO_WINDOW` |

`ProcessTreeUnconfirmed`는 공개 계층 호환을 위해 `RunnerTimeout`의 하위 클래스로 유지한다. 따라서 `except RunnerTimeout`으로 받은 예외가 트리 종료 확정을 뜻하지는 않는다. 종료 확정은 정확히 `RunnerTimeout` 클래스로 발생한 경우뿐이며, `ProcessTreeUnconfirmed`는 timeout 없이 정상 종료한 뒤의 미확인에도 발생한다. `RunnerTimeout` docstring도 이 의미로 정정했다.

`SubprocessRunner`의 실행 파일 해석 규칙은 다음과 같다.

- 전달한 환경에 `PATH`가 없거나 비어 있으면 `HostNotFound`다. 부모 프로세스의 `PATH`로 대체하지 않는다.
- 디렉터리 없는 이름(`codex`)은 해석 결과의 디렉터리가 전달한 `PATH`의 **절대 경로 항목** 중 하나와 같아야 한다. 아니면 `HostNotFound`다.
  - 빈 항목·상대 경로 항목은 허용 목록에 넣지 않는다.
  - 이는 Windows의 `shutil.which`가 현재 작업 디렉터리를 먼저 탐색해 cwd의 `codex.cmd`를 고르는 경우를 거절하기 위한 것이다.
- 디렉터리를 포함한 실행 파일 이름은 절대 경로여야 한다. 상대 경로는 `HostNotFound`다.
- Windows에서 `shutil.which`는 확장자 목록(`PATHEXT`)을 전달한 환경이 아니라 부모 프로세스의 `os.environ`에서 읽는다. 이 차이와 위 cwd 거절 동작은 Windows에서 실행하지 않았다(**NOT_RUN**).

오류 매핑은 다음과 같다.

| 상황 | 상태 경로 | `logout` 경로 |
|---|---|---|
| 실행 파일 없음 | `UNSUPPORTED`/`host_absent` | `UNSUPPORTED`/`host_absent` |
| timeout·출력 초과·트리 종료 미확인·기타 runner 오류 | `PROVIDER_FAILURE`/known | 효과가 불명확하므로 `UNKNOWN_OUTCOME`/unknown |

트리 종료 미확인(`ProcessTreeUnconfirmed`)을 bridge가 어떤 `native_code`로 표시하는지는 이번 변경에서 확인·수정하지 않았다. `bridge.py`는 변경하지 않았다. 위 표는 이 예외가 `RunnerError`·`RunnerTimeout` 계열로 처리된다는 전제다.

`logout` 뒤 결과는 상태 관측으로 판정한다.

- 로그아웃이 관측됨: `unauthenticated`
- 종료 코드가 실패이고 같은 계정이 여전히 로그인됨: `PROVIDER_FAILURE`/`logout_failed`
- 그 밖의 경우: `unknown` (GuardedAdapter가 host fence를 건다)

## 8. 기존 VM runtime 재사용

bridge는 Codex 실행을 직접 구현하지 않는다. 주입한 `vm_runtime` 객체에 실행을 위임한다. 이 객체는 다음 메서드를 가져야 한다.

- `start(handle, request) -> (job_id, JobState)`
- `observe(handle, request, job_id) -> JobState`
- `interrupt(handle, request, job_id) -> JobState`

`HostHandle`에는 다음 값만 들어간다. 토큰은 없다.

- provider, runtime, connection ID, revision, fingerprint
- 호스트 locator, 실행 파일 이름
- allowlist 환경변수

예외와 결과 처리는 다음과 같다.

- `start`·`interrupt`의 원시 예외와 형식 오류: `UNKNOWN_OUTCOME`
- `observe`의 예외: `PROVIDER_FAILURE`
- request ID는 시작을 시도한 시점에 소비된다. 같은 ID로 다시 시작하면 `DUPLICATE_REQUEST`다. 인증·버전 확인에서 거절된 요청의 ID는 소비되지 않는다.
- `observe`와 `interrupt`가 동시에 실행되면 receipt 저장은 compare-and-set으로 처리한다.
  - VM 호출 전에 읽은 receipt가 이미 다른 호출에 의해 바뀌었으면, 덮어쓰지 않고 현재 저장된 receipt를 반환한다.
  - 따라서 terminal 상태(예: `cancelled`)가 non-terminal 상태로 되돌아가지 않는다.
- `interrupt_job`도 실행 전 확인(버전·binding·현재 계정 인증)을 거친다. 실행 중 사용자가 호스트에서 로그아웃하거나 계정을 바꾸면 이 bridge로는 작업을 중단할 수 없다(§11).
- 실행 인가는 `GuardedAdapter(authorize_dispatch=...)`로 제품의 VM/AGS 인가에 연결해야 한다. 시험의 `lambda request: True`는 합성 fixture다.
- `JobReceipt.state=succeeded`는 provider 관측일 뿐이며 VM 완료 판정이 아니다.

## 9. 미검증 가정 (공식 문서로 확인하지 못함)

| 항목 | 코드의 가정 |
|---|---|
| 실행 파일 | `codex` (생성자에서 변경 가능) |
| 버전 확인 | `codex --version`, 출력이 `codex-cli X.Y.Z` 형식. 최소 지원 버전은 모름 |
| 상태 조회 | `codex login status` |
| 로그인됨 문구 | `Logged in using ChatGPT`(구독), `Logged in using an API key`(API 키), 종료 코드 0 |
| 로그아웃 문구 | `Not logged in` |
| 만료 문구 | `expired`, `log in again`, `re-authenticate` |
| 계정 식별자 | `Account ID: <값>` 또는 `account: <값>` 형식의 줄. **현재 CLI가 이를 제공하는지 확인하지 못함** |
| 로그인 시작 | `codex login`. 비대화형으로 브라우저 로그인을 진행하고 성공하면 종료 코드 0으로 종료한다고 가정. TTY 필요 여부 미확인 |
| 로그아웃 | `codex logout` |
| 출력 위치 | stdout/stderr 중 어디인지 미확인 (병합해서 읽음) |
| 단일 계정 | 하나의 설정 위치(`CODEX_HOME`)에 활성 로그인 하나 |
| Windows | npm 설치본이 `codex.cmd`일 때 `CreateProcess` 경유 실행 |
| 자손 프로세스 | 버전·상태·logout 명령이 종료 뒤에도 유지되어야 하는 자손을 같은 process group·Job Object에 남기지 않음(남기면 runner가 종료함) |

위 가정이 실제와 다르면 fixture는 통과해도 실제 호스트에서 실패할 수 있다. 이때 결과는 `host_unsupported`·`unknown`·`account_unknown`이며 fail-closed로 동작한다. 검증된 형식이 확인되면 `protocol=`로 교체한다.

## 10. 검증 상태

```bash
python -m unittest discover -s tests -p 'test_host_codex.py' -v
```

| 항목 | 상태 |
|---|---|
| 합성 runner·합성 VM runtime fixture | 개발용 단위 시험. 이 문서 작성 시점에 실행 결과 기록 없음 |
| `SubprocessRunner` 한계(timeout·출력 상한·종료 코드) | 로컬 `sys.executable -c` 합성 프로세스로 시험을 추가함. 실행 결과 기록 없음. 실제 Codex와 무관 |
| `SubprocessRunner` 프로세스 트리(POSIX 전용 시험) | 로컬 Python 자식·손자 프로세스로 시험을 추가함. 실행 결과 기록 없음<br>대상: 출력 상한 초과·timeout·정상 종료 뒤 손자 종료, timeout 지연 없음, spawn handle 종료, 종료 미확인의 `ProcessTreeUnconfirmed` 분류, leader 회수 전 group 재관측, 이미 회수된 leader의 group 신호 생략 |
| Windows 분기 판정(공통 시험) | 가짜 process 객체와 mock으로 Job 관측 없이는 미확인, `ActiveProcesses` 0 관측 시에만 확인됨을 시험함. spawn이 트리 추적을 호출함을 시험함. 실제 Windows Job Object는 만들지 않음. 실행 결과 기록 없음 |
| 실제 Codex CLI 왕복(`SubprocessRunner`) | **NOT_RUN** |
| 실제 구독 로그인·만료·계정 전환 | **NOT_RUN** (AIK-022) |
| 사전 probe / governed 적합성 / 제품 실행 | **NOT_RUN** (AIK-082 / AIK-086 / AIK-044) |
| Windows 실행 | **NOT_RUN**. 대상: `codex.cmd`, `CREATE_NO_WINDOW`/`CREATE_NEW_PROCESS_GROUP`, Job Object 할당·종료·재관측, `taskkill /T /F` fallback, PATH/cwd/`PATHEXT` 해석 |

fixture가 다루는 범위는 다음과 같다.

- 호스트 부재·미지원 버전·만료의 분리
- 미지원 버전에서 ProviderAdapter 경로(`start_job`·`auth_status`·`connect`·`begin_sign_in`·`sign_out`·`interrupt_job`) 차단
- 계정 불명·형식 불일치·토큰형 식별자
- API 키 로그인 거절과 환경변수 allowlist
- GuardedAdapter 경유 해제→로그인 대기→완료→실행
- 외부 계정 전환·외부 재로그인의 stale 처리와 재연결 revision
- 기한 초과·실패 종료·poll 오류로 끝난 로그인이 stale을 해제하지 않음
- 재시작 뒤 채택한 binding의 stale 처리와 더 큰 revision 발급
- 로그인 기한 종료(미지원 버전의 probe 포함)·취소
- 해제 실패(known/unknown)와 host fence, 다른 계정 로그아웃 거절
- runner timeout·출력 초과, 실행 파일 해석 제한
- VM runtime 미연결·실패·중복 request
- observe/interrupt, 동시 observe·interrupt의 terminal 보존, 로그아웃 뒤 interrupt 거절
- connect/reconnect의 만료·로그아웃·부재 실패 분류

Linux 통과는 개발 검증이며 Windows 지원 증거가 아니다.

## 11. 한계

- 다음 상태는 프로세스 메모리에만 있다: revision 기억, stale 표시, 소비된 request ID, receipt, 대기 중 로그인.
- 재시작 뒤 처음 제시된 binding은 **stale로 채택**된다.
  - 실행(`start_job`/`interrupt_job`)과 `auth_status`의 인증 확인은 `reconnect()`가 새 revision을 발급할 때까지 `STALE_CONNECTION`/`reconnect_required`로 거절된다.
  - bridge가 시작한 로그인도 채택된 binding을 재검증하지 않는다.
- 이 조치로도 재시작을 넘는 완전한 펜싱은 되지 않는다. bridge는 재시작 전에 발급한 최신 revision을 모르기 때문이다.
  - 예: 재시작 전 rev 1·2·3이 발급되었고 rev 3이 최신이다. 재시작 뒤 `reconnect(rev 1)`은 rev 2를 발급한다. 그 결과 재시작 전에 펜싱된 rev 2와 같은 값이 다시 유효해지고 rev 3은 거절된다.
  - 재연결 자체는 현재 호스트 계정을 다시 확인한다. 그러나 재시작 중 일어난 외부 로그인 변경(A→B→A)은 탐지하지 못한다.
  - 영속 binding과 reconciliation은 AIK-031/033/035/038의 범위다.
- 재시작 뒤 `previous=` 없이 `connect()`를 호출하면, 그 connection이 아직 채택되지 않은 경우 revision이 1부터 다시 시작한다. 영속 binding이 있으면 `reconnect(previous)`를 사용한다. 이때 `previous`보다 큰 revision이 발급된다.
- 관측과 실행 사이에 사용자가 호스트에서 로그인을 바꾸는 경합은 OS 수준에서 막지 못한다. 실행 전에 한 번 더 대조할 뿐이다.
- `interrupt_job`은 현재 계정 인증을 요구한다. 작업 실행 중 호스트 로그아웃·계정 전환·버전 변경이 일어나면 이 bridge로는 그 작업을 중단할 수 없다. 이런 경우의 중단은 VM runtime/supervisor 쪽 경로가 필요하다.
- 로그인 기한은 bridge 호출 시점에만 집행된다(§3). 호출이 없으면 기한이 지난 로그인 프로세스가 남는다. ProviderAdapter 경로가 버전 미지원으로 거절되기 전에 끝난 로그인은 재검증되지 않는다.
- `SubprocessRunner`의 한계:
  - 프로세스 트리 종료 범위는 POSIX의 같은 process group, Windows의 전용 Job Object 안의 프로세스다. `setsid`·새 session으로 빠져나간 자손과 Job에서 breakaway한 프로세스는 종료하지 않는다.
  - Windows에서 `Popen` 반환 직후 Job에 할당하기 전에 자식이 만든 프로세스는 Job에 속하지 않아 종료·관측 대상이 아니다. 이 경합 창은 NOT_RUN이다.
  - Windows에서 Job 할당에 실패하면 `taskkill /T /F`로 종료를 시도하지만 결과는 항상 미확인(`ProcessTreeUnconfirmed`)이다. Job의 `ActiveProcesses`가 0일 때만 종료 확인으로 본다(NOT_RUN).
  - 트리 종료를 확인하지 못하면 `ProcessTreeUnconfirmed`로 끝난다. 아직 끝나지 않은 읽기·stdin thread와 pipe는 정리하지 않고 남긴다. 반복되면 호출마다 thread와 최대 상한만큼의 메모리가 남을 수 있다.
  - `waitid`가 없거나 `/proc`이 없는 POSIX 플랫폼에서는 트리 종료를 확인하지 못해 미확인으로 끝난다. 이 플랫폼들은 지원 대상이 아니다.
  - 로그인 프로세스 handle의 `terminate()`는 직접 자식이 이미 종료됐으면 아무것도 종료하지 않는다(브라우저 등 남은 프로세스 보존). 기한을 넘겨 종료된 로그인 프로세스의 브라우저 측 세션은 정리하지 않는다.
  - Windows의 `PATHEXT`는 부모 프로세스 환경에서 읽힌다(§7, NOT_RUN).
- streaming 실행, 장시간 작업 재연결, 다중 profile 동시 사용은 구현하지 않았다.
