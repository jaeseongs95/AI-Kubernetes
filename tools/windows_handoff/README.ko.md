# AIK Windows 설치 검증 패키지

이 ZIP은 **Windows GUI, 포함된 Python 엔진, 별도 worker, Windows DPAPI**의 기존 기반 구현을 다른 Windows PC에서 직접 설치하고 검증하기 위한 전달본입니다. 실제 실행 결과와 화면을 보존한 뒤 이 검증이 설치한 앱을 제거합니다. 별도 Python, Node.js, Git 또는 개발 소스가 필요하지 않습니다.

현재 앱은 개발용 foundation spike입니다. 실제 벤더 계정 저장·로그인, 모델 선택, AGS/VM 제품 연결은 아직 구현 완료된 기능이 아닙니다. 설치 파일은 게시자 서명을 완료한 출시본이 아닙니다.

## 실행

1. 신뢰하는 GitHub Actions 실행에서 `windows-client-handoff-package-...` 아티팩트를 받습니다. 아티팩트 안의 `AIK-Windows-Client-Handoff-....zip`과 `.sha256` 파일을 확인합니다. SHA-256은 전달 중 파일이 바뀌었는지 비교하는 값이며, 함께 바뀐 서명 없는 파일의 게시자 신뢰를 증명하지 않습니다. 신뢰하는 CI 실행의 다운로드 및 빌드 기록과 함께 확인합니다.
2. ZIP을 쓰기 가능한 폴더에 **모두 압축 해제**합니다. 예: `다운로드\AIK 설치 검증`. 압축 폴더 안에서 바로 실행하지 않습니다. 한글과 공백이 있는 경로를 지원합니다.
3. Windows x64의 **일반 사용자 세션**에서 `run-validation.cmd`를 실행합니다. 관리자 권한으로 실행할 필요가 없습니다. 이 파일은 Windows에 기본 포함된 x64 Windows PowerShell 5.1을 실행하며, 그 프로세스에서만 스크립트 실행 정책을 지정합니다. 시스템의 영구 정책이나 권한은 바꾸지 않습니다. 조직에서 강제한 정책이 실행을 막는다면 그 정책을 임의로 변경하지 말고 해당 차단 결과를 보존합니다.
4. 잠시 표시되는 GUI의 세 검증 카드와 실행 결과를 확인합니다. 검증은 합성 테스트 데이터만 사용하며 실제 벤더 인증정보를 요청하지 않습니다. 앱 설치·GUI 실행·worker·DPAPI 검사 후 이번 실행이 설치한 앱만 제거합니다.
5. 창에 표시된 `Evidence:` 경로를 엽니다. 기본 위치는 현재 사용자의 `%LOCALAPPDATA%\AIK\ClientHandoffEvidence\<매번 새 ID>`입니다. 결과를 다른 사람에게 전달하려면 해당 ID 폴더 전체를 압축합니다. 스크립트는 이 증거 폴더를 자동으로 삭제하지 않습니다.

출력 위치를 지정하려면 기본 **Windows PowerShell**에서 다음을 실행할 수 있습니다. 매번 지정한 상위 폴더 아래에 새 ID 폴더를 만들기 때문에 기존 증거를 덮어쓰지 않습니다.

```powershell
& "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File ".\run-validation.ps1" -EvidenceParent "$env:USERPROFILE\Documents\AIK evidence"
```

종료 코드 `0`은 이번 패키지 및 기반 기능 검사가 통과했다는 뜻입니다. `1`은 무결성·설치·실행·제거 등의 실패, `2`는 실제 Windows 실행 환경이 없어 차단됐음을 뜻합니다. 실패 시에도 생성된 증거를 확인합니다. 검증이 비정상 종료됐다면 설치 완료·제거 완료 여부는 JSON의 실제 항목으로 판단합니다.

## 보존되는 실제 증거

