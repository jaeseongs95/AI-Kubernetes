# AIK-046·047 — 연결 온보딩·관리 GUI (부분 구현)

상태: **구현(부분)**. 이 문서는 AIK-046(첫 실행 온보딩·벤더 로그인 GUI)과 AIK-047(시크릿 공간·계정·연결 관리 GUI)을 함께 다룬다.

현재 코드가 있는 부분은 다음과 같다.

- 엔진 쪽 연결 서비스(`src/aik_connect/`)와 엔진 명령(`connections.*`)
- IPC 경계(`src/aik_ipc/connections.cjs`, `desktop.cjs`, `preload.cjs`)
- main이 띄우는 API 키 입력 창(`apps/desktop/electron/key-entry.cjs`, `src/aik_ipc/key_entry_preload.cjs`)
- smoke의 키 입력 창 probe

- 벤더 connector 4개(각 벤더 패키지의 `connector.py`)와 composition root(`apps/desktop/engine/aik_engine_entry.py`)
- renderer 화면(연결 관리 route, 온보딩 4단계, `public/key-entry.html`)

시험과 Linux smoke 결과는 §8에 있다. 실제 키·공식 로그인·Windows 실행은 NOT_RUN이다. 이 문서는 `planning/` 장부의 상태와 완료 판정을 바꾸지 않는다.

## 1. 목적과 수용 기준 대응

목적은 두 가지다.

- 사용자가 GUI에서 벤더 연결(API 키 또는 공식 호스트 구독 로그인)을 추가·확인·교체·삭제할 수 있게 한다.
- 이 과정에서 앱 renderer가 API 키를 보지 않게 한다.

수용 기준은 장부(`planning/backlog.sqlite3`) 원문이다. 상태 열은 코드·시험 근거이며 원장 판정이 아니다.

| 기준 | 구현 | 상태 |
| --- | --- | --- |
| 046-1 벤더·계정·구독/API 실행 경로를 선택하고 공식 인증 흐름을 시작/완료한다 | 온보딩 1~3단계: `connections.catalog`에서 connector 선택 → 별칭 → API는 `request_key_entry`(add), 구독은 `start_login`·`login_state`·`cancel_login`·`complete_login` | 코드·시험 있음. 실제 키 저장·공식 로그인 NOT_RUN |
| 046-2 API 키 입력은 마스킹하고 저장 후 실비밀을 화면 상태에 유지하지 않는다 | main 소유 키 입력 창(password 입력, 제출 즉시 입력란 비움). 앱 renderer에는 키 입력란·키 명령이 없다 | 시험·Linux smoke(창 격리·password) 있음 |
| 046-3 연결 확인→총괄 모델 설정으로 이어지며 만료/취소/오류 후 재시도 지점을 표시한다 | 온보딩 4단계: 확인되면 "총괄 모델 설정(AIK-049, 준비 중)" 안내, 취소·오류 때 실패 단계 재시도 버튼, `LOGIN_PENDING`·`NOT_SIGNED_IN`·`ACCOUNT_CHANGED`·`KEY_REJECTED` 재시도 | 화면 있음. 총괄 모델 설정 자체는 AIK-049 |
| 047-1 계정 별칭·벤더·인증 유형·연결 상태·최종 확인 시각·사용 범위를 표시한다 | 연결 관리 목록: `connections.list` DTO(`account_label`, `display_name`, `method`, `state`, `verified_at`·`last_check`, `usage`) | 화면 있음. 연결이 있는 목록은 실제 키 없이 렌더하지 않았다 |
| 047-2 연결 검사·키 교체·로그아웃·재인증·삭제를 GUI에서 수행한다 | `check`, `request_key_entry`(rotate), `logout`(구독), `start_login`(재인증), `delete`(영향 확인 후) | 코드·시험 있음. API 연결의 로그아웃은 연결 삭제(§7) |
| 047-3 원문 비밀과 민감 오류는 표시하지 않고 잠금 상태와 진행 중 작업 영향을 보여준다 | 고정 코드만 한국어 문구로 표시, `locked` → "잠김", `usage.external_checked=false` → "실행 중 작업 영향: 확인 불가(VM 미연결)" | 화면 있음. 실제 작업 영향은 VM 연결(AIK-044) 뒤 |

## 2. 구성

