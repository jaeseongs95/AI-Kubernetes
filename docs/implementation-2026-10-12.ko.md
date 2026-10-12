# 기존 태스크 구현·실행 기록 — 2026-10-12

**AIK-001·002의 기반 구현과 실제 실행 검증을 완료했다.** 설치형 upstream 검사기를 구현하고, 같은 Windows CI job에서 Electron·Tauri의 설치된 GUI 버튼으로 동일 Python sidecar·별도 worker·Windows 사용자 DPAPI를 실행했다. 설치·메모리 관측·화면·제거까지 통과했으며, Windows PowerShell 5.1 전달 경로도 실제 재검증했다. AIK-003은 기존의 깨끗한 후보 Windows·개발 도구 부재 조건을 ADR이 선택한 Windows 11 표준 사용자 환경에서 검증하지 못했으므로 `blocked`다.

이 기록은 [개발 태스크 정본](../planning/backlog.sqlite3)의 기존 순서와 완료 기준에 따른 구현 결과다. 요구사항 29개·태스크 90개·완료 기준 311개·의존성 365개를 변경하지 않았다. 기계가 읽는 결과는 [implementation-validation.json](../planning/implementation-validation.json)에 있다.

## 1. 구현한 코드

| 구성 | 구현 내용 | 경로 |
|---|---|---|
| Windows GUI | 한국어 React 화면, 엔진 연결·고정 worker·Windows 보관 확인 버튼과 실제 결과 표시 | `spikes/windows-foundation/src/` |
| 앱과 엔진 경계 | 제한된 preload, 발신 창·frame·URL·판본·workspace·request ID 검증, capability를 사용하는 private pipe | `spikes/windows-foundation/electron/` |
| Python sidecar·worker | 고정 JSON-lines 프로토콜, 별도 worker 프로세스에서 실제 임시 파일 생성·읽기·해시 대조·삭제 | `spikes/windows-foundation/python/aik_spike/server.py` |
| Windows 보관 시험 | 합성 데이터를 사용자 DPAPI로 보호하고 암호문 파일에 기록·읽기·복호화·정리 | `spikes/windows-foundation/python/aik_spike/vault.py` |
| Windows 설치·검증 | Python runtime을 묶은 NSIS 사용자별 개발 설치본, 실제 설치 GUI 실행·보고서·화면·제거 검사 | `packaging/`, `scripts/windows/`, `.github/workflows/windows-foundation.yml` |
| Tauri 후보 비교 | 명시적으로 허용한 세 Rust IPC 명령, 같은 frozen Python·worker·DPAPI, WebView2 판본·프로세스별 메모리·실제 창 관측 | `spikes/tauri-comparison/`, `.github/workflows/windows-stack-comparison.yml` |
| Windows 기본 PowerShell 전달 | 기존 UTF-8 source의 CP1252 해석 결함 재현, BOM 적용, Windows PowerShell 5.1에서 설치본 검사 실행 | `scripts/windows/`, [실제 전달 검증 근거](../evidence/windows-handoff/38226204128/README.ko.md) |
| upstream 계약 검사 | 고정 AGS/VM 설치 bytes·wheel identity·공개 명령·schema/façade·실제 호환 결과 대조 | `tools/inspect_upstream.py`, `contracts/upstream/` |

GUI renderer는 세 가지 제한된 메서드만 받는다. 임의 명령·경로·비밀 값을 요청하거나 응답에 넣는 프로토콜은 거부한다. sidecar 시작 오류·시간 제한·예상하지 못한 종료는 사용자 비밀이나 입력을 그대로 노출하지 않는 오류로 바꾼다. Windows 동일 사용자 프로세스끼리의 OS 격리까지 구현한 것으로 해석하지 않는다.

구현 결정 `DEC07: confirmed`는 Electron 44.7.0·React 19.2.8·TypeScript 7.0.2·Python 3.14.8 sidecar를 채택한다. Tauri 2.12.3 실행본은 후보별 실측과 재현을 위한 독립 비교 코드로 유지한다. 결정 근거·지원 조건은 [Windows ADR](windows-packaging.ko.md)에 있다.

