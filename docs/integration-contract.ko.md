# AGS·VM 설치형 연동 계약 v1

**대상 태스크: AIK-001.** 2026-10-12에 현재 upstream과 설치 가능한 배포 표면을 직접 대조했다. AGS MCP 조회와 VM 설치형 공개 명령은 실행됐다. **현재 두 배포물의 governed 연동은 차단된다.** 이 차단을 모델·인증·도구 실행의 성공으로 바꾸지 않는다.

## 1. 고정한 소스와 실제 설치물

| 항목 | 고정값 | 확인 방법 |
|---|---|---|
| AGS 기준선·관측한 main | `56fef8bd189377b80f4020f506e717ab292b260a` | 원격 branch와 전체 Git tree 조회 |
| AGS 버전 label | `2.9.0` | Codex/Claude plugin manifest와 package metadata |
| AGS 설치 형태 | 공식 source archive의 설치 트리 | **1,473개 파일, 11,650,691 bytes**를 원격 Git blob과 전부 대조 |
| VM 기준선·관측한 main | `32bb0f9dd9f024045d24487312b50f5b703573a3` | 원격 branch와 Git tree 조회 |
| VM 배포판 | `flowmarshal-engine 0.2.0a1` | 고정 소스에서 wheel 생성, non-editable 설치 후 metadata와 import 위치 확인 |
| VM 설치 코드 | root/engine Python **86개 파일** | 설치된 파일을 고정 소스 bytes와 대조 |
| VM wheel SHA-256 | `fe7cc253f2a401fd9dc618ee2b3d1af0f791129246ec47d82c88a7ccfd60389e` | 실제 설치한 wheel과 `direct_url.json`의 archive identity 대조 |

설치 검사 환경은 Linux, Python 3.12.14, Node 24.19.0이다. 별도 venv에서 VM과 Codex Python 패키지를 설치했고, 기존 환경의 정확히 일치하는 Pydantic 2.13.5·cryptography 50.0.1을 공유했다. Codex Python 패키지와 CLI binary 배포판은 모두 0.147.0이다. **깨끗한 Windows 설치나 실제 provider qualification의 증거로 사용하지 않는다.**

경로별 Git blob·SHA-256, archive tree digest와 wheel identity는 [baseline.json](../contracts/upstream/baseline.json)에 있다. 원시 명령·표준 출력·오류·MCP 응답은 [installed-inspection.json](../contracts/upstream/installed-inspection.json)에 보존한다. 이 기준선은 검사 근거의 판본을 고정한다. VM의 AGS 채택 동작에 한 commit을 하드코딩하라는 뜻은 아니다.

## 2. 실제 실행 결과

| 설치형 검사 | 실제 결과 |
|---|---|
| AGS `node mcp-server/dist/server.mjs` → MCP `initialize` | **PASS** |
| AGS MCP `tools/list` | **PASS**, 28개 도구 |
| VM 설치된 공개 console `--help` | **PASS**, exit 0 |
| VM 공개 `project init` | **PASS**, 격리 임시 프로젝트·Profile 생성 |
| VM 공개 `status --project-id …` | **PASS**, Core snapshot·history_valid·단계·다음 동작 반환 |
| 생성한 VM SQLite identity | **PASS**, user_version 4, application_id `1179469105`, integrity `ok` |
| VM이 요구하는 A2 RPC `fm/hello` | **BLOCKED**, JSON-RPC `-32601 Method not found` |
| VM 공개 `governance check-plugin --plugin-root …` | **BLOCKED**, exit 2, `GOVERNANCE_CONTRACT_MISMATCH` |

호환 검사에서 실제 관측한 첫 원인은 **AGS 설치 트리의 `host-integration.json` 부재**다. 전체 원격 tree와 archive에서도 해당 파일이 없다. 일반 Codex/Claude plugin manifest가 없는 것이 아니다. 두 종류의 manifest를 구분한다.

생성된 임시 원장은 Project 1개, Task contract 0개, Attempt 0개, provider call 0개였다. 실제 모델 호출·인증·native 도구 효과·Goal 완주는 이 검사의 범위가 아니며 모두 `NOT_RUN`이다. 사용자의 계정·시크릿·기존 운영 원장은 검사하지 않았다.

## 3. 권위와 참조

| 단일 소유자 | 소유 객체 | AIK의 연결 방식 |
|---|---|---|
| **VM Core** | Project, Goal/Plan revision, Task, Attempt, RuntimeJob, intent/receipt, evidence, verdict, 실행 사용량 | 공개 façade와 owner query로 요청·조회하고 원래 ID를 참조 |
| **AGS** | convergence root, workflow run/stage, host attestation, PEER 전달·ACK, 감사 gate | 공개 MCP·선언된 설치형 CLI/host surface를 소비 |
| **AIK** | connection, model route, pool, placement, resource lease, 입력·산출물 전송 메타데이터 | VM의 정확한 실행 참조와 AIK 배정 참조를 결속 |

