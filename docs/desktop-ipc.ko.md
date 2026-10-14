# AIK-004 — 데스크톱 IPC 경계 (부분 구현)

renderer와 Electron main 사이의 제한된 IPC를 제공한다. 명령은 다음과 같다.

- 기본 명령: `protocol.handshake`, `system.status`, AIK-045가 더한 읽기 전용 `engine.info`·`readmodel.sources`.
- AIK-046·047 연결 명령: 읽기 4개, 쓰기 8개. 목록은 [connections-gui](connections-gui.ko.md) §4에 있다.
- AIK-087 프로젝트 폴더 명령: 읽기 2개(`workspaces.list`·`scope`), 쓰기 4개(`select`·`archive`·`restore`·`request_folder`). 목록은 [workspaces-gui](workspaces-gui.ko.md)에 있다.

어떤 renderer 명령도 API 키나 폴더 경로를 싣지 않는다. 폴더는 main이 띄우는 OS 폴더 선택 창에서만 고르고, 고른 경로는 main이 엔진으로 바로 보낸다. 키는 main이 띄우는 별도 키 입력 창에서만 입력하고, 그 창은 별도 채널을 쓴다(아래 "키 입력 창 채널").

이 문서는 제품 GUI·VM/AGS·vault broker 전체 연결이 끝났다고 말하지 않는다. 데스크톱 셸 연결은 [desktop-shell](desktop-shell.ko.md)에 있다.

## 요청·응답의 소유권

`src/aik_ipc/desktop.cjs`는 요청에서 다음을 검증한다.

- 정확한 다섯 요청 필드
- UUID v4
- workspace
- 앱 window/main frame/URL

payload 검사는 명령에 따라 다르다.

- 연결 외 명령: payload는 빈 객체여야 한다.
- 연결 명령: `src/aik_ipc/connections.cjs`의 `readPayload`가 명령별 필드 집합을 정확히 요구하고, 필드마다 값을 검사한다.
  - `connection_id`·`provider`: 식별자 규칙 `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`.
  - `account_label`: 1~128자, 앞뒤 공백 없음, 제어·서식 문자(`\p{C}`) 없음.
  - `expected_revision`: 1 이상의 safe integer.
  - `acknowledge_impact`: bool.
- 필드 사이 조합도 검사한다.
  - `connections.start_login`: 새 로그인은 `provider`·`account_label`, 재인증은 `connection_id`만 준다.
  - `connections.request_key_entry`: `add`는 `provider`·`account_label`만, `rotate`는 `connection_id`·`expected_revision`만 준다.
- 프로젝트 폴더 명령: `src/aik_ipc/workspaces.cjs`의 `readWorkspacePayload`가 같은 방식으로 검사한다. `name`은 `account_label`과 같은 규칙이고, `folder_name`은 `/ \ : * ? " < > |`·제어 문자·끝 공백/점·`.`/`..`를 거절한다. `workspaces.request_folder`는 `open`이 `name`만, `create`가 `name`·`folder_name`만, `rebind`가 `workspace_id`·`expected_revision`만 준다.
- 크기는 `JSON.stringify` 없이 문자 수의 상한으로 추정한다. 상한은 전체 frame 16,384바이트다.

검증 직후 원시 객체를 버리고 동결한 primitive snapshot을 사용한다. 동결한 payload는 handler 문맥의 `payload`로 main에 전달된다.

응답도 명령별로 정해진 필드만 하나씩 복사·동결해 반환한다.

- `system.status`: `state`·`director_state`.
- `engine.info`: 엔진 버전·플랫폼 family·상태 저장소·read model 상태 등 8개.
- `readmodel.sources`: source별 cursor·gap·conflict·stale.
  - `stale`과 `open_gaps`가 서로 맞지 않거나 `authoritative`가 false가 아닌 응답은 `INVALID_RESPONSE`다.
