# AIK-017 — 겹치는 비밀값·진단 문자열·정제 한도 회귀

단일 대상 태스크는 **AIK-017**이다. 기준 main은 `c1608647e9ff3c759031efe56b6e62994499586c`, 수정 전 정제기 Git blob은 `eb2902c1ff7a85265c42d36dba45555f98cd2cbc`다. 로컬에 복원한 원본과 기존 테스트를 각각의 Git blob과 바이트 대조한 뒤 시험했다.

## 수정 전 재현

신규 테스트의 `test_01`~`test_08`을 수정 전 소스에서 실행했다. **8개 중 실패 7개·오류 1개, 통과 0개**다. 두 비밀값의 겹침, 일반 패턴과 등록 비밀의 겹침, 부분 키의 겹침, quoted JSON 진단값, 약어 필드명, 유니코드 예외, 실패한 등록의 원자성, envelope를 제외한 크기 검사 결함을 구분했다. fixture는 실제 자격증명이 아닌 합성 문자열이다.

## 수정 후 및 근거

기존 `tests/test_security_redaction.py`의 17개는 변경하지 않았고, 신규 21개를 추가했다. Python 3.13.5/Linux의 로컬 실행에서 **38/38 PASS, 실패·오류·skip 0**을 확인했다. [로컬 결과와 소스 해시](local-validation.json)를 보존한다.

검증 소스 `d8fb632c6bd7493e2f53f8e35d9b8ae3387682e2`의 [실행 38343374083](https://github.com/jaeseongs95/AI-Kubernetes/actions/runs/38343374083)에서 Windows Server 2022와 Ubuntu 24.04의 Python 3.14.8로 **정제 시험 38개 및 원장 회귀 53개가 각각 통과**했다. 두 환경에서 실제 `planning/backlog.sqlite3`의 AIK-017 본문·완료 기준·선행 작업을 먼저 조회했다. 기록된 선행은 AIK-004 blocked, AIK-009 todo, AIK-011 todo였다.

이어 같은 소스의 prepare job에서 원장을 검증·갱신하고 [결과](ci-validation.json)를 생성했다. `record_ledger.py`는 AIK-017에만 evidence 1개와 `todo → blocked`의 `status_change` 이력 1개를 추가한다. 90개 태스크·311개 완료 기준·365개 의존성·스키마와 다른 태스크의 모든 필드가 보존됨을 대조하고 SQL/Markdown 및 README 집계를 갱신했다. 소스 해시 3개는 로컬 시험과 모두 일치한다.

정본 DB의 새 Git blob은 `97b9248ea0dafc1fba576552378a57a052820561`, SHA-256은 `8a2aca6fc5ac89e0578fb25dbf7a0c602c41012a99bb8d38c4ea6bc52c0d1c0c`다. 준비 job은 명시된 다섯 파일의 불변 blob만 생성했고 branch/commit은 수정하지 않았다. 결과를 회수한 뒤 임시 쓰기 권한 workflow를 삭제해 최종 변경에 포함하지 않았다.

실제 vault·provider·설치형 제품의 모든 출력 호출부는 이번 시험에서 **NOT_RUN**이다. 이 수정을 이유로 선행 AIK-004·009·011이나 AIK-017 전체 완료를 선언하지 않는다.
