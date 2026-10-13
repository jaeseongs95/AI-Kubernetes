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
| `runner.py` | 주입형 `HostRunner`(`source`, `run`, `spawn`), `RunResult`, 고정 코드 예외, 환경변수 allowlist, 표준 라이브러리 `SubprocessRunner`(실사용 **NOT_RUN**) |
| `protocol.py` | 미검증 CLI 가정을 모은 `CodexCliProtocol`. `LoginState`, `LoginMethod`, `ParsedStatus`를 정의한다. 다른 protocol 객체로 교체할 수 있다 |
| `bridge.py` | `CodexHostBridge`(ProviderAdapter 구현), `HostCapability`(connection capability), `HostHandle`, `GUI_COMMANDS` |

## 3. GUI command와 코드 경로

| GUI command | 메서드 | 동작 |
|---|---|---|
| `host.codex.auth.start` | `start_login(connection_id)` / `begin_sign_in(binding)` | 공식 로그인 명령을 `spawn`한다. 출력은 버린다. 상태는 `pending`이다 |
| `host.codex.auth.status` | `probe(binding=None)` / `auth_status(binding)` | `--version`과 `login status`를 실행해 상태를 분리 보고한다 |
| `host.codex.auth.reconnect` | `reconnect(binding)` / `connect(connection_id)` | 관측 계정으로 binding을 확인한다. 변경이 있었으면 revision을 올린다 |
| `host.codex.auth.sign_out` | `sign_out(binding)` | `logout`을 요청한다. 그 뒤 상태를 관측해 결과를 확인한다 |
| `host.codex.auth.cancel` | `cancel_login()` | 대기 중인 로그인 프로세스를 종료한다 |

어떤 경로도 unsupported만 반환하는 stub이 아니다. 로그인 프로세스에는 절대 기한(기본 600초, 최대 3600초)이 있다. 기한을 넘기면 bridge가 종료하고 상태를 다시 관측한다.

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
| `reasons` | 고정 코드 tuple | 아래 표 참조 |
| `implementation_state` | `implemented` | bridge 구현 상태 |
| `auth_qualification` | 항상 `NOT_RUN` (AIK-022) | 다른 값으로 생성하면 `ValueError` |
| `probe_qualification` | 항상 `NOT_RUN` (AIK-082) | 다른 값으로 생성하면 `ValueError` |
| `governed_qualification` | 항상 `NOT_RUN` (AIK-086) | 다른 값으로 생성하면 `ValueError` |
| `source` | `synthetic_fixture` / `live` | runner가 스스로 선언한 값 |

`reasons` 코드는 다음과 같다: `host_absent`, `host_unsupported`, `host_state_unknown`, `auth_required`, `auth_expired`, `auth_pending`, `non_subscription_login`, `account_unknown`, `account_changed`, `connection_stale`, `connection_unbound`, `vm_runtime_unavailable`.

`execution_ready=True`도 실제 인증 성공이나 governed 적합성을 뜻하지 않는다. 호스트가 없거나 지원되지 않아도 연결 정의는 등록할 수 있다. 다만 그 상태는 실행 불가로 표시한다.

ProviderAdapter 경로의 표현은 다음과 같다.

- 호스트 부재: `UNSUPPORTED` / `host_absent`
- 인증 만료: `AuthState.EXPIRED`
- API 키 로그인: `UNSUPPORTED` / `non_subscription_login`

## 5. 계정 binding과 revision

- `ConnectionBinding.credential_ref`는 호스트 handle `host-session://codex-cli/<profile>`이다. 토큰이나 세션 파일 경로가 아니다.
- `account_fingerprint`는 **호스트가 상태 출력으로 제공한 계정 식별자**에서 계산한다. 계산 방식은 도메인 분리 SHA-256이며, AIK-011 계약의 64자리 hex 형식에 맞추기 위한 것이다. 사용자 label, 토큰, 파일 내용에서 유도하지 않는다. 원래 식별자는 저장하지도 반환하지도 않는다.
- 아래 경우에는 계정을 확인할 수 없다고 보고 `unknown`으로 둔다. 이때 `connect`는 `AUTH_REQUIRED`/`account_unknown`을 반환하고, `auth_status`는 `unknown`을 반환한다. 실행은 항상 막힌다.
  - 호스트가 식별자를 제공하지 않음
  - 형식이 맞지 않음
  - 토큰처럼 보이는 값
  - 서로 다른 값이 여러 개 있음
