# AIK-011 adapter 계약 구현 근거 — 2026-10-13

- 대상 태스크: **AIK-011 한 건** / ProviderAdapter 인증·실행 공통 계약
- 기준 `main`: `bc3496c7b78148bd1344e7a5b784e231cdecc067`
- 구현: `src/aik_provider/{__init__.py,contracts.py,controller.py}`
- 스키마: `contracts/provider-adapter/v1.schema.json`
- 계약/제한 설명: `docs/provider-adapter.ko.md`
- 검증: `tests/test_provider_contract.py`, `python -m unittest discover -s tests -p test_provider_contract.py -v`
- CI: `.github/workflows/provider-adapter-contract.yml`, Windows Server 2022 + Ubuntu 24.04 (Python 3.14.8)
- 시험 유형: 독립 synthetic mock 계약·계정 바인딩·경합·rate-limit·불명확 결과 회귀. 실제 벤더/공식 호스트/인증/VM/AGS 호출 없음
- 원장 상태/선행: AIK-011 `todo`, AIK-010 미완료. 정본 SQLite·기존 의존성·기준·12개 지원 matrix route 변경 없음
- 실제 provider qualification: **NOT_RUN**. 가짜 adapter의 capability disclosure를 운영 qualified로 광고하지 않음

CI 실행 판정과 재현 가능한 증거는 해당 commit에서 실행된 GitHub Actions 결과가 기준이다. 한 태스크에 대한 코드 구현 기록이며 제품·선행 인수 완료 근거는 아니다.
