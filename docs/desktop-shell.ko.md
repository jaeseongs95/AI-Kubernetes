# AIK-045 데스크톱 GUI 셸 (`apps/desktop`)

상태: **구현(부분)**. 대응 작업은 AIK-045이고 설계 원칙은 PRN-OS·PRN-VENDOR·PRN-SOLID다. 선행 작업 AIK-004·043·044가 끝나지 않았으므로 AIK-045는 done이 아니다. 이 문서는 원장 상태를 바꾸지 않는다.

## 구성

| 위치 | 역할 |
|---|---|
| `apps/desktop/electron/main.cjs` | Electron main. spike(AIK-002)의 보안 설정을 그대로 쓰고, renderer IPC는 AIK-004 `DesktopRouter`로만 받는다 |
| `apps/desktop/electron/platform.cjs` | OS 차이를 모은 유일한 파일: 엔진 실행 파일 이름, 엔진에 넘길 환경 변수 allow list, spawn 옵션. 다른 파일에서 `process.platform`을 쓰면 시험이 실패한다 |
| `apps/desktop/electron/engine.cjs` | `EngineClient`(stdio JSON-lines, 프로세스별 nonce, 64KiB frame, 동시 4건, 기한. `connections.*`는 엔진의 연결 전용 줄에서 순서대로 처리되므로 150초)와 `EngineSupervisor`(실패 뒤 5초 간격 재시작, 버전 불일치는 재시작하지 않음) |
| `apps/desktop/electron/key-entry.cjs` | AIK-046/047 main 소유 API 키 입력 창. 전용 preload(`src/aik_ipc/key_entry_preload.cjs`)·채널(`aik:key-entry:v1`)·발신자 검사. 키는 이 창에서 엔진으로만 간다 |
| `apps/desktop/src/` | React renderer: 화면 7개, 연결 상태 표시줄, typed client(`api.ts`) |
| `apps/desktop/engine/aik_engine_entry.py` | `python -I`로 소스 엔진을 띄우는 진입점. 벤더 connector를 조립하는 composition root(공통 패키지는 벤더 패키지를 import하지 않는다) |
| `src/aik_desktop_engine/` | 엔진(Python 표준 라이브러리만). 읽기 명령 `system.status`, `engine.info`, `readmodel.sources`와 AIK-046/047 `connections.*`(주입된 `aik_connect.ConnectionService`로 전달) |
| `src/aik_ipc/desktop.cjs`, `connections.cjs`, `preload.cjs` | AIK-004 경계에 읽기 명령 두 개와 비밀 없는 연결 명령을 더했다 |

데이터 흐름: renderer → preload(`window.aikDesktop`) → `DesktopRouter`(발신자·workspace·형식 검증, 응답 DTO 복사·동결) → main의 reader → `EngineSupervisor` → 엔진 프로세스.

## 화면

| 경로 | 상태 | 표시 내용 |
|---|---|---|
| `#/overview` 총괄 | 엔진 연결 | 시스템 상태, 엔진 정보, read model source 표(cursor·gap·conflict·stale). read model은 표시용 사본(`authoritative=false`)임을 항상 적는다 |
| `#/settings` 설정 | 엔진 연결 | 엔진 버전·프로토콜·플랫폼·Python·런타임 종류·상태 저장소·read model·director 상태(읽기 전용, 경로는 표시하지 않음) |
| `#/connections` 연결 관리 | 엔진 연결 | AIK-046·047. 연결 목록·검사·키 교체·이름 변경·로그아웃·재인증·삭제와 첫 연결 온보딩. `data-state`는 `NOT_CONFIGURED`·`UNAVAILABLE`·`EMPTY`·`READY` 중 하나 |
| `#/goals` Goal | 미연결 | AIK-051(VM 연결 필요) |
| `#/workers` worker | 미연결 | AIK-052 |
| `#/evidence` 근거 | 미연결 | AIK-054 |
| `#/usage` 사용량 | 미연결 | AIK-055 |

미연결 화면은 "미연결 · 준비 중"과 연결할 작업 번호만 보여 준다. 빈 표나 예시 숫자는 그리지 않는다(AC2).

## 연결 상태 표시줄 (AC3)

renderer는 5초마다 세 읽기 명령을 호출하고, 결과를 다음 중 하나로 표시한다.

| 값 | 표시 | 조건 |
|---|---|---|
| `connected` | 연결됨 | 최근 조회 성공 |
| `unavailable` | 엔진 연결 불가 | 엔진이 없거나 응답하지 않음(`BACKEND_UNAVAILABLE`·`BACKEND_TIMEOUT`) |
| `incompatible` | 버전 불일치 | 엔진이 다른 프로토콜 버전을 알림(`BACKEND_INCOMPATIBLE`) |
| `stale` | 오래된 상태 | read model source에 cursor gap이 있거나, 마지막 성공이 15초보다 오래됨. 마지막으로 확인한 값과 시각을 함께 보여 준다 |

