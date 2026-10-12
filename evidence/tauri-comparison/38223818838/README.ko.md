# 실제 Windows 후보 비교 검증 — 38223818838

**판정: AIK-002 범위 PASS.** 동일 job에서 Electron과 Tauri의 사용자별 NSIS 설치, 설치된 GUI의 실제 세 버튼, frozen Python sidecar·별도 worker·Windows 사용자 DPAPI, 메모리 관측, 화면 capture와 제거를 실행했다.

- 소스 commit: `a20d3bef3f4865fc81cd3495ecc437b7a9b57cea`
- 실행: <https://github.com/jaeseongs95/AI-Kubernetes/actions/runs/38223818838>
- job: `114726304615` / `installed-candidates`; 첫 시도 전체 success.
- 실행 시간: 2026-10-12 08:28:55–08:46:22 UTC.
- 실제 환경: Windows Server 2022 Datacenter 10.0.20348, AMD64, logical CPU 2개, RAM 8,584,425,472 bytes, administrator, runner image `20261004.326.1`.
- Python 23개 중 22 PASS·1 non-Windows 전용 검사 SKIP, Node 8/8 PASS·0 SKIP, Rust 6/6 PASS·0 ignored.

## 관측 결과

| 관측 | Electron 44.7.0 | Tauri 2.12.3 |
| --- | --- | --- |
| 설치·세 실제 작업·제거 | PASS | PASS |
| sidecar / worker PID | 1712 / 2164 | 1960 / 7096 |
| frozen CPython | 3.14.8 | 3.14.8 |
| 설치 파일 크기 | 118,717,783 bytes | 226,652,464 bytes |
| 설치 파일 서명 | NotSigned | NotSigned |
| native/main 관측 | Node RSS 99,901,440 bytes | Win32 working set 22,339,584 bytes |
| renderer 관측 | 87,344 KB | native snapshot 69,255,168 bytes; 직후 외부 snapshot 70,422,528 bytes |

두 설치본의 engine SHA256은 `3622009e92310d1593d67a35c84d5ceb7d5817b048c795a5d58d22a0c501c922`로 같고, Python DLL hash도 일치한다. worker는 별도 PID에서 42 bytes 합성 파일을 실제 기록·fsync·재읽기·hash·삭제했다. vault는 실제 `windows-user-dpapi` 경로로 합성 값을 보호·복원·삭제했다.

Tauri NSIS는 Evergreen WebView2 offline installer를 포함하며 이 실행은 이미 설치돼 있던 **131.0.2903.86** runtime을 재사용했다. 사용 중인 WebView2 여섯 프로세스의 file version과 binary hash가 사전 설치본과 일치한다. runtime이 없는 환경의 설치는 `NOT_RUN`이다. Tauri의 WebView2 browser·renderer·GPU·utility·crashpad 프로세스는 원문에서 각각 확인할 수 있다.

메모리는 세 작업 직후의 단일 관측이다. Electron은 `process.memoryUsage().rss`와 `app.getAppMetrics()`의 renderer PID에 해당하는 `memory.workingSetSize`를 읽는다. Tauri는 Win32 `GetProcessMemoryInfo`와 직후 `.NET Process.WorkingSet64`를 사용한다. main과 browser 역할 배치, 전체 프로세스 범위, snapshot 시점이 같지 않으며 공유 memory를 합산하면 중복 계상이 가능하다. 전체 앱 합계·peak·장기 평균·속도 우위를 이 관측으로 판정하지 않는다. `elapsed_ms`의 시작점도 각 셸 초기화 코드 안에 있으며 외부 process 생성 시각부터 측정한 값이 아니다.

## 파일과 원문 보존

`build/`와 `evidence/`는 GitHub evidence ZIP의 원래 트리를 그대로 유지한다. JSON·로그·PNG 13개가 추출 원본과 byte 단위로 일치한다. 두 installer도 별도 다운로드해 Windows 보고서의 크기·SHA256과 대조했다. ZIP 전체 digest와 크기를 GitHub artifact metadata에 대조한 결과는 `ci-validation.json`에 있다. 실행 파일 자체는 GitHub의 unsigned installer artifact에 보관한다.

`job-114726304615.log`와 세 GitHub metadata 파일은 게시 전에 signed URL query, Authorization, 알려진 token prefix와 credential assignment 형태를 검사했다. **자격값 마스킹은 0건이다.** 로그의 UTF-8 BOM 3 bytes만 제거하여 원본 110,236 bytes가 게시본 110,233 bytes로 바뀌었으며, CRLF 1,470개는 유지됐다. 원본·게시본 hash와 변환 내역은 `sanitization-manifest.json`에 있다. 경로·PID·버전·시험 출력은 유지했다.

실행 당시 `stack-comparison.json`의 설명 문자열에는 renderer API를 `getProcessMemoryInfo()`로 잘못 적은 부분이 있었다. 실제 코드와 수집값은 `app.getAppMetrics()` 기반이다. **raw artifact는 수정하지 않고**, `measurement-api-correction.json`에 정정과 README·harness의 설명 문자열만 바꾼 diff·소스 hash를 기록했다. 측정식·동작·판정 조건은 변경하지 않았다.

## 화면 검토와 미실행 범위

`evidence/screenshot.png`는 Electron client 화면, `evidence/tauri/screenshot.png`는 실제 Tauri HWND의 PrintWindow capture다. 세 작업의 한글 표기·버튼·PASS 상태·PID·frozen runtime 텍스트를 직접 열어 확인했다. 주요 텍스트와 버튼의 겹침은 없었다. Electron capture는 1008×689, Tauri native window capture는 1044×788이며 화면 chrome·viewport가 같다고 주장하지 않는다. 아래쪽 scroll 영역 전체의 시각 검증은 포함하지 않는다.

`evidence/tauri/screenshot-desktop-crop.png`에는 Windows taskbar가 아래쪽을 덮은 부분이 있어 보조 증거로만 사용한다. 판독용 Tauri primary capture는 `screenshot.png`다. 원문 harness의 `visual_review: NOT_RUN`은 자동 capture 시점의 상태로 보존하고, 이후 직접 이미지 검토는 별도 검증 기록에 남겼다.

Windows 11 clean standard-user 지원 확정, 개발 도구가 물리적으로 없는 PC, WebView2 부재 설치, publisher 서명·업데이트 trust, 실제 벤더·AGS·VM 제품 integration은 이 비교에서 `NOT_RUN`이다. 정식 출시 조건인 AIK-003의 판정은 별도로 유지한다.
