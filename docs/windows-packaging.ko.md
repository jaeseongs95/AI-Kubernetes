# Windows GUI·sidecar·설치 방식 — AIK-002 / AIK-003

작성일: 2026-10-12. 이 문서는 첫 구현 후보와 실행 가능한 검증 경로를 기록한다. **현재 개발 설치물은 AIK-002 foundation spike**이며, 제품 설치본이나 Windows 11 지원 인증을 뜻하지 않는다. 실제 결과는 해당 소스 커밋의 CI 로그와 `installed-spike.json`으로 판정한다.

## 1. 스택 결정과 VM 자산 검토

첫 후보는 **Electron 44.7.0 + React/TypeScript renderer + CPython 3.14.8 sidecar**다. Electron은 Chromium과 Node.js를 포함한 앱 실행 환경을 제공한다. 이번 spike는 renderer → 제한된 preload API → Electron main → 상속 pipe의 JSON-lines → Python sidecar → 고정 worker / Windows DPAPI 순서로 실제 프로세스를 연결한다. [Electron 개요](https://www.electronjs.org/docs/latest), [배포 방식](https://www.electronjs.org/docs/latest/tutorial/application-distribution)

| 판단 항목 | Electron 후보 | Tauri 2 대안 | 결정 |
|---|---|---|---|
| React/TypeScript 표현층 | 기존 컴포넌트 구조 참고 가능 | 같은 표현층 참고 가능 | UI 언어로 후보를 구분하지 않음 |
| 설치 시 GUI runtime | Electron 배포에 Chromium/Node 포함 | WebView2 다운로드·offline installer·fixed runtime 중 선택 필요 | 초기 spike는 runtime을 함께 묶는 Electron 선택 |
| Python 엔진 | 묶인 실행 파일과 private pipe 연결 | external binary/sidecar 연결 가능 | Python 도메인 코드를 GUI로 옮기지 않음 |
| 빌드 | Node + Windows Python packaging | Rust/MSVC + Node + Windows Python packaging | 이번 환경에서 구현·진단할 경계 수를 줄임 |
| renderer 권한 | Node 비활성, context isolation, 제한된 IPC | capability와 command 제한 | 각 후보의 실제 권한 설정을 검증해야 함 |
| 설치 크기·메모리 | 이번 CI에서 설치 크기와 프로세스 메모리 기록 | 같은 부하의 실행 측정 전 | Tauri 대비 수치 우위를 주장하지 않음 |

Tauri의 Windows 배포는 NSIS 또는 MSI이며 WebView2 공급 전략을 명시해야 한다. 인터넷 없이 설치하려면 해당 runtime을 포함하는 방식을 사용할 수 있다. 이 구조적 차이가 후보 비교의 근거이며, 아직 실행하지 않은 Tauri의 설치·메모리를 측정값으로 쓰지 않는다. [Tauri Windows installer](https://v2.tauri.app/distribute/windows-installer/)

### 고정 VM 소스에서 확인한 재사용 범위

검사한 VM 커밋은 `32bb0f9dd9f024045d24487312b50f5b703573a3`다. `apps/desktop/src/App.tsx`는 `MockEngineClient`를 생성하고 `BrowserPlatformAdapter`를 사용한다. `contracts.ts`의 `EngineClient`는 query/subscribe 및 Goal·Plan·Attempt 명령의 표현 계약이고, `CommandContext`는 actor/client/request/idempotency 식별자를 담는다. 현재 `WorkspaceSnapshot`에는 `engineSchemaVersion: 2`와 `productStatus: "NO-GO"`가 선언돼 있다. 따라서 이 DTO를 운영 엔진의 현재 스키마나 실행 성공의 근거로 그대로 복사하지 않는다. [고정 App.tsx](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/apps/desktop/src/App.tsx), [고정 contracts.ts](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/apps/desktop/src/contracts.ts)

재사용 대상은 화면 구성 원칙, 상태·오류 표시, renderer의 `EngineClient`/`PlatformAdapter` 경계다. 실제 운영 transport는 공개된 VM application/query 경계에 별도 adapter를 연결해야 한다. UI 안에 VM 도메인 권한이나 Plan 활성화 결정을 복제하지 않는다. VM 문서의 Tauri shell/capabilities는 뒤에 추가할 구조로 기술돼 있으므로, 이미 검증된 Tauri 런타임이 존재한다고 전제하지 않는다. 이번 spike는 이 설계 경계를 참고해 새로 만든 좁은 IPC 검증기이며 VM GUI 코드 이식 완료를 주장하지 않는다. [고정 GUI 설계 문서](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/gui-interface-design.md)

## 2. Windows 지원 후보와 측정 조건

아래 값은 **AIK-003에서 정한 최초 지원·측정 후보**다. 깨끗한 Windows 11 실행 증거와 AIK-083의 출시 계약이 붙기 전에는 검증 완료 지원표로 게시하지 않는다.

| 항목 | 최초 후보 / 결정 |
|---|---|
| OS | Windows 11 25H2 및 26H2의 지원 중인 Home/Pro x64, 최신 보안 누적 업데이트 |
| CPU | x64, 최소 후보 2 logical CPU / 기준 4 logical CPU |
| RAM | 최소 후보 8 GiB / 기준 16 GiB |
| 여유 디스크 | 앱·설치·업데이트를 위한 최소 후보 2 GiB / 기준 4 GiB. 사용자 workspace·산출물은 별도 |
| 화면·입력 | 최소 후보 1280×800, 키보드·마우스. 기준 1920×1080, DPI 100/125/150% 검사 |
| GPU | 첫 원격 모델 제어부에 별도 연산 GPU 요구를 두지 않음. 로컬 모델 자원은 후속 선택 범위 |
| 설치 권한 | 사용자별 설치, 일상 실행은 표준 사용자 권한 |
| 파일 시스템 | 사용자 쓰기 권한이 있는 로컬 NTFS, 한글·공백 포함 경로 검사. UNC/network share/OneDrive 동기화 중인 실행 DB는 최초 지원 범위에서 보류 |
| 오프라인 | 설치 및 로컬 설정·이력 열람은 runtime 다운로드 없이 제공. 벤더 호출에는 해당 공식 endpoint 연결 필요 |
| 네트워크 | 첫 범위는 Windows 기본 인증서 신뢰와 직접 HTTPS 연결. 기업 프록시·사용자 CA·단절 복구는 해당 연결 시나리오에서 별도 판정 |
| 첫 출시 플랫폼 gate | Windows x64. ARM64 및 macOS/Linux 제품 배포를 첫 출시 필수 gate에 추가하지 않음 |

Microsoft의 2026-09-29 지원표에서 Windows 11 25H2(build 26200)와 26H2(build 26300)가 지원 중임을 확인했다. 릴리스 직전에는 그때의 지원 종료와 누적 업데이트를 다시 확인한다. [Microsoft 지원 버전](https://learn.microsoft.com/ko-kr/windows/release-health/supported-versions-windows-client)

`windows-2022` CI는 설치 경로, 실제 GUI, frozen runtime, native DPAPI의 연결 가능성을 검증한다. GitHub의 Windows runner는 관리자 계정이며 UAC가 비활성화돼 있다. 따라서 PASS여도 Windows 11 표준 사용자, 깨끗한 실기, 최소 사양 판정을 대신하지 않는다. 실제 OS build, CPU, 메모리, 관리자 여부와 runner image는 결과 JSON에 남긴다. [GitHub runner 명세](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)

### 성능·지속 실행 측정 부하

AIK-083은 다음 조건과 출시 임계값을 **제품 성능 측정 전에** 고정한다. AIK-062는 해당 고정본의 측정 결과를 제출하고, 미충족이면 AIK-064를 통과시키지 않는다.

| 부하 | 고정할 측정 조건 | 수집 값 |
|---|---|---|
| 시작 | 최소/기준 사양, cold/warm 구분, 각 20회 | 첫 표시와 조작 가능까지의 p50/p95, GUI/sidecar 별 사용 메모리 |
| 대기 | 벤더 0/3개 연결 메타데이터, 10분 | GUI/엔진의 평균·peak working set, CPU, 유휴 재호출 수 |
| 제어부 | 보이는 세션 20개, worker 4개, GUI 조작 100회 | 입력 응답 p95/p99, event backlog, cancel→local acknowledgement |
| 병렬 실행 | 동일 목표와 성공 조건, 순차/4 worker 제한 병렬 비교 | 성공 수, 로컬 scheduling 시간, 실제 provider 대기 시간, 사용량 |
| 장기 실행 | 기준 사양 8시간, worker 기동/반환 반복과 네트워크 단절·회복 | 메모리 증가, orphan/누수, 상태 복구, 사용자에게 남은 결과 접근성 |

이번 spike의 한 번짜리 `elapsed_ms`와 main/renderer 메모리는 부품 관측값이다. provider 지연은 없는 고정 worker이므로 위 제품 성능·지속 실행 임계값의 PASS로 환산하지 않는다. 구체적 출시 임계값과 workload fixture의 최종 고정은 AIK-083의 남은 작업이다.

## 3. 설치·runtime bundle

개발 artifact는 `AIK Foundation Spike`라는 독립 appId/name의 **NSIS 사용자별 설치 EXE**다. UI는 Electron 배포로 묶고 Python sidecar는 **PyInstaller onedir**로 빌드한다. `resources/engine/aik-engine.exe`와 `_internal`의 Python DLL/표준 라이브러리를 함께 설치한다. 사용자 컴퓨터의 `python`, `node`, Rust, npm을 실행 시 찾는 fallback은 제공하지 않는다.

PyInstaller는 현재 Python interpreter와 발견한 의존성을 함께 묶으며 사용자는 별도 Python 설치 없이 실행할 수 있다. Windows bundle은 Windows에서 만든다. onedir를 선택한 이유는 포함 파일과 DLL을 직접 검사하고 sidecar/worker의 기동 실패를 추적하기 쉽기 때문이다. [PyInstaller 동작](https://pyinstaller.org/en/latest/operating-mode.html), [플랫폼별 빌드](https://pyinstaller.org/en/latest/)

| 구성 | 고정 위치 / 버전 |
|---|---|
| GUI package | `spikes/windows-foundation/package.json`, `package-lock.json` |
| Electron | 44.7.0, 실제 Chromium/embedded Node 버전은 실행 시 관측 |
| 빌드용 Node | CI 24.21.0 LTS. Electron 내부 Node 버전과 별도 |
| Python bundle | CPython 3.14.8 x64 |
| Python freezer | PyInstaller 6.22.3 |
| Python build 의존성 | `packaging/requirements-build.windows.lock`, 모든 필수 wheel 버전·SHA256 고정 |
| Windows installer | electron-builder 26.15.3, `packaging/electron-builder.cjs` |
| CI actions | checkout/setup-node/setup-python/upload-artifact의 커밋 SHA 고정 |

CPython 3.14.8의 공식 릴리스와 PyInstaller의 Python 3.14 지원을 확인했다. 빌드는 Python 버전·아키텍처가 다르면 중지하고, 해시 잠금 설치 후 `pip check`를 실행한다. 빌드 JSON은 실제 package 목록과 sidecar·Python DLL의 SHA256을 기록한다. 이 설정은 의존성 재해석을 줄이지만 모든 Windows installer가 비트 단위로 동일하다는 주장은 하지 않는다. [Python 3.14.8](https://www.python.org/downloads/release/python-3148/), [PyInstaller 변경 이력](https://pyinstaller.org/en/stable/CHANGES.html), [Node 24.21.0](https://nodejs.org/en/blog/release/v24.21.0)

## 4. 서명·업데이트 신뢰·제거 정책

이번 artifact 이름에는 `dev-unsigned`가 들어간다. `signExecutable: false`, `forceCodeSigning: false`로 개발 빌드에서 인증서를 자동 사용하지 않으며, 실행 파일 메타데이터는 제품명이 아닌 spike 이름으로 넣는다. 설치 검증은 `Get-AuthenticodeSignature`의 실제 결과도 기록한다. [electron-builder Windows 설정](https://www.electron.build/v26/docs/win/)

정식 제품의 기준은 다음과 같다. 아직 이 기준을 충족한 정식 설치물을 만들었다는 뜻은 아니다.

- 사용자에게 표시할 게시자와 일치하는 Authenticode 서명, SHA-256, 신뢰 가능한 timestamp를 사용한다. 서명키·암호를 저장소나 앱 안에 넣지 않는다. 정식 빌드는 서명 설정이 없으면 실패해야 한다.
- 첫 업데이트 방식은 **서명을 검증한 전체 설치본의 사용자 실행**으로 정한다. 자동 업데이트 service는 이번 spike에 포함하지 않는다. 서명/게시자/버전/해시의 검증, downgrade 차단, migration 실패 시 일관된 복구를 AIK-057/058/061에서 연결한다.
- 기본 제거는 앱 실행 파일을 제거하고 사용자 workspace와 이력은 보존한다. 자격 증명 해제와 전체 로컬 데이터 삭제는 제품 GUI의 별도 명시적 작업으로 제공하고 실행 중 작업·백업·복구 정책과 묶는다. spike NSIS의 `deleteAppDataOnUninstall: false`가 제품 데이터 삭제 GUI의 완료를 뜻하지 않는다.
- 이번 CI는 spike의 실제 설치·GUI 실행·제거를 확인한다. 제품 업데이트, 다른 사용자 계정의 접근, 정식 appData 보존·완전 삭제, 서명 거부 시험은 아직 해당 제품 gate의 실행 대상이 아니다.

Windows DPAPI는 로그인한 Windows 사용자 자격에 연결된 저장 보호다. 동일 사용자 권한의 다른 앱과의 격리까지 제공한다고 해석하지 않는다. spike의 공개 synthetic 데이터 생성·암호화·디스크 왕복·복호화·정리는 [Windows vault spike 설명](../spikes/windows-foundation/windows-vault-spike.ko.md)에 기록돼 있다.

## 5. 실제 실행 절차와 결과 계약

빌드 컴퓨터에서 Node 24.21.0, Windows x64 CPython 3.14.8과 PowerShell을 사용한다. 아래 명령은 저장소 루트 기준이다.

```powershell
npm ci --prefix spikes/windows-foundation --no-audit --no-fund
$env:PYTHONPATH = Join-Path (Get-Location) 'spikes/windows-foundation/python'
python -m unittest discover -s spikes/windows-foundation/tests -v
$env:AIK_SPIKE_PYTHON = (Get-Command python).Source
npm test --prefix spikes/windows-foundation
npm run build --prefix spikes/windows-foundation
./scripts/windows/build-sidecar.ps1
npm run package:win --prefix spikes/windows-foundation
./scripts/windows/verify-installed-spike.ps1
```

CI는 각 native command가 실패하면 다음 빌드 단계로 진행하지 않는다. 설치 검사에서는 다음 동작을 수행한다.

1. 해당 build의 설치 EXE 하나를 선택해 해시·크기·서명 상태를 기록한다.
2. 저장소 밖의 임시 **한글·공백 경로**에 설치하고 GUI exe, sidecar exe, Python DLL의 존재·해시를 확인한다.
3. 빈 외부 작업 디렉터리에서 설치된 GUI를 실행한다. child PATH에는 Windows 시스템 경로만 남기며 `PYTHONHOME`, `PYTHONPATH`, `NODE_OPTIONS`, `ELECTRON_RUN_AS_NODE`를 제거한다.
4. 실제 창의 renderer 버튼을 통해 typed IPC의 health·고정 worker·native DPAPI 검사를 요청한다. main에서 해당 부품을 직접 호출한 결과로 바꾸지 않는다.
5. 앱이 쓴 보고서와 screenshot을 회수한다. Windows·보이는 창·renderer Node 차단·세 검사 PASS·frozen Python 3.14.8·`product_integration: NOT_RUN`을 모두 요구한다.
6. 설치된 uninstaller로 제거하고 GUI/sidecar 실행 파일이 제거됐는지 확인한다. 이 실행이 만든 임시 디렉터리만 정리한다.

PATH에서 개발 도구를 숨기는 검사는 외부 Python/Node에 대한 실행 의존성을 찾기 위한 검사다. GitHub runner에 개발 도구가 실제로 하나도 없다는 증거로 바꾸지 않는다. 연결된 실제 사용자 시크릿이나 벤더 계정은 사용하지 않는다.

| 산출물 | 내용 |
|---|---|
| `packaging/build/sidecar-build.json` | 빌드 revision·실제 Python package 목록·engine/DLL 해시 |
| `packaging/evidence/installed-spike.json` | OS·권한·설치본 해시, install/bundle/GUI/uninstall 판정 |
| `packaging/evidence/application-report.json` | 실제 GUI가 생성한 health/worker/vault 응답과 측정값 |
| `packaging/evidence/screenshot.png` | 실제 renderer 화면 캡처 |
| GitHub Actions artifact | 위 근거와 검사에 사용한 개발 설치본. 릴리스 게시와 별개 |

exit code는 PASS `0`, FAIL `1`, 실행 조건 미충족 BLOCKED `2`다. Windows가 없거나 실제 실행을 하지 못한 경우 `NOT_RUN/BLOCKED`로 남긴다. Linux의 렌더러 build, IPC 단위시험, worker 시험은 각각의 범위만 증명한다. **AIK-002 전체 완료에는 Windows 실행 증거와 후보 선택 검토가, AIK-003 전체 완료에는 지원 대상 Windows 11의 깨끗한 설치 검증이 추가로 필요하다.**
