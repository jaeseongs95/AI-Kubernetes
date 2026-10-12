# 태스크를 따라 개발하면 완제품이 되는가: 완료성 검토

- 검토일: **2026-10-12**
- 계획 버전: **v0.4**
- 비교 기준: [기존 82개 태스크, dc6d60b](https://github.com/jaeseongs95/AI-Kubernetes/commit/dc6d60b67c0ccd2e56849992440de2eb639aa36c)
- 정본: [개발 SQLite](../planning/backlog.sqlite3) · [전체 태스크·완료 기준](development-backlog.ko.md) · [검증 기록](../planning/validation.json)
- 제품 상태: **구현 전. 이 문서의 PASS는 계획의 완료성·추적 관계에 대한 판정이다.**

## 결론

**기존 82개 태스크를 문구 그대로 충족하는 것만으로는 완제품이 나온다고 판단할 수 없었다.** 사용자 프로젝트 대신 샘플 폴더에서 두 벤더의 읽기 작업만 수행하고, 사용자가 결과 파일을 가져가 쓸 수 없는 앱도 기존 대표 실행 검증을 통과할 수 있었다. 후속 연합 기능을 개발해도 그 변경이 들어간 새 Windows 설치본을 출시하는 단계가 없었다.

이를 보완해 **8개 태스크를 추가하고, 기존 39개 태스크의 산출물·검증 방법·완료 기준을 보강했다.** 사용자 요구에서 도출한 **12개 인수 흐름 P01~P12**를 SQLite `requirements`에 구체적인 입력·관찰 결과·실패 차단 조건으로 기록했다. 각 흐름은 구현, GUI 또는 설치 복구 화면, 실제 통합 검증, 최종 배포 판정에 연결된다.

수정된 계획은 **정의한 Windows 첫 제품을 개발하고 검증해 인계하는 데 필요한 흐름을 포함한다.** `done`은 각 태스크의 구현·검증 근거가 있고, 최종 설치본이 정해진 사용자 시나리오를 실제 통과했다는 뜻으로 사용해야 한다. 상태값만 바꾸거나 단위 시험만 통과하는 것으로 제품 완료를 판정하지 않는다.

## 1. 어떤 관점으로 검토했는가

세 관점에서 독립적으로 기존 DB·완료 기준·설계 문서를 읽고, **모든 개별 기준을 지키면서도 제품은 불완전할 수 있는 반례**를 만들었다. 수정 후 같은 반례가 여전히 허용되는지도 다시 검토했다.

| 관점 | 확인한 연결 | 판정 방식 |
| --- | --- | --- |
| 사용자 여정 | 설치·연결·프로젝트·목표·결과 사용·다시 열기 | 실제 사용자 행동에 담당 태스크와 관찰 가능한 결과가 있는지 확인 |
| 실행 계약 | 총괄·VM·AGS·route·lease·도구 효과·의존 작업 | 생산자/소비자 계약, 효과 전후 중단, qualification의 시작 가능성 검토 |
| 출시·운영 | 설치물·업데이트·일관 복원·GUI 유지보수·후속 배포 | 해당 기능이 포함된 같은 설치본에서 끝까지 검증되는지 확인 |

추가로 SQLite 무결성·외래키·의존성 그래프·선행 순서·생성본·기존 장부 도구의 회귀를 검사한다. 이 검사들은 제품 구현을 대신하지 않는다. 실제 Windows 설치, 벤더 로그인, 모델 실행, VM/AGS gate 또는 원격 클러스터를 실행한 것으로 기록하지 않는다.

## 2. 기존 태스크에서 통과할 수 있었던 반례와 수정

### PC-01. 사용자 프로젝트가 실제 실행 위치와 연결되지 않아도 통과 가능

기존 007/021은 workspace 설정과 연결 사용 범위를 다뤘지만, GUI에서 사용자가 고른 폴더를 VM project와 native/API의 실제 `cwd`에 결속하는 책임이 명시적이지 않았다. 샘플 프로젝트만 사용할 수 있거나, 화면은 사용자 폴더를 보여주는데 실행은 다른 위치에서 진행되는 구현이 가능했다.

**수정:** 084가 프로젝트 열기·생성·전환, canonical root와 VM project ID, 입력 bytes/digest/revision, 후속 Task가 소비할 선행 산출물을 결속한다. 087이 폴더 선택·생성·전환 GUI를 제공한다. 050/059에서 실제 실행 위치·한글/공백 경로·재시작·접근 상실까지 검증한다. 입력 누락·변조·stale·다른 workspace는 효과 전에 차단한다. [수정 태스크 084](development-backlog.ko.md#aik-084) · [087](development-backlog.ko.md#aik-087)

### PC-02. 두 벤더가 읽기 작업만 해도 개발 제품으로 출시 가능

기존 044의 검증 방법은 `read-only Goal 1개·독립 Task 2개`였고, 059도 실제 읽기 도구 실행을 요구했다. 이 조합은 실제 인증·모델·관측·검토·lease 반환을 보여주지만, 파일을 만들고 수정하고 검증 명령을 실행해 목표를 완성하는 능력을 요구하지 않았다. 선행 산출물이 후속 입력으로 전달되지 않는 문제도 독립 작업만으로는 드러나지 않았다. [기존 044·059의 기준](https://github.com/jaeseongs95/AI-Kubernetes/blob/dc6d60b67c0ccd2e56849992440de2eb639aa36c/docs/development-backlog.ko.md)

**수정:** 085에 실제 파일 읽기·생성·수정·한정된 검증 명령을 추가하고, 035가 native/API 에이전트 실행에 연결한다. 044와 059는 다음 합성 흐름을 필수로 수행한다.

1. 사용자가 고른 실제 프로젝트에서 RAW Goal을 제출한다.
2. 실제 총괄/VM 준비 경로가 A와 B, 그리고 두 산출물을 소비하는 C를 구성한다.
3. 두 벤더 자원이 실제 파일을 생성·수정하고 검증 명령을 실행한다.
4. 독립 검사에 의도적으로 한 번 실패한 뒤, 기존 인가·VM 재작업 절차로 수정한다.
5. 최종 Goal Test와 사용 가능한 결과 파일을 얻고, 후속 정정도 같은 Goal의 올바른 revision에 반영한다.

시험 입력은 준비할 수 있지만 Plan/Task/완성 산출물을 미리 넣어 성공을 만들 수 없다. worker가 바꿀 수 없는 사전 고정 oracle로 결과 내용·bytes·digest와 실제 명령의 exit/output을 대조한다. 모델이 “완료했다”고 말하는 것만으로 통과하지 않는다. 분석 전용 자원은 그 능력에 맞게 쓸 수 있으며, 모든 공급자에게 같은 shell 기능을 강제하지 않는다. [085](development-backlog.ko.md#aik-085) · [044](development-backlog.ko.md#aik-044) · [059](development-backlog.ko.md#aik-059)

### PC-03. 모델 호출은 중복되지 않아도 파일 변경은 중복될 수 있음

기존 실행 ID와 복구 기준은 주로 Attempt·lease·provider job을 연결했다. 파일 쓰기가 성공한 직후 도구 결과를 기록하기 전에 중단되면, 모델 job을 중복 발사하지 않더라도 같은 도구를 다시 실행해 파일에 두 번 기록할 수 있었다.

**수정:** 029/035/039/085/086/060에서 개별 `tool_call/effect identity`, request digest, 효과 전 intent, 효과 후 durable receipt, 모델에 결과를 반환하는 경계를 나눈다. 실제 파일 변경에서 각 경계에 중단·취소·늦은 결과를 주입한다. 기존 결과 재전송과 효과 재실행을 구분하며, 비멱등 효과가 불명확하면 기존 VM unknown/reconciliation 절차까지 중단한다. 임의 외부 명령에 정확히 한 번의 효과를 보장한다고 주장하지 않는다. [035](development-backlog.ko.md#aik-035) · [039](development-backlog.ko.md#aik-039) · [086](development-backlog.ko.md#aik-086)

### PC-04. 결과를 조회해도 사용자가 가져가 쓰지 못할 수 있음

기존 037/054는 artifact·digest·evidence 조회를 포함했다. 그러나 종료된 임시 worker의 경로만 보여주거나, worker 정리와 앱 재시작 뒤 실제 파일이 사라지는 구현까지 명시적으로 막지는 못했다.

**수정:** 037/038/054/059가 결과를 사용자 프로젝트 또는 약속한 관리 저장소에 인계하고, GUI에서 열기·저장/내보내기를 수행하며, worker 반환·재시작 뒤에도 같은 bytes/digest를 사용할 수 있도록 요구한다. 원본 근거와 비밀 제거된 표시용 derivative는 각각의 identity와 provenance를 유지한다. 보관 만료·외부 변경·접근 실패를 숨기지 않는다. [037](development-backlog.ko.md#aik-037) · [054](development-backlog.ko.md#aik-054)

### PC-05. 각 DB가 정상이어도 제품 전체 복원은 틀릴 수 있음

AIK 설정은 시점 T1, VM 이력은 T2, AGS 근거와 실제 artifact는 T3에서 가져오더라도 각각의 DB 무결성은 통과할 수 있다. 참조 문자열을 보존하는 것만으로는 완료 Goal의 실제 산출물·검토 근거·진행 job이 일치한다고 할 수 없다.

**수정:** 058이 설정-only 이동과 전체 제품 상태 복원을 구분한다. 각 소유자의 공개 checkpoint·정지/재개·복원 절차를 공통 recovery manifest에 결속하고 버전·시점·hash·실제 참조와 외부 job identity를 확인한다. 혼합 checkpoint·누락/변조 artifact는 복원 성공을 차단한다. 완료 Goal은 실제 결과를 다시 열 수 있어야 하고, active/unknown job은 기존 VM/provider의 reconciliation으로 처리한다. AIK가 VM DB를 직접 수정하는 새로운 복구 권위를 만들지 않는다. [058](development-backlog.ko.md#aik-058)

### PC-06. 유지보수 backend만 만들고 GUI 사용자는 복구하지 못할 수 있음

설정 export/import, 업데이트·백업·rollback 코드가 있어도 터미널 명령으로만 사용할 수 있다면 GUI 제품의 유지보수 여정은 완성되지 않는다. 앱이 시작되지 않는 상황의 복구 진입점도 필요하다.

**수정:** 088이 GUI 및 설치본과 함께 제공되는 복구 화면을 담당한다. 058은 이 화면이 소비할 backend 계약을 제공하고, 088/059가 개발 도구 없는 환경에서 설정 이동·전체 백업·업데이트 중단·새 사용자 복원·재인증·이전 결과 열기를 수행한다. [088](development-backlog.ko.md#aik-088)

### PC-07. 후속 기능은 개발돼도 그 기능이 들어간 제품은 출시되지 않을 수 있음

기존 064의 선행 집합은 M0~M4의 66개뿐이었고, M5/M6 개발 뒤 새로운 최종 배포 gate는 없었다. 연합·로컬 모델이 개발 모드에서 동작하더라도 이전 설치본만 사용자에게 전달되는 상태를 배제할 수 없었다.

**수정:** 089와 090에서 각각 새 Windows 설치본을 만들고, 이전 지원 버전의 업그레이드·복원과 기존 제품 회귀, 새 기능의 GUI·실제 실행을 같은 판본으로 검증한다. 이전 064의 성공을 새 설치본의 증거로 그대로 재사용할 수 없다. 074도 원격 읽기만 확인하지 않고, 다른 root에 입력을 준비한 뒤 의존 쓰기·검증·재작업·결과 반환과 단절 복구를 수행한다. [074](development-backlog.ko.md#aik-074) · [089](development-backlog.ko.md#aik-089) · [090](development-backlog.ko.md#aik-090)

## 3. 실행할 수 있는 개발 순서인가

태스크의 번호는 식별자다. **착수 순서는 `sequence`와 `task_dependencies`, `v_ready_tasks`로 판단한다.** 새 083은 제품 인수 계약을 정하는 M0 작업이므로 004보다 앞에 놓인다.

이번 검토에서는 숫자 그래프의 순환뿐 아니라 **완료 기준 문장이 요구하는 숨은 선행 조건**도 점검했다. 예를 들어 082가 완성된 agent loop의 운영 적합성을 먼저 요구하면, 082를 선행으로 갖는 035를 만들기 전에 035를 실행해야 하는 문제가 생긴다. 재검토에서는 026의 제어 루프 구현 기준에 실제 합성 목표 완료를 요구하는 표현도 찾아 수정했다.

| 단계 | 완료의 의미 | 다음 검증과의 관계 |
| --- | --- | --- |
| 001/031 | 실제 VM 공개 접합점, route·lease·Attempt·효과 권위 계약 확보 | upstream seam이 부족하면 변경·호환 배포물 확보가 선행 |
| 012~015, 025~035, 084/085 | adapter·제어 흐름·도구·입력 결속 구현; 계약 fixture와 실제 임시 파일/명령 효과 검사 | fixture 성공은 공급자 운영 적합성으로 광고하지 않음 |
| 082 | 실제 adapter/native 최소 진단 probe의 모델·effort·계정·receipt 및 계약 가능성 | 전체 governed agent loop의 성공을 선행으로 요구하지 않음 |
| 086 | 구현된 경로의 필수 역할·도구·route/lease·효과·기본 복구를 실제 설치형 패키지에서 검증 | 성공한 조합만 일반 운영의 qualified 자원으로 광고 |
| 044 | 실제 A/B→C 개발 목표·독립 실패·재작업·결과 인계 | 역할 계약 coverage와 대표 합성 목표를 구분 |
| 059/064 | P01~P10을 같은 Windows 배포 후보에 결속 | 설치본에서 실제 사용자 여정을 다시 검증해 첫 출시 판정 |
| 089/090 | 후속 기능을 포함한 새 후보의 패키징·업그레이드·회귀·사용자 여정 | 이전 판본의 PASS를 자동 상속하지 않음 |

최초 운영 qualification을 시작할 수 있도록 019/033에는 **고정된 test-only candidate binding**이 있다. 086 및 같은 계약을 재사용하는 후속 자원 qualification에만 사용하며, 기존 사용자 인가·budget/lease·AGS A2/VM gate는 모두 적용한다. qualification PASS를 그 최초 시험의 선행으로 요구하지 않는다. 일반 실행은 사용할 역할·도구 조합에서 실제 qualification을 통과한 경로만 사용한다.

M6 기능 태스크는 기존 057의 빌드 절차로 자기 기능이 포함된 후보 설치본을 검사하고 090에 제공한다. 아직 수행하지 않은 090의 최종 PASS를 기능 구현의 선행으로 요구하지 않는다. 076/077은 원격 연합 개발과 병렬로 준비할 수 있으며, 확장의 실제 qualification 전에 지원 범위를 고정한다.

### VM 주입점은 여전히 구현으로 입증해야 할 통합 조건

고정 VM 기준선은 하나의 runtime 객체를 `EngineApplication`에 주입하고, 내부 Dispatcher가 Attempt·provider call·runtime intent를 예약한 뒤 runtime을 호출한다. runtime 객체를 주입할 수 있는 접합점은 있으므로 확장이 불가능하다고 판정하지 않았다. 다만 AIK의 route/connection/pool/lease가 **정확한 VM Attempt의 효과 발생 전에** 연결되고 재시작 조회·취소도 같은 계정을 사용하는지는 별도 구현 계약과 설치형 qualification이 필요하다. [application.py](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/src/flowmarshal/engine/application.py) · [runtime.py](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/src/flowmarshal/engine/runtime.py) · [사용자 workflow](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/docs/engine-user-workflow.md)

001/031은 이 접합이 부족하면 upstream 변경·호환 artifact 확보 전 완료할 수 없게 했다. 081의 A2 plugin conformance와 082의 관측 probe를 실행 접합 전체가 이미 해결됐다는 증거로 취급하지 않는다. 이는 확인된 통합 준비 조건이며, 현재 제품에서 장애를 재현했다는 주장이 아니다.

## 4. SQLite에 들어간 완제품 인수 흐름

P01~P12는 **기존 사용자 요구에서 도출한 검증 계약**으로 `origin=derived`다. 원래 사용자 요구 U01~U09와 확정 결정 DEC01~DEC05는 보존했다. 최소 흐름의 본문은 현재 DB에 들어 있고, 083은 이를 나중에 새로 정의하는 작업이 아니라 지원 조합·소유자·oracle·측정 조건에 결속하는 작업이다.

| 요구 ID | 필수 사용자 흐름 | 실제 판정에서 확인할 것 |
| --- | --- | --- |
| P01 | 설치·멀티벤더 연결·만료·재인증·계정 교체/해제 | 선택한 실제 계정·connection으로 실행, 필수 경로 live 근거 |
| P02 | 사용자 프로젝트 열기·생성·전환 | 화면 root와 VM/native/API cwd, 입력 bytes/digest/revision 일치 |
| P03 | 총괄·개발·검토 역할의 모델 지정 | 요청·실측 model/effort·계정 binding과 허용 fallback |
| P04 | RAW Goal·A/B→C·파일 수정·검증 실패·재작업·후속 정정 | 실제 산출물·고정 oracle·VM 최종 판정·정확한 Goal revision |
| P05 | 인가·비밀·동시성/예산 한도 | 허용 작업의 연속 실행, 범위 밖 효과 차단, 실제 trust profile |
| P06 | 도구 효과 중단·취소·늦은 결과 | intent/receipt와 실제 효과, 중복 없음 또는 명시적 unknown 중단 |
| P07 | 결과 열기·저장·worker 정리 후 다시 사용 | 인계한 bytes/digest·보관 범위·근거 provenance |
| P08 | 창 재열기·재시작·감독 재개·자원 반환 | 같은 Goal/미처리 지시/job 복원, 자체 worker와 borrowed host 구분 |
| P09 | 설정 이동·전체 백업·업데이트 실패·새 사용자 복원 | 소유자별 일관 checkpoint·실제 artifact·재인증·GUI 복구 |
| P10 | 고정 Windows 설치본의 품질·성능·지원·배포 | 동일 hash/matrix, 사전 측정 기준, 필수 FAIL/NOT_RUN 차단 |
| P11 | 연합이 포함된 새 Windows 제품 | 실제 두 클러스터 의존 작업·원격 효과/결과·업데이트·기본 회귀 |
| P12 | 선택한 확장이 포함된 새 Windows 제품 | 선언한 추가 자원·정책·조직/배포 경계의 GUI/live 및 기존 회귀 |

각 흐름의 전체 문구와 담당 태스크는 [생성된 장부](development-backlog.ko.md)에 있다. SQL로도 확인할 수 있다.

```sql
SELECT r.id, r.title, r.description, t.id, t.title, t.status
FROM requirements AS r
JOIN task_requirements AS tr ON tr.requirement_id = r.id
JOIN tasks AS t ON t.id = tr.task_id
WHERE r.id GLOB 'P[0-9][0-9]'
ORDER BY r.id, t.sequence;
```

## 5. 어느 범위까지 해야 어떤 완제품인가

단일 클러스터 Windows 출시 뒤 연합과 확장을 진행하는 순서는 기존 **DEC06: proposed**를 유지한다. 첫 Windows 버전이라는 사용자 확정 요구와 개발 단계 제안을 구분한다.

| 제품 범위 | 최종 판정 | 기본 선행 포함 작업 수 | 포함되는 결과 |
| --- | --- | ---: | --- |
| 첫 Windows 제품 | 064 | **72** | P01~P10, 인증 공간·모델 지정·실제 프로젝트 작업·GUI·설치/운영 |
| 연합 Windows 제품 | 089 | **83** | 첫 제품과 M5 기본 연합 기능, P11, 새 설치본·업데이트 |
| 제안 기본 확장 제품 | 090 | **88** | 앞선 제품과 076~079 로컬 LLM·추가 adapter SDK·정책·조직, P12 |
| 현재 장부 전체 로드맵 | 090 및 선택 기능을 포함한 판본의 근거 | **90** | 066 Cloud 생성과 080 Kubernetes도 구현·실측·출시 검증 |

표의 숫자는 **gate 자신과 모든 전이적 선행 작업을 포함한 집합 크기**다. 첫 출시 72개는 DB의 `first_release` 전체와 정확히 일치한다. M5에는 12개, M6에는 6개 태스크가 있지만, 그중 066/080은 선택 기능이다.

### 선택 기능을 실제 출시 조건에 반영하는 방법

- M5 기본은 065·067~075다. 066의 Cloud worker 생성을 포함하면 067의 scope 확정 때 `089 → 066` 의존성을 추가한다.
- M6 제안 기본은 076~079다. 080의 Kubernetes를 포함하면 확장 qualification 전에 `090 → 080` 의존성을 추가한다.
- 선택한 기능은 실제 선행 작업이므로 미완료 상태에서 gate를 시작·완료할 수 없다. `validate`로 순환·범위·상태를 검사하고 SQL/보고서를 재생성한다.
- 미선택 기능은 `todo`/미래 범위로 유지하며 해당 기능의 지원이나 전체 로드맵 완료를 주장하지 않는다. 이미 완료된 gate의 범위를 넓히면 영향받는 후행 상태를 다시 검토하고 새 판본의 근거로 재판정한다. 기존 출시 이력과 근거는 보존한다.

Cloud 생성 경로가 지원되지 않는 것과 연합 기능 자체를 구현하지 않은 것은 다르다. **M5는 실제 독립 클러스터 두 개의 연합을 반드시 통과해야 한다.** Kubernetes는 별도 배포 adapter이므로 특정 Cloud provider의 생성 API를 선행으로 요구하지 않는다. 080은 034/038/039의 lease·수명·복구와 071/074의 위임·연합 계약을 사용한다.

네 선택 조합도 검증했다.

| 선택 조합 | 064 | 089 | 090 | 090에서 남는 미선택 작업 |
| --- | ---: | ---: | ---: | --- |
| 기본 | 72 | 83 | 88 | 066, 080 |
| Cloud 066 | 72 | 84 | 89 | 080 |
| Kubernetes 080 | 72 | 83 | 89 | 066 |
| 모두 선택 | 72 | 84 | 90 | 없음 |

모든 조합에서 첫 출시 범위가 유지되고 의존성 순환·뒤순서 의존은 없었다. 이 조합 검사는 메모리의 그래프 또는 별도 DB 사본에서 수행했으며 실제 선택 범위를 임의로 변경하지 않았다.

## 6. 최종 판정에 필요한 증거

release manifest는 최소한 다음을 한 판본에 묶는다.

| 증거 | 목적 |
| --- | --- |
| source commit·installer hash·runtime manifest·지원 matrix revision/digest | 서로 다른 빌드의 성공을 섞지 않음 |
| P01~P12 중 선택 범위의 여정·필수 조합별 결과 | 구현 목록과 실제 사용자 완료를 연결 |
| model/effort/계정·Goal/Attempt·lease/job·tool effect identity | 설정·실행·효과·복구가 같은 대상인지 대조 |
| 실제 산출물·입력/결과 hash·독립 oracle·검증 명령 결과 | 자기선언·완성 fixture·stale 파일로 성공하는 것을 차단 |
| 업데이트·소유자별 checkpoint/restore·재시작·중복 효과 검증 | 다시 사용할 수 있는 제품 상태를 입증 |
| 사전 고정 품질/성능 조건·독립 검토·지원 안내 | 미충족 조건을 숨기거나 사후 완화하지 않음 |

지원 Windows 버전·CPU·installer 형식·정확한 provider/role 조합·성능 수치는 001/003/083의 선행 결정에서 확정한다. 이 값들을 임의로 정해 현재 지원 사실처럼 쓰지 않았다. 결정 전에는 해당 후행 구현/출시가 준비됐다고 처리할 수 없으며, 결정한 필수 조건이 FAIL/NOT_RUN이면 제품 완료를 차단한다.

## 7. 실제 수행한 검증과 남아 있는 일

실제 실행한 장부 도구 및 계획 구조 회귀 **65개가 모두 통과**했다. 기존 도구 회귀 53개와 이번 사용자 여정·출시 범위·qualification 순서 회귀 12개를 포함하며 실패·건너뜀은 0개다.

| 검증 항목 | 결과 |
| --- | --- |
| SQLite integrity / 외래키 / 신뢰 schema·버전·값·이력 | PASS, 무결성 `ok`, 외래키 위반 0 |
| SQL 복원 후 모든 테이블 행·schema·user_version | 정본과 동일 |
| SQLite→SQL/Markdown 생성본 | 정확히 일치, 90개 태스크·311개 기준·29개 요구 본문 포함 |
| 의존성 순환·실행 순서 | 순환 0, 뒤순서 의존 0 |
| 첫 출시 gate | 064 자신과 전체 선행이 first_release 72개와 정확히 일치 |
| 기본/Cloud/Kubernetes/전체 네 선택 조합 | 모두 유효, 전체 선택 시 090이 90개 전체 포함 |
| 이전 사용자 요구·확정 결정·단계 소속·기존 이력 | 보존 확인 |
| 기존 장부 도구 코드·schema·53개 회귀 파일·최초 설계 | 바이트 보존 확인 |
| 실제 제품 상태 | 90개 모두 `todo`, 제품 완료 증거 0 |

검증 중 정본 DB는 변경되지 않았다. 최종 SHA256은 `e17ac9d22f6cca6ebb0341d6b14cd9a3677c97ff17fec4ddc95b7c5485410df0`다. 전체 명령·테스트별 결과·환경·파일 hash는 [validation.json](../planning/validation.json)에 기록했다. Windows 제품, 실제 벤더 모델/도구, upstream 제품 코드, 원격 연합 실행은 이번 실행 결과에 포함하지 않는다.

**모든 제품 태스크는 여전히 `todo`이며 제품 완료 증거는 0개다.** 이번 검토는 제품 코드를 대신 작성하거나 실제 provider 실행을 수행한 것이 아니다. 현재 주장할 수 있는 결과는 “주요 불완전 제품의 반례를 차단하도록 개발·인수·배포 계획을 연결하고 그 구조를 검증했다”는 것이다.

수정된 완료 기준을 실제 구현과 동일 설치본 증거로 모두 충족하면, 선언한 범위의 완제품으로 판정할 수 있다. 첫 Windows 제품과 연합·확장 전체를 같은 완료 상태로 취급하지 않으며, 지원 범위 밖의 임의 목표 성공을 약속하지 않는다.

## 근거와 변경 이력

- 기존 82개 태스크와 읽기 중심 live 기준: [dc6d60b의 장부](https://github.com/jaeseongs95/AI-Kubernetes/blob/dc6d60b67c0ccd2e56849992440de2eb639aa36c/docs/development-backlog.ko.md).
- 최초 목표·VM/AGS 권위·JEV 원칙: [최초 설계](initial-design.ko.md), SQLite U01~U09·DEC01~DEC05.
- VM 접합점·내부 dispatch: 위 고정 SHA의 [application.py](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/src/flowmarshal/engine/application.py), [runtime.py](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/src/flowmarshal/engine/runtime.py), [CLI](https://github.com/jaeseongs95/vibemarshal/blob/32bb0f9dd9f024045d24487312b50f5b703573a3/src/flowmarshal/engine/cli.py).
- 이전 장부 도구·보안 경계 감사는 [v0.3 감사 보고서](audit-2026-10-12.ko.md)에 역사적 결과로 보존한다.
- 현재 판본의 무결성·회귀·복원·생성본·범위 검증과 파일 hash는 [validation.json](../planning/validation.json)에 기록한다.