- **현재 가정한 CLI가 계정 식별자를 출력하지 않는다면, 이 bridge로는 실행 binding을 만들 수 없다(fail-closed).**
- 실행 전(`start_job`, `interrupt_job`)에는 두 가지를 확인한다. binding의 revision·fingerprint가 bridge가 기억하는 값과 같아야 한다. 다시 실행한 `login status`의 계정도 같아야 한다. 그 뒤에 VM runtime을 호출한다.

**외부 로그인 변경**은 다음과 같이 처리한다.

- bridge 밖에서 일어난 로그아웃·만료·다른 계정 로그인·API 키 로그인·계정 불명을 한 번이라도 관측하면, 해당 connection을 stale로 표시한다.
- 원래 계정으로 돌아와도(A→B→A, 로그아웃 후 재로그인) `STALE_CONNECTION`/`reconnect_required`가 유지된다.
- `reconnect()`가 새 revision을 발급해야 다시 실행할 수 있다. 이전 revision의 binding은 계속 거절된다.
- 예외는 하나다. bridge가 그 binding으로 직접 시작한 로그인(`begin_sign_in`)이 같은 계정으로 끝난 것을 관측하면, revision을 유지한 채 재검증된 것으로 본다. 이 경로는 GuardedAdapter의 인증 예약 해제와 맞물린다.

## 6. 비밀 처리