| 파일 | 내용 |
|---|---|
| `handoff-validation.json` | 실행한 PowerShell 버전·에디션·기본 문자 인코딩·문화권, 실제 Windows 정보, 각 검사 결과, 원본/BOM 스크립트의 실제 parser 결과 |
| `handoff-manifest.json` | 패키지가 만들어진 소스 commit, 설치 파일 및 원본/변환 스크립트 SHA-256, 예상 engine·Python DLL SHA-256 |
| `client-environment.json` | 설치 전 원래 PATH에서 수집한 OS 판본·계정 토큰·CPU/RAM·개발 도구 관측. 실행 ID와 manifest SHA-256에 연결됨 |
| `client-preparation.template.json` | 같은 실행에 연결된 환경 준비 기록의 빈 양식. 실행자가 실제 준비 근거를 기입해야 함 |
| `verifier.stdout.log`, `verifier.stderr.log` | 기본 PowerShell 5.1이 실행한 설치 검증의 출력 |
| `installed-check/packaging/evidence/installed-spike.json` | 실제 설치·GUI·runtime·제거와 환경 관측 결과 |
| `installed-check/packaging/evidence/application-report.json` | 실제 앱이 기록한 GUI, frozen Python, 별도 worker, DPAPI 결과 |
| `installed-check/packaging/evidence/screenshot.png` | 실제 앱 화면. 파일 생성과 사람이 읽을 수 있는 올바른 화면인지에 대한 검토는 구분됩니다. |

패키지 자체의 `provenance/`에는 **패키지를 만들기 전에 CI에서 검증했던 과거 증거**가 들어 있습니다. 새 PC에서 얻은 증거는 위의 새 ID 폴더에 생성됩니다. 두 실행의 시점과 환경을 혼동하지 않습니다. 로컬 실행에서 GitHub 실행 ID는 만들어 넣지 않으며, 실제 설치된 engine·Python DLL과 패키지 manifest의 hash를 대조해 전달본의 런타임을 식별합니다.

Manifest의 `source_commit`은 이미 빌드된 설치 파일과 엔진의 출처입니다. `source_identity.files`는 패키징 시 현재 작업 폴더에서 읽은 도구·스크립트 입력의 hash이며, 과거 설치 파일을 새 도구로 감쌌다면 그 도구가 과거 commit에 포함됐다는 뜻은 아닙니다. 제공된 CI workflow는 새 소스를 checkout한 뒤 같은 실행에서 설치 파일을 빌드·검증하고 ZIP을 만듭니다.

## Windows 지원 판정 범위

검증 프로그램이 `PASS`를 기록해도 **깨끗한 Windows 11 일반 사용자 환경의 지원 자격을 자동으로 PASS로 바꾸지 않습니다.** 설치 전 수집기는 OS의 client/server 구분·build·EditionID·DisplayVersion, 실제 토큰의 elevation과 Administrators SID 포함 여부, CPU/RAM, 원래 PATH·uninstall registry·일반 설치 위치의 개발 도구를 관측합니다. 비승격 관리자 토큰에 남은 deny-only 관리자 SID도 확인합니다. Python WindowsApps 실행 별칭은 별도 관측하며, 기본 .NET runtime만으로 SDK 설치를 판정하지 않습니다. 읽기 실패·탐색 한도 초과는 `INCOMPLETE`로 남습니다.

관측 범위에서 도구를 발견하지 않았다는 사실만으로 새 환경을 입증할 수는 없습니다. 실제 Windows 클라이언트·깨끗한 설치 준비·표준 사용자 실행은 환경 준비 기록과 새 설치 결과를 함께 검토합니다. `qualification.windows11_standard_user_clean_machine`과 `qualification.developer_tools_absence`는 `NOT_RUN`을 유지합니다.

GitHub의 Windows Server 2022 관리자 runner에서 Python/Node 경로를 제외한 실행은 전달본과 포함 runtime의 동작 근거입니다. 실제 깨끗한 Windows 11 일반 사용자 검증을 대신하지 않습니다. 게시자 서명, 업데이트 신뢰, 완제품 기능과 완제품 데이터 제거 검증도 이 패키지의 통과 범위에 포함되지 않습니다.

## 새 실행 증거 검토