AGS의 workflow lease와 AIK의 자원 lease는 별개다. AIK의 lease 만료, provider terminal, Worker의 완료 문장으로 VM Task를 완료하지 않는다. 새 AIK Task·재시도 권위 원장을 만들거나 VM SQLite를 직접 수정하지 않는다.

[integration-contract.json](../contracts/upstream/integration-contract.json)은 객체 소유자, 입력·효과의 생산자와 소비자, 차단 과제와 판본 변경 규칙을 기계적으로 읽을 수 있게 기록한다. [task-execution-binding.schema.json](../contracts/upstream/task-execution-binding.schema.json)은 **실행 Task에 사용할 제안 결속 형식**이며 권한 토큰이나 구현 완료된 dispatch API가 아니다.

효과 직전 결속에는 VM instance/project/Goal revision/Plan revision/Task/Attempt/Execution Spec digest/intent/provider call과 AIK cluster/route revision/connection revision/account fingerprint/pool/placement/lease fencing token/matrix digest/input manifest/workspace root 참조가 필요하다. 보안키나 로그인 원문은 여기에 넣지 않는다.

VM의 준비 역할은 Task·Goal 등록 전에도 호출될 수 있다. 이 단계는 기존 project·Goal 계보·role call/request·receipt 결속을 보존한다. Task 단계의 ID 요구를 맞추기 위해 가짜 Task·Attempt를 만들지 않는다.

## 4. 현재 공개 표면과 부족한 부분

### AGS 설치 표면

Codex manifest는 `.codex-plugin/plugin.json`, MCP 설정은 `.mcp.json`이며 실제 실행은 `node mcp-server/dist/server.mjs`다. Claude manifest는 `claude-plugin/.claude-plugin/plugin.json`이고 `${CLAUDE_PLUGIN_ROOT}/mcp-server/dist/server.mjs`를 실행한다. host wrapper용 `mcp-server/dist/host-attestation-api.mjs`도 설치물에 있다.