| 위치 | 역할 |
| --- | --- |
| `src/aik_connect/ports.py` | 벤더 중립 port. `ConnectorInfo`, `KeyVerifier`(API 경로), `HostLogin`(구독 경로), 결과 타입 `CheckResult`·`LoginStatus`. 벤더 패키지를 import하지 않는다 |
| `src/aik_connect/service.py` | `ConnectionService`. `aik_connection.ConnectionStore`(metadata), 사용자별 vault(비밀), connector를 묶는다. 모든 결과는 고정 코드가 든 plain DTO다 |
| 벤더 패키지의 `connector.py` | OpenAI API, Anthropic API, Claude 구독(Claude Code), ChatGPT 구독(Codex). 각 port를 기존 adapter·host bridge 위에 얇게 구현한다 |
| `apps/desktop/engine/aik_engine_entry.py` | composition root. connector를 만들어 엔진 `main`의 `connectors`·`vault_factory`로 넘긴다. vault는 처음 쓸 때 연다(`LazyUserVault`). 만들 수 없는 connector는 catalog에서 빠진다. `src` 밖에 둔다 |
| `src/aik_desktop_engine/server.py` | 엔진. `connections.*` 명령을 주입된 `ConnectionService`로 보낸다 |
| `apps/desktop/electron/engine.cjs` | `EngineClient`. 연결 명령과 payload를 엔진에 전달한다. `connections.*`는 엔진에서 순서를 기다릴 수 있어 기한이 150초다 |
| `src/aik_ipc/connections.cjs`, `desktop.cjs`, `preload.cjs` | renderer IPC. payload를 필드별로 검사하고, 결과를 동결 DTO로 복사한다. 경계 규칙은 [desktop-ipc](desktop-ipc.ko.md)에 있다 |
| `apps/desktop/electron/key-entry.cjs`, `src/aik_ipc/key_entry_preload.cjs` | main이 띄우는 키 입력 창과 그 전용 채널 `aik:key-entry:v1` |
| `apps/desktop/electron/main.cjs` | 위를 조립한다. 명령별 handler·기한, 키 입력 창 생성, smoke probe |
| `apps/desktop/src/connections.tsx`, `onboarding.tsx`, `loginFlow.tsx`, `connectionText.ts`, `public/key-entry.*` | 연결 관리 route(`#/connections`), 온보딩 4단계, 공식 로그인 흐름, 한국어 문구·오류 코드 번역, 키 입력 창 페이지 |

데이터 흐름은 두 갈래다.

- 키가 없는 명령: renderer → `window.aikDesktop` → `DesktopRouter` → main handler → `EngineSupervisor` → 엔진 → `ConnectionService`.
- 키가 있는 명령: 키 입력 창 → `window.aikKeyEntry` → main `KeyEntry` → `EngineSupervisor` → 엔진 → `ConnectionService` → vault.

### 2.1 connector port

- `ConnectorInfo`는 벤더 하나와 실행 경로 하나를 나타낸다. 필드는 `provider`, `execution_kind`(`api`·`subscription`), `official_host`, `host_profile`, `display_name`(1~64자)이다. `method`는 `api` → `api_key`, `subscription` → `host_login`이다.
- 서비스는 `(provider, execution_kind)` 하나에 connector 하나만 받는다. 실행 경로에 맞는 port를 구현하지 않은 connector는 생성 시점에 거절한다.
- `KeyVerifier.accepts(secret)`는 네트워크 없이 형식만 본다. `verify(connection)`는 저장된 `credential_ref`의 키를 벤더에 확인한다. connector가 vault에서 키를 꺼내는 방식은 connector 구현이 정하며, 이 문서 작성 시점에 확인하지 않았다.
- `HostLogin`은 공식 호스트 CLI의 로그인을 시작·관찰·취소한다. 로그인과 토큰은 호스트가 소유한다. `connect`는 호스트가 지금 보고하는 계정을 `account_binding`으로 돌려준다.
- port는 비밀을 반환하지 않는다. 결과 코드는 대문자 고정 코드(`[A-Z][A-Z0-9_]{1,63}`)만 허용한다.

### 2.2 ConnectionService

- 생성 인자: 절대 경로인 설정 DB 경로, connector 목록, `vault_factory`, `workspace_id`(기본 `local`), `reference_sources`.
- 엔진에서는 `default_settings_path()`와 `open_user_vault`로 만든다. connector가 없거나 생성이 실패하면 서비스가 없고, 모든 연결 명령은 `CONNECTIONS_NOT_CONFIGURED`로 답한다.
- 모든 공개 메서드는 하나의 `RLock` 안에서 실행된다.
- vault는 처음 필요할 때 연다. 등록 시 참조 확인도 `_LazyChecker`를 거쳐 쓰기 때만 vault를 연다.
- 첫 쓰기에서 workspace `local`(이름 `로컬 작업공간`)을 만든다.

### 2.3 키 입력 창

