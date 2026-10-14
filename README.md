# AI-Kubernetes

> **Federated, vendor-neutral AI agent compute orchestration**  
> 필요한 지능 자원을 장착하고, 필요한 만큼 확장하고, 작업이 끝나면 반환한다.

**AI-Kubernetes**는 OpenAI, Anthropic, 기타 API·CLI·Cloud 세션·로컬 모델 등 이질적인 AI 실행 자원을 하나의 **논리적 워커 풀**로 관리하고, 독립 작업을 분산 실행하며, **클러스터끼리도 PEER로 연결**하기 위한 초기 설계 프로젝트다.

단순한 LLM API 라우터나 새로운 subagent 프레임워크가 아니다. **작업 계약 → 자원 탐색 → 배치 → 실행 → 결과 회수 → 검증 → 자원 반환**을 벤더와 실행 위치에 종속되지 않게 만드는 상위 제어 계층을 지향한다.

> **상태: AIK-001·002 기반 구현과 검증 완료, AIK-003의 깨끗한 Windows 설치 검증은 차단 상태.** 설치형 upstream 계약을 고정하고, 같은 Windows CI job에서 Electron·Tauri의 실제 설치·GUI·Python/worker IPC·사용자 DPAPI·메모리·제거를 검증했다. Windows 기본 PowerShell 5.1 전달 스크립트도 실제 검사와 설치 앱 실행을 통과했다. [구현·실행 기록](docs/implementation-2026-10-12.ko.md)에 최초 실행부터 최신 결과까지 보존한다. 멀티벤더 인증 공간·총괄 모델 지정·Windows GUI 완제품과 첫 출시는 미완료다.