- 연결 명령: `connections.cjs`의 `CONNECTION_SNAPSHOTS`가 형식을 검사한다. 다음 응답은 `INVALID_RESPONSE`다.
  - 상태 값이 정해진 집합 밖이다.
  - `execution_kind`와 `method`가 맞지 않는다(`api`↔`api_key`, `subscription`↔`host_login`).
  - 시각이 UTC `Z` 형식이 아니다.
  - `usage.blocking`이 `references`보다 크다.
  - `store=ABSENT`인데 목록이 비어 있지 않다.
  - 연결 ID가 중복된다.
  - `idle` 로그인에 `provider`가 있다.
  - 목록 상한을 넘는다(연결 256, 참조 1,024, connector 32).
- 프로젝트 폴더 명령: `workspaces.cjs`의 `WORKSPACE_SNAPSHOTS`가 형식을 검사한다. 상태·조치 값이 정해진 집합 밖이거나, 실행 가능(`ready`)하지 않은 선택에 실행 폴더(`cwd`)가 있거나, 목록의 `active` 표시가 선택과 다르거나, 보관 상태와 `archived` 상태가 어긋나면 `INVALID_RESPONSE`다.

원본이 비동기 처리 중 또는 반환 후 변경되어도 검증 결과에 반영되지 않는다.

Proxy·accessor·추가 필드·사용자 정의 thenable을 거절하며, 요청의 `toJSON`을 실행하지 않는다.

backend 오류는 고정 코드/문구로 바꾸고, 원시 예외·stack·민감 필드를 반환하지 않는다.

- 잘못된 protocol version은 `INCOMPATIBLE_PROTOCOL`과 `supported_versions=[1]`로 표시한다.
- main handler가 `BackendError("BACKEND_INCOMPATIBLE")`를 던지면 엔진과 앱의 버전 불일치로 그대로 전달한다.
- 연결 명령에서는 다음 코드도 그대로 전달한다.
  - 엔진이 거절하며 보낸 고정 코드 중 `CONNECTION_CODES`에 있는 것.
  - main의 `KEY_ENTRY_BUSY`·`KEY_ENTRY_UNAVAILABLE`.
- 프로젝트 폴더 명령에서는 `WORKSPACE_CODES`에 있는 엔진 코드와 main의 `FOLDER_PICKER_BUSY`·`FOLDER_PICKER_UNAVAILABLE`도 그대로 전달한다.
- 목록에 없는 코드(예: 엔진의 `INTERNAL_ERROR`)와 다른 예외는 모두 `BACKEND_UNAVAILABLE`이다.

handshake 결과의 `boundary`는 `RENDERER_NO_SECRETS`다. 이전 값 `RENDERER_READ_ONLY`에서 바뀌었다. renderer에 쓰기 명령이 생겼지만 비밀을 다루는 명령은 없다는 뜻이다. 이 값으로 분기하는 소비자는 함께 바꿔야 한다.

앱 문서는 `aik://app/index.html`이며, 같은 문서의 in-page fragment(`#/overview` 같은 hash route)는 같은 발신자로 인정한다. query string·다른 경로·다른 origin은 계속 `UNTRUSTED_SENDER`다.

## 응답 기한과 물리적 동시성

`new DesktopRouter({readStatus, requestTimeoutMs: 5000})`은 비동기 상태 조회를 기본 5초에 `BACKEND_TIMEOUT`으로 종료한다. 설정 범위는 1~60,000ms다.

`readers`는 처리하는 명령(읽기 명령과 비밀 없는 연결 쓰기 명령)마다 handler 하나를 받는다. handler 호출은 `handler(workspaceId, {signal, requestId, payload})` 형태다. 두 번째 인자는 동결된 문맥이며, 기한이 지나면 AbortSignal을 알린다. 기존 한 인자 callback도 동작한다.

`commandTimeoutsMs`로 명령별 기한을 정한다. 값은 1~1,800,000ms의 정수다. main이 설정한 값은 다음과 같다.

| 명령 | router 기한 |
| --- | --- |
| `system.status`·`engine.info`·`readmodel.sources` | 기본 5,000ms |
| `connections.request_key_entry` | 1,800,000ms(키 입력 창이 열려 있는 동안) |
| 그 밖의 `connections.*`(읽기 포함) | 155,000ms |

엔진 클라이언트(`apps/desktop/electron/engine.cjs`)에는 별도 기한이 있다.

