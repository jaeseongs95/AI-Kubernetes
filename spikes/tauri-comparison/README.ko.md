# AIK-002 Windows 데스크톱 후보 비교

이 디렉터리는 기존 Electron 기반 Windows foundation spike와 비교하기 위한 **독립 Tauri 2 실행본**이다. 제품 셸을 교체하지 않는다. 실제 Windows CI 결과가 나오기 전까지 Tauri 설치·GUI·worker·DPAPI 실행 결과는 `NOT_RUN`이다.

AIK-002의 기존 검증 문구인 `지원 후보별 설치성·IPC·worker 기동·메모리 비교`를 실행 가능한 검증으로 구체화한다. 같은 workflow job에서 같은 commit의 Electron과 Tauri를 빌드·설치하고, 한 번 freeze한 동일 CPython sidecar를 두 앱에 포함한다. 두 설치본에서 `engine_sha256`이 같은지 검사한다.

## 범위와 고정 버전

| 항목 | Electron 후보 | Tauri 비교 후보 |
| --- | --- | --- |
| 위치 | `spikes/windows-foundation` | 이 디렉터리 |
| 네이티브 셸 | Electron 44.7.0 | Tauri 2.12.3, Rust 1.99.0 |
| 렌더러 | React 19.2.8, TypeScript 7.0.2, Vite 8.2.2 | 동일 버전·화면 배치·세 개 확인 버튼 |
| GUI 엔진 공급 | Electron에 Chromium 포함 | NSIS에 Evergreen WebView2 offline installer 포함 |
| 패키징 | 기존 electron-builder 설정 | Tauri CLI 2.12.2, tauri-build 2.7.2, NSIS currentUser |
| Python·worker | CPython 3.14.8, PyInstaller 6.22.3 onedir | 왼쪽과 같은 freeze 출력 |
| 최초 지원 후보 | Windows 11 x64 | 비교 실행도 Windows x64 한정 |
| CI 관측 환경 | Windows Server 2022 | 같은 job의 같은 runner |

npm과 Cargo의 직접 의존성을 정확한 버전으로 고정하고 생성된 `package-lock.json`과 `Cargo.lock`을 포함한다. Rust official distribution checksum과 crates.io registry를 확인해 버전을 선택했다. `npm ci`와 Cargo `--locked`로 의도하지 않은 의존성 재해결을 방지한다. WebView2는 Evergreen 공급 방식이므로 미리 설치된 호환 runtime을 재사용할 수 있으며, 실행 당시 실제 판본과 바이너리 hash를 별도 기록한다. 이 runtime을 Cargo/npm lock이 고정한다고 주장하지 않는다.

## 동일 작업과 권한 경계

세 버튼은 실제 React `onClick`을 통해 `run_check` Tauri IPC를 호출한다. Rust는 `health`, `worker_probe`, `vault_probe` enum만 받는다. renderer는 비밀 값, 실행 파일, 경로, argv, shell, 보고서 결과를 제출할 수 없다. AppManifest에 등록한 세 명령 `is_smoke`, `run_check`, `finish_smoke`만 local main window capability로 허용한다. 네이티브에서도 창 label·local origin·경로를 검사한다.

Rust가 설치 디렉터리의 `engine/aik-engine.exe`만 실행하고, 비공개 stdin/stdout pipe를 통해 기존 Python 계약을 그대로 사용한다. 중첩 duplicate key, frame 크기, nonce, 요청 UUID, workspace, command, response schema를 검사한다. Python 프로세스와 worker는 Windows Job Object에 포함된다. 종료 시 stdin EOF와 제한 시간 내 프로세스 회수로 자손을 정리한다.

`health`는 실제 frozen sidecar와 판본·PID를 확인한다. `worker_probe`는 별도 worker가 42 bytes 합성 파일을 쓰고 fsync·읽기·SHA256·삭제하는 작업이다. `vault_probe`는 현재 Windows 사용자 DPAPI로 합성 값을 보호·복원·삭제한다. 실제 벤더 로그인, 모델 지정, AGS·VM 제품 경로는 이 비교로 구현 완료되지 않는다.

`finish_smoke`는 renderer가 제출한 PASS를 받지 않는다. 네이티브가 보관한 실제 응답 세 개와 실제 window visibility로 report를 만든다. 스모크 자동화는 실제 화면의 세 결과가 PASS임을 확인한 뒤 이 명령을 호출한다. 보고서 작성 후 창을 열어 두어 PowerShell이 실제 HWND와 WebView2 프로세스·화면을 관측한다.

## 실제 실행 절차

Windows x64, pinned Node·Python·Rust, MSVC build tools를 가진 개발 환경에서 저장소 root 기준으로 실행한다. 패키징에 쓰이는 빌드 도구가 설치 앱의 실행 의존성이라는 뜻은 아니다.

```powershell
cd spikes/windows-foundation
npm ci --no-audit --no-fund
npm run build
cd ../..
./scripts/windows/build-sidecar.ps1
cd spikes/windows-foundation
npm run package:win
cd ../..
./scripts/windows/verify-installed-spike.ps1
cd spikes/tauri-comparison
npm ci --no-audit --no-fund
npm run build
cd src-tauri
$env:AIK_SPIKE_PYTHON = (Get-Command python).Source
cargo test --locked --target x86_64-pc-windows-msvc
cd ..
npm run package:win
cd ../..
./scripts/windows/verify-stack-comparison.ps1
```

