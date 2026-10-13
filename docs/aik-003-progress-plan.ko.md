# AIK-003 진행 방안 (모델 생성)

> **성격:** Claude API가 생성한 진행 방안이다. 입력 문서만 근거로 했으며 저장소 소스·장부와 독립 대조하지 않았다. 새 실행 권한, 원장 상태 변경, 서명 집행을 만들지 않는다. "자료에 없음"은 입력에 포함하지 않은 범위를 뜻한다. 결정 근거로 쓰기 전에 원문 증거를 확인한다.

## 생성 조건

| 항목 | 값 |
| --- | --- |
| 생성 시각 (UTC) | 2026-10-13 06:59:33 ~ 07:01:25 |
| 모델 / 설정 | `claude-opus-5-5`, effort `high`, 스트리밍, `max_tokens` 32000 |
| 응답 | HTTP 200, `msg_011CfyoasEZb3k37XcMoN7a6`, `stop_reason=end_turn` |
| 입력 | `docs/development-backlog.ko.md`(AIK-003 절, 125~163행), `docs/windows-packaging.ko.md`, `tools/windows_handoff/README.ko.md`, `evidence/windows-client-intake/2026-10-12/README.ko.md`. 입력 정본 SHA-256 `1ac2de4a959b5f49ec46d648bebcda1bc69d50450c1db6613394414130f0922f` |
| 입력 기준 commit | `9ab9caf` (`origin/main`, PR #10 병합) |
| usage | 일반 입력 4, 캐시 쓰기(5분) 21,890, 캐시 읽기 0, 출력 12,248 (thinking 4,675 포함) |

요청에 `cache_control`을 지정하지 않았으나 Gateway가 5분 캐시 쓰기를 자동 적용한 것으로 관측됐다. 비용은 Opus 5.5 단가로 계산한 약 $0.354이며 계정 청구액과 대조하지 않았다.

> 근거 자료: `docs/development-backlog.ko.md`(AIK-003 절), `docs/windows-packaging.ko.md`(ADR), `tools/windows_handoff/README.ko.md`, `evidence/windows-client-intake/2026-10-12/README.ko.md`
> 이 문서는 진행 방안입니다. 원장 상태 변경, 새 실행 승인, 서명 집행은 포함하지 않습니다(§6 참조).

---

## 1. 현재 상태 요약

| 항목 | 값 |
|---|---|
| 상태 | `blocked` (P0, Release, `decision`, E09) |
| 최종 수정 | `2026-10-12T12:22:34Z` |
| 핵심 차단 사유 | 원래 AC2와 verification이 요구하는 "개발 도구가 없는 깨끗한 후보 Windows" 실행 증거가 없음 |

### 1.1 실제 PASS로 기록된 증거

PASS 범위는 모두 **Windows Server 2022 관리자 CI**입니다.

| 증거 ID | 위치 | PASS 범위 |
|---|---|---|
| `EV-003-INSTALL-20261012` | `evidence/windows-foundation/38219549819/verification.json` | runtime 공급, 설치, 제거 |
| `EV-003-POLICY-20261012` | `docs/windows-packaging.ko.md` | 결정 문서: Windows x64 후보, runtime 포함 NSIS, 서명·업데이트·데이터 정책, 성능 측정 부하 |
| `EV-003-HANDOFF-20261012` | `evidence/windows-handoff/38226204128/verification.json` | ZIP 재추출 후 Windows PowerShell 5.1 Desktop 실행: 신규 설치, GUI, frozen Python, worker, DPAPI, 제거, 8개 검사 PASS (SHA256 `d15c51d0…8f25f37`) |
| `EV-003-LEGACY-20261012` | `evidence/reviews/2026-10-12-powershell-legacy-encoding.json` | 인코딩 결함 재현(원본 오류 2 / BOM 사본 오류 0), `Get-FileHash` 실패를 .NET SHA256으로 수정. 환경 qualification과 분리됨 |
| `EV-003-CLIENT-INTAKE-20261012` | `evidence/windows-client-intake/2026-10-12/local-validation.json` | 수집기·검사기 구현, 로컬 113 tests PASS. 보존된 Server 증거는 무결성 PASS, 청정 클라이언트 판정은 BLOCKED |
| `EV-003-CLIENT-INTAKE-WINDOWS-20261012` | `evidence/windows-client-intake/2026-10-12/windows-ci-validation.json` | commit `312fa94…` / CI 38247899230 전체 성공: 회귀 41, Python 엔진 23, Node 8, PowerShell 5.1 수집 6 probe, 설치·GUI·worker·DPAPI·제거 PASS. 개발 도구 146개 관측으로 BLOCKED 판정이 정상 동작함 |

### 1.2 NOT_RUN 또는 미충족 항목

- 깨끗한 Windows 11에서 표준 사용자로 실행한 기록 없음. NOT_RUN (`EV-003-CLIENT-INTAKE-WINDOWS`, handoff README "Windows 지원 판정 범위")
- `qualification.windows11_standard_user_clean_machine`, `qualification.developer_tools_absence`: NOT_RUN
- 게시자 서명, 업데이트 신뢰, 제품 통합, 완제품 데이터 제거 검증: NOT_RUN (ADR §4, handoff README)
- verification의 "문서 검토" 수행 기록: **자료에 없음**
- AIK-083 출시 임계값과 workload fixture 최종 고정: AIK-083의 남은 작업 (ADR §2)

---

## 2. 남은 완료 기준과 통과·차단 조건

`EV-003-POLICY-20261012`는 "기존 AC2 및 verification"만 미충족으로 기록했습니다. AC1·AC3·AC4가 공식적으로 충족 판정됐다는 원장 기록은 **자료에 없음**입니다.

| 기준 | 현재 근거 | 통과 조건 | 차단 조건 |
|---|---|---|---|
| **AC1** 지원 버전·CPU·최소 자원·설치 형식 | ADR §2 후보표(Win11 25H2/26H2 Home/Pro x64, 2/4 logical CPU, 8/16 GiB, 디스크 2/4 GiB), ADR §3 NSIS 사용자별 설치 | 후보 명시는 완료. 깨끗한 Win11 증거로 후보를 확인 | 깨끗한 Win11 증거와 AIK-083 계약 없이 "검증 완료 지원표"로 게시하는 경우 (ADR §2) |
| **AC2** 개발 도구 없는 환경의 runtime 공급 | Server 관리자에서 PATH를 격리해 PASS (`EV-003-INSTALL`, `EV-003-HANDOFF`) | 깨끗한 Win11 표준 사용자 실행에서 handoff exit 0, 검사기 `READY_FOR_REVIEW`, 독립 검토 기록 | Server, 관리자(비승격 포함), 도구 발견, 관측 오류, 준비 기록 누락 → `BLOCKED`. 변조, hash·시간 모순 → `FAIL` |
| **AC3** 서명·업데이트 신뢰·제거 후 데이터 기준, macOS/Linux 제외 | ADR §4 기준 확정(Authenticode, SHA-256, timestamp, 전체 설치본의 사용자 실행 업데이트, 기본 제거 시 workspace 보존), ADR §2에서 Windows x64만 gate로 지정 | 기준 문서화는 됨. 구체적 서명 수단 결정 필요 여부는 §5와 §6에서 미결로 다룸 | 서명 키나 암호가 저장소·앱에 포함되는 경우, 정식 빌드가 서명 설정 없이 성공하는 경우 |
| **AC4** hardware·경로·오프라인/네트워크·측정 부하 | ADR §2 측정 부하표(시작, 대기, 제어부, 병렬, 장기). provider 대기와 로컬 scheduling을 분리 | 부하 조건 결정은 됨. 임계값은 AIK-083이 측정 전에 고정 | spike의 `elapsed_ms`나 메모리를 제품 성능 PASS로 환산하는 경우 (ADR §2) |
| **verification** 깨끗한 후보 환경의 설치 spike와 문서 검토 | 실행은 Server CI만 있음 | §3 절차 완료 후 독립 검토 결과와 문서 검토 결과를 원장에 기록 | 위 AC2 차단 조건과 같음. 문서 검토 미기록 |

---

## 3. 깨끗한 Windows 11 표준 사용자 검증 절차

근거: `tools/windows_handoff/README.ko.md`, ADR §2·§6·§7.

### 3.1 준비물

| 준비물 | 기준 | 근거 |
|---|---|---|
| OS | Windows 11 25H2(build 26200) 또는 26H2(build 26300), Home/Pro x64, 최신 보안 누적 업데이트 | ADR §2, README "새 실행 증거 검토" |
| Hardware | 최소 후보 2 logical CPU / 8 GiB / 여유 2 GiB / 1280×800. 기준 후보 4 / 16 GiB / 4 GiB / 1920×1080 | ADR §2 |
| 준비 방식 | `clean_install` 또는 `clean_vm_image` | README 2단계 |
| 계정 | 표준 사용자. 비승격 관리자 토큰(deny-only 관리자 SID 포함)은 차단됨 | README "지원 판정 범위", 검사기 표 |
| 개발 도구 | Python, Node 등 미설치. 검증 PC에는 Python을 설치하지 않음 | README 4단계 |
| 전달 패키지 | 신뢰하는 Actions 실행의 `windows-client-handoff-package-...` 아티팩트(ZIP과 `.sha256`) | README "실행" 1단계 |
| 신뢰 hash | 같은 신뢰 CI 빌드의 `build-handoff.json`에 있는 `manifest_sha256` | README "새 실행 증거 검토" |
| 검토용 컴퓨터 | Python과 저장소의 `tools/review_windows_client.py` | README 4단계 |

**주의:** 패키지는 환경 수집기(`collect-client-environment.ps1`, wrapper schema 2)를 포함한 빌드여야 합니다. 이전 run 38226204128의 증거는 "사전 환경 관측 누락"으로 BLOCKED 처리됐습니다(intake README). commit `312fa94…` 이후의 빌드가 해당하지만, CI 38247899230의 handoff 패키지 아티팩트가 현재 내려받을 수 있는 상태인지는 **자료에 없음**입니다. 받을 수 없다면 새 신뢰 CI 실행이 필요합니다.

### 3.2 실행 순서

1. **환경 준비 기록:** 검증 시작 전에 실제 준비 시각, 방법, 설치 이미지 출처, 개발 도구 설치 이력을 남깁니다. 원본 근거는 나중에 증거 폴더의 `preparation/` 아래에 보존합니다.
2. **패키지 확인:** ZIP과 `.sha256`을 대조합니다. 이 hash는 전달 중 변경 여부만 확인하며 게시자 신뢰를 증명하지 않습니다.
3. **압축 해제:** 쓰기 가능한 폴더에 모두 풉니다(예: `다운로드\AIK 설치 검증`). 압축 폴더 안에서 바로 실행하지 않습니다. 한글과 공백이 포함된 경로를 사용해 지원 경로 조건도 함께 확인합니다.
4. **실행:** 표준 사용자 세션에서 `run-validation.cmd`를 실행합니다. 조직 정책이 실행을 막으면 정책을 바꾸지 말고 차단 결과를 보존합니다.
5. **화면 확인:** GUI의 세 검증 카드와 결과를 직접 봅니다. 합성 데이터만 사용하며 벤더 인증정보는 입력하지 않습니다.
6. **종료 코드 기록:** `0` PASS, `1` 실패, `2` 차단.
7. **준비 기록 작성:** 증거 폴더의 `client-preparation.template.json`을 `client-preparation.json`으로 복사하고 다음을 기입합니다.
   - `operator`
   - `prepared_at`: 검증 시작 전 시각, 시간대 포함 ISO 8601
   - `method`, `description`, `development_tools_installed`
   - `records`: `preparation/` 하위 파일별 상대 경로와 SHA-256
   - `execution_id`, `manifest_sha256`, `client_environment_sha256`는 생성된 값을 그대로 둡니다. 빈 칸을 추정으로 채우지 않습니다.
8. **증거 이동:** `%LOCALAPPDATA%\AIK\ClientHandoffEvidence\<ID>` 폴더 전체를 압축해 검토용 컴퓨터로 옮깁니다.
9. **검사기 실행:**
   ```powershell
   python tools/review_windows_client.py --evidence-root "<실행ID 폴더>" --expected-manifest-sha256 "<신뢰 build-handoff.json의 manifest_sha256>"
   ```
   제출된 폴더 안의 값을 신뢰 근거로 쓰지 않습니다.

### 3.3 수집되는 증거

`handoff-validation.json`, `handoff-manifest.json`, `client-environment.json`, `client-preparation.json`(작성본), `verifier.stdout.log`, `verifier.stderr.log`, `installed-check/packaging/evidence/installed-spike.json`, `application-report.json`, `screenshot.png`, `preparation/` 첨부, 검사기 출력 JSON.

### 3.4 판정 방법

| 단계 | 통과 | 차단·실패 |
|---|---|---|
| handoff 실행 | exit 0 | 1 FAIL, 2 BLOCKED. 비정상 종료 시 JSON 항목으로 설치·제거 여부를 판단 |
| 검사기 | `READY_FOR_REVIEW` / 0 | `BLOCKED` / 2, `FAIL` / 1 |
| 독립 검토(사람) | 준비 진술, 이미지 출처, 화면, 최신 보안 업데이트, hardware, 경로, 오프라인 조건을 확인 | 하나라도 확인 불가 |
| 원장 | 검토 결과를 AIK-003 근거로 기록. 상태 변경은 원장 담당자가 결정 | 검사기는 원장을 바꾸지 않음 |

`READY_FOR_REVIEW`는 지원 판정이 아닙니다. 검사기가 출력하는 qualification은 `NOT_RUN`을 유지합니다.

**절차상 미결 사항(자료에 없음):**
- 최소 후보와 기준 후보 사양을 모두 실행해야 하는지
- 오프라인 조건을 어떤 방법으로 확인하는지(예: 네트워크 차단 실행 여부)
- DPI 100/125/150% 검사가 AIK-003 범위에 포함되는지
- Python WindowsApps 실행 별칭이 관측될 때의 판정 효과

실행 전에 담당자가 정해야 합니다.

---

## 4. 작업 주체 구분

### 4.1 Linux 클라우드 세션에서 가능한 일

- 저장소의 단위 테스트 실행. 기존 기록에 Linux PowerShell 7.6.6 연결 상태의 113 tests 실행이 있음 (`local-validation.json`)
- 제출된 증거 폴더에 `tools/review_windows_client.py` 실행(Python 표준 라이브러리 기반)
- 보존 증거의 hash 대조, 기존 Server 증거 재검토 (`historical-server-review.json`)
- 서명 방식 ADR 보강안, 준비 기록 작성 안내, 원장 기록 문안 등 문서 초안 작성
- 원장 문안 초안 작성(상태 변경은 하지 않음)
- GitHub Actions 실행·아티팩트 다운로드 권한이 이 세션에 있는지는 **자료에 없음**

### 4.2 사용자가 직접 해야 하는 일

- 깨끗한 Windows 11 실기 또는 VM과 표준 사용자 계정 준비, 최신 보안 업데이트 적용
- 실제 준비 근거와 진술 작성(`client-preparation.json`, `preparation/`)
- `run-validation.cmd` 실행과 GUI 화면 직접 확인
- 신뢰하는 CI 빌드와 `manifest_sha256` 지정
- 독립 검토, 문서 검토 판정, 원장 상태 변경 승인
- 서명 인증서와 게시자 선택, 키 보관 결정(§5)

Linux 세션은 실제 Windows GUI, 설치, DPAPI를 실행할 수 없습니다. ADR §5는 "Windows가 없거나 실제 실행을 하지 못한 경우 NOT_RUN/BLOCKED"로 규정합니다.

---

## 5. 서명 방식 결정

### 5.1 자료로 확정된 제약 (ADR §4)

- 게시자와 일치하는 Authenticode 서명, SHA-256, 신뢰 가능한 timestamp
- 서명 키와 암호를 저장소나 앱 안에 두지 않음
- 정식 빌드는 서명 설정이 없으면 실패해야 함. 현재 개발 빌드는 `signExecutable: false`, `forceCodeSigning: false`, 산출물 이름에 `dev-unsigned` 포함
- 업데이트는 서명을 검증한 전체 설치본을 사용자가 실행하는 방식. downgrade 차단과 migration 복구는 AIK-057/058/061에서 다룸
- 설치 검증에서 `Get-AuthenticodeSignature` 결과를 기록함

### 5.2 결정이 필요한 선택 축

구체 상품, 비용, 발급 절차, Windows 평판 동작은 **자료에 없음**입니다. 채택 전에 공식 문서로 확인해야 합니다.

| 선택 축 | 선택지 예 | 판단 기준(자료 근거) |
|---|---|---|
| 인증서·게시자 | 서명 주체, 표시 게시자명 | 사용자에게 표시할 게시자와 일치하는가 |
| 키 보관 | 하드웨어 보관, 외부 서명 서비스, CI 비밀값 | 키가 저장소나 앱에 들어가지 않는가 |
| 서명 시점 | CI 빌드 단계, 별도 릴리스 단계 | 서명 설정이 없을 때 정식 빌드가 실패하는가. electron-builder Windows 설정과의 연동 가능성 ([문서](https://www.electron.build/v26/docs/win/)) |
| timestamp | timestamp 제공처 | 신뢰 가능한 timestamp인가 |
| 검증 방법 | `Get-AuthenticodeSignature` 기록, 서명 거부 시험 | 서명 거부 시험은 아직 제품 gate 범위가 아님 (ADR §4) |

**미결:** AC3가 "기준"을 정하라고 하므로 ADR §4로 충분한지, 아니면 인증서와 서명 수단의 구체 선택까지 AIK-003에서 해야 하는지는 원장 담당자의 판단 사항입니다. 이에 대한 판단 기록은 **자료에 없음**입니다.

---

## 6. 위험, 미결 사항, 한계

### 6.1 위험과 미결 사항

- **준비 진술 의존:** 수집기가 도구를 찾지 못했다고 해서 깨끗한 환경이 입증되지는 않습니다(README). 판정은 준비 기록과 이미지 출처의 신뢰성에 의존합니다.
- **신뢰 기준선:** 수집기를 포함한 신뢰 빌드의 아티팩트가 현재 사용 가능한지는 자료에 없습니다.
- **서명 없는 설치본:** 게시자 신뢰가 없습니다. Windows 11에서 경고나 차단이 나타나는지는 자료에 없으며, 발생 시 화면과 결과를 보존해야 합니다.
- **지원 버전 변동:** 25H2/26H2 지원은 2026-09-29 Microsoft 표 기준이며 릴리스 직전에 재확인해야 합니다(ADR §2). 검사기는 최신 지원 여부를 자동 조회하지 않습니다.
- **조직 정책:** 실행 정책이 강제된 환경에서는 차단될 수 있습니다.
- **증거 바이트 보존:** 줄바꿈 변환으로 hash가 바뀌는 문제는 `.gitattributes`의 `evidence/** -text`로 수정됐습니다(intake README). 새로 추가하는 증거도 같은 규칙을 지켜야 합니다.
- **선후 작업:** AIK-083 임계값이 미고정이며, 미충족 시 AIK-062/064가 차단됩니다.
- **범위 한계:** DPAPI는 같은 사용자 권한의 다른 앱과 격리되지 않습니다. 현재 앱은 foundation spike이며 제품 통합은 NOT_RUN입니다.
- **원장 맥락:** 당시 원장은 done 2 · blocked 1 · todo 87이었고, AIK-003이 선행 작업 없는 유일한 미완료 태스크였습니다(intake README). 현재 수치는 자료에 없습니다.

### 6.2 이 문서의 한계

- 이 문서는 **새로운 실행 권한을 만들지 않습니다.** 사용자 PC나 VM에서의 실행, CI 실행, 아티팩트 게시, 서명 집행, 실제 시크릿이나 벤더 계정 사용을 승인하지 않습니다.
- AIK-003의 상태(`blocked`), 완료 기준, 의존성, 다른 태스크의 상태를 바꾸지 않습니다.
- 기존 NOT_RUN 항목을 PASS로 재해석하지 않습니다. Server 관리자 CI의 PASS는 깨끗한 Windows 11 표준 사용자 검증을 대신하지 않습니다.
- §3.4와 §5.2의 미결 항목은 담당자가 결정하기 전까지 정해진 것으로 간주하지 않습니다.