- `connections.*`에는 기본 기한과 150초 중 큰 값을 쓴다. 엔진은 연결 명령을 별도 작업 스레드에서 하나씩 처리하므로, 연결 명령은 앞선 벤더 검사·로그인을 기다릴 수 있다. 상태·read model 조회는 이 줄을 거치지 않아 기다리지 않는다.
- 기한을 넘기면 `ENGINE_REQUEST_TIMEOUT`으로 클라이언트를 실패 처리한다.

**응답 타임아웃은 실제 backend 작업 종료가 아니다.**

- abort를 무시한 Promise는 실제로 resolve/reject될 때까지 동시성 슬롯과 중복 방어 기록을 점유한다. 이때 신규 상태 조회는 `ROUTER_BUSY`로 차단된다.
- 늦은 결과/예외는 성공 응답으로 바뀌거나 원문으로 노출되지 않는다.
- 쓰기 명령의 `BACKEND_TIMEOUT`은 변경이 일어나지 않았다는 뜻이 아니다. renderer는 목록을 다시 읽어 결과를 확인해야 한다.
- 열린 키 입력 창은 닫힐 때까지 슬롯 하나를 점유한다.
- 재전송 저장 공간이 남아 있다면 handshake는 backend 포화 상태에서도 응답한다. 이때도 `backend_state=NOT_QUALIFIED`를 유지한다.

기한은 이벤트 루프가 실행되는 비동기 대기에 적용한다. 동기 무한 루프를 선점하거나 native 프로세스를 강제 종료하지 않는다. 멈춘 backend의 교체/재시작은 supervisor 작업이며, 이 모듈이 그 권한을 만들지 않는다.

## 재전송과 범위

UUID 대소문자는 같은 request identity로 정규화한다. 미종료 요청은 보관 시간이 지나도 삭제하지 않으며, 실제 작업 종료 뒤 기본 10분간 replay fence를 유지한다. 시계 오류/역행은 기록을 조기에 삭제하는 근거가 되지 않는다. 기본 동시 요청은 쓰기 명령을 포함해 8개, replay 기록은 최대 4096개다. 전체 보관 한도는 65,536개, 설정 가능한 보관 시간은 1초~24시간이다.

연결 쓰기 명령도 같은 replay fence를 거친다. 이 fence는 메모리에만 있다. 앱이나 엔진이 재시작한 뒤의 중복 실행은 막지 않는다. 연결 쓰기의 충돌은 `expected_revision`과 엔진 쪽 `REVISION_CONFLICT`로 막는다.

비동기 처리 후 원래 window/main frame과 현재 workspace를 다시 확인한다. 다른 window, 파괴된 frame 또는 다른 활성 workspace에는 결과를 반환하지 않는다. 같은 workspace ID를 사용하는 동안 권한이 바뀌는 경우의 revision/인가 검사는 별도 제품 binding에 필요하다. 메모리 replay fence를 재시작 내구성이나 외부 효과 exactly-once 보장으로 해석하지 않는다.

## 키 입력 창 채널

API 키는 `aik:desktop:v1`을 지나지 않는다. main(`apps/desktop/electron/key-entry.cjs`)이 별도 키 입력 창과 전용 채널 `aik:key-entry:v1`을 소유한다.

- **창**
  - 앱 창의 modal 자식이며 URL은 `aik://app/key-entry.html`이다.
  - 설정은 `sandbox`·`contextIsolation`, `nodeIntegration` 끔, `devTools` 끔이다. 새 창·이동·webview는 막는다.
  - 한 번에 하나만 연다(`KEY_ENTRY_BUSY`).
- **preload**
  - `src/aik_ipc/key_entry_preload.cjs`는 `window.aikKeyEntry`의 `context`·`submit`·`cancel`만 노출한다.
  - 앱 창은 이 preload를 싣지 않는다. 키 입력 창에는 `aikDesktop`이 없다.
- **발신자 검사**
  - 메시지마다 열린 창의 `webContents`, 그 main frame, URL `aik://app/key-entry.html`을 확인한다.
  - 다르면 `UNTRUSTED_SENDER`다. 창이 없거나 파괴됐을 때도 같다.
