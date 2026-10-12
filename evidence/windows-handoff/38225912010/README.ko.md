# 첫 Windows handoff 시도: 회귀 검사 실패

- 소스: `fe93df71a19759590515327c6cfb1d7b27d95c8c`
- [실행 38225912010](https://github.com/jaeseongs95/AI-Kubernetes/actions/runs/38225912010), job `114733248512`: **FAILURE**.
- 회귀 검사 8개 중 7 PASS, 1 FAIL. installer 빌드·실제 앱 실행은 아직 수행되지 않아 **NOT_RUN**이다.

`test_tampered_delivered_script_is_rejected_before_any_install`이 기대한 변조 거부 메시지 대신 `Get-FileHash`를 찾을 수 없다는 오류로 실패했다. legacy Get-FileHash script cmdlet의 가용성에 의존한 결함이다. pwsh→Python→WindowsPowerShell5.1에 상속된 PSModulePath가 환경 원인으로 추정되지만, 첫 실패에서 그 경로 값 자체를 캡처하지 않았으므로 확인된 오류와 추론을 구분한다.

수정 commit `4b948f57020f3e507683de6e538544b9bac558e7`은 wrapper의 SHA 계산만 .NET SHA256과 File.OpenRead streaming으로 바꾸고, stream/hasher를 finally에서 해제했다. 기존 변조 거부·installer/runtime·실제 GUI·자격값 검사 조건을 유지했다. [별도 새 실행 38226204128](../38226204128/README.ko.md)에서 같은 Windows 회귀 검사와 실제 PS5.1 설치 경로가 통과했다.

원본 CI 로그와 metadata를 `metadata/`에, 원인·수정·hash 및 노출 정보 검사 결과를 `failure-analysis.json`에 보존한다. 첫 실패의 status와 원문은 성공으로 덮어쓰지 않았다.
