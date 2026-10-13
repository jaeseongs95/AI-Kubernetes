# AIK-017 — 겹치는 비밀값·진단 문자열·정제 한도 회귀

단일 대상 태스크는 **AIK-017**이다. 기준 main은 `c1608647e9ff3c759031efe56b6e62994499586c`, 수정 전 정제기 Git blob은 `eb2902c1ff7a85265c42d36dba45555f98cd2cbc`다. 로컬에 복원한 원본과 기존 테스트를 각각의 Git blob과 바이트 대조한 뒤 시험했다.

## 수정 전 재현

신규 테스트의 `test_01`~`test_08`을 수정 전 소스에서 실행했다. **8개 중 실패 7개·오류 1개, 통과 0개**다. 두 비밀값의 겹침, 일반 패턴과 등록 비밀의 겹침, 부분 키의 겹침, quoted JSON 진단값, 약어 필드명, 유니코드 예외, 실패한 등록의 원자성, envelope를 제외한 크기 검사 결함을 구분했다. fixture는 실제 자격증명이 아닌 합성 문자열이다.

## 수정 후 및 근거

기존 `tests/test_security_redaction.py`의 17개는 변경하지 않았고, 신규 21개를 추가했다. Python 3.13.5/Linux의 로컬 실행에서 **38/38 PASS, 실패·오류·skip 0**을 확인했다. [로컬 결과와 소스 해시](local-validation.json)를 보존한다. Windows Server 2022/Ubuntu 24.04의 Python 3.14.8 검증은 해당 GitHub Actions 실행과 구분한다.

`record_ledger.py`는 실제 정본 SQLite를 먼저 조회하고, 검증 후 AIK-017에만 evidence 1개와 `todo → blocked` 상태 이력 1개를 추가한다. 90개 태스크·311개 완료 기준·365개 의존성·스키마와 다른 태스크의 모든 필드가 보존되는지 대조하고 SQL/Markdown 및 README 집계를 갱신한다. 생성 결과와 소스 판본은 `ci-validation.json`에 기록한다.

실제 vault·provider·설치형 제품의 모든 출력 호출부는 이번 시험에서 **NOT_RUN**이다. 이 수정을 이유로 선행 AIK-004·009·011이나 AIK-017 전체 완료를 선언하지 않는다.
