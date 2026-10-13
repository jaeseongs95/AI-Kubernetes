# AIK-008 — AGS·VM 설치형 호환성 진단 및 upstream 차단 사유

## 범위와 판정

이 작업은 개발 원장 **AIK-008 — AGS·VM 설치형 적합성 진단과 upstream 결함 목록** 한 건의 *진단·후속 검증 기반*을 구현한다. 정본 `planning/backlog.sqlite3`에서 AIK-001과 **미완료 AIK-083**이 선행이다. 따라서 원장의 `todo` 상태·기존 완료 기준·의존성을 변경하거나 `done`으로 승격하지 않는다.

기존 AIK-001 설치형 검사기 `tools/inspect_upstream.py`가 확보한 원본 검사 JSON과 `contracts/upstream/baseline.json`, `support-matrix.json`, `integration-contract.json`을 독립 입력으로 받는 **버전형, GUI 소비 가능, 비밀 없는 집계기**를 추가했다. 동일한 CLI로 새 설치형 검사도 명시적으로 실행할 수 있지만, 이 코드·단위시험과 저장된 역사적 검사 결과만으로 실제 Windows 운영 적합성·AGS A2 지원·벤더 model/effort 관측이 PASS가 된 것은 아니다.

## 사용

저장소 루트에서 Python 3.14 표준 라이브러리만 사용한다.

```bash
# 기존 2026-10-12 실제 실행 기록을 역사적 근거로 진단한다.
# 차단 상태는 정상적으로 exit 2를 반환한다.
python tools/diagnose_upstream.py

# CI/snapshot 검토: 차단 JSON 내용은 보존하면서 셸 종료코드만 0으로 한다.
python tools/diagnose_upstream.py --allow-blocked

# 실제 설치물로 새 non-invasive 검사 실행 (경로는 명시적으로 설정).
python tools/diagnose_upstream.py --live \
  --ags-root "/actual/installed-ags" \
  --vm-python "/actual/installed-vm-python" \
  --node "/actual/node" \
  --output "/outside-repository/upstream-diagnosis.json"

python -m unittest discover -s tests -p test_upstream_diagnostic.py -v
```

`--live`는 내부에서 기존 upstream 검사기의 AGS 파일 SHA·MCP `initialize/tools/list/fm/hello`와 설치된 VM 패키지, schema, 임시 프로젝트/읽기·plugin conformance를 *새 외부 임시 작업 공간*에 검사한다. 실제 제공자 호출·실사용 credential/VM 업무 원장 변경은 하지 않는다. 출력 파일은 저장소 및 개발 원장 내부에 저장할 수 없다. raw 설치 검사의 stdout·stderr·로컬 경로는 GUI 진단 결과로 되풀이하지 않는다.

## 반환 계약

`aik-upstream-diagnostic-v1`에는 `source_kind`(`historical`/`live`), 관측 시각, baseline/inspection/matrix/integration SHA-256, 고정 AGS/VM commit, 지원 matrix revision, `compatibility`(`INCOMPATIBLE`/`UNVERIFIED`), `status=BLOCKED`, `qualification=NOT_QUALIFIED`, 고정 코드 `codes`, `upstream_blockers`, `gui`, 12개 route별 `codes`가 들어간다.

집계기는 baseline과 **실제로 관측된 파일 목록의 path/SHA-256 전체**를 대조한다. VM wheel/version/schema 및 provenance도 독립 확인한다. JSON 상의 `PASS` 문자열만으로 A2를 통과시키지 않고 `fm/hello`에 유효한 서버 epoch가 있는지 별도로 확인하며 VM `check-plugin` 명령의 실제 종료코드를 요구한다. support matrix 내의 **각 route**가 해당 baseline·AGS/VM commit·revision을 계속 가리키는지도 검사한다.

현재 고정 설치본의 관찰 결과는 AGS byte·VM wheel은 검사 통과했으나 **`fm/hello` namespace 미지원과 VM plugin conformance 실패**로 upstream 적합성이 `INCOMPATIBLE`이다. 이 문제는 `AIK-081`에 연결하고, 모델/추론 수준의 실제 runtime 관측은 `AIK-082`로, 영속 route/Attempt 결속은 `AIK-031` 등으로 따로 기록한다. 실제 A2 plugin 출시·실행 확인 없이 호환 `PASS`를 발행하지 않는다.

## 개별 경로와 출시 조건

첫 Windows 후보 지원 matrix의 OpenAI/Anthropic, API/공식 구독, director/developer/reviewer 12개 조합은 각자 **인증 관측, adapter 구현, model probe, governed qualification** 상태를 가진다. 미실행/미지원된 특정 경로를 다른 경로에 전파해 이미 성공한 결과를 덮지 않으며, 동시에 AGS/VM 공통 gate가 불일치하는 동안 어떤 route도 qualified로 광고하지 않는다. `source_kind=historical`은 녹색 테스트가 나와도 실제 사용 환경의 재검증을 뜻하지 않는다.

여기서 표시하는 `upstream_blockers`는 기존 `integration-contract.json`의 알려진 문제 ID와 맞추며 사용자/GUI에 넣는 코드는 고정된 영문 값이다. 명령 예외·원시 제공자 응답·환경 변수·credential을 오류 문자열에 합치지 않는다.

## 검증 성격

Windows Server 2022와 Ubuntu 24.04에서 역사적 실험 자료에 대한 **14개** 독립/변조 반례와 CLI 검사를 CI로 실행한다. AGS 설치형 runtime, Windows 11 일반 사용자 실기, 실제 A2 호환 plugin, vendor 모델·effort live probe, AGS/VM 운영 경로는 CI에서 실행하지 않는다. 이 구현은 **진단기가 역사적 불일치와 누락을 안전하게 차단하는 기능**만 검증한다. 후속 실설치 conformance, Windows 실기, 독립 확인은 AIK-008 완료 전에 여전히 필요하다.
