# 실제 Windows 설치 GUI 검증 — run 38219549819

[원격 Windows 실행](https://github.com/jaeseongs95/AI-Kubernetes/actions/runs/38219549819)은 소스 `56523355d9cf3a2437989d1dd26f2e5ced1666d2`의 첫 시도이며, 설치·runtime bundle·실제 renderer 버튼을 통한 GUI/worker/DPAPI·제거를 모두 통과했다. 환경은 Windows Server 2022 Datacenter x64, 관리자 계정이다.

| 파일 | 근거 |
|---|---|
| `workflow-run.json`, `windows-job.log` | 실제 원격 실행 metadata와 원문 job 로그 |
| `sidecar-build.json` | 고정 CPython 및 실제 frozen runtime·package·DLL 해시 |
| `installed-spike.json` | 설치본·환경·설치·실행·제거·해시 판정 |
| `application-report.json`, `screenshot.png` | 실제 설치된 창에서 수행한 검사 결과와 화면 |
| `verification.json` | 원본 파일·설치본·build→installed hash와 worker 부모 PID의 교차 대조 |

원본 JSON·로그·PNG를 변경 없이 보존했다. `verification.json`만 저장소 상대 경로로 정리하고 임시 다운로드 경로를 제외한 후속 검토 기록이다. 원본 검토 기록의 hash도 포함한다.

설치 EXE는 118,718,430 bytes이며 SHA-256은 `955d27bd62158b029e659ea04a65807599b8ac40f1ccd40190acdb6beea83a16`이다. 실제 다운로드 파일과 CI 기록이 일치했다. 개발 설치본은 [해당 run의 installer artifact](https://github.com/jaeseongs95/AI-Kubernetes/actions/runs/38219549819/artifacts/11732924948)에 있고, 보존 기한은 2026-10-19T07:49:02Z다. 소스와 빌드 명령은 저장소에 보존한다. 이 EXE는 `NotSigned` 상태의 개발 검증용이다.

실제 GUI·worker·DPAPI 성공은 AIK-002 범위의 근거다. Windows 11의 깨끗한 표준 사용자 설치, 실제 벤더 모델, AGS/VM governed 실행, 제품 성능·서명·업데이트·완제품 출시는 `NOT_RUN`이다.
