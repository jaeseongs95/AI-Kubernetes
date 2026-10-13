# AIK-011 — 인증 전환·실행 경합 수정 근거

단일 대상은 **AIK-011**이다. 기준 main은 `1a43ae80d1ebbd817b977172fc2cf76d31bb17e0`, 이전 controller Git blob은 `076bb08606e4ae800f40027a9e4d8bf04d1d99c1`이다. `contracts.py`, provider JSON 스키마와 기존 36개 시험은 변경하지 않았다.

## 실제 원장 조회와 반례

검증 소스 `e74f4917eba7d001cdaad1235fcebb34740612f3`의 [실행 38353834586](https://github.com/jaeseongs95/AI-Kubernetes/actions/runs/38353834586)에서 `planning/backlog.sqlite3`를 읽기 전용으로 조회했다. AIK-011은 todo, 선행 AIK-001은 done, AIK-010은 todo였고 ready 태스크는 0개였다. 기존 완료 기준 세 개는 그대로 읽어 확인했다.

`record_evidence.py`는 별도 임시 디렉터리에 정확한 이전 controller를 복원해 신규 `test_01`~`test_08`을 실행했다. **선택한 8개 테스트가 모두 실패**했으며, 로그인/로그아웃을 나눠 검사한 한 테스트의 subtest를 포함해 실패 기록은 9개다. 오류/skip은 0개다. 이 실패는 의도적으로 이전 결함을 재현한 결과이며 수정본 테스트 실패와 구분한다. 실제 자격증명이 아닌 합성 계정·상태·callback만 사용했다.

## 수정 후 검증

동일 실행의 Windows Server 2022와 Ubuntu 24.04에서 **기존 36개+신규 24개=60/60 PASS**, 원장 회귀 **53/53 PASS** 및 정본 검증이 각각 통과했다. Python 3.14.8 CI 실행이며 로컬 실행이나 실제 벤더 로그인을 했다고 기록하지 않는다.

이후 같은 소스의 prepare job에서 원장을 검증·갱신하고 [결과 및 소스 해시](ci-validation.json)를 생성했다. AIK-011에만 evidence 1개와 `todo → blocked`의 `status_change` 이벤트 1개를 추가했다. 다른 89개 태스크의 모든 필드, 완료 기준 311개, 의존성 365개와 스키마를 대조해 보존하고 SQL/Markdown·README 집계를 갱신했다.

생성된 정본 DB Git blob은 `241f72295d8f07a05b498089962c47b3c97e104a`, SHA-256은 `fc15a6892876c583780fe34ada120f3a36a1ad985c16f1264f0c029ee491a362`다. 준비 workflow는 지정된 다섯 파일의 불변 Git blob만 생성했으며 branch/commit을 바꾸지 않았다. 결과 회수 후 임시 쓰기 권한 workflow를 삭제했다. 검증된 controller·기존 테스트의 bytes는 원장 기록 커밋에서 변경하지 않는다.

## 범위

[변경 동작과 제한](../../../docs/provider-auth-lifecycle.ko.md): pending 인증 문맥 보존, 원래 binding으로만 해제, 불명확한 인증/중단 결과의 재호출 차단, callback 재진입 거절, 인가 후 계정 재검사다. 하나의 동기 GuardedAdapter 인스턴스 안에서의 방어이며 다른 프로세스/호스트의 직접 변경을 격리하지 않는다.

선행 AIK-010과 공식 호스트/Windows 설치형 제품 통합은 미완료다. 공통 어댑터 합성 회귀 결과를 live provider·OS 격리·내구성 있는 복구의 완료 근거로 전환하지 않는다.
