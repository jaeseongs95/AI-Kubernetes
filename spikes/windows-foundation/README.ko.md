# Windows 기반 연결 spike — AIK-002

Electron·React/TypeScript 화면에서 제한된 IPC를 거쳐 실제 Python sidecar와 별도 테스트 worker를 실행한다. Windows에서는 현재 사용자 DPAPI로 합성 값을 보호·파일 저장·복원·삭제한다. 벤더 계정, 실제 모델, AGS/VM 작업을 실행하는 제품 runtime은 아직 이 앱에 포함되지 않는다.

## 실행 경로

GUI 버튼 → 한정된 preload 메서드 → 앱 main의 발신 창·frame·URL·schema 검증 → 전용 stdin/stdout pipe의 Python sidecar → 고정 worker 또는 Windows DPAPI 왕복.

renderer는 Node API, 임의 shell·경로·명령, raw secret 조회 메서드를 받지 않는다. sidecar는 시작 시 상속 pipe로 받은 임시 capability를 확인하며 API key·Python 주입 환경 변수를 자식에게 전달하지 않는다. IPC 제한과 Windows 동일 사용자 프로세스 사이의 OS 격리는 별개다.

### 개발 환경에서 실행

Node 24와 Python 3.12 이상이 필요하다. Windows 번들 빌드는 [Windows 패키징 절차](../../docs/windows-packaging.ko.md)의 고정 버전을 사용한다. 아래 명령은 이 디렉터리 기준이다.

```powershell
npm ci --no-audit --no-fund
npm run build
$env:AIK_SPIKE_PYTHON = (Get-Command python).Source
npm test
npm start
```

Linux에서도 renderer 빌드와 Python/Node pipe 시험을 수행할 수 있다. Windows vault는 `NOT_RUN / WINDOWS_REQUIRED`를 반환한다.

```bash
npm ci --no-audit --no-fund
npm run build
AIK_SPIKE_PYTHON="$(command -v python)" npm test
python -m unittest discover -s tests -p 'test_*.py' -v
```

`AIK_SPIKE_PYTHON`은 개발자 설정용 절대경로다. 패키징된 앱은 이 값을 사용하지 않고 `resources/engine/aik-engine.exe`만 실행한다. 테스트 worker는 임시 합성 파일만 다루며 다른 프로그램이나 명령을 입력받지 않는다.

### Windows 설치본 만들기와 검사

저장소 루트에서 다음 순서로 실행한다. 빌드 도구 설치 및 버전·해시 검증은 패키징 문서를 따른다.

```powershell
./scripts/windows/build-sidecar.ps1
Push-Location spikes/windows-foundation
npm run package:win
Pop-Location
./scripts/windows/verify-installed-spike.ps1
```

CI는 실제 설치된 앱을 저장소 밖 공백·한글 경로에서 실행한다. 자식 PATH에서 개발 도구를 제외하고, 표시된 창의 버튼을 통해 health·worker·vault를 실행한다. main이 받은 backend 응답과 화면의 상태가 일치해야 통과하며 renderer가 제출한 결과 객체를 증거로 삼지 않는다. 설치본 hash, 실행 보고서, 화면 캡처와 제거 결과를 남긴다.

GitHub의 Windows Server runner 실행은 Windows 11 일반 사용자 PC의 지원 qualification을 대체하지 않는다. unsigned 개발 설치본은 첫 제품 릴리스가 아니다.

## 시험 판정

- `PASS`: 해당 기반 동작을 실제로 수행했다.
- `NOT_RUN`: 해당 OS에서 실행할 수 없거나 제품 통합을 수행하지 않았다.
- `FAIL`: 실행한 동작 또는 검증 계약에서 오류가 발생했다.
- GUI smoke 전체 `BLOCKED`: Windows native 시험 등 필요한 항목이 `NOT_RUN`이다.

모든 보고서는 `product_integration: NOT_RUN`을 유지한다. 이 spike 결과를 AIK-009의 제품 vault나 AIK-044/059/064의 실제 제품 검증 완료로 사용할 수 없다.

## 구성

| 경로 | 역할 |
|---|---|
| `src/` | 한국어 React 화면과 제한된 typed client |
| `electron/` | renderer 경계·프로토콜·sidecar 수명·GUI smoke |
| `python/aik_spike/server.py` | bounded JSON-lines server·실제 고정 worker |
| `python/aik_spike/vault.py` | Windows 사용자 DPAPI 시험 |
| `tests/` | 잘못된 IPC·실제 process pipe·파일 효과·native vault 시험 |

VM의 기존 React 구성과 EngineClient 책임 분리는 검토했다. 기존 화면의 `MockEngineClient`와 schema 2 DTO는 이 앱에 연결하지 않았다. 실제 VM façade와 저장 schema 4의 계약은 별도 [통합 계약](../../docs/integration-contract.ko.md)에서 고정한다.