1. 검증 전에 환경을 준비한 실제 시각·방법·설치 이미지 출처·개발 도구 설치 이력을 남깁니다. 검증 실행 뒤 새 증거 폴더의 `client-preparation.template.json`을 `client-preparation.json`으로 복사하고 실제 기록을 기입합니다. `execution_id`, `manifest_sha256`, `client_environment_sha256`는 생성된 값을 유지합니다. 환경을 준비하지 않았다면 빈 칸을 추정으로 채우지 않습니다.
2. `operator`에 준비 담당자, `prepared_at`에 **검증 시작 전의 실제 준비 시각**(시간대 포함 ISO 8601), `method`에 `clean_install` 또는 `clean_vm_image`, `description`에 구체적인 준비 방법을 기록합니다. `development_tools_installed`는 실제 도구 설치 여부입니다. 비어 있는 기본 양식은 검토 차단 상태로 처리됩니다.
3. 원본 설치·이미지 출처 등 준비 근거를 증거 폴더의 `preparation/` 아래에 보존하고 `records`에 각각의 상대 경로와 SHA-256을 기록합니다. 예: `{"path":"preparation/install-record.txt","sha256":"실제 파일의 64자리 SHA-256"}`. 수집한 환경과 준비 담당자의 진술은 별도로 기록됩니다.
4. 폴더 전체를 검토용 컴퓨터로 옮긴 뒤 아래 검사기를 실행합니다. Python은 **검토용 컴퓨터**에서 사용합니다. 검증 PC의 개발 도구 부재 조건을 위해 검증 중 Python을 설치할 필요가 없습니다.

```powershell
python tools/review_windows_client.py --evidence-root "D:\AIK evidence\실행ID" --expected-manifest-sha256 "신뢰한 빌드 기록의 manifest_sha256"
```

`--expected-manifest-sha256`은 신뢰하는 CI 빌드의 `build-handoff.json`에서 가져옵니다. 제출된 폴더 내부의 값만 다시 복사해서 신뢰 근거로 사용하지 않습니다. 도구는 입력 파일을 수정하지 않고 JSON을 표준 출력으로 내보냅니다. 원하면 검토용 출력 파일로 저장할 수 있습니다.

| 상태 / 종료 코드 | 의미 |
|---|---|
| `READY_FOR_REVIEW` / `0` | manifest·새 실행·설치본·runtime·화면·환경·준비 첨부의 연결이 일치하여 독립 검토할 수 있음 |
| `BLOCKED` / `2` | 실제 실행 미통과, 후보 Windows/표준 사용자 조건 불충족, 도구 발견, 관측 오류, 준비 기록 누락 등 |
| `FAIL` / `1` | 파일 변경·잘못된 hash·실행 ID·시간 모순·위험한 경로·잘못된 JSON 등 |

`READY_FOR_REVIEW`는 지원 판정이 아닙니다. 담당자의 환경 준비 진술과 이미지 출처, 화면, 최신 보안 업데이트·hardware·경로·오프라인 조건을 독립 검토하고 AIK-003 원장에 근거를 기록해야 합니다. 검사기는 원장 상태를 바꾸지 않습니다. 판본 후보는 기존 ADR의 Windows 11 25H2/build 26200 및 26H2/build 26300, Home/Pro 계열을 사용하며 최신 지원 여부를 자동 조회하지 않습니다. 과거 Server CI 증거는 무결성을 검토할 수 있지만 청정 클라이언트 검증에는 `BLOCKED`입니다.

## 인코딩 처리

기존 `scripts/windows/verify-installed-spike.ps1`의 소스 파일은 바꾸지 않았습니다. ZIP에 포함되는 실행 사본에 UTF-8 BOM을 붙여 기본 Windows PowerShell 5.1이 한글 경로를 올바르게 읽게 했습니다. 원본은 `provenance/verify-installed-spike.original.ps1`로 보존하며, manifest에 변환 방식과 양쪽 hash가 있습니다. ASCII로 작성된 시작 스크립트가 모든 패키지 구성원과 이 변환을 검사한 후 고정된 설치 검증 스크립트만 실행합니다.