- main이 앱 창의 modal 자식으로 띄운다. `sandbox`, `contextIsolation`이 켜져 있다. `nodeIntegration`, `devTools`, `spellcheck`, webview, 새 창, 페이지 이동은 막는다. `setContentProtection(true)`로 화면 캡처를 막는다. 캡처 차단은 OS가 지원하는 곳에서만 동작하며 실측하지 않았다(NOT_RUN).
- preload는 `window.aikKeyEntry`의 `context`·`submit`·`cancel` 세 함수만 노출한다. 앱 창은 이 preload를 싣지 않는다. 키 입력 창에는 `aikDesktop`이 없다.
- 한 번에 창 하나만 연다. 두 번째 요청은 `KEY_ENTRY_BUSY`다.
- 제출이 실패하면 창이 열린 채로 오류 코드를 받는다. 사용자는 고쳐서 다시 낼 수 있다. 저장되면 창이 닫히고, renderer는 키 없는 결과(`outcome`, `connection`, `check`)만 받는다.

### 2.4 renderer 화면

화면은 다음을 지킨다. Linux smoke에서 연결 관리 화면(`EMPTY`, 온보딩 1단계)과 키 입력 창을 렌더해 스크린샷으로 확인했다. 단계별 실제 동작(키 저장, 로그인)은 NOT_RUN이다.

- 앱 화면에 키 입력 칸을 두지 않는다. 키 추가·교체는 `requestKeyEntry`로만 한다.
- `connections.list`의 `store`가 `ABSENT`이면 첫 실행 전 상태로 보여 주고, 오류로 표시하지 않는다.
- 연결마다 label, `display_name`, method, `state`, `verified_at`, `last_check`, `usage`를 보여 준다. `usage.references`가 `null`이면 사용처를 "확인 불가"로 표시한다. `external_checked`가 `false`이면 외부 사용 여부를 "알 수 없음"으로 표시한다.
- 쓰기 명령에는 마지막으로 받은 `revision`을 보낸다. `REVISION_CONFLICT`를 받으면 목록을 다시 읽는다.
- 삭제 전에 `connections.impact`를 보여 준다. 사용자가 확인했을 때만 `acknowledge_impact=true`로 보낸다.
- 키 저장 결과의 `check.state`가 `verified`가 아니면 "저장됐지만 확인되지 않음"으로 표시하고 다시 검사·재입력을 제공한다(§7 참조).
- 확인되면 다음 단계 "총괄 모델 설정(AIK-049, 준비 중)"을 비활성 버튼과 설명으로 보여 준다. 취소·오류 때는 실패한 단계(키 입력/로그인, 별칭, connector 선택)를 다시 시도하는 버튼을 보여 준다.
- `#route-connections`의 `data-state`는 첫 응답 전 `LOADING`, 그 뒤 `NOT_CONFIGURED`·`UNAVAILABLE`·`EMPTY`·`READY` 중 하나다. smoke는 첫 로드가 끝난 뒤의 값을 기록한다.
- 온보딩은 `connections.catalog`의 connector에서 고르게 한다. API는 `requestKeyEntry("add")`, 구독은 `startLogin` → `loginState` 관찰 → `completeLogin` 순서로 진행한다.
- `public/key-entry.html`은 `aikKeyEntry`만 쓴다. 입력 칸은 `id="key-input"`, `type="password"`이고, 준비되면 `document.body.dataset.ready = "1"`을 설정한다. smoke probe가 이 둘을 확인한다.

## 3. 비밀이 지나는 경로

API 키는 다음 순서로만 지나간다.

1. 사용자가 키 입력 창(별도 sandbox renderer 프로세스)의 password 칸에 입력한다.
2. preload가 `aik:key-entry:v1` 채널로 `{op: "submit", secret}`을 main에 보낸다.
3. main `KeyEntry.handle`이 발신자를 검사한다. 그 창의 `webContents`, main frame, URL `aik://app/key-entry.html`이어야 한다. 이어서 키 형식을 검사한다.
   - 문자열이어야 하고, 비어 있으면 안 된다.
   - UTF-8로 16,384바이트 이하여야 한다.
   - 앞뒤 공백과 제어 문자가 없어야 한다.
4. main `submitKey`가 엔진 명령 `connections.add_key` 또는 `connections.rotate_key`의 payload에 키를 넣는다. 이 payload는 엔진 stdin 파이프로 JSON 한 줄로 간다. 보낸 뒤 `payload.secret = null`로 참조를 버린다.
5. 엔진 `Engine.dispatch`가 `ConnectionService`를 호출한다. 응답 뒤 `payload.clear()`로 frame의 참조를 버린다.
6. 서비스가 connector의 `accepts`로 형식을 보고, `vault.put`(추가) 또는 `vault.replace`(교체)로 vault에 넣는다. 그 뒤 connector의 `verify`가 `credential_ref`로 벤더에 확인한다.

