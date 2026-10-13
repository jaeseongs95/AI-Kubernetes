# AIK-008 — upstream 결함 목록과 경로별 연결

## 범위

이 문서는 개발 원장 **AIK-008 — AGS·VM 설치형 적합성 진단과 upstream 결함 목록**에서 기존 구현에 빠져 있던 부분만 설명한다. 기존 진단기(`src/aik_compat/diagnostics.py`, `tools/diagnose_upstream.py`, [진단 문서](upstream-diagnostic.ko.md))는 수정하지 않았다. 원장 상태(`todo`)·완료 기준·의존성(AIK-001, 미완료 AIK-083)은 변경하지 않으며, 이 구현으로 AIK-008을 완료 처리하지 않는다.

새 모듈 `src/aik_compat/upstream_defects.py`는 기존 `aik-upstream-diagnostic-v1` 결과를 입력으로 받아 `aik-upstream-defects-v1` 결함 목록을 만든다. 설치 프로그램 실행, provider 호출, credential 읽기, 네트워크 접근은 하지 않는다.

## 추가된 내용

| 영역 | 내용 |
| --- | --- |
| 식별값 (완료 기준 1) | `identity`는 **baseline 계약의 기대값**이며 설치물에서 관측한 값이 아니다. 이를 `identity.identity_source = "baseline_contract"`로 표시한다. `identity.ags`: revision·version label·archive SHA-256·tree digest·manifest 파일 수·manifest digest·`host-integration.json` 존재 여부(`PRESENT`/`ABSENT`/`UNKNOWN`). `identity.vm`: revision·distribution·version label·wheel SHA-256·SQLite schema revision·domain schema version·manifest digest. 진단 입력 baseline/matrix/integration/inspection SHA-256도 기록한다 |
| 설치물 대조 표시 | `identity.ags.installed_match`·`identity.vm.installed_match`: 진단 코드 `AGS_ARTIFACT_MISMATCH`/`VM_ARTIFACT_MISMATCH`가 있으면 `MISMATCH_REPORTED`, 없으면 `NOT_CONFIRMED`다. 설치형 conformance를 실행하지 않으므로 `MATCH`는 출력하지 않는다. `UPSTREAM_IDENTITY_MISMATCH`는 `findings[]`에 그대로 남는다 |
| 상태 (완료 기준 1) | `compatibility`(`INCOMPATIBLE`/`UNVERIFIED`)와 소문자 `compatibility_state`. `compatible`은 예약값이며 이 모듈은 출력하지 않는다 |
| 결함 목록 | `integration-contract.json`의 `unresolved_gates`마다 소유자(`AGS`/`VM`/`AIK`/`UNCLASSIFIED`), upstream 여부, 종류, 연결 태스크, 증거 참조, 필요한 변경, 후속 태스크를 기록한다. 진단 코드가 재현한 결함은 `OBSERVED`, 계약에만 선언된 결함은 `DECLARED`다. 계약에 없는데 관측된 결함은 `declared_in_contract=false`로 추가한다. `RESOLVED`는 없으며 모든 결함의 `resolution`은 `REQUIRES_INSTALLED_CONFORMANCE`다 |
| AIK-081 연결 (완료 기준 2) | `linkage.a2_plugin`: A2 namespace·plugin conformance 코드, AGS 판본(baseline 기대값), 관련 AGS 결함 |
| AIK-082 연결 (완료 기준 2) | `linkage.model_effort`: runtime(`openai_api_tool_loop`, `codex_app_server`, `anthropic_api_tool_loop`, `claude_code_cli`)별 경로 목록과 model/effort 관측. `route_states[]`에 경로마다 자신의 `state`·`codes`를 따로 둔다. 그룹의 `state`·`codes`는 `state_scope = "AGGREGATE_OF_ROUTES"`로 표시한 집계값이며, 개별 경로 판단에는 `route_states[]`를 사용한다. 실제 live probe가 없으므로 관측은 `NOT_RUN`이다. matrix 문서에 `probe_status=PASS`가 적혀 있어도 `REPORTED_REQUIRES_LIVE_EVIDENCE`로만 표시한다 |
| 경로 분리 (완료 기준 2) | `routes[]`와 `model_effort[].route_states[]`의 상태·코드는 그 경로 자신의 코드에서만 계산한다. 한 경로의 stale/미지원이 다른 경로 항목을 바꾸지 않는다. 모든 경로가 `qualified=false`, `dispatchable=false`다 |
| GUI 사유 (완료 기준 3) | `gui.reasons[]`: 고정 코드, 분류, `aik.upstream.<code>` 메시지 키, 연결 태스크. 원시 stdout/stderr·로컬 경로·provider 문구는 포함하지 않는다. `observed_at`은 timezone이 있는 ISO 8601일 때만, `matrix_revision`·AGS/VM revision은 제한된 label(또는 음이 아닌 정수)일 때만 출력하고 그 외에는 `null`로 둔다 |

