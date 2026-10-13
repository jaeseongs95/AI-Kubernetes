# AI-Kubernetes 프로젝트 분석 보고 (모델 생성)

> **성격:** Claude API가 생성한 분석이다. 입력 문서만 근거로 했으며 저장소 소스·장부와 대조해 독립 검증하지 않았다. 아래 수치와 판단은 작성 시점의 문서 내용에 대한 모델 해석이므로 결정 근거로 쓰기 전에 원문을 확인한다. "자료에 없음"은 입력에 포함하지 않은 범위를 뜻하며 실제로 없다는 뜻이 아니다.

## 생성 조건

| 항목 | 값 |
| --- | --- |
| 생성 시각 (UTC) | 2026-10-13 06:53:26 ~ 06:54:43 |
| 모델 / 설정 | `claude-opus-5-5`, effort `high`, 스트리밍, `max_tokens` 32000 |
| 응답 | HTTP 200, `msg_011Cfyo7p8HoSRivhhAoJQXP`, `stop_reason=end_turn` |
| 입력 | 아래 5개 문서와 `git ls-files` 파일 구조(src·tools·tests·contracts). 입력 정본 SHA-256 `8482bfca7b3e60a659bfa4e14149cd9c38f03a42785e56f7b5df848711850b3b` |
| 입력 문서 | `README.md`, `docs/initial-design.ko.md`, `docs/integration-contract.ko.md`, `docs/provider-adapter.ko.md`, `docs/runtime-storage.ko.md` |
| 입력 기준 commit | `1fc3947` (`origin/main`, PR #9 병합) |
| 제외 | 소스 코드, `evidence/`, `docs/development-backlog.ko.md` 등 그 밖의 문서 |
| usage | 일반 입력 4, 캐시 쓰기(5분) 35,947, 캐시 읽기 0, 출력 8,127 (thinking 2,906 포함) |

요청에 `cache_control`을 지정하지 않았으나 Gateway가 5분 캐시 쓰기를 자동 적용한 것으로 관측됐다. 비용은 Opus 5.5 단가로 계산한 약 $0.342이며 계정 청구액과 대조하지 않았다.

## 1) 프로젝트 목적과 아키텍처 요약

### 목적
- 이질적인 AI 실행 자원을 하나의 **논리적 워커 풀**로 관리하는 **벤더 독립 상위 제어 계층**을 만든다. 대상 자원은 구독 호스트, API 키, 로컬 모델, 원격 클러스터다.
- 지향하는 흐름은 README의 "작업 계약 → 자원 탐색 → 배치 → 실행 → 결과 회수 → 검증 → 자원 반환"이다.
- 독립 클러스터 간 **PEER 연합**도 핵심 목표로 둔다.
- 비목표는 LLM 라우터나 subagent 프레임워크, 모델 학습, 약관·쿼터 우회, Kubernetes API 호환이다.
- 첫 제품 목표는 다음 세 가지다.
  - 멀티벤더 인증 공간
  - 총괄 에이전트 모델 지정
  - Windows GUI 운영

### 아키텍처 핵심

| 계층 | 소유 (integration-contract §3) |
|---|---|
| **VM Core** (VibeMarshal) | Project, Goal/Plan, Task, Attempt, intent/receipt, evidence, verdict |
| **AGS** | convergence root, workflow run, host attestation, PEER 전달·ACK, 감사 gate |
| **AIK** (본 프로젝트) | connection, model route, pool, placement, resource lease, 전송 메타데이터 |

- **2단계 스케줄링:** Global은 "어느 클러스터", Local은 "그 클러스터의 어떤 워커"를 정한다.
- **중복 원장 금지:** AIK는 VM의 Task/Attempt를 복제하지 않고 외부 ID로 참조만 한다.
- **lease 기반 실행:** 상태는 `DISCOVERED → … → RELEASED`이며 `UNKNOWN`일 때는 자동 재발사를 금지한다.
- **기술 스택 (DEC07: confirmed):** Electron 44.7.0, React 19.2.8, TypeScript 7.0.2, Python 3.14.8 sidecar, NSIS 사용자별 설치본.

---

## 2) 현재 구현 상태와 진척

### 장부 상태 (README 기준)
- 90개 태스크, 311개 완료 기준, 365개 의존성
- **done 2** (AIK-001·002), **blocked 6** (003·004·005·011·017·083), **todo 82**, **ready 0**
- 004·017의 차단 사유: 자료에 없음

### 구현·검증된 항목

| 영역 | 산출물 | 확인된 결과 |
|---|---|---|
| upstream 계약 (AIK-001) | `tools/inspect_upstream.py`, `contracts/upstream/*` | AGS MCP initialize·tools/list(28개) PASS. VM CLI·`project init`·`status` PASS. **`fm/hello` -32601, `check-plugin` GOVERNANCE_CONTRACT_MISMATCH로 BLOCKED**. 예상 종료값은 3 |
| Windows 기반 (AIK-002) | Electron 설치 GUI, Tauri 비교, PS 5.1 전달 | CI 실행 3건 PASS (38219549819 / 38223818838 / 38226204128). Windows Server 2022 기준 |
| runtime 저장소 (AIK-005) | `src/aik_runtime/` | SQLite schema v2(`AIKR`), revision 충돌, 원자 transaction, 강제 종료 복구, migration 검증. **원장 상태는 blocked** |
| provider 계약 (AIK-011) | `src/aik_provider/`, `contracts/provider-adapter/v1.schema.json` | fake adapter 회귀 시험 (Windows/Ubuntu CI). 실제 provider 호출 없음 |
| 지원 matrix | `support-matrix.json` | 12개 조합 모두 `not_implemented` / `NOT_CHECKED` / `NOT_RUN` |

### 설명 자료가 없는 항목
- `src/aik_security/redaction.py`, `src/aik_compat/diagnostics.py`, `src/aik_ipc/*.cjs`의 기능 상세: 자료에 없음
- `contracts/product-acceptance/`(orders.csv, settlement.py 등), `tools/product_acceptance.py`, `tools/review_windows_client.py`의 목적: 자료에 없음
  - README의 P01~P12 인수 요구와 관련되는지도 명시돼 있지 않다.
- 제공된 파일 목록(src·tools·tests·contracts 범위)에는 React/TypeScript GUI 제품 소스가 보이지 않는다. 다른 디렉터리에 있는지는 자료에 없음.

### 미완료 (README 명시)
- 멀티벤더 인증 공간, 총괄 모델 지정, Windows GUI 완제품, 첫 출시
- 깨끗한 Windows 11 검증 (`NOT_RUN`)

---

## 3) 설계상 강점

1. **권위 경계가 명확하다.** VM, AGS, AIK의 소유 객체를 표로 고정했다. AGS workflow lease와 AIK 자원 lease도 구분한다.
2. **상태를 분리해 실패를 숨기지 않는다.**
   - ACK, 접수, 실행 성공, 검증 통과, 최종 완료를 구분한다.
   - `authenticated` ≠ `job_start supported` ≠ `qualified`로 둔다.
   - interrupt 수락을 종료로 간주하지 않는다.
3. **멱등성과 불명확성을 보수적으로 처리한다.**
   - `UNKNOWN_OUTCOME`이면 신규 dispatch를 차단한다.
   - 원래 binding으로만 observe/interrupt하고, 기본 계정으로 fallback하지 않는다.
4. **fail-closed 기본값을 쓴다.**
   - `authorize_dispatch` 콜백이 없으면 실행을 거절한다.
   - `account_isolation=unknown`이면 시작을 거절한다.
   - migration 실패를 빈 DB로 덮지 않는다.
5. **비밀 경계를 지킨다.**
   - `secret://`와 `host-session://`를 강제하고 서로 변환하지 않는다.
   - 저장소와 스키마에 비밀, RAW 프롬프트, verdict 필드가 없다.
6. **증거 정직성이 높다.**
   - 각 문서가 "미검증" 범위를 명시한다.
   - Linux 성공을 Windows 성공으로, 회귀 PASS를 live 증거로 확대하지 않는다.
   - 누락된 AGS manifest를 임의로 작성하지 않는다.
7. **추적 가능성을 확보했다.** SQLite 장부, SQL 스냅샷, 기준선 digest, wheel 원본 보존으로 재현성을 확보했다.

---

## 4) 위험·모순·미결 사항

### A. 진척 차단 (최우선 위험)

| # | 내용 | 근거 |
|---|---|---|
| A1 | **장부 전체가 정체 상태다.** 차단 사슬은 `AIK-003(깨끗한 Win11 NOT_RUN) → AIK-083 → AIK-005`이다. | "ready 0개", "AIK-083은 AIK-003의 실제 환경 검증을 기다린다" |
| A2 | **외부 upstream 의존이 막혀 있다.** AGS에 `host-integration.json`이 없고 A2 RPC가 미지원이어서 governed 연동이 불가하다. 해소 주체는 upstream이다. | "현재 두 배포물의 governed 연동은 차단된다", "`fm/hello` … -32601" |
| A3 | **VM runtime port의 결속 정보가 부족하다.** route/connection/account/lease를 운반하지 않고, trace에 Task ID가 없다. upstream extension이 필요할 수 있다. | "현재 port의 `read`, `read_stored`, `interrupt` 인자는 thread/turn 위주" |

### B. 프로세스·문서 모순

| # | 내용 | 근거 |
|---|---|---|
| B1 | **의존성 순서를 벗어난 구현이다.** AIK-005와 011을 선행 미완료 상태에서 "단일 태스크 지시"로 구현했다. 선행 결과에 따라 재작업될 위험이 있다. | "선행 AIK-083 미완료", "AIK-010은 미완료" |
| B2 | **설계문서가 최신 계획을 따라가지 못한다.** 초기 설계는 "개발 계획 v0.3"을 참조하지만 README는 v0.4다. | initial-design 후속 주석 vs README "계획 v0.4" |
| B3 | **결정된 사항이 '열린 결정'으로 남아 있다.** §14의 7번(구현 언어·패키징)과 8번(라이선스)은 이미 DEC07과 MIT License로 정해졌다. | initial-design §14 vs README DEC07·라이선스 절 |
| B4 | **README가 비대칭적이다.** AIK-005 구현은 별도 절로 설명하지만, 마찬가지로 구현된 AIK-011(provider 계약)은 blocked 목록에만 있다. | README 본문 |
| B5 | **연합의 위상이 모호하다.** 연합을 "핵심 제품 목표로 유지"한다면서 M5로 미뤘고, 이 순서는 `DEC06: proposed`(미확정)다. 초기 설계의 M2 연합안과도 다르다. | README 개발 순서, initial-design §13 |

### C. 기술적 위험

| # | 내용 | 근거 |
|---|---|---|
| C1 | **최소 권한 원칙과 native 권한 요구가 충돌한다.** Codex는 full-access/never, Claude는 bypassPermissions를 요구해 OS 수준 격리가 없다. | "read 전용 역할을 native OS sandbox라고 설명하지 않는다" vs §9 "최소 권한" |
| C2 | **계정 충돌 방지가 메모리에만 있다.** 재시작하거나 외부에서 같은 호스트에 로그인하면 통제할 수 없다. 영속 reconciliation은 AIK-031/033/035/038의 몫이다. | "현재 Python 프로세스 메모리에만 존재한다" |
| C3 | **저장소 확장성이 불명확하다.** transaction마다 DB 전체 사본을 만들고 무결성 검사를 하므로 비용이 DB 크기에 비례한다. | "대규모 운영 DB의 처리량이나 지연 시간을 보장하지 않는다" |
| C4 | **내구성 한계가 있다.** Windows rename의 전원 장애 내구성은 미보장이다. 잠금은 협력 프로세스에만 유효하다. | runtime-storage "파일 위치와 동시성", "스키마와 복구" 절 |
| C5 | **검증 환경과 목표 환경이 다르다.** upstream 검사는 Linux + Python 3.12.14, 목표는 Python 3.14.8이다. Windows 검증은 Server 2022 CI다. VM은 Windows owner lock만 지원한다. | integration-contract §1·§6, README |
| C6 | **GUI 계약 판본이 어긋난다.** VM GUI DTO는 `engineSchemaVersion: 2`, Core는 schema revision 4다. 기본 client는 `MockEngineClient`다. | integration-contract §4 |
| C7 | **upstream이 프리릴리스다.** VM 배포판은 `flowmarshal-engine 0.2.0a1`이다. 재빌드하면 digest가 바뀌어 재검증이 필요하다. | integration-contract §1·§7 |

### D. 자료로 판단 불가
- AIK-004·017의 차단 사유, AIK-010의 내용: 자료에 없음
- 깨끗한 Windows 11 환경을 확보할 일정이나 수단: 자료에 없음
- 연합 PEER 전송 규약과 coordinator 선출 방식: 미결로 명시됨 (initial-design §14)

---

## 5) 다음 단계 제안 (우선순위)

| 순위 | 제안 | 이유 |
|---|---|---|
| **1** | **깨끗한 Windows 11 표준 사용자 환경을 확보하고 AIK-003 검증을 실행한다.** 이미 준비된 `tools/windows_handoff/`를 활용한다. | 차단 사슬(003→083→005)의 근원이며, 현재 ready 태스크가 0개다 |
| **2** | **upstream 보완을 병행해 요청한다.** AGS `host-integration.json`과 A2 namespace, VM runtime port의 결속 확장이 대상이다 (AIK-008/081/032, 031). | 해소 주체가 외부라 리드타임이 길다. 일찍 착수해야 한다 |
| **3** | **의존성 외 구현의 관리 규칙을 정한다.** AIK-005와 011의 재검증 조건을 장부에 명시하고, AIK-010 상태를 확인한다. | B1의 재작업 위험을 통제하기 위함이다 |
| **4** | **영속 binding과 reconciliation을 설계한다** (AIK-031/033/035/038). runtime 저장소의 Lease·fencing과 `GuardedAdapter`의 메모리 상태를 연결하는 방안을 우선 정의한다. | C2: 멀티계정을 안전하게 운영하기 위한 전제다 |
| **5** | **native 권한 정책을 결정한다.** full-access/bypass 요구에 대해 격리 대안과 위험 수용 범위를 082/086 이전에 문서화한다. | C1: 보안 원칙과 직접 충돌한다 |
| **6** | **문서를 정합화한다.** initial-design의 참조 판본·열린 결정을 갱신하고, README에 AIK-011 요약을 추가한다. 미설명 모듈(security, compat, ipc, product-acceptance)도 문서화한다. | B2~B4: 저비용으로 혼선을 줄인다 |
| **7** | **저장소 성능 기준을 측정한다.** DB 크기별 transaction 비용을 측정하고 증분 검사 여부를 검토한다. | C3: 운영 규모 확대 전에 필요하다 |
| **8** | **DEC06(연합 단계화)을 확정한다.** | B5: 핵심 목표와 일정 사이의 모호성을 해소한다 |