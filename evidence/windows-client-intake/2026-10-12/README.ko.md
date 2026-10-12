# AIK-003 청정 Windows 클라이언트 증거 수집·검토 코드

기준 commit: `7fc8525957fd37d31a2f6d18345816920da0e8b6`. 이 변경은 기존 AIK-003 하나의 남은 검증 경로를 구현한다. 당시 원장은 done 2·blocked 1·todo 87·ready 0이며, AIK-003은 선행 없는 유일한 미완료 태스크다.

## 구현

- 전달 패키지에 ASCII PowerShell 5.1 환경 수집기를 포함하고 구성원 SHA-256 검사 후 호출한다. 설치·자식 PATH 변경 전에 OS 판본/실제 CPU architecture·토큰 elevation/관리자 그룹·CPU/RAM·원래 PATH/registry/일반 설치 경로를 관측한다.
- 새 wrapper schema 2는 실행 ID·manifest·환경 JSON의 hash를 연결하고 환경 준비 기록의 빈 양식을 생성한다. 관측 범위와 읽기 실패를 보존한다.
- Python 표준 라이브러리 검사기는 신뢰한 빌드 manifest hash, 실제 설치본/engine/DLL·새 GUI/PNG·환경·준비 첨부를 연결한다. 결과는 `READY_FOR_REVIEW`, `BLOCKED`, `FAIL`이며 qualification과 원장은 자동 변경하지 않는다.
- CI가 수집 전에 부모 PATH를 지우던 경로를 수정했다. Windows Server의 실제 관리자·개발 도구 관측과 새 설치 증거의 무결성/클라이언트 차단을 함께 검증하도록 연결했다. 예상 차단 종료 코드 2는 검증 후 CI 단계 성공으로 처리한다.

## 이번 로컬 검증

`python -m unittest discover -s tests -v`는 실제 Linux PowerShell 7.6.6 실행기를 연결한 상태에서 **113개 모두 PASS**, 실패·오류·skip 0개였다. 이 중 새 증거 검사기 31개, 전달 패키지 10개를 포함한다. 새 합성 fixture에는 실제 Windows 검증이 아니라는 표식을 넣었다. 파일/manifest/runtime hash 변조, 이전 보고서 재사용, 시간·실행 ID 모순, 비승격 관리자, 도구/관측/준비 기록 누락, qualification 과장, 경로 이탈·symlink, JSON 자료형 및 CLI 종료 코드를 확인한다.

보존된 실제 Server 실행 `38226204128`은 새 검사기로 무결성 오류 없이 `BLOCKED`가 나왔다. Server·미선택 build·관리자·사전 환경 관측 누락이 이유이며, qualification은 모두 `NOT_RUN`이다. 입력 원본은 변경하지 않았다.

- [로컬 검증 기록과 검사한 소스 hash](local-validation.json)
- [보존된 Server 실행의 새 검사 결과](historical-server-review.json)
- [전달 패키지와 실제 준비 기록 사용법](../../../tools/windows_handoff/README.ko.md)

## 원장 판정

AIK-003의 원래 AC2 및 verification은 **개발 도구가 없는 깨끗한 지원 후보 Windows에서의 설치 spike와 문서 검토**를 요구한다. 이번 작업은 그 증거 수집·검토 코드를 구현했다. 새 코드의 실제 Windows PowerShell 5.1 수집은 후속 CI 실행으로 검증하고, 실제 깨끗한 Windows 11 표준 사용자 검증은 별도로 필요하다. 태스크는 `blocked`를 유지한다. 태스크 정의·완료 기준·의존성 및 다른 89개 태스크의 상태는 그대로 보존한다.