## 2. 실제 Windows 실행 이력

### 2.1 최초 Electron 기반 검증 — 38219549819

이 첫 실행 시점에는 Electron 경로만 실제 검증했으므로 AIK-002의 후보별 비교를 완료하지 않았다. 아래 원본 수치와 성공 근거를 보존하고, 후속 비교·전달 실행 결과를 이어 기록한다.

- 구현 소스: [`56523355d9cf3a2437989d1dd26f2e5ced1666d2`](https://github.com/jaeseongs95/AI-Kubernetes/commit/56523355d9cf3a2437989d1dd26f2e5ced1666d2)
- 원격 실행: [Windows foundation / run 38219549819](https://github.com/jaeseongs95/AI-Kubernetes/actions/runs/38219549819), 첫 시도 성공
- 실제 환경: Windows Server 2022 Datacenter x64, build 20348, 2 logical CPU, 약 8 GiB RAM, 관리자 계정
- 원본 근거: [Windows 실행 자료](../evidence/windows-foundation/38219549819/README.ko.md)

| 검증 | 실제 결과 |
|---|---|
| Python 시험 | 23개 중 **22 PASS, 1 SKIP**. SKIP은 비 Windows 전용 거부 검사 |
| Windows native DPAPI | 위 Python 시험에 포함된 실제 왕복·암호문 손상 검사 **2개 PASS** |
| Node IPC·프로세스 시험 | **8/8 PASS** |
| React/TypeScript 빌드 | PASS |
| frozen CPython·installer 생성 | PASS |
| 저장소 밖 한글·공백 경로 설치 | PASS |
| 설치된 실제 GUI → IPC → sidecar | PASS |
| 별도 worker의 파일 효과 | 42 bytes 생성·재읽기·해시 대조·삭제 PASS |
| GUI → 사용자 DPAPI 왕복 | PASS |
| 설치된 uninstaller로 제거 | PASS |

앱 보고서의 `packaged: true`, `frozen: true`, `window_visible: true`와 세 검사 결과를 확인했다. 실제 worker PID `5736`의 부모가 sidecar PID `7128`과 일치했다. 설치된 engine·Python DLL은 build 보고서의 해시와 일치하며, 원본 report·screenshot·다운로드한 설치 EXE도 독립 재계산한 hash와 일치했다.

![실제 Windows 설치 GUI의 세 검사 완료 화면](../evidence/windows-foundation/38219549819/screenshot.png)

개발 설치본은 118,718,430 bytes, SHA-256 `955d27bd62158b029e659ea04a65807599b8ac40f1ccd40190acdb6beea83a16`이며 서명 상태는 `NotSigned`다. 실제 모델·사용자 계정·AGS/VM 작업을 실행하는 완제품 설치본은 아직 아니다.

### 2.2 같은 Windows job의 Electron·Tauri 비교 — 38223818838

기존 AIK-002 `verification`의 `지원 후보별 설치성·IPC·worker 기동·메모리 비교`를 충족하도록 두 후보를 같은 소스와 같은 Windows 실행기에서 차례로 빌드·설치했다. 한 번 freeze한 CPython 실행본을 공유하고, 설치된 두 engine의 SHA256과 Python DLL hash를 대조했다.

- 검증 소스: [`a20d3bef3f4865fc81cd3495ecc437b7a9b57cea`](https://github.com/jaeseongs95/AI-Kubernetes/commit/a20d3bef3f4865fc81cd3495ecc437b7a9b57cea)
- 실제 실행: [Windows desktop stack comparison / 38223818838](https://github.com/jaeseongs95/AI-Kubernetes/actions/runs/38223818838), job `114726304615`, 첫 시도 전체 success
- 구간: 2026-10-12 08:28:55–08:46:22 UTC
- 환경: Windows Server 2022 Datacenter 10.0.20348, AMD64, logical CPU 2개, RAM 8,584,425,472 bytes, 관리자 계정, runner image `20261004.326.1`
- 원문: [검증 설명](../evidence/tauri-comparison/38223818838/README.ko.md), [실행·hash 검증 JSON](../evidence/tauri-comparison/38223818838/ci-validation.json), [raw 비교 보고서](../evidence/tauri-comparison/38223818838/evidence/stack-comparison.json), [원본 로그의 게시본](../evidence/tauri-comparison/38223818838/job-114726304615.log)

| 관측 | Electron 44.7.0 | Tauri 2.12.3 |
| --- | --- | --- |
| 설치·GUI 세 작업·제거 | PASS | PASS |
| frozen Python | 3.14.8 | 3.14.8 |
| sidecar / worker PID | 1712 / 2164 | 1960 / 7096 |
| worker 효과 | 42 bytes 기록·fsync·재읽기·hash·삭제 | 같은 작업 PASS |
| vault | 실제 사용자 DPAPI 왕복·삭제 | 같은 작업 PASS |
| unsigned installer 크기 | 118,717,783 bytes | 226,652,464 bytes |
| native/main 메모리 | Node RSS 99,901,440 bytes | Win32 working set 22,339,584 bytes |
| renderer 메모리 | 87,344 KB | native snapshot 69,255,168 bytes; 직후 외부 snapshot 70,422,528 bytes |

같은 job에서 Python은 **23개 중 22 PASS·1 non-Windows 전용 검사 SKIP**, Node는 **8/8 PASS·0 SKIP**, Rust는 **6/6 PASS·0 ignored**였다. Rust 시험은 실제 Python child·별도 worker·Windows DPAPI를 실행했다. 설치 단계에서는 source tree 밖 한글·공백 경로와 빈 working directory를 사용했고, 앱 PATH에서 개발 도구 경로를 제외했다. 두 설치본의 공통 engine SHA256은 `3622009e92310d1593d67a35c84d5ceb7d5817b048c795a5d58d22a0c501c922`다.

Tauri NSIS는 Evergreen WebView2 offline installer를 포함했다. 실제 GUI는 사전 설치돼 있던 **131.0.2903.86**을 재사용했으며, WebView2 여섯 프로세스의 file version·binary hash가 사전 설치본과 일치했다. runtime이 없는 PC의 설치 검증은 `NOT_RUN`이다.

메모리는 세 작업 직후 단 한 번 관측했다. Electron은 `process.memoryUsage().rss`와 `app.getAppMetrics()`에서 해당 renderer PID의 `memory.workingSetSize`를 읽고, Tauri는 Win32 `GetProcessMemoryInfo`와 직후 `.NET Process.WorkingSet64`를 사용했다. native/main과 WebView2 browser의 역할 배치·process 범위·시점이 달라 전체 합계나 성능 우위로 환산하지 않았다. `elapsed_ms` 역시 두 셸의 초기화 코드 안에서 시작하므로 외부 process 생성부터의 startup benchmark로 사용하지 않는다.

[Electron 실제 화면](../evidence/tauri-comparison/38223818838/evidence/screenshot.png)과 [Tauri PrintWindow 화면](../evidence/tauri-comparison/38223818838/evidence/tauri/screenshot.png)을 직접 열어 3/3 완료, PID, 한글, 주요 버튼·텍스트의 겹침 여부를 확인했다. capture 크기는 각각 1008×689와 1044×788이며 동일 viewport를 전제하지 않는다. Tauri desktop crop에는 taskbar가 아래쪽을 덮은 부분이 있어 보조 자료로만 사용한다. [root 독립 화면·결속 검토](../evidence/tauri-comparison/38223818838/root-review.json)를 함께 보존한다.

ZIP 두 개의 digest·크기를 GitHub metadata와 대조했고, installer 두 개·report·PNG·공유 engine·Python DLL의 hash 연결도 확인했다. 원문 artifact의 13개 파일은 `build/`·`evidence/` 트리와 bytes를 그대로 유지했다. 게시용 로그·metadata의 자격값 마스킹은 0건이며, 로그는 UTF-8 BOM 3 bytes만 제거하고 CRLF 1,470개를 유지했다. 정확한 원본·게시본 hash와 변환은 [sanitization manifest](../evidence/tauri-comparison/38223818838/sanitization-manifest.json)에 있다.

실행 당시 비교 보고서의 설명 문자열에는 renderer API를 `getProcessMemoryInfo()`로 잘못 적은 부분이 있었다. 실제 측정 코드는 `app.getAppMetrics()`다. 원문은 보존하고 README·harness 설명 문자열만 정정했으며, 전후 source hash·diff·PowerShell AST 결과를 [정정 기록](../evidence/tauri-comparison/38223818838/measurement-api-correction.json)에 남겼다. 측정값·실행 동작·판정 조건은 변경하지 않았다.

### 2.3 Windows PowerShell 5.1 전달 경로 — 38226204128

깨끗한 Windows에 검증 자료를 전달하는 경로를 검토하다가 기존 스크립트의 **UTF-8 BOM 부재**를 발견했다. Windows PowerShell 5.1의 CP1252 source 해석에서는 한글 bytes가 따옴표로 해석되어 parser 오류 2개가 났다. PowerShell 7에서의 최초 PASS만으로 이 기본 Windows 실행 경로를 검증할 수 없었다.

첫 전달 시도에서는 `Get-FileHash` 모듈 의존으로 실행이 실패했고, .NET SHA256 streaming으로 바꿔 해당 의존을 제거했다. [첫 실패 38225912010](../evidence/windows-handoff/38225912010/README.ko.md)은 후속 성공과 구분해 보존했다.

소스 [`4b948f57020f3e507683de6e538544b9bac558e7`](https://github.com/jaeseongs95/AI-Kubernetes/commit/4b948f57020f3e507683de6e538544b9bac558e7)의 [실제 Windows 전달 검증 / 38226204128](https://github.com/jaeseongs95/AI-Kubernetes/actions/runs/38226204128)에서 원본 CP1252 해석 오류 **2개**, BOM을 추가한 실행 사본의 오류 **0개**를 확인했다. 원본 verifier는 그대로 보존하고 전달 ZIP의 사본에 BOM 3 bytes만 추가했다. ZIP을 checkout 밖 다른 한글·공백 경로에 풀어 **Windows PowerShell 5.1.20348.5622 / Desktop**으로 새 설치를 실행했고, 실제 GUI·별도 worker·사용자 DPAPI·제거까지 **PASS**했다. 새 sidecar PID는 816, worker PID는 6404였다.

검증한 독립 전달 ZIP은 118,784,150 bytes, SHA256 `a6edd83fd952257d5d7f731c23162aeaca30974e0cc303bbd7b0210c9e7dd23e`다. 내부 unsigned installer는 118,718,153 bytes, SHA256 `16811e29d6e65cb5f24f072d3a8896d155c6a46bff83a376d01c2e0c1ad5c95a`다. 이전 비교 실행의 installer와 각각의 소스·hash를 구분했다. [원본 실행·전달 근거](../evidence/windows-handoff/38226204128/README.ko.md), [신규 handoff raw report](../evidence/windows-handoff/38226204128/artifact/handoff/evidence/8d415a8070b94a67aa92effb29e24801/handoff-validation.json), [실제 CI 로그](../evidence/windows-handoff/38226204128/metadata/windows-job.log)에 보존한다.

이 실행은 기본 PowerShell 전달 호환성에 대한 실제 검증이다. 지원 qualification은 여전히 `NOT_RUN`이며, Windows 11 표준 사용자·개발 도구가 물리적으로 없는 깨끗한 환경의 실행을 대신하지 않는다.

## 3. 실제 upstream 대조와 결함 수정

고정 AGS archive 1,473개 Git blob과 VM Python 소스 86개를 대조했다. non-editable VM wheel을 실제 설치해 공개 명령을 실행했고, 재현에 필요한 원본 wheel과 LICENSE·NOTICE를 [설치 입력 근거](../evidence/upstream/2026-10-12/README.ko.md)에 보존했다.

| 실제 명령·관측 | 결과 |
|---|---|
| AGS MCP initialize / tools/list | PASS, 28개 도구 |
| VM 공개 console 도움말·project init·status | exit 0 |
| 새 임시 VM 원장 | SQLite revision 4, application ID 1179469105, integrity `ok` |
| 생성한 VM Task/Attempt/provider call | 각각 0건 |
| AGS `fm/hello` | `-32601 Method not found` |
| VM `governance check-plugin` | exit 2, `GOVERNANCE_CONTRACT_MISMATCH`; 필요한 `host-integration.json` 부재 |

VM의 실제 `EngineApplication(runtime=…)`, `CodexRuntimePort`, 효과 직전 `OperationTraceScope`를 확인했다. AIK가 새 Task·재시도 권위를 만들지 않고 기존 공개 접점에 route/connection/account/pool/lease를 결속하는 계약을 고정했다. AGS↔VM의 현재 호환 실패는 후속 통합 차단으로 유지한다.

독립 코드·입력 검토에서 잘못된 backend 필드 타입, 과도한 JSON 정수, 비 ASCII capability, child 시작·종료 오류, GUI 상태 대조의 결함을 수정했다. 계약 JSON Schema에서는 digest 뒤의 마지막 개행을 허용하는 결함을 발견했다. 세 digest의 정확한 길이를 제한한 후 정식 Draft 2020-12 validator의 **독립 fixture 59개(positive 2, negative 57)**가 모두 통과했다. 기존 장부 65개와 새 upstream 검사기 7개를 합한 **72개 회귀 시험도 통과**했다.

## 4. 완료 판정과 남은 실행 조건

| 태스크 | 현재 판정 | 남은 조건 |
|---|---|---|
| AIK-001 | **done** | 설치형 공개 인터페이스 기준선·소유권·지원 matrix·결속 계약과 독립 대조 완료. 실제 governed runtime은 후행 범위 |
| AIK-002 | **done** | 기존 세 완료 기준에 더해 후보별 실제 설치·IPC·worker·메모리 비교와 원문·화면·hash 독립 검토 완료. 구현 선택은 `DEC07: confirmed` |
| AIK-003 | **blocked** | 정책·runtime bundle·Server 설치는 통과. 개발 도구가 없는 깨끗한 후보 Windows 설치는 미실행 |

AIK-003의 기존 원문은 “깨끗한 후보 Windows 환경”과 “개발 도구가 없는 환경”을 요구한다. 현재 CI에서 PATH를 제한한 결과를 머신의 개발 도구 부재로 바꾸지 않는다. 선택한 Windows 11 표준 사용자 환경에서 동일 개발 설치본의 작은 설치·GUI·worker·DPAPI·제거 검증이 필요하다. 003을 완료하려고 제품 전체 E2E나 모든 지원 버전의 최종 qualification까지 요구하는 것은 아니다.

현재 상태는 **done 2개, blocked 1개, todo 87개, ready 0개**다. 083은 001·002·003에 의존하며, 뒤의 주요 구현 태스크도 083을 따른다. 기존 의존성과 완료 기준은 그대로 유지한다. 완료된 후보 비교와 PowerShell 전달 검증을 근거로 보존하고, 깨끗한 후보 Windows 검증은 실제 실행 증거가 확보되기 전까지 차단 상태를 유지한다.

## 5. 사용자 요청 기능의 제품 상태

| 요청 | 현재 구현·검증한 부분 | 제품 기능으로 남은 부분 |
|---|---|---|
| 멀티벤더 시크릿 공간 | Windows 사용자 암호화·파일 저장·복원 경계 시험 | 실제 자격 증명 CRUD·해제·rotation·벤더별 연결·GUI 관리 |
| Dot 역할의 총괄 모델 지정 | VM 공개 역할·runtime seam·모델/인증/도구 지원 계약 | 모델 발견·프로필 설정·총괄 연결·실제 모델 실행·재시작 routing |
| Windows GUI 출시 | 설치되는 GUI와 실제 엔진·worker·보관 호출 | 프로젝트·목표·실행·결과·복구 화면, 제품 E2E와 서명된 출시본 |

각 기능은 기존 태스크에 남아 있으며, 기반 spike 성공으로 제품 기능을 완료 처리하지 않았다. 실제 계정·모델·도구 qualification, 의존 개발 목표, GUI 제품 검증과 최종 배포는 각각 082·086·044·059·064의 근거가 필요하다.