- **메시지**
  - `{op: "context"}`: 응답은 `mode`·`display_name`·`account_label`만 담는다.
  - `{op: "cancel"}`: 창을 닫는다.
  - `{op: "submit", secret}`: 다음 키 형식 검사를 통과해야 한다.
    - 문자열이고, 비어 있지 않다.
    - UTF-8로 16,384바이트 이하다.
    - 앞뒤 공백과 제어 문자가 없다.
  - 그 밖의 형태는 `INVALID_REQUEST`다.
- **전달**
  - main은 키를 엔진 명령 `connections.add_key`·`connections.rotate_key`의 payload로 한 번 보낸 뒤 참조를 버린다.
  - 결과는 `connectionWithCheck`로 검사한다.
  - renderer의 `connections.request_key_entry`는 `{outcome, connection, check}`만 받는다. `outcome`은 `saved` 또는 `cancelled`이며, 결과에 키나 `credential_ref`는 없다.
- **기한**
  - router 기한(30분)이 지나면 창을 닫고 `BACKEND_TIMEOUT`으로 끝낸다.
  - 이미 보낸 엔진 요청은 멈추지 않는다. 그래서 저장이 끝났을 수 있다.

키가 지나는 전체 경로와 한계는 [connections-gui](connections-gui.ko.md) §3에 있다.

## 프로세스·권한 경계

| 경로 | 책임 및 실제 범위 |
| --- | --- |
| sandboxed renderer | `preload.cjs`의 `window.aikDesktop`만 호출한다. 노출 함수는 handshake·status·engineInfo·readmodelSources와 연결 함수들이다. Node·shell·전체 filesystem·raw secret API가 없다. 키를 싣는 명령도 없다 |
| sandboxed 키 입력 창 | `key_entry_preload.cjs`의 `window.aikKeyEntry`(context·submit·cancel)만 호출한다. 키를 입력받는 유일한 화면이다 |
| trusted Electron main | 실제 webContents/frame/URL/활성 workspace, 명령별 handler, 키 입력 창과 그 발신자 검사를 소유한다 |
| Python 엔진 | `connections.*`를 주입된 `aik_connect.ConnectionService`로 처리한다. 서비스가 없으면 `CONNECTIONS_NOT_CONFIGURED`다 |
| 총괄·worker·vault broker | 이번 모듈에서 제품 전체 미연결. 동일 Windows 사용자 native 경로의 OS 권한은 IPC 제한과 별개다 |

설치 스파이크의 기존 프로브 IPC를 제품 IPC가 설치됐다는 증거로 사용하지 않는다. 이 라이브러리는 실제 제품 Electron main에 등록하고 preload 두 개를 패키징한 뒤, 설치형 검증을 추가로 받아야 한다.

## 검증·원장

```bash
node --test tests/test_desktop_ipc.cjs tests/test_desktop_ipc_lifecycle.cjs tests/test_desktop_ipc_queries.cjs
node --test tests/test_desktop_ipc_connections.cjs
node --test apps/desktop/tests/key-entry.test.cjs
python tools/backlog.py validate
```

첫 줄의 세 파일은 기존 10개 테스트와 AIK-045 시점의 신규 25개 테스트다. 초기 소스에 대한 8개 실패 반례와 수정 후 실행은 [근거 기록](../evidence/desktop-ipc/2026-10-13-lifecycle/README.ko.md)에 구분한다. 2026-10-14 연결 명령을 더한 뒤 네 파일 48개가 모두 통과했고, `apps/desktop/tests/key-entry.test.cjs`를 포함한 `apps/desktop` 시험 15개도 통과했다(Linux). Windows 결과는 CI로만 기록한다.

정본 `planning/backlog.sqlite3`에는 AIK-004의 구현 근거와 상태 이력을 기록하고, 미완료 AIK-083 및 제품 통합 조건 때문에 **blocked**로 유지한다. 다른 태스크·완료 기준·의존성은 변경하지 않는다. 단위시험 통과는 Windows 11 설치형 제품·OS principal 격리·VM/AGS 통합의 완료가 아니다.