키가 남지 않게 한 곳은 다음과 같다. 근거는 코드 구조이며 시험 실행 기록은 없다.

- 앱 renderer: `add_key`·`rotate_key`는 renderer 명령이 아니다. renderer가 받는 결과에는 키·`credential_ref`가 없다.
- 응답: 엔진·서비스는 payload를 되돌려 보내지 않는다. 실패는 고정 코드로만 답한다. 처리하지 못한 예외는 `INTERNAL_ERROR`이며 traceback을 보내지 않는다.
- 로그: 이 변경의 코드는 키를 기록하지 않는다. smoke의 `observed` 기록은 router handler 결과만 담는다. `submitKey` 경로는 여기에 들어가지 않는다.
- DB: `connections.sqlite3`에는 `credential_ref`만 저장된다. 키는 vault에만 저장된다.

한계는 다음과 같다.

- Python·JavaScript 문자열은 지울 수 없다. `null` 대입과 `clear()`는 참조만 버린다. 키 입력 창 DOM, IPC 직렬화, 파이프 버퍼, JSON 문자열에 사본이 남을 수 있다.
- 등록 때 `VaultCredentialRefChecker`가 `resolve`로 참조를 확인한다. 이때 평문이 잠시 메모리에 생긴다([connection](connection.ko.md) 참조).
- 같은 OS 사용자의 native 프로세스는 이 경계 밖이다([credential-broker](credential-broker.ko.md) §5).
- 키 입력 창도 Chromium renderer 프로세스다. "renderer가 키를 보지 않는다"는 말은 앱 창 renderer에만 해당한다.

## 4. 명령표

### 4.1 renderer 명령 (IPC `aik:desktop:v1`)

| renderer 명령 | preload 함수 | payload | 처리 | 결과 |
| --- | --- | --- | --- | --- |
| `connections.catalog` | `connectionCatalog` | 없음 | 엔진 같은 이름 | `connectors[]`(`provider`, `execution_kind`, `method`, `display_name`) |
| `connections.list` | `connections` | 없음 | 엔진 같은 이름 | `store`(`ABSENT`·`READY`), `connections[]` |
| `connections.impact` | `connectionImpact` | `connection_id` | 엔진 같은 이름 | `references[]`(`kind`, `id`, `removable`), `external_checked` |
| `connections.login_state` | `loginState` | 없음 | 엔진 같은 이름 | 로그인 DTO |
| `connections.check` | `checkConnection` | `connection_id` | 엔진 같은 이름 | `connection`, `check` |
| `connections.rename` | `renameConnection` | `connection_id`, `expected_revision`, `account_label` | 엔진 같은 이름 | `connection` |
| `connections.delete` | `deleteConnection` | `connection_id`, `expected_revision`, `acknowledge_impact` | 엔진 같은 이름 | `connection_id`, `removed_references`, `secret_deleted` |
| `connections.logout` | `logoutConnection` | `connection_id`, `expected_revision` | 엔진 같은 이름 | `connection` |
| `connections.start_login` | `startLogin` | `provider`, `account_label`, `connection_id` | 엔진 같은 이름 | 로그인 DTO |
| `connections.cancel_login` | `cancelLogin` | 없음 | 엔진 같은 이름 | `cancelled`, `login` |
| `connections.complete_login` | `completeLogin` | 없음 | 엔진 같은 이름 | `connection`, `check` |
| `connections.request_key_entry` | `requestKeyEntry` | `mode`, `provider`, `account_label`, `connection_id`, `expected_revision` | main이 키 입력 창을 연다. 엔진 명령이 아니다 | `outcome`(`saved`·`cancelled`), `connection`, `check` |

payload 조합 규칙은 다음과 같다.

- `start_login`: 새 로그인은 `provider`와 `account_label`을 주고 `connection_id`는 `null`이다. 재인증은 `connection_id`만 준다.
- `request_key_entry`:
  - `add`는 `provider`와 `account_label`만 준다.
  - `rotate`는 `connection_id`와 `expected_revision`만 준다.

`request_key_entry`를 받으면 main은 창을 열기 전에 엔진에서 문맥을 확인한다.

- `add`: `connections.catalog`에서 그 벤더의 API connector를 찾는다. 없으면 `UNSUPPORTED_PROVIDER`다.
- `rotate`: `connections.list`에서 연결을 찾는다. 없으면 `UNKNOWN_CONNECTION`, API 연결이 아니면 `WRONG_METHOD`다.

키 입력 창의 `context`에는 `mode`, `display_name`, `account_label`만 들어간다.

### 4.2 엔진 전용 명령 (main → 엔진)

