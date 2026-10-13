# AIK-011 — 인증 전환·실행 경합 수정 근거

단일 대상은 **AIK-011**이다. 기준 main은 `1a43ae80d1ebbd817b977172fc2cf76d31bb17e0`, 이전 controller Git blob은 `076bb08606e4ae800f40027a9e4d8bf04d1d99c1`이다. `contracts.py`, provider JSON 스키마와 기존 36개 시험은 변경하지 않았다.

`record_evidence.py`는 실제 정본 `planning/backlog.sqlite3`를 읽기 전용으로 조회한다. 임시 별도 디렉터리에 정확한 이전 controller를 복원해 신규 `test_01`~`test_08` 반례를 실행한 뒤, 현재 checkout의 기존 36개+신규 24개 suite를 검증한다. 실패 반례는 실제 자격증명이 아닌 합성 계정/상태/callback만 사용한다. 로컬 실행이나 실제 벤더 로그인을 했다고 기록하지 않는다.

두 운영체제 CI가 통과한 뒤 같은 소스의 준비 작업에서 AIK-011에만 evidence 1개와 `todo → blocked`의 상태 이력 1개를 추가한다. 다른 89개 태스크의 모든 필드, 완료 기준 311개, 의존성 365개와 스키마를 대조하고 SQL/Markdown·README 집계를 갱신한다. 생성된 바이트·소스 해시·원장 비교 결과는 `ci-validation.json`에 기록한다.

준비 workflow는 지정한 다섯 파일의 immutable Git blob만 생성하며 branch/commit을 변경하지 않는다. 결과 회수 후 해당 임시 workflow를 삭제한다. 실제 Git 반영과 최종 회귀 결과는 별도로 확인한다.

선행 AIK-010과 공식 호스트/Windows 설치형 제품 통합은 미완료다. 본 실행은 공통 어댑터의 합성 회귀 검증이며 live provider/OS 격리/내구성 있는 복구의 완료 근거가 아니다.