자동 실행 정의는 `.github/workflows/windows-stack-comparison.yml`이다. `GITHUB_SHA`, `GITHUB_RUN_ID`를 비교 증거에 사용하므로 같은 workflow 실행이라는 최종 gate는 실제 CI에서 판정한다. 로컬 두 앱의 검사는 실행할 수 있으나 workflow run identity를 만들어서 PASS로 처리하지 않는다.

Tauri installer는 `src-tauri/target/x86_64-pc-windows-msvc/release/bundle/nsis/`에 생성된다. 임의로 만든 한글·공백 경로에 사용자별 silent install 후, source tree 밖의 빈 working directory에서 실행한다. 앱 child PATH에서 개발 도구를 제외하고 Python override를 지운다. 설치된 Python DLL과 실제 frozen 결과를 확인한다. 이 절차는 개발 도구가 물리적으로 없는 Windows 11 환경을 대체하지 않는다.

## 증거와 비교 해석

결과는 저장소 root의 `packaging/evidence/`에 보존한다.

- 기존 Electron: `installed-spike.json`, `application-report.json`, `screenshot.png`.
- Tauri: `tauri/installed-spike.json`, `tauri/application-report.json`, `tauri/screenshot.png`, 가능하면 `tauri/screenshot-desktop-crop.png`, stdout/stderr.
- 두 후보 비교: `stack-comparison.json`; installer bytes·hash, 설치/제거, 같은 frozen engine hash, 세 작업, 각 보고서 및 한계 포함.
- CI artifact: 두 unsigned installer와 위 증거. 정식 release나 publisher 서명 완료본은 아니다.

| 관측값 | 시점·방법 | 비교할 때의 한계 |
| --- | --- | --- |
| Electron main RSS | 세 작업 후 Node `process.memoryUsage().rss` | Tauri native main과 셸 구조가 다름 |
| Electron renderer working set | 세 작업 후 Electron `getProcessMemoryInfo().workingSetSize` | KB 단위; 전체 Chromium process 합계가 아님 |
| Tauri native main working set | 세 작업 후 Win32 `GetProcessMemoryInfo` | bytes 단위; WebView2는 별도 프로세스 |
| Tauri process tree | 같은 native snapshot의 현재 자손 | 종료한 worker는 없고 공유 메모리는 중복 계상될 수 있음 |
| WebView2 renderer/browser/GPU/utility | native report 직후, 화면 capture 전 `.NET Process.WorkingSet64` | native snapshot과 엄밀히 동시에 수집되지 않음; 각 역할을 구분해 보존 |
| WebView2 실제 판본 | Tauri runtime API와 현재 자손의 executable FileVersion·SHA256 | runtime 부재 PC의 설치 검증은 별도 `NOT_RUN` |
| Installer 크기 | 실제 검증한 unsigned NSIS file bytes | Tauri는 offline WebView2 공급 포함, Electron은 Chromium 포함이라는 구체적 정책 비교 |

측정값은 단일 관측이다. 전체 앱의 최고/평균 메모리, 시작 성능 우위, 장시간 안정성 또는 정식 제품의 성능을 단정하지 않는다. 현재 Tauri renderer의 Node 접근 여부는 네이티브에서 직접 관측하지 않아 `null / NOT_MEASURED_BY_NATIVE_BACKEND`로 둔다. 명시적 capability와 제한된 IPC 구현을 근거로 삼되, 이를 동적 침투 검증 PASS로 바꾸지 않는다.

PowerShell의 screenshot capture PASS는 실제 visible HWND를 capture했다는 뜻이다. 저장된 이미지의 내용·겹침·한글 표시 등은 실제 이미지를 연 별도 검토에서 판정한다. renderer가 실패해 native report에 도달하지 못해도 종료 전에 가능한 화면과 로그를 보존한다.

CI가 실제 PASS여도 Windows 11 clean standard-user support qualification, 서명·배포·업데이트 trust, 실제 제품 integration은 `NOT_RUN`이다. AIK-002의 비교 결과와 AIK-003의 정식 출시 gate를 분리한다.

## 선택 근거와 대안

두 후보 모두 React/TypeScript UI를 사용할 수 있다. VM의 현 GUI에서 화면 구조와 계약 분리 원칙을 참고할 수 있지만, MockEngineClient·schema가 다른 DTO·브라우저용 상태를 운영 backend로 복사하면 안 된다. Python 공개 façade와 별도 adapter로 연결하는 제품 과제가 남는다.

현재 구현 후보인 Electron은 이미 작성한 Node/Python bridge와 Chromium bundle 경로를 계속 사용한다. Tauri 비교본은 Rust·WebView2·Windows toolchain 및 capability의 구현·배포 차이를 실제로 확인한다. 측정 전에 Tauri가 반드시 더 작거나 빠르다고 결론 내리지 않는다. 비교 결과만으로 Electron 경로를 자동 교체하지 않는다.

## 공식 근거

- Tauri Windows installer·NSIS·WebView2 공급 모드: <https://v2.tauri.app/distribute/windows-installer/>
- Tauri configuration·resource map·Windows bundle: <https://v2.tauri.app/reference/config/>
- Tauri Rust 호출과 sync/async 실행: <https://v2.tauri.app/develop/calling-rust/>
- Tauri capability와 application command 권한: <https://v2.tauri.app/security/capabilities/>
- Tauri 2.12.3 crate: <https://crates.io/crates/tauri/2.12.3>
- tauri-build 2.7.2 crate: <https://crates.io/crates/tauri-build/2.7.2>
- Rust distribution manifest: <https://static.rust-lang.org/dist/channel-rust-1.99.0.toml>