| 엔진 명령 | 보내는 곳 | payload |
| --- | --- | --- |
| `connections.add_key` | main `KeyEntry`의 `submitKey`만 보낸다 | `provider`, `account_label`, `secret` |
| `connections.rotate_key` | main `KeyEntry`의 `submitKey`만 보낸다 | `connection_id`, `expected_revision`, `secret` |

두 명령은 renderer 명령 집합(`desktop.cjs`의 `COMMANDS`)에 없다. renderer가 보내면 `UNSUPPORTED_COMMAND`다. 성공 결과 형태는 `connections.check`와 같다. 엔진은 명령마다 payload 키가 정확히 맞는지 검사한다. 값은 `null`·bool·int·16,384자 이하 문자열만 받는다. 값의 의미는 서비스가 검사한다.

## 5. 오류 코드

renderer가 받는 연결 오류 코드는 `CONNECTION_CODES`에 있는 것뿐이다. 엔진이 다른 코드를 보내면 main에서 `BACKEND_UNAVAILABLE`로 바뀐다. 예: `INTERNAL_ERROR`, `UNAUTHORIZED`, `INVALID_REQUEST`.

| 코드 | 뜻 | 사용자 조치 |
| --- | --- | --- |
| `CONNECTIONS_NOT_CONFIGURED` | 엔진에 연결 서비스가 없다(connector 미조립 또는 생성 실패) | 사용자가 고칠 수 없다. 설치·개발 설정을 확인한다 |
| `UNSUPPORTED_PROVIDER` | 이 벤더·실행 경로를 맡는 connector가 없다 | 목록(catalog)에 있는 항목을 고른다 |
| `INVALID_SECRET` | 키가 형식 검사를 통과하지 못했다 | 키를 다시 복사해 앞뒤 공백 없이 넣는다 |
| `INVALID_INPUT` | 요청 값이 잘못됐다 | 화면을 다시 읽고 다시 시도한다 |
| `INVALID_LABEL` | 표시 이름 규칙에 맞지 않는다 | 앞뒤 공백·제어 문자 없는 이름으로 고친다 |
| `VAULT_UNAVAILABLE` | 사용자 비밀 저장소를 쓸 수 없다 | OS 비밀 저장소 상태를 확인하고 다시 시도한다 |
| `VAULT_LOCKED` | 비밀 저장소가 잠겼거나 접근이 거부됐다 | OS 사용자 세션·저장소 잠금을 풀고 다시 시도한다 |
| `VAULT_ERROR` | 비밀 저장소가 실패했다 | 다시 시도한다. 반복되면 진단을 요청한다 |
| `DUPLICATE_CONNECTION` | 같은 label 또는 같은 계정의 연결이 이미 있다 | 다른 이름을 쓰거나 기존 연결을 쓴다 |
| `UNKNOWN_CONNECTION` | 연결이 없다 | 목록을 다시 읽는다 |
| `REVISION_CONFLICT` | 그 사이 연결이 바뀌었다 | 목록을 다시 읽고 다시 시도한다 |
| `CONNECTION_IN_USE` | 사용 중이다(미확인 또는 제거 불가 참조) | 영향 범위를 보고, 제거 불가 참조를 먼저 정리한다 |
| `STORE_BUSY` | 연결 저장소가 사용 중이다 | 잠시 뒤 다시 시도한다 |
| `STORE_UNAVAILABLE` | 연결 저장소를 쓸 수 없다 | 진단을 요청한다 |
| `KEY_REJECTED` | 벤더가 새 키를 거절했다. 연결과 vault 항목은 남기지 않았다 | 키를 다시 확인해 넣는다 |
| `KEY_REJECTED_CLEANUP_UNCONFIRMED` | 벤더가 새 키를 거절했고, 새 연결을 지우지 못했다 | 목록에서 그 연결을 삭제한다 |
| `SECRET_CLEANUP_UNCONFIRMED` | 등록이 실패했고 새로 넣은 키의 삭제를 확인하지 못했다 | 연결은 저장되지 않았다. vault에 키가 남았을 수 있다. 정리 화면은 없다 |
| `WRONG_METHOD` | 이 동작은 연결의 실행 경로에 맞지 않는다 | API는 키 교체·삭제를, 구독은 로그인·로그아웃을 쓴다 |
| `LOGIN_IN_PROGRESS` | 다른 공식 로그인이 진행 중이다 | 진행 중인 로그인을 끝내거나 취소한다 |
| `LOGIN_START_FAILED` | 공식 로그인을 시작하지 못했다 | 공식 호스트 CLI 설치·실행 여부를 확인한다 |
| `NO_LOGIN` | 시작한 공식 로그인이 없다 | 로그인을 다시 시작한다 |
| `LOGIN_PENDING` | 공식 로그인이 아직 끝나지 않았다 | 공식 로그인 창을 마친 뒤 다시 누른다 |
| `NOT_SIGNED_IN` | 공식 호스트가 로그인되어 있지 않다 | 공식 호스트에서 로그인을 마친 뒤 다시 시도한다 |
| `ACCOUNT_CHANGED` | 공식 호스트가 다른 계정으로 로그인되어 있다 | 원래 계정으로 다시 로그인하거나, 새 연결로 등록한다 |
| `LOGOUT_FAILED` | 공식 호스트 로그아웃이 실패했다 | 공식 호스트에서 직접 로그아웃 상태를 확인한다 |
| `KEY_ENTRY_BUSY` | 키 입력 창이 이미 열려 있다 | 열린 창을 마치거나 닫는다 |
| `KEY_ENTRY_UNAVAILABLE` | 키 입력 창을 열지 못했다 | 앱을 다시 시작한다 |

