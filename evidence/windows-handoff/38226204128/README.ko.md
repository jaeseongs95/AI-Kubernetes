# 실제 Windows PowerShell 5.1 전달본 검증

- 소스: `4b948f57020f3e507683de6e538544b9bac558e7`
- [Windows client handoff 실행 38226204128](https://github.com/jaeseongs95/AI-Kubernetes/actions/runs/38226204128), 첫 attempt, job `114734211621`: **SUCCESS**.
- 실제 환경: Windows Server 2022 Datacenter, x64, 관리자 runner. **깨끗한 Windows 11 일반 사용자 검증은 NOT_RUN**이다.

## 실제로 통과한 내용

기존 installer를 빌드하고 한 번 설치 검증한 후, 그 정확한 installer를 독립 ZIP으로 만들었다. ZIP을 checkout 밖의 다른 한글·공백 폴더에 압축 해제하고 기본 Windows PowerShell **5.1.20348.5622 / Desktop**으로 다시 실행했다. 별도 Python/Node runtime 경로를 제외한 실행에서 신규 설치, GUI, frozen Python 3.14.8, 별도 worker, 현재 사용자 DPAPI, 제거가 모두 통과했다. 패키지 구성원과 BOM 변환 및 실제 설치된 engine·Python DLL identity 검사도 통과했다.

기본 encoding은 **Windows-1252(1252), en-US**였다. 같은 실제 legacy parser가 원본 UTF8NoBOM verifier에서 오류 **2개**, BOM을 추가한 실행 사본에서 오류 **0개**를 관측했다. 원본 파일을 수정하거나 검사 조건을 완화하지 않았다. 첫 run에서 발견한 Get-FileHash 모듈 의존은 .NET SHA256 streaming으로 제거했다. [첫 실패 근거](../38225912010/README.ko.md)는 성공 근거와 별도로 보존한다.

## 원본과 검토 근거

`artifact/` 아래는 GitHub evidence artifact의 디렉터리 구조와 bytes를 그대로 보존했다. 특히 `artifact/evidence/`는 ZIP을 만들기 전 실행, `artifact/handoff/evidence/8d415a8070b94a67aa92effb29e24801/`는 ZIP을 압축 해제한 뒤 새로 실행한 결과다. 각각의 시각, GUI·sidecar·worker PID 및 application report hash가 다르며 재포장한 과거 결과를 신규 실행으로 계산하지 않았다.

- [신규 handoff raw report](artifact/handoff/evidence/8d415a8070b94a67aa92effb29e24801/handoff-validation.json)
- [신규 실제 앱 raw report](artifact/handoff/evidence/8d415a8070b94a67aa92effb29e24801/installed-check/packaging/evidence/application-report.json)
- [신규 실제 화면](artifact/handoff/evidence/8d415a8070b94a67aa92effb29e24801/installed-check/packaging/evidence/screenshot.png)
- [다운로드·identity·신규 실행·자격값 종합 검증](verification.json)
- [실제 CI 전체 로그](metadata/windows-job.log)

새 화면을 직접 열어 3/3 완료, 정상 한글, 카드·버튼 겹침 없음, sidecar **816**·worker **6404**가 실제 report와 일치함을 확인했다. 별도의 독립 reviewer도 raw JSON·hash·화면을 검토했다. 최종 책임자인 root도 화면과 hash chain·신규 실행·실제 legacy parser 결과를 직접 검토했으며 [최종 검토 기록](root-review.json)에 보존했다. 자동 생성 raw report의 `visual_review: NOT_RUN`은 변경하지 않았으며, 수행한 시각 검토는 `verification.json`에 따로 기록했다.

Manifest의 소스 입력 hash는 **실제 Windows checkout의 CRLF bytes**를 기록한다. 로컬 LF 파일과 raw SHA가 다른 경우 줄바꿈만 CRLF로 변환해 동일한 소스임을 확인했으며, 원본 artifact나 manifest를 다시 쓰지 않았다. ZIP 내부 원본 verifier→BOM 사본은 BOM 3 bytes 추가 외 모든 bytes가 동일하다.

## 실제 다운로드한 전달본

| 파일 | bytes | SHA-256 |
|---|---:|---|
| `AIK-Windows-Client-Handoff-4b948f57020f.zip` | 118,784,150 | `a6edd83fd952257d5d7f731c23162aeaca30974e0cc303bbd7b0210c9e7dd23e` |
| `AIK-Foundation-Spike-0.1.0-win-x64-dev-unsigned.exe` | 118,718,153 | `16811e29d6e65cb5f24f072d3a8896d155c6a46bff83a376d01c2e0c1ad5c95a` |

- [검증한 독립 ZIP artifact](https://github.com/jaeseongs95/AI-Kubernetes/actions/runs/38226204128/artifacts/11736453349): 2026-10-19 만료 예정.
- [검증한 unsigned installer artifact](https://github.com/jaeseongs95/AI-Kubernetes/actions/runs/38226204128/artifacts/11736503454): 2026-10-19 만료 예정.
- [원본 evidence artifact](https://github.com/jaeseongs95/AI-Kubernetes/actions/runs/38226204128/artifacts/11736737827): 2026-10-26 만료 예정.

3개 GitHub artifact ZIP을 실제 다운로드하여 API metadata의 digest·byte 수와 대조했다. 독립 전달 ZIP과 installer는 그 artifact ZIP 내부 파일이므로 위 표와 GitHub artifact ZIP 자체의 hash는 서로 다르다. 내부 전달 ZIP의 11개 구성원, manifest hash, installer bytes, 사전 검증 provenance, 새 설치 runtime/app/PNG identity까지 다시 검증했다. 대용량 ZIP/EXE는 Git에 넣지 않고 작은 원본 증거와 다운로드 검증 기록을 보존했다.

## 남아 있는 자격 조건

`windows11_standard_user_clean_machine`, `developer_tools_absence`, `release_signing`, `product_integration`은 모두 **NOT_RUN**이다. PATH에서 도구를 제외해 실행한 것은 컴퓨터에 개발 도구가 설치되지 않았다는 증명이 아니다. Windows Server 관리자 CI 통과는 실제 깨끗한 Windows 11 일반 사용자 지원 자격을 대신하지 않는다. 이 앱은 합성 기반 기능을 검증하는 unsigned foundation spike이며 실제 벤더 계정 저장·모델 라우팅·AGS/VM 제품 통합·완제품 출시가 통과한 것으로 계산하지 않는다.
