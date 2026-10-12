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
| `verifier.stdout.log`, `verifier.stderr.log` | 기본 PowerShell 5.1이 실행한 설치 검증의 출력 |
| `installed-check/packaging/evidence/installed-spike.json` | 실제 설치·GUI·runtime·제거와 환경 관측 결과 |
| `installed-check/packaging/evidence/application-report.json` | 실제 앱이 기록한 GUI, frozen Python, 별도 worker, DPAPI 결과 |
| `installed-check/packaging/evidence/screenshot.png` | 실제 앱 화면. 파일 생성과 사람이 읽을 수 있는 올바른 화면인지에 대한 검토는 구분됩니다. |

패키지 자체의 `provenance/`에는 **패키지를 만들기 전에 CI에서 검증했던 과거 증거**가 들어 있습니다. 새 PC에서 얻은 증거는 위의 새 ID 폴더에 생성됩니다. 두 실행의 시점과 환경을 혼동하지 않습니다. 로컬 실행에서 GitHub 실행 ID는 만들어 넣지 않으며, 실제 설치된 engine·Python DLL과 패키지 manifest의 hash를 대조해 전달본의 런타임을 식별합니다.

Manifest의 `source_commit`은 이미 빌드된 설치 파일과 엔진의 출처입니다. `source_identity.files`는 패키징 시 현재 작업 폴더에서 읽은 도구·스크립트 입력의 hash이며, 과거 설치 파일을 새 도구로 감쌌다면 그 도구가 과거 commit에 포함됐다는 뜻은 아닙니다. 제공된 CI workflow는 새 소스를 checkout한 뒤 같은 실행에서 설치 파일을 빌드·검증하고 ZIP을 만듭니다.

## Windows 지원 판정 범위

검증 프로그램이 `PASS`를 기록해도 **깨끗한 Windows 11 일반 사용자 환경의 지원 자격을 자동으로 PASS로 바꾸지 않습니다.** 해당 컴퓨터가 실제로 Windows 11 클라이언트인지, 개발 도구가 없는 새 환경인지, 관리자 권한 없이 실행했는지는 환경 준비 기록 및 실제 결과와 함께 검토해야 합니다. `qualification.windows11_standard_user_clean_machine`과 `qualification.developer_tools_absence`는 자동 판정하지 않아 `NOT_RUN`으로 남습니다.

GitHub의 Windows Server 2022 관리자 runner에서 Python/Node 경로를 제외한 실행은 전달본과 포함 runtime의 동작 근거입니다. 실제 깨끗한 Windows 11 일반 사용자 검증을 대신하지 않습니다. 게시자 서명, 업데이트 신뢰, 완제품 기능과 완제품 데이터 제거 검증도 이 패키지의 통과 범위에 포함되지 않습니다.

## 인코딩 처리

기존 `scripts/windows/verify-installed-spike.ps1`의 소스 파일은 바꾸지 않았습니다. ZIP에 포함되는 실행 사본에 UTF-8 BOM을 붙여 기본 Windows PowerShell 5.1이 한글 경로를 올바르게 읽게 했습니다. 원본은 `provenance/verify-installed-spike.original.ps1`로 보존하며, manifest에 변환 방식과 양쪽 hash가 있습니다. ASCII로 작성된 시작 스크립트가 모든 패키지 구성원과 이 변환을 검사한 후 고정된 설치 검증 스크립트만 실행합니다.