다른 위치에 나오는 코드는 다음과 같다.

- 키 입력 창 응답:
  - `UNTRUSTED_SENDER`, `INVALID_REQUEST`, `BACKEND_UNAVAILABLE`, `BACKEND_TIMEOUT`를 쓴다.
  - 엔진 오류는 `key-entry.cjs`의 `RETRY_CODES`에 있는 코드만 그대로 전달한다. 나머지는 `BACKEND_UNAVAILABLE`이다.
- 검사 결과의 `code`:
  - 서비스가 정하는 값은 `CHECK_FAILED`(connector 예외 또는 잘못된 결과), `ACCOUNT_CHANGED`, `SIGNED_OUT`(로그아웃 뒤)이다.
  - 그 밖의 값은 connector가 정하는 고정 코드다.
- 로그인 상태의 `code`:
  - 서비스가 정하는 값은 `LOGIN_OBSERVE_FAILED`(관찰 실패)다.
  - 그 밖의 값은 connector가 정한다.

## 6. 상태 값

### 6.1 connection `state`

판정 순서는 다음과 같다.

1. 현재 `binding_revision`에서 마지막 검사가 `verified`가 아니었으면 그 검사 상태를 쓴다.
2. 아니면, connector가 없을 때 `unsupported`다.
3. 아니면, `verified_at`이 있으면 `verified`, 없으면 `unverified`다.

| 값 | 뜻 |
| --- | --- |
| `verified` | 저장된 확인 시각이 있다. 이번 실행에서 다시 검사했다는 뜻은 아니다 |
| `unverified` | 확인 시각이 없다(새 연결, 키 교체 뒤, 거절·로그아웃 뒤) |
| `unauthenticated` | 마지막 검사에서 벤더가 키를 거절했거나 호스트가 로그인되어 있지 않았다 |
| `locked` | 마지막 검사에서 자격 증명을 읽을 수 없었다(판정은 connector) |
| `account_changed` | 마지막 검사에서 호스트가 다른 계정을 보고했다(구독) |
| `unknown` | 마지막 검사가 결론을 내지 못했다(네트워크, connector 실패 등) |
| `unsupported` | 이 엔진에 그 벤더·실행 경로의 connector가 없다 |

### 6.2 검사 `state`(`check.state`, `last_check.state`)

| 값 | 저장소에 미치는 영향 |
| --- | --- |
| `verified` | `mark_verified`로 확인 시각을 기록한다 |
| `unauthenticated` | 확인된 연결이면 `invalidate`로 확인 시각을 지운다 |
| `account_changed` | 위와 같다. 구독 연결에서 호스트 계정이 등록 계정과 다를 때 서비스가 이 값으로 바꾼다 |
| `locked` | 바꾸지 않는다 |
| `unknown` | 바꾸지 않는다 |

### 6.3 로그인 `state`

| 값 | 뜻 |
| --- | --- |
| `idle` | 시작한 로그인이 없다. `provider`는 `null`이다 |
| `pending` | 공식 로그인이 진행 중이다. 이때는 새 로그인을 시작할 수 없다 |
| `signed_in` | 호스트가 로그인을 마쳤다고 보고했다. `complete_login`으로 연결을 묶어야 끝난다 |
| `failed` | 로그인 또는 관찰이 실패했다 |
| `cancelled` | 로그인이 취소됐다 |

로그인 DTO의 `connection_id`는 재인증일 때만 채워진다. 새 로그인용으로 미리 만든 ID는 완료 전까지 renderer에 보내지 않는다. `complete_login`은 `pending`이 아니면 상태와 상관없이 `connect`를 시도한다. `NOT_SIGNED_IN`이면 로그인 기록을 그대로 둔다.

## 7. 설계 결정