- 토큰·세션 파일·키를 읽거나 복사하거나 저장하지 않는다. 로그에도 남기지 않는다. 사용하는 신호는 공식 CLI의 종료 코드와 상태 출력뿐이다.
- CLI 출력은 parser가 enum과 계정 식별자 후보만 추출한 뒤 버린다. 오류에는 고정 `ProviderFault` 코드만 넣는다. runner 예외를 `__context__`/`__cause__`로 연결하지 않는다.
- `RunResult.output`과 `HostHandle.environment`는 `repr`에서 제외한다.
- 환경변수는 allowlist에 있는 이름만 전달한다. 목록은 `PATH`, `SYSTEMROOT`, `HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `TEMP`, `CODEX_HOME`, `LANG` 등이며, 고정값 `NO_COLOR=1`을 추가한다.
- `OPENAI_API_KEY`를 포함한 그 밖의 변수는 항상 제외한다. 따라서 구독 인증이 API 키 실행으로 바뀌지 않는다. allowlist 밖의 이름이 들어오면 runner가 실행 전에 거절한다.
- 호스트가 API 키로 로그인된 상태(`Logged in using an API key ...`)는 구독 경로로 쓰지 않는다. 마스킹된 키 문자열도 보관하지 않는다.
- bridge는 감사 로그를 남기지 않는다. 외부 경계로 출력할 때의 정제는 AIK-017 호출부의 책임이다.

## 7. runner 제한

| 항목 | 값 |
|---|---|
| 명령 timeout | 기본 15초, 최대 120초, 절대 기한 |
| 출력 상한 | 기본 64 KiB, 최대 1 MiB. 초과하면 프로세스를 종료하고 `host_output_limit` |
| stdin | 닫음 |
| stderr | stdout에 병합 (상태 문구가 stderr로 나올 수 있다는 가정) |
| 로그인 프로세스 출력 | 전부 버림 |
| 실행 방식 | `shell=False`, 실행 파일은 allowlist된 `PATH`로 `shutil.which` 해석 |
| Windows | `CREATE_NO_WINDOW` |

오류 매핑은 다음과 같다.

| 상황 | 상태 경로 | `logout` 경로 |
|---|---|---|
| 실행 파일 없음 | `UNSUPPORTED`/`host_absent` | `UNSUPPORTED`/`host_absent` |
| timeout·출력 초과·기타 runner 오류 | `PROVIDER_FAILURE`/known | 효과가 불명확하므로 `UNKNOWN_OUTCOME`/unknown |

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
- request ID는 시작을 시도한 시점에 소비된다. 같은 ID로 다시 시작하면 `DUPLICATE_REQUEST`다.
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
| 로그인 시작 | `codex login`. 비대화형으로 브라우저 로그인을 진행하고 완료되면 종료한다고 가정. TTY 필요 여부 미확인 |
| 로그아웃 | `codex logout` |
| 출력 위치 | stdout/stderr 중 어디인지 미확인 (병합해서 읽음) |
| 단일 계정 | 하나의 설정 위치(`CODEX_HOME`)에 활성 로그인 하나 |
| Windows | npm 설치본이 `codex.cmd`일 때 `CreateProcess` 경유 실행 |

위 가정이 실제와 다르면 fixture는 통과해도 실제 호스트에서는 `host_unsupported`·`unknown`·`account_unknown`으로 실패할 수 있다. 이때는 fail-closed로 동작한다. 검증된 형식이 확인되면 `protocol=`로 교체한다.

## 10. 검증 상태

```bash
python -m unittest discover -s tests -p 'test_host_codex.py' -v
```

| 항목 | 상태 |
|---|---|
| 합성 runner·합성 VM runtime fixture | 개발용 단위 시험. 이 문서 작성 시점에 실행 결과 기록 없음 |
| 실제 Codex CLI 왕복(`SubprocessRunner`) | **NOT_RUN** |
| 실제 구독 로그인·만료·계정 전환 | **NOT_RUN** (AIK-022) |
| 사전 probe / governed 적합성 / 제품 실행 | **NOT_RUN** (AIK-082 / AIK-086 / AIK-044) |
| Windows 실행 (`codex.cmd`, `CREATE_NO_WINDOW`) | **NOT_RUN** |

fixture가 다루는 범위는 다음과 같다.

- 호스트 부재·미지원 버전·만료의 분리
- 계정 불명·형식 불일치·토큰형 식별자
- API 키 로그인 거절과 환경변수 allowlist
- GuardedAdapter 경유 해제→로그인 대기→완료→실행
- 외부 계정 전환·외부 재로그인의 stale 처리와 재연결 revision
- 로그인 기한 종료·취소
- 해제 실패(known/unknown)와 host fence
- runner timeout·출력 초과
- VM runtime 미연결·실패·중복 request
- observe/interrupt

Linux 통과는 개발 검증이며 Windows 지원 증거가 아니다.

## 11. 한계

- 다음 상태는 프로세스 메모리에만 있다: revision 기억, stale 표시, 소비된 request ID, receipt, 대기 중 로그인. 재시작 뒤에는 처음 제시된 binding을 채택하고 fingerprint만 대조한다. 따라서 재시작 중에 일어난 외부 로그인 변경(A→B→A)은 탐지하지 못한다. 영속 binding과 reconciliation은 AIK-031/033/035/038의 범위다.
- 재시작 뒤 `connect()`를 `previous=` 없이 호출하면 revision이 1부터 다시 시작한다. 영속 binding이 있으면 `reconnect(previous)`를 사용한다.
- 관측과 실행 사이에 사용자가 호스트에서 로그인을 바꾸는 경합은 OS 수준에서 막지 못한다. 실행 전에 한 번 더 대조할 뿐이다.
- `SubprocessRunner`의 한계:
  - 기한이 지나면 직계 프로세스만 종료한다. 손자 프로세스가 pipe를 붙잡고 있는 경우는 처리하지 않는다.
  - 기한을 넘겨 종료된 로그인 프로세스의 브라우저 측 세션은 정리하지 않는다.
- streaming 실행, 장시간 작업 재연결, 다중 profile 동시 사용은 구현하지 않았다.