VM은 별도의 `host-integration.json`에서 `mcp-server`, `scope-baseline`, `scope-compare`, `acceptance-cli` entrypoint와 각 `executionClosure`를 찾는다. MCP의 `plan_workflow`, `open_convergence_root`, `claim_workflow_attempt`, `start_guarded_workflow`, `record_stage_result`, `finalize_workflow`, `abort_workflow` 및 반환 필드를 소비한다. 경로·내용 digest·Node·도구·응답·모델 class 표를 실제 설치물에서 확인하는 것이 VM의 계약이다. **누락된 manifest를 AIK가 임의 작성해서 현재 배포물이 호환된다고 처리하지 않는다.** [AGS manifest](https://github.com/jaeseongs95/agent-governance-suite/blob/56fef8bd189377b80f4020f506e717ab292b260a/.codex-plugin/plugin.json) · [VM 소비 계약](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/src/flowmarshal/engine/governance_gate.py)

### VM façade·runtime seam

공개 console entrypoint는 `flowmarshal-engine = flowmarshal.engine.console_host:main`이다. 설치된 `EngineApplication`은 `prepare`, `revise`, `authorize`, `run_once`, `observe`, `pause`, `cancel`, `status`, `replan`, `final_report`, `attempt_detail`, `usage_summary`, `recovery_status`, `model_binding_status`를 제공한다. 정확한 signature와 소스 위치는 [vm-public-surface.json](../contracts/upstream/vm-public-surface.json)과 설치 introspection 결과에 있다.

`EngineApplication(runtime=…)` 주입과 `CodexRuntimePort`는 실제 존재한다. VM은 create/start/resume 직전 durable intent를 만들고 authorization·입력 freshness를 다시 확인한 뒤 `OperationTraceScope`에 `attempt_id`, `intent_id`, `call_id`, thread/turn과 deadline을 둔다. **기존 접점을 재사용할 수 있는 근거다.**

다만 현재 port의 `read`, `read_stored`, `interrupt` 인자는 thread/turn 위주이며 AIK route/connection/account/lease를 직접 운반하지 않는다. trace에는 Task ID와 AIK 배정 결속 자체가 없다. AIK-031은 공개 port·trace·정확한 owner Attempt query를 결합한 adapter 계약을 구현하고, 효과 전 binding과 재시작·취소·계정 전환·서로 다른 계정의 같은 thread ID를 검증해야 한다. 그 접점으로 보장할 수 없는 경로는 upstream의 명시 extension·배포물 보완을 받아야 한다. private helper override나 기본 계정으로의 우회는 채택하지 않는다. [VM runtime](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/src/flowmarshal/engine/runtime.py) · [OperationTraceScope](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/src/flowmarshal/engine/operation_trace.py)

### 세 가지 schema와 GUI 재사용

| 구분 | 실제 값·상태 | 적용 |
|---|---|---|
| VM SQLite schema revision | `ENGINE_SCHEMA_REVISION = 4` | 저장 원장·migration/history 구분 |
| VM 도메인 schema label | `ENGINE_SCHEMA_VERSION = "1.0"` | typed domain 문서의 schema_version |
| 기존 VM GUI DTO | `engineSchemaVersion: 2`, `apiVersion: "1"` | MockEngineClient 프로토타입 계약 |

VM GUI 자산은 `apps/desktop/src/{App.tsx,screens.tsx,components.tsx,contracts.ts,platform.ts,styles.css}`에 있다. React/TypeScript의 표현 방식과 권위 상태를 외부 client에서 받는 구조는 참고할 수 있다. 현재 `App.tsx`의 기본 client는 `MockEngineClient`이며 실제 Engine/SQLite 연결이 아니다. 현재 tree에는 실행 가능한 Tauri shell/권한 설정이 없다. `PlatformCapabilities.shell`의 `tauri` 값과 GUI 설계 문서를 구현 완료로 해석하지 않는다.

기존 `CommandContext`는 `actorRef/clientInstanceId/requestId/idempotencyKey`이며 `ExpectedState`는 historySequence와 Goal/Plan digest다. Python façade는 이 전체 GUI envelope를 인자로 받지 않는다. AIK IPC adapter가 명령 인가·중복 요청·판본 결속을 맡고 실제 VM owner query를 연결해야 한다. schema2 DTO와 수동 `activatePlan` 흐름을 현재 schema4 승인 계약에 그대로 복사하지 않는다. [GUI 계약](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/apps/desktop/src/contracts.ts) · [현재 façade](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/src/flowmarshal/engine/application.py)

## 5. 첫 출시 지원 matrix

DEC12의 **제안** 범위인 OpenAI/Anthropic × API key/지원 native 구독 × 총괄/개발/검토 **12개 필수 조합**을 [support-matrix.json](../contracts/upstream/support-matrix.json)에 고정했다. 사용자 확정 요구와 구현 대상 제안의 상태를 구분한다. 현재 adapter는 모두 `not_implemented`, 인증은 `NOT_CHECKED`, 실제 probe·qualification은 `NOT_RUN`이며 운영 가능한 자원으로 광고하지 않는다.

| 역할 | 최소 파일·도구 능력 | 구분 |
|---|---|---|
| 총괄 | read, VM 준비·실행 요청·상태 회수·중단 | 실제 구현은 worker에 배정하고 Core 권위를 유지 |
| 개발 | **read/create/update/bounded_verify 모두 필수** | 실제 파일 생성·수정과 제한한 검증 명령의 효과·receipt 필요 |
| 검토 | read; bounded_verify는 해당 검사 역할에 추가 선택 | 분석 전용 자원에 개발 shell 전체 능력을 강제하지 않음 |

API 자원에는 AIK-035/085의 실제 도구 gateway가 필요하다. matrix의 `workspace.read_file/create_file/update_file/run_verification`은 구현해야 할 도구 계약명이다. 현재 작동하는 기능이라고 표시하지 않는다. native Codex의 현 VM 계약은 turn별 full-access/never, Claude는 provider가 관측한 bypassPermissions를 요구한다. 모델에 주는 논리적 scope와 실제 OS 권한은 다르며, read 전용 역할을 native OS sandbox라고 설명하지 않는다. 정확한 정책·role/tool 조합은 082와 086에서 실제 확인한다.

검증 순서는 **022 인증 → 082 최소 live 사전 probe → 086 설치형 governed runtime/tool qualification → 044 의존 개발 목표·독립 실패/재작업 → 059 동일 Windows 설치본 GUI → 064 출시 판정**이다. 이전 단계 성공으로 뒤 단계의 성공을 대신하지 않는다.

## 6. 효과·오류·복구 소비 계약

VM이 provider create/start/resume의 `RuntimeIntentRecord`를 먼저 쓰고 실제 runtime receipt를 소비한다. API 도구의 파일·명령 효과는 그 안에서 추가로 발생하므로 AIK-035/085가 tool call identity·정확한 Attempt·route binding·target·input/output digest·멱등 정책을 결속한 효과 전후 기록을 제공해야 한다. 이 결과는 명시한 VM adapter와 독립 validation이 소비한다. 모델 응답을 실제 파일 변경 증거로 쓰지 않는다. 외부 효과는 VM의 typed effect receipt와 대상 재관측 계약을 유지한다.

| 오류·상태 | 관측/계약 | AIK 처리 |
|---|---|---|
| `GOVERNANCE_CONTRACT_MISMATCH` | 이번 공개 설치 검사에서 실제 관측 | 해당 설치 identity를 차단하고 기대/관측·원문을 보존 |
| A2 `fm/hello`의 `-32601` | 이번 설치에서 실제 관측 | A2 namespace 미지원으로 표시; token·RPC를 흉내 내지 않음 |
| `BINDING_REQUIRED` / `BINDING_INVALID` | AGS 공개 host 관측 계약 | 관측 없음·잘못된 결속을 분리; 모델 JSON을 서명 관측으로 승격하지 않음 |
| `CORE_CAPABILITY_REQUIRED` / `CORE_CAPABILITY_DENIED` | VM 공개 capability 경계 | 사용자 인가를 host authority에서 전달; 역할에 발급자를 넘기지 않음 |
| `STALE_EXECUTION_INPUT` | VM 효과 직전 freshness 계약 | 기존 요청을 실행하지 않고 owner의 revision/recovery로 연결 |
| `MODEL_LOCK_*` / `MODEL_BINDING_CHANGED` | VM 모델 잠금 계약 | 임의 다른 모델·계정으로 fallback하지 않음 |
| `GOVERNANCE_PROVIDER_UNSUPPORTED` | 현 VM provider 관측 계약 | 해당 경로의 부족한 관측을 표시하고 qualification 차단 |
| `RUNTIME_OWNER_LOCK_UNAVAILABLE` | 현 VM runtime은 Windows owner lock만 지원 | Linux metadata 성공을 runtime 성공으로 확대하지 않음 |
| 효과·provider 상태 `unknown` | VM receipt·재관측 계약 | 원래 계정과 binding으로 먼저 관측; 자동 재실행 금지 |

원래 binding에는 계정·connection 판본이 포함된다. 새로 선택한 모델이나 기본 connection으로 기존 job을 조회·취소하지 않는다. receipt를 잃었거나 provider가 중단을 확인하지 않은 상태에서 lease가 만료됐다는 이유로 새 효과를 시작하지 않는다. [AGS host 관측·오류](https://github.com/jaeseongs95/agent-governance-suite/blob/56fef8bd189377b80f4020f506e717ab292b260a/docs/host-execution-attestation.md)

## 7. 다음 구현을 막는 조건

1. **AIK-008/081/032:** VM 소비 manifest와 A2 namespace·verifier·profile/strict dispatch가 일치하는 실제 AGS 배포물이 필요하다. 새 artifact의 공개 적합성 검사를 통과해야 한다.
2. **AIK-031/033/035:** 기존 VM Task/Attempt에 route/connection/pool/lease와 입력·도구 효과를 결속하는 adapter 및 재시작 routing을 구현해야 한다.
3. **AIK-082/086:** 실제 계정·모델·역할·도구를 사용한 Windows 실행이 필요하다. 현재 metadata·명령 smoke는 이를 충족하지 않는다.

upstream이나 지원 범위가 바뀌면 matrix·기준선 revision, DEC12 상태와 변경 사유, 영향받는 조합, 폐기/재검증하는 evidence를 함께 갱신한다. 새 wheel을 빌드하면 archive digest가 달라질 수 있다. 이전 wheel의 검증을 새 파일에 그대로 붙이지 않는다.

## 8. 재현

검사할 AGS 설치 트리와 **위 wheel이 non-editable 설치된 Python**을 지정한다. 검증에 사용한 정확한 [VM wheel과 라이선스](../evidence/upstream/2026-10-12/artifacts/)를 함께 보존했다. 같은 소스를 다시 빌드한 wheel은 ZIP timestamp와 build backend에 따라 archive digest가 바뀔 수 있으므로, 고정 기준선의 재현에는 이 원본을 사용한다. 이 wheel은 설치형 계약 검사의 입력이며 Windows GUI 제품에 연결한 runtime이 아니다. 도구는 새 임시 상태만 만들고 provider 인증·사용자 원장 설정을 자식 환경에 전달하지 않는다. 현재 기준선의 예상 종료값은 호환 차단을 뜻하는 **3**이다.

```bash
python tools/inspect_upstream.py \
  --ags-root /absolute/installed/agent-governance-suite \
  --vm-python /absolute/vm-venv/bin/python \
  --output /absolute/results/upstream-inspection.json

python -m unittest discover -s tests -p test_upstream_contract.py -v
```

검사기의 별도 7개 테스트는 변조·누락 bytes, 경로 탈출·symlink, 중복/빈 manifest·JSON, 사용자 인증 설정 격리와 역할 matrix를 확인한다. 이 테스트의 통과 건수와 위 실제 설치 명령 결과를 분리한다.