- **키 교체는 `rebind` 뒤 `vault.replace`.**
  - `credential_ref`는 바꿀 수 없다. 그래서 같은 vault 항목의 값을 바꾼다.
  - 먼저 새 marker로 `rebind`해 확인 시각을 지우고 `binding_revision`을 올린다. 그 뒤 vault 값을 바꾸고 다시 검사한다.
  - `vault.replace`가 실패하면 연결은 `unverified`로 남고, vault에는 옛 키가 남는다. 이후 검사는 옛 키를 확인한다.
- **API 연결에는 로그아웃이 없다.** 키를 없애는 방법은 연결 삭제다. API 연결에 `logout`을 보내면 `WRONG_METHOD`다. 삭제 뒤 vault 항목 삭제를 확인하지 못하면 `secret_deleted=false`로 알린다. 이때 연결은 이미 지워졌다.
- **구독 로그아웃은 공식 호스트에 로그아웃을 요청한다.** port 주석상 이 PC의 로컬 효과다. 연결은 남고 확인 시각만 지운다. `expected_revision`이 현재 revision과 다르면 호스트를 호출하기 전에 `REVISION_CONFLICT`로 거절한다.
- **API 연결의 `account_binding`은 무작위 marker(`key-` + 24 hex)다.** 키에서 파생하지 않는다. 그래서 같은 키를 두 연결로 등록해도 감지하지 못한다. 구독 연결은 호스트가 보고한 계정을 쓰므로 같은 계정의 중복 등록은 `DUPLICATE_CONNECTION`이다.
- **벤더가 거절한 새 키는 남기지 않는다.**
  - `add_api_key`는 등록한 뒤 검사한다. 검사가 `unauthenticated`이면 연결과 vault 항목을 지우고 `KEY_REJECTED`로 답한다. 키 입력 창은 열린 채로 남아 키를 고쳐 넣을 수 있다.
  - 검사가 `unknown`(네트워크 실패 등)이나 `locked`이면 연결을 남긴다. 키 입력 창은 `outcome: "saved"`로 닫히고, 나중에 다시 검사할 수 있다.
  - 키 교체(`rotate_key`)에서는 옛 키를 다시 읽지 않으므로 되돌리지 않는다. 벤더가 새 키를 거절하면 연결은 `unauthenticated`로 남는다.
- **삭제 확인은 bool이다.**
  - renderer는 `acknowledge_impact`만 보낸다. 서비스는 삭제 시점의 영향 범위를 다시 계산해 그대로 `force` 삭제에 넘긴다.
  - 따라서 사용자가 본 뒤 늘어난 제거 가능 참조도 함께 지워진다. 제거 불가 참조가 하나라도 있으면 거절한다.
- **읽기는 저장소를 만들지 않는다.**
  - 설정 DB가 없거나 workspace `local`이 없으면 `list`는 `store: "ABSENT"`, 빈 목록이다. `impact`·`check`는 `UNKNOWN_CONNECTION`이다.
  - `catalog`·`login_state`는 저장소를 열지 않는다. 읽기는 vault를 열지 않는다.
  - `ConnectionStore` 생성자는 상세 파일을 만들기 때문에, 읽기는 설정 DB와 `connections.sqlite3`가 둘 다 있을 때만 저장소를 연다.
- **연결 명령은 엔진의 별도 줄에서 하나씩 처리한다.**
  - 엔진 `serve`는 `connections.*` 요청을 작업 스레드 하나의 큐에 넣는다. 상태·read model 조회는 기존 줄에서 바로 답한다. 그래서 벤더 검사가 길어도 상태 표시줄 조회가 막히지 않는다(`test_a_slow_connection_command_does_not_hold_back_status`).
  - 응답 순서는 요청 순서와 다를 수 있다. 엔진 클라이언트는 `request_id`로 짝을 맞춘다.
  - 연결 명령끼리는 차례를 기다린다. 그래서 엔진 클라이언트 기한은 `connections.*`에 150초, main router 기한은 155초, 키 입력 창은 30분이다.
- **검사·로그인 상태는 메모리에만 있다.**
  - `ConnectionService`의 마지막 검사 기록(`_checks`)과 진행 중 로그인(`_login`)은 엔진 프로세스 메모리에만 있다.
  - 엔진이 재시작하면 마지막 검사 결과와 로그인 진행 상태가 사라진다. 저장소의 `verified_at`만 남는다.
  - 예를 들어 `locked`·`unknown`이던 연결은 재시작 뒤 `verified`로 보일 수 있다.
- **workspace는 `local` 하나다.** 프로젝트 선택은 AIK-087이다.

## 8. 시험

실행 명령(저장소 루트 기준):

```bash
python -m unittest tests.test_connect_service tests.test_desktop_engine -v
node --test tests/test_desktop_ipc_connections.cjs
node --test apps/desktop/tests/key-entry.test.cjs
```

