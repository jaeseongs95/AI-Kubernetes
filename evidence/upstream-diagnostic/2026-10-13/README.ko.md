# AIK-008 — AGS/VM 설치형 진단 구현 근거

- 선택 태스크: **AIK-008 단일 태스크**
- 기준 main commit: `594629961272dfe80d739739212d687f54868134`
- 정본 원장: `planning/backlog.sqlite3` (상태·의존성·AC 변경 없음)
- 기존 기준 근거: `contracts/upstream/installed-inspection.json` / `baseline.json` / `support-matrix.json` / `integration-contract.json`
- 신규 모듈: `src/aik_compat/diagnostics.py` (바이트·버전·host namespace·plugin 게이트·모델 관측·route별 이유)
- CLI: `tools/diagnose_upstream.py` (역사적 snapshot 기본, 명시적 `--live` 설치형 조사)
- 회귀: `tests/test_upstream_diagnostic.py`, 14개 실제 고정 baseline·변조·반례 검증
- CI: `.github/workflows/upstream-compatibility.yml` (Windows Server 2022/Ubuntu 24.04)
- 역사적 검사상 미해결: `AGS_A2_NAMESPACE_UNAVAILABLE`, `AGS_HOST_INTEGRATION_MANIFEST_MISSING`, `AIK_DURABLE_ROUTE_BINDING_NOT_IMPLEMENTED`
- 실제 제품 runtime/model/effort qualification: **NOT_RUN**
- 상태: AIK-083이 아직 미완료이므로 AIK-008은 원장상 `todo` 유지. 증거는 구현 부품/독립 단위시험 수준.

CI에서 성공한 여부는 변경 원본 커밋에 결속한 GitHub Actions 실행으로 판단한다. 역사적 환경의 실행 근거를 신규 실행으로 가장하지 않는다.
