# AIK-017 구현 근거 — OutputRedactor

- 대상: AIK-017 (프롬프트·로그·오류·내보내기 시크릿 제거), **단일 태스크**
- 기준 main: `8e19d42d1092dd9a781a5fd6b3d88f9c4325075c`
- 구현: `src/aik_security/{__init__.py,redaction.py}`
- 시험: `tests/test_security_redaction.py` (synthetic test-only canaries, 17 tests)
- CI: `.github/workflows/secret-redaction.yml` (Windows Server 2022 + Ubuntu 24.04)
- 설계·한계: `docs/secret-redaction.ko.md`
- 검증 명령: `python -m unittest discover -s tests -p test_security_redaction.py -v`
- 제품/실제 벤더/Windows 11 클린 환경: **NOT_RUN**
- 원래 선행 조건: AIK-004·AIK-009·AIK-011 미완료; 원장 태스크 상태·정의·완료 기준·의존성 불변
- 이 모듈은 원시 비밀을 받는 신뢰 영역에서 출력 경계를 정제하기 위한 부품이며 제품 전체 연결/인증을 완료하지 않는다.

CI 실제 판정은 GitHub Actions 동일 소스 revision 실행 결과를 확인한다.