## 엔진이 보고하는 값

상태·read model 명령은 관측한 값만 돌려주고 아무것도 만들지 않는다. `connections.*` 쓰기 명령만 연결 메타데이터(설정 DB 옆 `connections.sqlite3`)와 사용자별 vault를 바꾼다. 연결 읽기 명령도 저장소가 없으면 빈 목록을 돌려줄 뿐 만들지 않는다.
- **상태 저장소**: `WRITABLE`·`ABSENT`·`NOT_WRITABLE`·`UNAVAILABLE` 중 하나다. `ABSENT`는 첫 사용 전이라는 뜻이며 오류가 아니다. 엔진은 디렉터리를 만들지 않는다.
- **read model**: `NOT_CONFIGURED`·`ABSENT`·`READY`·`BUSY`·`INVALID`·`SCOPE_MISMATCH` 중 하나다. 엔진은 DB를 만들지 않는다.
- **director**: AIK에는 아직 VM 연결 설정이 없어서 항상 `NOT_CONFIGURED`다. 그래서 시스템 상태는 정상 환경에서도 `DEGRADED`다.

엔진 설정은 개발 환경 변수로만 받는다. renderer는 바꿀 수 없다.
- `AIK_DESKTOP_PYTHON`: 절대 경로
- `AIK_DESKTOP_READMODEL`, `AIK_DESKTOP_USER_SCOPE`: 둘 다 주거나 둘 다 빼야 한다

패키지 모드는 `resources/engine/aik-engine(.exe)`를 쓴다. 이 frozen 엔진의 빌드는 AIK-057이다.

## 실행과 검증

```bash
cd apps/desktop && npm ci && npm run build && npm test
AIK_DESKTOP_PYTHON="$(python3 -c 'import sys;print(sys.executable)')" npx electron . --smoke --report-dir=/tmp/aik-desktop-smoke
python -I tools/check_desktop_smoke.py /tmp/aik-desktop-smoke/report.json --expect PASS --connection connected
python -m unittest tests.test_desktop_engine
node --test tests/test_desktop_ipc.cjs tests/test_desktop_ipc_lifecycle.cjs tests/test_desktop_ipc_queries.cjs tests/test_desktop_ipc_connections.cjs
```

- **smoke 동작**: 7개 경로를 차례로 열어 경로마다 스크린샷을 남기고 `report.json`을 쓴다.
  - AIK-046/047: renderer bridge로 키 입력 창을 열고, 두 창의 bridge가 섞이지 않았는지와 입력란이 password인지 확인한 뒤 스크린샷(`key-entry.png`)을 남기고 취소한다. 키는 입력하지 않는다. API 키 connector가 없으면 `NOT_RUN`이다.
  - 판정은 renderer의 주장이 아니라 main이 직접 받은 엔진 응답으로 정한다.
  - 엔진 응답이 있으면 PASS(exit 0), 엔진이 없으면 BLOCKED(2), 셸 결함이면 FAIL(1)이다.
- **Linux에서 실행할 때**: Chromium sandbox(`app.enableSandbox()`)는 root 실행을 거부한다.
  - 일반 사용자로 실행해야 한다.
  - CI는 Ubuntu에서 setuid `chrome-sandbox` helper를 쓴다.
  - 이 때문에 셸의 보안 설정을 끄지 않는다.
- **CI**: `desktop-shell.yml`이 windows-2022와 ubuntu-24.04에서 각각 smoke 두 번(엔진 있음 PASS, 엔진 없음 `unavailable`)을 돌리고, 보고서와 스크린샷을 artifact로 남긴다.

## NOT_RUN과 한계

- **패키징**: 설치본과 frozen 엔진(AIK-057·061)은 아직 없다. 패키지 모드에서는 preload와 router를 `src/aik_ipc`에서 앱 리소스로 옮겨야 한다.
- **실제 운영 데이터**: 실제 키 저장·벤더 검사와 공식 로그인은 실행하지 않았다(NOT_RUN). VM Goal, worker, 근거, 사용량은 화면에 연결되지 않았다(AIK-044 선행).
- **macOS**: `platform.cjs`에 항목이 없어 엔진을 띄우지 않고 "엔진 연결 불가"로 표시한다.
- **접근성·트레이·재접속 UX**: 기본 키보드 이동과 `aria-live`만 있다. 나머지는 AIK-056이다.
- **workspace**: IPC 요청의 `workspace_id`는 `local`로 고정이다. 이 값은 앱 창의 라우팅 범위다. 사용자가 고른 프로젝트 폴더(AIK-087)는 엔진의 `workspaces.select`로 저장하는 데이터이고, 이 값을 바꾸지 않는다([workspaces-gui](workspaces-gui.ko.md)).