## 위조·불일치 입력 처리 (fail-closed)

입력 진단이 `status≠BLOCKED`, `qualification≠NOT_QUALIFIED`, `ready_for_dispatch≠false`, 경로 `qualified≠false`, `provider_calls≠0`, 알 수 없는 코드, source와 맞지 않는 `installed_conformance`를 가지면 `DiagnosticError`로 거절한다. 기존 진단기가 만들 수 없는 PASS 주장이기 때문이다. baseline/matrix/integration bytes의 SHA-256이 진단에 기록된 값과 다르거나, 경로 ID·provider·auth·role·AGS/VM revision이 일치하지 않아도 거절한다.

**저장된 진단 파일(`--diagnostic`)은 재계산으로 결속한다.** CLI는 `--inspection`(지정하지 않으면 저장소의 역사적 설치 검사 기록)으로 `diagnostics.inspect`를 다시 실행하고, `observed_at`을 제외한 전체 내용이 저장된 진단과 같은지 비교한다(`verify_saved_diagnostic`). 코드·호환성·경로 상태를 지우거나 `inspection_sha256`을 바꾼 파일은 `DIAGNOSTIC_INPUT_MISMATCH`로 거절한다. 저장소의 역사 기록과 함께 쓸 때는 `source_kind=historical`만 허용하므로, 그 기록을 `live`로 주장하는 파일도 거절한다.

CLI 실패 응답은 기존처럼 `code=INVALID_DEFECT_INPUT`, exit 1을 유지하고, 원인을 고정 코드 `cause_code`로 함께 반환한다(예: `UNTRUSTED_DIAGNOSTIC`, `DIAGNOSTIC_INPUT_MISMATCH`, `INVALID_SUPPORT_MATRIX`, 읽기 실패는 `INPUT_UNREADABLE`, 형식 오류는 `INPUT_MALFORMED`, 예상하지 못한 입력 구조는 `UNEXPECTED_INPUT_STRUCTURE`). `gui.reasons[]`에 같은 코드와 메시지 키를, `gui.next_step`에 `FIX_DEFECT_INPUT`를 넣는다. 경로나 예외 문구는 포함하지 않는다.

### 남은 한계

- `--inspection`으로 다른 검사 기록을 지정하면 진단의 `source_kind`(`historical`/`live`)는 그 기록을 만든 쪽의 주장이며 오프라인에서 증명하지 않는다. `live`여도 `installed_conformance=REQUIRES_REVIEW`이고 PASS가 아니다.
- `observed_at`은 재계산 비교에서 제외하고 형식만 검사한다. 시각 값 자체의 진위는 확인하지 않는다.
- Python API `build_defect_report`는 주장·digest만 검사하고 재계산하지 않는다. 프로세스 안에서 `inspect`로 만든 진단이 아니면 먼저 `verify_saved_diagnostic`을 호출해야 한다.

## 사용

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
# 저장된 역사적 설치 검사 기록 → 결함 목록 (차단 상태는 exit 2)
python -m aik_compat.upstream_defects
# snapshot 검토용: JSON은 그대로, 종료 코드만 0
python -m aik_compat.upstream_defects --allow-blocked
# tools/diagnose_upstream.py --live 등으로 저장한 진단 결과를, 그 진단을 만든 검사 기록과 함께 소비
python -m aik_compat.upstream_defects --diagnostic "$env:TEMP\upstream-diagnosis.json" --inspection "$env:TEMP\installed-inspection.json" --output "$env:TEMP\upstream-defects.json"
python -m unittest discover -s tests -p test_upstream_defects.py -v
```

`--output`은 저장소 밖 경로만 허용한다. 실제 설치물 검사는 기존 `tools/diagnose_upstream.py --live`가 담당하며, 이 모듈은 그 결과를 소비만 한다. `--diagnostic`에 맞는 검사 기록을 주지 않으면 재계산 결과가 달라 거절된다.

## 검증 성격과 미검증 항목

- 단위시험은 저장소에 고정된 계약 JSON과 메모리 내 변조만 사용한다. Linux 실행은 개발 검증이며 Windows 지원 근거가 아니다.
- **NOT_RUN:** 실제 AGS/VM 설치형 conformance(`--live`), Windows 11 실기, A2 호환 AGS 배포물, 벤더별 model/effort live probe(AIK-082), governed qualification(AIK-086), 실제 `--live` 진단 파일과 그 검사 기록에 대한 재계산 대조.
- 역사적 검사 기록(`source_kind=historical`)으로 만든 결함 목록은 현재 설치 환경의 재검증이 아니다. 결함 목록의 `OBSERVED`는 해당 기록 시점의 관측을 뜻한다.
- 이 결과는 VM/AGS gate나 경로 qualification을 대신하지 않으며, 어떤 경우에도 `PASS`·`qualified`·`compatible`을 발행하지 않는다.