[개발 계획·검토](docs/development-plan.ko.md) · [완제품 완료성 검토](docs/product-completeness.ko.md) · [이전 장부 도구 감사](docs/audit-2026-10-12.ko.md) · [전체 개발 태스크](docs/development-backlog.ko.md) · [SQLite 장부](planning/backlog.sqlite3) · [최초 설계문서](docs/initial-design.ko.md) · [Agent Governance Suite](https://github.com/jaeseongs95/agent-governance-suite) · [VibeMarshal](https://github.com/jaeseongs95/vibemarshal)

## 첫 제품의 사용자 경험 — 구현 목표

1. **멀티벤더 인증 공간:** GUI에서 여러 벤더 계정, API 키, 지원되는 구독 호스트를 연결·확인·교체·해제한다. 실제 비밀은 Windows 사용자 저장소 또는 공식 호스트 저장소가 보관하고 앱에는 비밀 참조만 노출한다.
2. **총괄 에이전트 지정:** Dot처럼 목표를 받고 작업을 위임·회수·보고하는 총괄의 실행 자원, 모델, 지원 추론 수준과 대체 정책을 설정한다. 총괄과 worker/검토 역할의 프로필은 분리한다.
3. **Windows GUI 운영:** 사용자 프로젝트 폴더를 열거나 만들고 목표를 제출한다. 실제 파일 생성·수정·검증·재작업, 결과 열기/저장, 자원·비용·인가 범위 확인, 중단·재시작·백업 복원까지 GUI로 제공한다. GUI 창과 실행 중인 총괄/worker의 수명은 구분한다.

구현 결정 `DEC07: confirmed`에 따라 **Electron 44.7.0·React 19.2.8·TypeScript 7.0.2·Python 3.14.8 sidecar**와 runtime을 포함한 NSIS 사용자별 설치본을 사용한다. VibeMarshal의 GUI 자산과 EngineClient 경계를 검토했고, 독립 Tauri 2 비교도 실제 실행했다. 구현 선택 근거와 최초 지원 후보 Windows 11 x64의 검증 조건은 [Windows ADR](docs/windows-packaging.ko.md)에 있다. 위 세 사용자 기능의 제품 구현은 후속 태스크에 남아 있다.

### 현재 실행으로 확인한 범위

| 실행 | 확인한 내용 | 근거 |
| --- | --- | --- |
| 최초 Electron 설치 검증 | 실제 GUI 버튼·frozen Python·별도 worker·DPAPI·제거 PASS | [38219549819](evidence/windows-foundation/38219549819/README.ko.md) |
| 같은 job의 Electron·Tauri 비교 | 두 후보 설치·세 작업·메모리·화면·제거 PASS. 설치 파일은 각각 118,717,783 / 226,652,464 bytes | [38223818838](evidence/tauri-comparison/38223818838/README.ko.md) |
| Windows PowerShell 5.1 전달 검증 | 기존 source의 CP1252 해석 오류 2개를 확인하고 전달본의 UTF-8 BOM 실행 사본에서 0개. 새 전달 경로로 실제 GUI·worker·DPAPI·제거 PASS | [38226204128](evidence/windows-handoff/38226204128/README.ko.md) |

비교 실행은 Windows Server 2022의 같은 실행기와 같은 frozen Python 실행본을 사용했다. Tauri는 offline WebView2 installer를 포함했고, 실행 때에는 사전 설치된 131.0.2903.86을 재사용했다. 단일 메모리 관측으로 전체 앱 성능 우위를 정하지 않았다. **Windows 11 표준 사용자·개발 도구가 없는 깨끗한 환경의 기반 설치 검증은 아직 `NOT_RUN`**이며 AIK-003의 차단 사유다.

### AIK-005 runtime 저장소

`src/aik_runtime/`에 pool·credential 참조·profile·placement·lease의 독립 SQLite 저장소를 구현했다. revision 충돌, 원자적 transaction, 실제 프로세스 강제 종료 뒤 복구와 schema migration을 검증한다. 운영 DB는 사용자 데이터 경로에 두며 개발 원장·Git 내부 경로를 거절한다. [API와 복구 계약](docs/runtime-storage.ko.md), [검증 근거](evidence/runtime-storage/2026-10-12/README.ko.md)에 범위를 기록했다. 선행 AIK-083이 미완료이므로 원장 상태는 **blocked**다.

## 왜 만드는가

오늘날 AI 작업은 모델·CLI·클라우드·계정별로 따로 운영되기 쉽다. 이 구조에서는 독립 테스트가 수십 개여도 사람이 세션을 직접 열고, 작업을 나누고, 결과를 수집하고, 중복·누락·비용을 확인해야 한다.

AI-Kubernetes의 목표는 **모델을 하나 더 만드는 것**이 아니라 **이미 보유하거나 합법적으로 접근 가능한 지능 자원을 통합 운영하는 것**이다.

- **장착(Attach):** 지원되는 구독 세션, API 키, 클라우드 실행 경로, 로컬 모델을 자원으로 등록한다.
- **발견(Discover):** 모델, 추론 수준, 도구, 권한, 지연, 비용, 동시성·사용량 한도를 실제 지원 범위대로 파악한다.
- **분해·배치(Schedule):** 작업 의존성을 보존하면서 서로 독립적인 일은 병렬로 분산한다.
- **검증(Verify):** 작업자의 완료 선언만으로 완료하지 않고 증거, 실행 관측, 독립 검토를 확인한다.
- **반환(Release):** 작업 종료 후 불필요한 임시 세션을 정리하고 유휴 워커를 최소화한다(scale-to-zero 지향).
- **연합(Federate):** 하나의 AI-Kubernetes 클러스터뿐 아니라 **독립 클러스터 여러 개를 PEER 통신으로 연결**한다.

## 핵심 원칙

1. **벤더 독립:** 컨트롤 플레인은 특정 모델 공급자의 인증 형식이나 세션 구현에 종속되지 않는다.
2. **실행 능력 중심:** 공급자가 아닌 capability, 정책, 한도, 가용성으로 자원 배치를 결정한다.
3. **구독·API 모두 자원:** 사용자가 지원되는 방법으로 연결한 **구독 기반 호스트 세션**과 **API 자격증명**을 모두 다루되, 둘을 동일한 인증 권한으로 간주하지 않는다.
4. **분산보다 거버넌스 우선:** ACK, 권한, 완료 판정, 증거, 원장과 복구 절차가 실행 경로를 뒷받침한다.
5. **최대 병렬화가 아니라 제약 내 최적 병렬화:** 작업 DAG, 동시성, 예산, rate limit, 서비스 약관, 데이터 정책을 먼저 만족해야 한다.
6. **다중 클러스터 연합:** 각 클러스터는 독립적으로 운영하고, 상위 연합은 능력·잔여 용량·작업 계약을 통해 협력한다.
7. **실패를 숨기지 않음:** 전송 확인, 처리 확인, 실행 성공, 검증 통과, 최종 완료를 구분한다.

## 구조

```mermaid
flowchart TD
    U["User: Goal / policy / approval"] --> F["AI-Kubernetes Federation Control Plane"]
    F --> R["Global capability registry & scheduler"]
    R <-->|"Federated PEER contracts"| A["Cluster A: Desktop / Local"]
    R <-->|"Federated PEER contracts"| B["Cluster B: Cloud"]
    R <-->|"Federated PEER contracts"| C["Cluster C: Remote / Organization"]
    A --> A1["Local VM / CLI agents"]
    B --> B1["Codex / Claude cloud workers"]
    C --> C1["API / local / other adapters"]
    A & B & C --> E["Evidence / artifacts / verdict"]
    E --> F
```

각 클러스터는 공통 인터페이스를 제공하되 내부 구현, 공급자 인증정보, 로컬 정책과 상세 작업 원장은 외부에 공개하지 않는다. 연합 레벨은 **어느 클러스터에 맡길지**, 로컬 레벨은 **그 클러스터의 어떤 워커에 맡길지** 결정한다.

### 프로젝트 간 역할

| 구성 요소 | 책임 |
| --- | --- |
| **AI-Kubernetes** | 클러스터 연합, 자원 등록·광고, 자원 할당(lease), 전역 배치, 오토스케일, 공급자 adapter 관리 |
| **[VibeMarshal (VM)](https://github.com/jaeseongs95/vibemarshal)** | Goal Contract, Task DAG, 계획/승인 경계, 실행·관측·검증·복구의 권위 워크플로 |
| **[Agent Governance Suite (AGS)](https://github.com/jaeseongs95/agent-governance-suite)** | 세션 PEER, ACK·handoff, 작업 계약·거버넌스 검사, 문맥 연속성, 증거·독립감사 |
| **Provider / host adapters** | 벤더별 공식 실행 경로, 세션·API 호출, capabilities, 실행 결과·사용량의 표준 변환 |
| **JEV (내부 계층)** | RAW 입력을 보존한 상태에서 확인된 메타정보를 정리·분류·평가해 에이전트의 스킬 선택을 보조. 최종 선택권은 에이전트에게 있음 |

VM과 AGS의 **현재 구현 상태는 각 원본 저장소 문서가 기준**이며, AI-Kubernetes는 이들 기능을 중복 구현하는 대신 인터페이스를 연결하도록 설계한다.

## 작업 흐름 예시

사용자가 다음 목표를 입력했다고 가정한다.

> “서로 독립적인 검증 사례 39개를 실행하고, 실패 사례를 구분해 근거와 함께 보고해.”

1. VM이 목표·허용 작업·완료 기준을 명확히 하고 Task DAG와 실행 후보를 만든다.
2. AI-Kubernetes가 각 클러스터의 capability, 할당 가능한 워커, 예산·쿼터·실행 제약을 조회한다.
3. 독립 작업만 병렬 배치한다. 선행 조건이 있는 작업은 준비 또는 대기 상태를 유지한다.
4. 각 worker는 격리된 입력으로 실행하고 결과·사용량·증거를 반환한다.
5. AGS 거버넌스와 VM 검증 계약에 따라 미완료·실패·불확실한 결과를 구분한다.
6. 결과를 취합하고, 필수 검토·승인 후 최종 완료 판정을 생성한다.
7. 임시 worker의 lease를 해제하고 가능한 실행환경을 종료한다.

**39개 작업 = 반드시 39개 세션**이라는 뜻은 아니다. 병렬도는 각 작업의 독립성, 공급자의 실제 동시성 한도, 캐시 효율, 예산과 외부 서비스 정책을 고려해 결정한다.

## 장착 가능한 자원: 목표 모델

| 자원 유형 | 예시 | 연결 방식 |
| --- | --- | --- |
| 지원되는 구독 호스트 | Codex CLI/Cloud, Claude Code/Cloud | 공식 호스트·세션 연결 인터페이스 및 허용된 사용자 권한 |
| 상용 API | OpenAI API, Anthropic API, 기타 호환 제공자 | 해당 제공자의 API 키/조직 설정 |
| 로컬 추론 | 로컬 LLM 서버, 온디바이스 모델 | 로컬 adapter·명시적 자원 한도 |
| 원격 클러스터 | 다른 AI-Kubernetes 설치 | 인증된 federation PEER |

**구독 결제와 API 요금은 서로 독립적일 수 있다.** 구독 자격증명을 API 키로 재사용하거나, 비공식 인증·약관 우회를 전제하지 않는다. 지원하지 않는 경로는 등록할 수 있어도 **실행 불가**로 표시한다.

## 최초 릴리스 목표

- Windows 설치본에서 여러 벤더의 API 및 지원 구독 호스트를 등록하고 시크릿 수명주기를 관리한다.
- 사용자가 선택한 실제 프로젝트에 총괄이 목표를 준비하고 **서로 다른 두 벤더의 qualified 자원**을 사용한다. A/B의 산출물을 후속 C가 소비해 파일 생성·수정·한정된 검증 명령을 수행한다.
- 독립 검사에 의도적으로 한 번 실패한 뒤 기존 VM 인가·재작업·최종 판정을 거쳐 결과를 완성한다. 실제 bytes/digest·명령 결과·모델·사용량·근거로 판정한다.
- 결과를 GUI에서 열기·저장하고 worker 반환·앱 재시작 뒤에도 사용한다. 후속 정정·중단·취소·감독 재개와 개별 도구 효과의 복구를 검증한다.
- 설정 이동·일관된 제품 상태 백업·새 사용자 복원·업데이트 실패 복구를 GUI 또는 함께 제공한 복구 화면으로 완료한다.
- P01~P10의 사용자 흐름, 설치·비밀 경계·장애·독립 검토·사전 품질/성능 기준을 같은 소스 판본의 플랫폼별 배포 후보에서 검증한다.

**개발 순서 제안:** 단일 클러스터의 Windows·Linux 배포(M0~M4)를 검증한 뒤, **독립 클러스터 2개 간 PEER 연결·위임·결과 회수(M5)**로 확장한다. 연합은 핵심 제품 목표로 유지한다. 이 단계화는 장부의 `DEC06: proposed`로 기록했으며, 사용자가 확정한 Windows·Linux/GUI 요구와 구분한다.

## 개발 장부

`planning/backlog.sqlite3`가 개발 태스크 정본이다(schema 2). 요구사항·출처·단계·우선순위·담당 구성요소·의존성·완료 기준·검증 방법·상태 이력·증거에 더해 설계 원칙(벤더 독립·OS 독립·SOLID), 플랫폼별 상태(Windows x64·Linux x64), 외부 선행, 변경 이력을 연결한다. **93개 태스크, 320개 완료 기준, 375개 의존성**을 관리하며, 최신 상태 수치는 [생성 장부](docs/development-backlog.ko.md)를 따른다. 첫 출시는 Windows와 Linux 둘 다이고 진행은 Windows가 먼저다. 개편 근거는 [원장 schema 2](docs/backlog-v2.ko.md)에 있다. P01~P12는 구체적인 사용자 행동·통과 결과·실패 차단 조건을 가진 파생 인수 요구사항이다.

| 단계 | 범위 | 태스크 |
| --- | --- | ---: |
| M0 | 통합 계약·플랫폼 기반·제품 인수 조건 | 10 |
| M1 | 인증 보관·멀티벤더 연결 | 15 |
| M2 | 총괄·실제 workspace/개발 도구·AGS/VM 적합성 | 28 |
| M3 | 데스크톱 GUI 실제 연결·프로젝트 선택 | 13 |
| M4 | Windows·Linux 유지보수·첫 배포 검증 | 9 |
| M5 | 탄력 운영·원격 PEER·연합 설치본 출시 | 12 |
| M6 | 로컬 모델·추가 벤더·조직·선택 배포·확장 출시 | 6 |

첫 제품(Windows·Linux)은 **75개** 태스크와 P01~P10을 064에서 판정한다. 연합 기본 제품은 누적 **86개**를 089, 제안 기본 확장 제품은 누적 **91개**를 090에서 새 설치본으로 판정한다. 선택 Cloud 생성 066과 Kubernetes 080을 포함하면 해당 gate의 실제 선행에 추가하며, 모두 포함한 전체 로드맵은 **93개**다. 미선택 기능은 미완료로 유지한다. 현재 범위와 의존성은 [생성 장부](docs/development-backlog.ko.md)가 기준이며, [완료성 보고서](docs/product-completeness.ko.md)는 90개 태스크 당시의 검토 기록이다.

개발 순서는 식별자 번호가 아닌 `sequence`와 의존성으로 판단한다. 082는 실제 사전 probe이고 086은 구현 뒤의 운영 적합성 검증이다. 최초 시험은 기존 인가·한도·AGS/VM gate를 지키는 명시적 candidate binding으로 수행하며, 실제 통과한 역할/도구 조합만 운영 자원으로 광고한다.

[전체 태스크와 완료 기준](docs/development-backlog.ko.md) · [Git에서 비교할 SQL 스냅샷](planning/backlog.sql)

장부 도구는 Python 표준 라이브러리만 사용한다. 제품 런타임과 별개다.

```bash
python tools/backlog.py validate
python tools/backlog.py list --ready
python tools/backlog.py list --ready --platform WIN-X64
python tools/backlog.py list --ready --platform LINUX-X64
python tools/backlog.py list --milestone M1
python -m unittest discover -s tests -v
```

유지보수·복원·상태 변경 절차는 [개발 계획](docs/development-plan.ko.md#개발-장부-사용법)에 있다.

## 설계 범위와 비범위

현재 범위는 **AI 실행 자원의 제어·연합·감사**다. 처음부터 Kubernetes 자체를 수정하거나 Kubernetes의 Pod/etcd/CNI API를 구현한다는 뜻이 아니다. 동일한 철학의 AI 전용 제어 계층을 먼저 만든다. Kubernetes 배포 지원은 추후 선택 가능한 adapter일 뿐 필수 전제가 아니다.

대규모 모델 학습, 임의 사용자 계정의 권한 공유, 공급자 제한 우회, 자동 결제, 무제한 세션 보장도 프로젝트의 목표에 포함하지 않는다.

## 프로젝트 상태

**2026-10-14 현재 원장:** 완료 2개, 차단 53개, 미착수 38개다. Linux를 포함한 출시 인수 계약과 플랫폼별 선행 검증을 정합화했다. 기존 완료 기준과 성능 임계값, 작업·플랫폼 상태는 유지한다. Linux 배포판·설치 형식, 깨끗한 Windows 설치, 실제 provider/VM/AGS 연결과 제품 인수는 미완료다.

**2026-10-12 · 계획 v0.4의 AIK-001·002 구현·검증 완료.** 설치형 upstream 검사기와 실제 Windows 설치 GUI 기반을 구현하고, Electron·Tauri 비교와 PowerShell 5.1 전달 경로를 실행했다. 장부의 기존 90개 태스크·311개 완료 기준·365개 의존성을 보존했으며, AIK-003의 깨끗한 Windows 11·개발 도구 부재 검증은 `blocked`로 남긴다. 이 기반 검증과 실제 벤더·사용자 프로젝트·총괄 모델·AGS/VM 제품 연동 및 출시 판정은 각각의 기존 태스크로 관리한다. 최신 태스크 상태는 [SQLite 생성 장부](docs/development-backlog.ko.md)를 따른다.

같은 날 단일 태스크 지시로 AIK-005의 저장소·마이그레이션을 구현했다. 구현·검증 근거는 추가하고 선행 AIK-083 미완료에 따른 `blocked` 상태와 원래 완료 기준을 유지한다.

## 라이선스

이 프로젝트의 자체 소스는 [MIT License](LICENSE)로 배포한다. 저장소에 보존한 upstream 산출물(예: `evidence/upstream/` 아래 VM wheel)과 제3자 구성요소는 각자의 라이선스를 따르며, 이 MIT 라이선스가 그 라이선스를 대체하지 않는다.