2026-10-14 Linux(x64) 개발 컨테이너에서 실행한 결과다. Windows 결과는 표 아래에 CI 결과로만 적는다.

| 시험 | 대상 | 결과 |
| --- | --- | --- |
| `tests/test_connect_service.py` | `ConnectionService`(fake connector·fake vault, 합성 데이터) | 14개 OK |
| `tests/test_desktop_engine.py` | 엔진 `connections.*` 명령, 연결 전용 처리 줄, composition root 실제 프로세스 | 16개 OK |
| `tests/test_connectors.py` | 벤더 connector 4개(가짜 transport·runner)와 서비스 결합 | 34개 OK |
| `tests/test_desktop_ipc_connections.cjs` | IPC payload 검사, 결과 DTO 검사, 키 입력 창 preload | 7개 pass |
| `apps/desktop/tests/key-entry.test.cjs` | `KeyEntry` 발신자 검사·키 형식·재시도·취소 | 5개 pass |
| 전체 Python 회귀 | — | Ran 1575, OK(skip 18) |
| smoke(엔진 있음) | 7개 경로, `key_entry` probe | `PASS`, `key_entry` `PASS` |
| smoke(엔진 없음) | 엔진 불가 표시 | `BLOCKED`(예상값) |

smoke 보고서·스크린샷은 [근거](../evidence/implementation/2026-10-14-connections-gui/README.ko.md)에 있다.

smoke `key_entry` probe의 동작은 다음과 같다.

- 앱 bridge의 `requestKeyEntry`로 키 입력 창을 연다(label `smoke probe`).
- 다음을 확인한다.
  - 키 입력 창의 bridge가 `cancel,context,submit`뿐이다.
  - 키 입력 창에 `aikDesktop`·`require`가 없다.
  - 앱 창에 `aikKeyEntry`가 없다.
  - 입력 칸이 `password`다.
  - 결과가 `cancelled`다.
- `key-entry.png`를 남긴 뒤 취소한다. 키는 입력하지 않는다.
- API connector가 없거나 catalog를 읽지 못하면 `NOT_RUN`이다. `FAIL`이면 smoke 전체가 실패한다.
- 앱 bridge 함수 목록이 바뀌어도 smoke가 실패한다.

시험 파일의 세부 사례는 이 문서에서 나열하지 않는다. Linux 통과는 개발 검증이며 Windows 지원 증거가 아니다.

Windows CI 결과(PR #27, `desktop-shell` `shell (windows-2022)`)는 다음과 같다.

- 엔진 시험: 16개 OK
- `apps/desktop` 시험: 15개 pass
- smoke(엔진 있음): `PASS`
- smoke(엔진 없음): `BLOCKED`

이 job 로그에는 `key_entry` 값이 따로 나오지 않는다. 그래서 Windows의 키 입력 창 probe 결과는 확정하지 않는다. 자세한 값은 [근거](../evidence/implementation/2026-10-14-connections-gui/README.ko.md)의 Windows CI 절에 있다.

## 9. NOT_RUN과 한계

- **실제 키**: 실제 API 키를 입력해 저장하고 벤더에 검사하는 동작은 NOT_RUN이다.
- **실제 공식 로그인**: Claude Code·Codex 공식 로그인, 취소, 로그아웃, 계정 변경 감지는 NOT_RUN이다(connector 작성 중).
- **vault backend 위 실행**: Windows DPAPI와 Linux 키링 위에서 연결 서비스를 실행하지 않았다(NOT_RUN). Windows 화면 캡처 차단(`setContentProtection`)도 NOT_RUN이다.
- **실행 중 작업 영향**: VM이 연결되지 않았다(AIK-044). 그래서 키 교체·로그아웃·삭제가 실행 중인 작업에 주는 영향을 확인하지 않았다. 엔진은 외부 참조 원천(`reference_sources`)을 주입하지 않는다. 따라서 `usage.external_checked`는 `false`이고, 외부 사용 여부는 알 수 없다.
- **총괄 모델 설정**: 어떤 연결을 총괄이 쓸지 고르는 설정은 AIK-049다. 이 문서 범위 밖이다.
- **패키징**: 패키지 모드의 preload·키 입력 창 페이지 배치와 frozen 엔진의 connector 조립은 AIK-057·061에 남아 있다.
- **목록 자동 갱신**: 연결 목록은 동작 뒤와 "새로고침"에서만 다시 읽는다.
- **고아 vault 항목**: `SECRET_CLEANUP_UNCONFIRMED`와 `secret_deleted=false` 뒤 남은 vault 항목을 찾거나 지우는 기능은 없다.
