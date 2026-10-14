# AIK-046·047 연결 온보딩·관리 GUI 로컬 근거 (2026-10-14)

범위: Linux(x64) 개발 컨테이너에서 실행한 데스크톱 셸 smoke 두 번. Windows 실행 근거가 아니다. 실제 API 키 입력·저장, 벤더 검사, 공식 로그인은 실행하지 않았다(NOT_RUN).

## 실행

- 셸: `apps/desktop`(Electron 44.7.0), 일반 사용자 `aiksmoke`, `xvfb-run`. Chromium sandbox는 켜 둔 채 실행했다.
- 엔진: 소스 엔진(`apps/desktop/engine/aik_engine_entry.py`, `python -I`). composition root가 connector 4개를 조립했다.
- 검사: `python -I tools/check_desktop_smoke.py`.

| 실행 | 결과 | 검사 도구 |
|---|---|---|
| 엔진 있음 (`connected/`) | `PASS`, exit 0 | `OK: PASS connected on linux` |
| 엔진 없음 (`no-engine/`) | `BLOCKED`, exit 2 | `OK: BLOCKED unavailable on linux` |

## 관측한 값 (`connected/report.json`)

- `routes`: 7개 모두 렌더. `connections` 경로의 `data-state`는 `EMPTY`(연결 없음, 온보딩 표시).
- `connections`: 엔진 `connections.list` 응답 `{"store": "ABSENT", "connections": []}`. 읽기는 저장소를 만들지 않는다.
- `key_entry`: `PASS`. renderer bridge의 `requestKeyEntry`로 키 입력 창을 열었다.
  - 창 격리: 키 입력 창 bridge가 `cancel,context,submit`뿐이고 `aikDesktop`·`require`가 없다. 앱 창에는 `aikKeyEntry`가 없다(`isolated: true`).
  - 입력란: `password`.
  - 키를 입력하지 않고 취소했고, renderer는 `cancelled`를 받았다.
- `renderer_bridge`: 16개 함수. 키를 싣는 함수는 없다.

## 파일과 sha256

| 파일 | sha256 |
|---|---|
| `connected/report.json` | `a27b680f93f46412f3ece85fff28c145f989bcb39c9031f8c95845c31e99b3da` |
| `no-engine/report.json` | `971aa2ed6fecc04b007f8a738704e7190324100d4983a80873c465b16c00c64f` |
| `connected/key-entry.png` | `62eda63a8f776197cf59a25c67c41a18133d6d727916c2d6f57ed7e707766a39` |
| `connected/route-connections.png` | `4268b72501a3bbc29e13c91da0e6b44dc6dc875eefa6e50469181bfe46f56849` |

`connected/`의 나머지 경로 스크린샷 6개도 함께 보존했다. 보고서에는 비밀 값·`secret://` 참조·로컬 경로가 없다.

이 smoke를 실행한 직후 렌더러에서 온보딩의 벤더 묶음 제목만 고쳤다(연결자 표시 이름 대신 provider 식별자). 수정 뒤 두 smoke를 다시 실행했고, 이 디렉터리의 파일이 그 재실행 결과다.

## NOT_RUN

- 실제 API 키 저장·벤더 검사, Claude Code·Codex 공식 로그인·로그아웃
- Windows DPAPI·Linux 키링 위 연결 서비스 실행, Windows 화면 캡처 차단
- 실행 중 작업 영향(VM 미연결, AIK-044)
- Windows 실기 실행. Windows는 아래 CI 결과만 있다.

## Windows CI (원장 `EV-AIK-046/047-SHELL-WIN-CI-20261014`)

PR #27 head `e155709`(merge `5168480`)의 `desktop-shell` `shell (windows-2022)` job([run 38513315966 / job 115686489947](https://github.com/jaeseongs95/AI-Kubernetes/actions/runs/38513315966/job/115686489947)):

| 단계 | 결과 |
|---|---|
| `tests.test_desktop_engine` | Ran 16, OK |
| `apps/desktop` `npm test` | 15 pass(키 입력 창 시험 5 포함) |
| `npm run build` | 성공 |
| smoke 엔진 있음 | `OK: PASS connected on windows` |
| smoke 엔진 없음 | `OK: BLOCKED unavailable on windows` |

- 아티팩트 `desktop-smoke-windows-2022-51684809c8cf16ed8c674c468c54b844be3c9739`(ID 11869953605), zip sha256 `4377aec5252d0c542bf398913b0b7c2624222e9c6b7b14d6e556b9556cc59e5b`.
- 이 job 로그에는 `key_entry` 값이 따로 출력되지 않는다. 검사기는 `PASS`와 `NOT_RUN`을 모두 허용하므로 Windows의 키 입력 창 probe 결과는 이 근거로 확정하지 않는다.
- 같은 head의 `desktop-ipc (windows-2022)`·`modules (windows-2022)`도 success였다.
