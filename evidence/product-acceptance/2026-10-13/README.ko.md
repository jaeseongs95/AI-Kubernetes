# AIK-083 후보 제품 인수 계약 구현 기록

- 작업 태스크: **AIK-083** 한 건
- 출발점: `main` `6fcb2ab2b9ee2bb07ce721f3a51e20256ae5f95e`
- 산출물: `contracts/product-acceptance/v1.json`, 불완전한 고정 입력 프로젝트, `tools/product_acceptance.py`, 12개 회귀 시험, 전용 Windows/Linux CI, `docs/product-acceptance-v1.ko.md`
- 제품 scope: M4 P01~P10, M5 P01~P11, M6 P01~P12; 각 항목 담당 task·GUI·gate 추적
- 고정 지원 matrix: `contracts/upstream/support-matrix.json` revision 1 / Git blob `b563f9f63b88f2a6e52a77f3e67acc871a98b88e`
- 실기 qualification: **NOT_RUN** (Windows 11 일반 사용자, 실제 벤더, VM/AGS governed A/B→C, 제품 GUI, 8시간 부하)
- 상태: `proposed / candidate_not_qualified`; AIK-003 미완료이므로 AIK-083을 done 또는 in_progress로 승격하지 않음
- 검증 수단: `python -m unittest discover -s tests -p test_product_acceptance.py -v` 및 `python tools/product_acceptance.py validate`
- 주의: CI 성공은 후보 계약의 구조·고정 정산 파일 oracle만 증명한다. 제품 출시·실제 독립 감사까지 완료했다는 근거가 아니다.

정본 `planning/backlog.sqlite3`는 이번 작업에서 변경하지 않는다. 증거 및 향후 검토를 위해 이 경로에 변경 범위를 남긴다.
