# AIK-087 프로젝트 폴더 GUI 로컬 근거 (2026-10-14)

범위: Linux(x64) 개발 컨테이너에서 실행한 데스크톱 셸 smoke 두 번. Windows 실행 근거가 아니다. VM이 없으므로 프로젝트 폴더 등록은 엔진이 `VM_NOT_CONFIGURED`로 막는다. native 폴더 선택 창은 열지 않았다(NOT_RUN).

## 실행

- 셸: `apps/desktop`(Electron 44.7.0), 일반 사용자 `aiksmoke`, 빈 HOME, `xvfb-run`. Chromium sandbox는 켜 둔 채 실행했다.
- 엔진: 소스 엔진(`apps/desktop/engine/aik_engine_entry.py`, `python -I`). workspace 서비스는 VM project port 없이 조립됐다.
- 검사: `python -I tools/check_desktop_smoke.py`. `PASS` 보고서는 패널 상태를 엔진 `workspaces.list` 답과 대조하고, 엔진이 없으면 패널이 `UNAVAILABLE`이어야 한다.

| 실행 | 결과 | 검사 도구 |
|---|---|---|
| 엔진 있음 (`connected/`) | `PASS`, exit 0 | OK |
| 엔진 없음 (`no-engine/`) | `BLOCKED`, exit 2 | OK |

smoke 뒤 HOME에 registry·설정·scope 파일이 생기지 않았다.

## 관측한 값

- `connected/report.json`
  - `workspaces`: 엔진 `workspaces.list` 답 `{"vm": "NOT_CONFIGURED", "registry": "ABSENT", "active": null, "workspaces": []}`. 읽기는 파일을 만들지 않는다.
  - `workspaces_panel`: `{"state": "EMPTY", "vm": "NOT_CONFIGURED"}`.
  - `routes`의 `data-state`: connections `EMPTY`, overview `NOT_CONFIGURED`, goals·workers·evidence·usage `NOT_CONNECTED`, settings `DEGRADED`.
  - `renderer_bridge`: 22개 함수. 폴더 경로를 싣는 함수는 없다(`requestFolder`는 mode·이름만 보낸다).
  - `key_entry`: `PASS`(앞 작업의 키 입력 창 probe가 계속 통과).
  - `not_run`에 "native folder dialog and registering a project folder (needs a VM project)"가 있다.
- `no-engine/report.json`: 패널 `UNAVAILABLE`, 엔진 명령은 `BACKEND_UNAVAILABLE`.
- `connected/route-settings.png`: 설정 화면 맨 위의 프로젝트 폴더 패널. VM 미연결 안내, "현재 프로젝트" 없음, 등록 폼(경로 입력란 없음)을 직접 확인했다.

## 파일과 sha256

| 파일 | sha256 |
|---|---|
| `connected/report.json` | `85a8a358c2d2602bcc27555df306eb8ba17f45b27f70d36c7660d8b8560aab14` |
| `no-engine/report.json` | `30edd0db6cd97760e19cc7b59518c5c9f509e48d61fc579f2413a144c80ff9cf` |
| `connected/route-settings.png` | `cd9688653f76df02eba3e78405e310f4aff6b0076d79b296cd8cb13aeec68154` |
| `connected/key-entry.png` | `62eda63a8f776197cf59a25c67c41a18133d6d727916c2d6f57ed7e707766a39` |

`connected/`의 나머지 경로 스크린샷 6개도 함께 보존했다. 보고서에는 비밀 값·`secret://` 참조·로컬 경로가 없다.

## 같은 커밋의 로컬 회귀

- Python `unittest discover -s tests`: 1584개 OK(skip 18). 그중 `tests.test_desktop_workspaces` 9개.
- IPC node 시험 53개 pass(`tests/test_desktop_ipc_workspaces.cjs` 5개 포함).
- `apps/desktop` node 시험 19개 pass(`folder-picker.test.cjs` 4개 포함), `npm run build` 성공.

## NOT_RUN

- 실제 VM project 생성·조회(공개 생성 command 없음). 그래서 등록된 폴더 행·전환·다시 선택·보관·복원·사용 범위 펼침은 실제 앱 화면에서 보지 못했다. 합성 DTO 시험만 있다.
- native 폴더 선택 창 실조작
- Windows 실기 실행. Windows는 아래 CI 결과만 있다.
- 한글·공백 경로와 키보드 조작을 포함한 설치본 E2E(AIK-057·064)
- native/API 작업자의 실제 cwd·생성 파일 위치 일치(AIK-044)
- Goal 입력·인가 revision 결속, 고아 VM project 위험

## Windows CI (원장 `EV-AIK-087-SHELL-WIN-CI-20261014`)

PR #29 head `8919ea4`(merge `66c6fef`)의 `desktop-shell` `shell (windows-2022)` job([run 38518102994 / job 115701372020](https://github.com/jaeseongs95/AI-Kubernetes/actions/runs/38518102994/job/115701372020)):

| 단계 | 결과 |
|---|---|
| `tests.test_desktop_engine`·`tests.test_desktop_workspaces` | Ran 25, OK(workspace 9 포함) |
| `apps/desktop` `npm test` | 19 pass(폴더 선택 창 시험 4 포함) |
| `npm run build` | 성공 |
| smoke 엔진 있음 | `OK: PASS connected on windows` |
| smoke 엔진 없음 | `OK: BLOCKED unavailable on windows` |

- 아티팩트 `desktop-smoke-windows-2022-fa24110e4f361668adc6fc7bd3be140245c3de55`(ID 11872458165), zip sha256 `47562b27dc97ec61ebb79c4e1b4a036ffd00fc6599740b4f476a819e2975dc6c`.
- 검사기는 프로젝트 폴더 패널 상태를 엔진 답과 대조한다. 패널 상태 값 자체는 로그에 따로 출력되지 않는다.
- 같은 head의 `desktop-ipc (windows-2022)`·`modules (windows-2022)`도 success였다.
